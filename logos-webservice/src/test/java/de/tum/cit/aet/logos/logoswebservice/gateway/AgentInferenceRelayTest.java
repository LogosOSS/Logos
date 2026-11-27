package de.tum.cit.aet.logos.logoswebservice.gateway;

import static de.tum.cit.aet.logos.logoswebservice.gateway.AgentInferenceRelayController.MESSAGES;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.asyncDispatch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import com.sun.net.httpserver.HttpsConfigurator;
import com.sun.net.httpserver.HttpsServer;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyStore;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import javax.net.ssl.KeyManagerFactory;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManagerFactory;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.web.context.request.RequestContextHolder;
import org.springframework.web.context.request.ServletRequestAttributes;
import org.springframework.web.server.ResponseStatusException;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

@Testcontainers
class AgentInferenceRelayTest {
    private static final String GRANT = "A".repeat(43);
    private static final String HASH = AgentInferenceRelayController.tokenHash(GRANT);
    private static final String TAG = "agent:192.0.2.7";
    private static final String INTERNAL = "test-inner-secret";
    private static final String PROVIDER_KEY = "test-operating-key";
    private static final String JSON = "{\"model\":\"claude-test\",\"messages\":[]}";
    private static final String EVENT = "event: ping\ndata: {}\n\n";
    @Container
    static final PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:17");
    @TempDir static Path tmp;

    private static HttpsServer server;
    private static String allowedOrigin;
    private static HttpClient client;
    private static JdbcTemplate db;
    private static final AtomicReference<String> MODE = new AtomicReference<>();
    private static final AtomicReference<List<String>> SEEN = new AtomicReference<>();
    private static final AtomicInteger CALLS = new AtomicInteger();
    private static final AtomicReference<CountDownLatch> RELEASE = new AtomicReference<>();

    private AgentInferenceRelay relay;
    private AgentInferenceRelayController controller;
    private GatewayInFlightLimit capacity;
    private ProviderSpendCapService cap;
    private MockMvc mvc;

