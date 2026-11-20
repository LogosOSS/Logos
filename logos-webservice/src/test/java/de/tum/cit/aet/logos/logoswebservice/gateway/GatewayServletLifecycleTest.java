package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.when;

import java.io.ByteArrayOutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.sql.DriverManager;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import de.tum.cit.aet.logos.logoswebservice.identity.entity.ApiKeyType;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.test.annotation.DirtiesContext;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.context.bean.override.mockito.MockReset;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

/** Real Boot shutdown must finish interrupted accounting before closing the data source. */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT, properties = {
    "spring.lifecycle.timeout-per-shutdown-phase=100ms",
    "logos.gateway.budget-reservation-reconcile-seconds=86400"
})
@Testcontainers
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_EACH_TEST_METHOD)
// Default after-method listeners restart cached contexts. This test deliberately closes its context.
@org.springframework.test.context.TestExecutionListeners(listeners = {
    org.springframework.test.context.support.DependencyInjectionTestExecutionListener.class,
    org.springframework.test.context.bean.override.BeanOverrideTestExecutionListener.class,
    org.springframework.test.context.support.DirtiesContextTestExecutionListener.class
}, mergeMode = org.springframework.test.context.TestExecutionListeners.MergeMode.REPLACE_DEFAULTS)
class GatewayServletLifecycleTest {
    @Container
    static PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:17")
        .withDatabaseName("logosdb").withUsername("postgres").withPassword("root");

    @DynamicPropertySource
    static void properties(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", postgres::getJdbcUrl);
        registry.add("spring.datasource.username", postgres::getUsername);
        registry.add("spring.datasource.password", postgres::getPassword);
        registry.add("spring.liquibase.enabled", () -> "true");
        registry.add("spring.liquibase.change-log", () -> "classpath:liquibase/changelog/master.xml");
        registry.add("spring.jpa.hibernate.ddl-auto", () -> "validate");
    }