    @BeforeAll
    static void createFixture() throws Exception {
        db = new JdbcTemplate(new DriverManagerDataSource(postgres.getJdbcUrl(), postgres.getUsername(), postgres.getPassword()));
        db.execute("""
            CREATE TYPE cloud_provider_type_enum AS ENUM ('openai', 'anthropic', 'logos');
            CREATE TABLE providers(id integer primary key, base_url text, api_key text,
                                   provider_type text, cloud_provider_type cloud_provider_type_enum,
                                   auth_name text, auth_format text);
            CREATE TABLE models(id integer primary key, name text);
            CREATE TABLE model_provider(provider_id integer, model_id integer, api_key text,
                                        primary key(provider_id, model_id));
            CREATE TABLE agent_sessions(id bigint primary key, status text, error text, inference_grant_hash text UNIQUE,
                                        inference_client_tag text, inference_provider_id integer, inference_model_id integer, model text);
            """);
        Path storePath = tmp.resolve("loopback.p12");
        var keytool = new ProcessBuilder(Path.of(System.getProperty("java.home"), "bin", "keytool").toString(),
            "-genkeypair", "-alias", "local", "-keyalg", "RSA", "-dname", "CN=127.0.0.1",
            "-ext", "SAN=IP:127.0.0.1", "-validity", "1", "-storetype", "PKCS12",
            "-keystore", storePath.toString(), "-storepass", "test-only", "-noprompt")
            .redirectErrorStream(true).start();
        assertThat(keytool.waitFor(30, TimeUnit.SECONDS)).isTrue();
        assertThat(keytool.exitValue()).isZero();
        var store = KeyStore.getInstance("PKCS12");
        try (var input = Files.newInputStream(storePath)) {
            store.load(input, "test-only".toCharArray());
        }
        var managers = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
        managers.init(store, "test-only".toCharArray());
        var trusts = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        trusts.init(store);
        var tls = SSLContext.getInstance("TLS");
        tls.init(managers.getKeyManagers(), trusts.getTrustManagers(), null);
        server = HttpsServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.setHttpsConfigurator(new HttpsConfigurator(tls));
        server.createContext("/", exchange -> {
            CALLS.incrementAndGet();
            var headers = exchange.getRequestHeaders();
            SEEN.set(List.of(exchange.getRequestURI().toString(),
                String.valueOf(headers.getFirst("Authorization")),
                String.valueOf(headers.getFirst("X-Logos-Agent-Relay-Secret")),
                String.valueOf(headers.getFirst("X-Logos-Client-Tag")),
                String.valueOf(headers.getFirst("X-Api-Key")),
                String.valueOf(headers.getFirst("anthropic-beta")),
                new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8)));
            String mode = MODE.get();
            try (exchange) {
                if ("redirect".equals(mode)) {
                    exchange.getResponseHeaders().set("Location", allowedOrigin + "/attempted-redirect");
                    exchange.getResponseHeaders().set("Set-Cookie", PROVIDER_KEY);
                    byte[] leak = PROVIDER_KEY.getBytes(StandardCharsets.UTF_8);
                    exchange.sendResponseHeaders(307, leak.length);
                    exchange.getResponseBody().write(leak);
                } else if ("hold".equals(mode)) {
                    exchange.getResponseHeaders().set("Content-Type", "text/event-stream");
                    exchange.sendResponseHeaders(200, 0);
                    exchange.getResponseBody().write(EVENT.getBytes(StandardCharsets.UTF_8));
                    exchange.getResponseBody().flush();
                    try { RELEASE.get().await(8, TimeUnit.SECONDS); }
                    catch (InterruptedException e) { Thread.currentThread().interrupt(); }
                } else {
                    byte[] data = "{\"input_tokens\":12}".getBytes(StandardCharsets.UTF_8);
                    exchange.getResponseHeaders().set("Content-Type", "application/json");
                    exchange.sendResponseHeaders(200, data.length);
                    exchange.getResponseBody().write(data);
                }
            }
        });
        server.start();
        allowedOrigin = "https://127.0.0.1:" + server.getAddress().getPort();
        client = HttpClient.newBuilder().sslContext(tls).followRedirects(HttpClient.Redirect.NEVER).build();
    }

    @AfterAll
    static void stop() {
        if (server != null) server.stop(0);
        if (client != null) client.close();
    }

    @BeforeEach
    void seed() {
        db.execute("TRUNCATE agent_sessions, model_provider, models, providers");
        db.update("INSERT INTO providers VALUES (3, ?, 'wrong-provider-key', 'cloud', 'logos', 'Authorization', 'Bearer {}')", allowedOrigin);
        db.update("INSERT INTO models VALUES (4, 'claude-test')");
        db.update("INSERT INTO model_provider VALUES (3, 4, ?)", PROVIDER_KEY);
        db.update("INSERT INTO agent_sessions VALUES (2, 'starting', null, ?, null, 3, 4, 'claude-test')", HASH);
        MODE.set("ok");
        SEEN.set(null);
        CALLS.set(0);
        RELEASE.set(new CountDownLatch(1));
        cap = mock(ProviderSpendCapService.class);
        relay = new AgentInferenceRelay(new NamedParameterJdbcTemplate(db), cap, allowedOrigin, client);
        capacity = new GatewayInFlightLimit(1);
        controller = new AgentInferenceRelayController(relay, capacity, INTERNAL);
        mvc = MockMvcBuilders.standaloneSetup(controller).build();
    }

    private org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder relayRequest(String path, String json) {
        return post(path).contentType("application/json").content(json)
            .header("Authorization", "Bearer " + GRANT).header("X-Logos-Agent-Relay-Secret", INTERNAL)
            .header("X-Logos-Client-Tag", TAG);
    }

    @Test
    void authenticatesAndBindsTheGrantThenForwardsOnlyTheOperatingKey() throws Exception {
        var result = mvc.perform(relayRequest(MESSAGES, JSON).queryParam("beta", "true").header("X-Api-Key", "caller-controlled")
                .header("anthropic-beta", "feature-test")).andExpect(request().asyncStarted()).andReturn();
        mvc.perform(asyncDispatch(result)).andExpect(status().isOk()).andExpect(content().json("{\"input_tokens\":12}"));
        verify(cap).check(3);
        assertThat(SEEN.get()).containsExactly("/v1/messages", "Bearer " + PROVIDER_KEY,
            "null", "null", "null", "feature-test", JSON);
        assertThat(db.queryForObject("SELECT inference_client_tag FROM agent_sessions WHERE id=2", String.class)).isEqualTo(TAG);
        assertThat(capacity.inFlight()).isZero();
    }

    @Test
    void rejectsWrongIdentityModelAndUnknownOrPausedGrantBeforeContact() throws Exception {
        mvc.perform(relayRequest(MESSAGES, JSON).header("X-Logos-Client-Tag", "agent:192.0.2.10"))
            .andExpect(status().isUnauthorized());
        mvc.perform(post(MESSAGES).contentType("application/json").content(JSON)
                .header("Authorization", "Bearer " + "B".repeat(43)).header("X-Logos-Agent-Relay-Secret", INTERNAL)
                .header("X-Logos-Client-Tag", TAG)).andExpect(status().isUnauthorized());
        mvc.perform(relayRequest(MESSAGES, JSON).header("X-Logos-Agent-Relay-Secret", "spoofed"))
            .andExpect(status().isUnauthorized());
        mvc.perform(relayRequest(MESSAGES, JSON).queryParam("model", "other")).andExpect(status().isBadRequest());
        mvc.perform(relayRequest(MESSAGES + "/count_tokens", JSON)).andExpect(status().isNotFound());
        for (String invalid : List.of("{\"model\":\"other\"}", "{\"model\":\"claude-test\",\"model\":\"claude-test\"}",
                JSON + "{}", "{\"messages\":[]}")) {
            mvc.perform(relayRequest(MESSAGES, invalid)).andExpect(status().isBadRequest());
        }
        db.update("UPDATE agent_sessions SET inference_client_tag='agent:192.0.2.99' WHERE id=2");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isUnauthorized());
        db.update("UPDATE agent_sessions SET status='paused', inference_client_tag=NULL WHERE id=2");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isUnauthorized());
        db.update("UPDATE agent_sessions SET status='running', error='cancel_requested:test' WHERE id=2");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isUnauthorized());
        assertThat(CALLS).hasValue(0);
        assertThat(capacity.inFlight()).isZero();
    }

    @Test
    void requiresCanonicalSessionModelAndGenuineBearerConfiguration() throws Exception {
        db.update("UPDATE providers SET auth_format='' WHERE id=3");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isUnauthorized());
        db.update("UPDATE providers SET auth_name='x-api-key', auth_format='Bearer {}' WHERE id=3");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isUnauthorized());
        db.update("UPDATE providers SET auth_name='', auth_format='' WHERE id=3");
        db.update("UPDATE agent_sessions SET model='different' WHERE id=2");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isUnauthorized());
        assertThat(CALLS).hasValue(0);
        db.update("UPDATE agent_sessions SET model='claude-test' WHERE id=2");
        var result = mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(request().asyncStarted()).andReturn();
        mvc.perform(asyncDispatch(result)).andExpect(status().isOk());
        assertThat(SEEN.get().get(1)).isEqualTo("Bearer " + PROVIDER_KEY);
    }

    @Test
    void rejectsAnUnapprovedOrNonOriginUrlAndDoesNotFollowOrLeakARedirect() throws Exception {
        db.update("UPDATE providers SET base_url = 'https://other.test' WHERE id=3");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isServiceUnavailable());
        db.update("UPDATE providers SET base_url = ? WHERE id=3", allowedOrigin + "/v1");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isServiceUnavailable());
        assertThat(CALLS).hasValue(0);
        db.update("UPDATE providers SET base_url = ?, cloud_provider_type='anthropic' WHERE id=3", allowedOrigin);
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isUnauthorized());
        assertThat(CALLS).hasValue(0);
        db.update("UPDATE providers SET cloud_provider_type='logos' WHERE id=3");
        MODE.set("redirect");
        var result = mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(request().asyncStarted()).andReturn();
        var response = mvc.perform(asyncDispatch(result)).andExpect(status().isBadGateway())
            .andExpect(header().doesNotExist("Location")).andExpect(header().doesNotExist("Set-Cookie"))
            .andReturn().getResponse();
        assertThat(response.getContentAsString()).doesNotContain(PROVIDER_KEY, INTERNAL, GRANT);
        assertThat(CALLS).hasValue(1);
        assertThat(capacity.inFlight()).isZero();
        assertThatThrownBy(() -> AgentInferenceRelay.origin("https://name:pass@example.test"))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> AgentInferenceRelay.origin("https://example.test/?x=1"))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void relaysStreamBeforeEofAndClosesIdleReadWhenSessionPauses() throws Exception {
        MODE.set("hold");
        var input = new MockHttpServletRequest("POST", MESSAGES);
        input.addHeader("Authorization", "Bearer " + GRANT);
        input.addHeader("X-Logos-Agent-Relay-Secret", INTERNAL);
        input.addHeader("X-Logos-Client-Tag", TAG);
        input.setContent(JSON.getBytes(StandardCharsets.UTF_8));
        RequestContextHolder.setRequestAttributes(new ServletRequestAttributes(input));
        var gotChunk = new CountDownLatch(1);
        var out = new ByteArrayOutputStream() {
            @Override public void flush() { gotChunk.countDown(); }
        };
        try (var io = Executors.newVirtualThreadPerTaskExecutor()) {
            var response = controller.messages(input);
            assertThat(response.getHeaders().getFirst("X-Accel-Buffering")).isEqualTo("no");
            var writing = io.submit(() -> {
                try {
                    response.getBody().writeTo(out);
                } catch (IOException expectedWhenRevoked) {
                    // A started event stream is cut off, not replaced with a new response.
                }
            });
            try {
                assertThat(gotChunk.await(3, TimeUnit.SECONDS)).isTrue();
                assertThat(out.toString(StandardCharsets.UTF_8)).isEqualTo(EVENT);
                assertThat(writing.isDone()).isFalse();
                db.update("UPDATE agent_sessions SET status='paused' WHERE id=2");
                relay.closeInactive();
                writing.get(3, TimeUnit.SECONDS);
                assertThat(capacity.inFlight()).isZero();
            } finally {
                RELEASE.get().countDown();
            }
        } finally {
            RequestContextHolder.resetRequestAttributes();
        }
    }
}