    @MockitoBean(reset = MockReset.NONE) JwtDecoder jwtDecoder;
    @MockitoBean(reset = MockReset.NONE) GatewayAuthService auth;
    @Autowired ConfigurableApplicationContext context;
    @Autowired JdbcTemplate jdbc;
    @Autowired GatewayInFlightLimit limit;

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void contextCloseCancelsUpstreamWaitAndSettlesBeforeDatabaseShutdown(boolean bodyStarted) throws Exception {
        var reached = new CountDownLatch(1);
        var disconnected = new CountDownLatch(1);
        AtomicReference<Socket> accepted = new AtomicReference<>();
        AtomicReference<Throwable> providerFailure = new AtomicReference<>();
        AtomicReference<Thread> requestThread = new AtomicReference<>();
        AtomicReference<Thread> responseThread = new AtomicReference<>();
        AtomicReference<java.io.InputStream> clientBody = new AtomicReference<>();
        context.getBean("gatewayResponseExecutor", org.springframework.core.task.SimpleAsyncTaskExecutor.class)
            .setTaskDecorator(task -> () -> {
                responseThread.set(Thread.currentThread());
                task.run();
            });
        try (ServerSocket provider = new ServerSocket(0);
             HttpClient client = HttpClient.newHttpClient();
             var verification = DriverManager.getConnection(postgres.getJdbcUrl(),
                 postgres.getUsername(), postgres.getPassword())) {
            Thread providerThread = Thread.ofVirtual().start(() -> {
                try (Socket socket = provider.accept()) {
                    accepted.set(socket);
                    socket.setSoTimeout(15000);
                    String headers = readHeaders(socket);
                    int length = headers.lines().filter(line -> line.toLowerCase(java.util.Locale.ROOT)
                        .startsWith("content-length:")).mapToInt(line -> Integer.parseInt(line.split(":", 2)[1].strip()))
                        .findFirst().orElse(0);
                    assertThat(socket.getInputStream().readNBytes(length)).hasSize(length);
                    if (bodyStarted) {
                        socket.getOutputStream().write(("HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                            + "Transfer-Encoding: chunked\r\n\r\n5\r\nfirst\r\n").getBytes(StandardCharsets.US_ASCII));
                        socket.getOutputStream().flush();
                    }
                    reached.countDown();
                    assertThat(socket.getInputStream().read()).isEqualTo(-1);
                    disconnected.countDown();
                } catch (Throwable e) {
                    providerFailure.set(e);
                }
            });
            try {
                String baseUrl = "http://127.0.0.1:" + provider.getLocalPort();
                int model = jdbc.queryForObject("INSERT INTO models (name) VALUES (?) RETURNING id", Integer.class,
                    "shutdown-model-" + bodyStarted);
                int upstream = jdbc.queryForObject("INSERT INTO providers (name, base_url, provider_type, cloud_provider_type, auth_name, auth_format) "
                    + "VALUES (?, ?, 'cloud', 'openai', 'Authorization', 'Bearer %s') RETURNING id", Integer.class,
                    "shutdown-provider-" + bodyStarted, baseUrl);
                int team = jdbc.queryForObject("INSERT INTO teams (name, default_cloud_rpm_limit, default_cloud_tpm_limit) "
                    + "VALUES (?, NULL, NULL) RETURNING id", Integer.class, "shutdown-team-" + bodyStarted);
                int keyId = jdbc.queryForObject("INSERT INTO api_keys (key_value, name, team_id, log) "
                    + "VALUES (?, 'shutdown-key', ?, 'BILLING') RETURNING id", Integer.class, "lg-shutdown-" + bodyStarted, team);
                GatewayKey key = new GatewayKey(keyId, "lg-shutdown", "shutdown-key", ApiKeyType.developer,
                    team, null, "test", false, null, 0, "BILLING");
                GatewayDeployment deployment = new GatewayDeployment(model, "shutdown-model", upstream, "shutdown-provider",
                    "cloud", "openai", baseUrl, null, "Authorization", "Bearer %s", "fake-key",
                    "CLOUD_NOT_IN_EU_BY_US_PROVIDER", null);
                when(auth.requireActiveKey(any())).thenAnswer(invocation -> {
                    requestThread.set(Thread.currentThread());
                    return key;
                });
                when(auth.deploymentsForModel(any(), any())).thenReturn(List.of(deployment));
                int port = context.getEnvironment().getRequiredProperty("local.server.port", Integer.class);
                var response = client.sendAsync(HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + "/v1/chat/completions"))
                    .header("Authorization", "Bearer lg-shutdown").header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString("{\"model\":\"shutdown-model\",\"messages\":[]}"))
                    .build(), HttpResponse.BodyHandlers.ofInputStream());
                assertThat(reached.await(10, TimeUnit.SECONDS))
                    .as("Provider received request; response=%s, provider failure=%s", response.getNow(null), providerFailure.get())
                    .isTrue();
                if (bodyStarted) {
                    var started = response.get(5, TimeUnit.SECONDS);
                    assertThat(started.statusCode()).isEqualTo(200);
                    clientBody.set(started.body());
                    assertThat(new String(clientBody.get().readNBytes(5), StandardCharsets.US_ASCII)).isEqualTo("first");
                    assertThat(responseThread.get().isVirtual()).isTrue();
                }
                assertThat(requestThread.get().isVirtual()).isTrue();
                assertThat(limit.inFlight()).isEqualTo(1);
                int logId = jdbc.queryForObject("SELECT id FROM log_entry WHERE api_key_id = ?", Integer.class, keyId);
                long start = System.nanoTime();
                context.close();
                assertThat(Duration.ofNanos(System.nanoTime() - start)).isLessThan(Duration.ofSeconds(8));
                assertThat(disconnected.await(2, TimeUnit.SECONDS)).isTrue();
                assertThat(providerFailure.get()).isNull();
                assertThat(requestThread.get().isAlive()).isFalse();
                if (bodyStarted) assertThat(responseThread.get().isAlive()).isFalse();
                assertThat(limit.inFlight()).isZero();
                try (var statement = verification.prepareStatement("SELECT result_status::text, cost_finalized, "
                        + "settled_cost_micro_cents, error_message FROM log_entry WHERE id = ?")) {
                    statement.setInt(1, logId);
                    try (var row = statement.executeQuery()) {
                        assertThat(row.next()).isTrue();
                        assertThat(row.getString(1)).isEqualTo("error");
                        assertThat(row.getBoolean(2)).isTrue();
                        assertThat(row.getLong(3)).isZero();
                        if (bodyStarted) {
                            assertThat(row.getString(4)).isIn("Relay interrupted", "Client request ended");
                        } else {
                            assertThat(row.getString(4)).isEqualTo("Interrupted while forwarding to cloud");
                        }
                    }
                }
                response.handle((value, failure) -> null).get(5, TimeUnit.SECONDS);
            } finally {
                if (context.isActive()) context.close();
                if (clientBody.get() != null) clientBody.get().close();
                Socket socket = accepted.get();
                if (socket != null) socket.close();
                provider.close();
                providerThread.join(5000);
            }
        }
    }

    private static String readHeaders(Socket socket) throws Exception {
        ByteArrayOutputStream header = new ByteArrayOutputStream();
        int b;
        while ((b = socket.getInputStream().read()) != -1) {
            header.write(b);
            byte[] bytes = header.toByteArray();
            int n = bytes.length;
            if (n >= 4 && bytes[n - 4] == '\r' && bytes[n - 3] == '\n'
                    && bytes[n - 2] == '\r' && bytes[n - 1] == '\n') {
                return header.toString(StandardCharsets.ISO_8859_1);
            }
            if (n > 65536) throw new IllegalStateException("Oversized fake-provider request headers");
        }
        throw new IllegalStateException("Request ended before headers");
    }
}
