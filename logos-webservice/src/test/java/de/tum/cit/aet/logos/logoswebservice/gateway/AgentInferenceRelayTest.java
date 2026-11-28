package de.tum.cit.aet.logos.logoswebservice.gateway;

import static de.tum.cit.aet.logos.logoswebservice.gateway.AgentInferenceRelayController.MESSAGES;
import static org.assertj.core.api.Assertions.assertThat;
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
    private static String registeredBase;
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
                                   auth_name text, auth_format text, privacy_level text);
            CREATE TABLE teams(id integer primary key, privacy_scope text);
            CREATE TABLE team_repositories(id integer primary key, team_id integer, agent_enabled boolean, repo_slug text, automatic_workflow_analysis boolean);
            CREATE VIEW team_privacy_providers AS
            SELECT t.id AS team_id, p.id AS provider_id FROM teams t CROSS JOIN providers p
              WHERE t.privacy_scope='WORLDWIDE' OR
                (t.privacy_scope='EU' AND p.privacy_level IN ('CLOUD_IN_EU_BY_EU_PROVIDER','CLOUD_IN_EU_BY_US_PROVIDER'));
            CREATE TABLE models(id integer primary key, name text);
            CREATE TABLE model_provider(provider_id integer, model_id integer, api_key text, endpoint text,
                                        primary key(provider_id, model_id));
            CREATE TABLE agent_sessions(id bigint primary key, status text, error text, inference_grant_hash text UNIQUE,
                                        inference_client_tag text, inference_provider_id integer, inference_model_id integer, model text,
                                        inference_base_url text,
                                        team_repository_id integer REFERENCES team_repositories ON DELETE SET NULL, repo_slug text,
                                        trigger_kind text, no_push boolean, created_by text NOT NULL);
            CREATE TABLE ai_workflow_analyses(agent_session_id bigint, team_repository_id integer, team_id integer,
                                               automatically_queued boolean NOT NULL);
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
                    exchange.getResponseHeaders().set("Location", registeredBase + "/attempted-redirect");
                    exchange.getResponseHeaders().set("Set-Cookie", PROVIDER_KEY);
                    byte[] leak = PROVIDER_KEY.getBytes(StandardCharsets.UTF_8);
                    exchange.sendResponseHeaders(307, leak.length);
                    exchange.getResponseBody().write(leak);
                } else if (mode.startsWith("error-") || "wrong-type".equals(mode)) {
                    byte[] message = PROVIDER_KEY.getBytes(StandardCharsets.UTF_8);
                    exchange.getResponseHeaders().set("Content-Type", "text/plain");
                    exchange.getResponseHeaders().set("Retry-After", mode.endsWith("-unsafe") ? PROVIDER_KEY : "5");
                    int code = mode.startsWith("error-") ? Integer.parseInt(mode.split("-")[1]) : 200;
                    exchange.sendResponseHeaders(code, message.length);
                    exchange.getResponseBody().write(message);
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
        registeredBase = "https://127.0.0.1:" + server.getAddress().getPort();
        client = HttpClient.newBuilder().sslContext(tls).followRedirects(HttpClient.Redirect.NEVER).build();
    }

    @AfterAll
    static void stop() {
        if (server != null) server.stop(0);
        if (client != null) client.close();
    }

    @BeforeEach
    void seed() {
        db.execute("TRUNCATE ai_workflow_analyses, agent_sessions, model_provider, models, providers, team_repositories, teams");
        db.update("INSERT INTO providers VALUES (3, ?, 'wrong-provider-key', 'cloud', 'logos', 'Authorization', 'Bearer {}', 'CLOUD_IN_EU_BY_EU_PROVIDER')", registeredBase);
        db.update("INSERT INTO models VALUES (4, 'claude-test')");
        db.update("INSERT INTO model_provider VALUES (3, 4, ?, NULL)", PROVIDER_KEY);
        db.update("INSERT INTO agent_sessions VALUES (2, 'starting', null, ?, null, 3, 4, 'claude-test', ?, null, null, null, false, 'test-operator')", HASH, registeredBase);
        MODE.set("ok");
        SEEN.set(null);
        CALLS.set(0);
        RELEASE.set(new CountDownLatch(1));
        cap = mock(ProviderSpendCapService.class);
        relay = new AgentInferenceRelay(new NamedParameterJdbcTemplate(db), cap, client);
        capacity = new GatewayInFlightLimit(1);
        controller = new AgentInferenceRelayController(relay, capacity, INTERNAL);
        mvc = MockMvcBuilders.standaloneSetup(controller).build();
    }

    private org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder agentPost(String path) {
        return post(path).header("X-Logos-Agent-Inference", "cloud");
    }

    private org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder relayRequest(String path, String json) {
        return agentPost(path).contentType("application/json").content(json)
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
        mvc.perform(agentPost(MESSAGES).contentType("application/json").content(JSON)
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
    void registryVersionSuffixUsesTheSameNativeMessagesSurfaceWithoutAnAllowlist() throws Exception {
        // Ordinary model registry endpoint metadata need not describe the
        // Messages surface and cannot change the pinned provider destination.
        db.update("UPDATE model_provider SET endpoint='https://unused.example/v1/chat/completions'");
        for (String suffix : List.of("/v1", "/v1/")) {
            String base = registeredBase + suffix;
            db.update("UPDATE providers SET base_url=? WHERE id=3", base);
            db.update("UPDATE agent_sessions SET inference_base_url=? WHERE id=2", base);
            var result = mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(request().asyncStarted()).andReturn();
            mvc.perform(asyncDispatch(result)).andExpect(status().isOk());
            assertThat(SEEN.get().getFirst()).isEqualTo("/v1/messages");
        }
        assertThat(CALLS).hasValue(2);
    }

    @Test
    void rejectsChangedBaseOrUnrelatedProviderTypeBeforeAnyContact() throws Exception {
        db.update("UPDATE providers SET base_url=? WHERE id=3", registeredBase + "/v1");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isUnauthorized());
        db.update("UPDATE providers SET base_url=?, cloud_provider_type='anthropic' WHERE id=3", registeredBase);
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isUnauthorized());
        db.update("UPDATE providers SET cloud_provider_type='logos' WHERE id=3");
        db.update("UPDATE agent_sessions SET inference_base_url=NULL WHERE id=2");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isUnauthorized());
        assertThat(CALLS).hasValue(0);
    }

    @Test
    void rejectsMalformedRegistryTargetsWithoutSendingCredentials() throws Exception {
        for (String base : List.of("http://example.test", "https:///no-host", "https://example.test:0",
                "https://example.test:", "https://example.test:65536", "https://example.test/#fragment",
                "https://example.test/?x=1", "https://name:pass@example.test", "https://example.test/v2",
                "https://example.test/tenant/v1", "https://[fe80::1%25eth0]/v1")) {
            db.update("UPDATE providers SET base_url=? WHERE id=3", base);
            db.update("UPDATE agent_sessions SET inference_base_url=? WHERE id=2", base);
            mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isServiceUnavailable());
        }
        assertThat(CALLS).hasValue(0);
        assertThat(AgentInferenceRelay.messagesTarget("HTTPS://EXAMPLE.TEST:443/v1/").toString())
            .isEqualTo("https://example.test/v1/messages");
    }

    @Test
    void doesNotFollowOrLeakARedirectFromTheRegisteredProvider() throws Exception {
        MODE.set("redirect");
        var result = mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(request().asyncStarted()).andReturn();
        var response = mvc.perform(asyncDispatch(result)).andExpect(status().isBadGateway())
            .andExpect(header().doesNotExist("Location")).andExpect(header().doesNotExist("Set-Cookie"))
            .andReturn().getResponse();
        assertThat(response.getContentAsString()).doesNotContain(PROVIDER_KEY, INTERNAL, GRANT);
        assertThat(CALLS).hasValue(1);
        assertThat(capacity.inFlight()).isZero();
    }

    @Test
    void suppressesUpstreamErrorDetailsAndRejectsUnexpectedSuccessContentType() throws Exception {
        MODE.set("error-429");
        var result = mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(request().asyncStarted()).andReturn();
        var response = mvc.perform(asyncDispatch(result)).andExpect(status().isTooManyRequests())
            .andExpect(header().string("Retry-After", "5")).andReturn().getResponse();
        assertThat(response.getContentAsString()).doesNotContain(PROVIDER_KEY);
        MODE.set("wrong-type");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isBadGateway());
        assertThat(capacity.inFlight()).isZero();
    }

    @Test
    void relaysSafeStatusClassesButNeverRemoteDiagnosticsOrUnsafeRetryMetadata() throws Exception {
        for (int code : List.of(400, 413, 422, 429, 503, 529)) {
            MODE.set("error-" + code + "-unsafe");
            var result = mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(request().asyncStarted()).andReturn();
            var response = mvc.perform(asyncDispatch(result)).andExpect(status().is(code))
                .andExpect(header().doesNotExist("Retry-After")).andReturn().getResponse();
            assertThat(response.getContentAsString()).doesNotContain(PROVIDER_KEY);
        }
        assertThat(capacity.inFlight()).isZero();
    }

    @Test
    void rejectsMissingAndInvalidRelayCredentialsBeforeAnUpstreamConnection() throws Exception {
        mvc.perform(agentPost(MESSAGES).contentType("application/json").content(JSON)).andExpect(status().isUnauthorized());
        mvc.perform(agentPost(MESSAGES).contentType("application/json").content(JSON)
            .header("X-Logos-Agent-Relay-Secret", INTERNAL)).andExpect(status().isUnauthorized());
        mvc.perform(agentPost(MESSAGES).contentType("application/json").content(JSON)
            .header("X-Logos-Agent-Relay-Secret", INTERNAL).header("Authorization", "Bearer " + GRANT))
            .andExpect(status().isUnauthorized());
        mvc.perform(agentPost(MESSAGES).contentType("application/json").content(JSON)
            .header("X-Logos-Agent-Relay-Secret", INTERNAL).header("Authorization", "Bearer " + GRANT)
            .header("X-Logos-Client-Tag", "agent:192.0.2.999")).andExpect(status().isUnauthorized());
        mvc.perform(relayRequest(MESSAGES, JSON).header("anthropic-beta", "not:allowed"))
            .andExpect(status().isBadRequest());
        mvc.perform(relayRequest(MESSAGES, "[]")).andExpect(status().isBadRequest());
        mvc.perform(relayRequest(MESSAGES, "{\"model\":42}")).andExpect(status().isBadRequest());
        assertThat(CALLS).hasValue(0);
    }

    @Test
    void rejectsMissingAndMalformedOperatingKeysRatherThanForwardingThem() throws Exception {
        db.update("UPDATE model_provider SET api_key=NULL");
        for (String candidate : List.of("", "no spaces", "a".repeat(513))) {
            db.update("UPDATE providers SET api_key=? WHERE id=3", candidate);
            mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isServiceUnavailable());
        }
        assertThat(CALLS).hasValue(0);
    }

    @Test
    void acceptsOperatingKeyRotationWithoutRetargetingTheSession() throws Exception {
        db.update("UPDATE model_provider SET api_key='rotated-key'");
        var result = mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(request().asyncStarted()).andReturn();
        mvc.perform(asyncDispatch(result)).andExpect(status().isOk());
        assertThat(SEEN.get().get(1)).isEqualTo("Bearer rotated-key");
        assertThat(SEEN.get().getFirst()).isEqualTo("/v1/messages");
        assertThat(CALLS).hasValue(1);
    }

    private void linkRepository() {
        db.update("INSERT INTO teams VALUES (6, 'EU')");
        db.update("INSERT INTO team_repositories VALUES (7, 6, true, 'Example/Repo', true)");
        db.update("UPDATE agent_sessions SET team_repository_id=7, repo_slug='example/repo' WHERE id=2");
    }

    @Test
    void checksLiveRepositoryAndExactProviderPrivacy() throws Exception {
        linkRepository();
        var result = mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(request().asyncStarted()).andReturn();
        mvc.perform(asyncDispatch(result)).andExpect(status().isOk());
        db.update("UPDATE teams SET privacy_scope='LOCAL' WHERE id=6");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isForbidden());
        db.update("UPDATE teams SET privacy_scope='EU' WHERE id=6");
        db.update("UPDATE providers SET privacy_level='CLOUD_NOT_IN_EU_BY_US_PROVIDER' WHERE id=3");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isForbidden());
        db.update("UPDATE providers SET privacy_level='CLOUD_IN_EU_BY_EU_PROVIDER' WHERE id=3");
        db.update("UPDATE team_repositories SET repo_slug='example/other' WHERE id=7");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isForbidden());
        db.update("UPDATE team_repositories SET repo_slug='Example/Repo', agent_enabled=false WHERE id=7");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isForbidden());
        db.update("DELETE FROM team_repositories WHERE id=7");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isForbidden());
        assertThat(CALLS).hasValue(1);
    }

    @Test
    void separatesOperatorRequestedAnalysesFromAutomaticAnalysesAndCoding() throws Exception {
        linkRepository();
        db.update("UPDATE team_repositories SET agent_enabled=false, automatic_workflow_analysis=false WHERE id=7");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isForbidden());
        db.update("UPDATE agent_sessions SET trigger_kind='analysis', no_push=true WHERE id=2");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isForbidden());
        db.update("INSERT INTO ai_workflow_analyses VALUES (2, 7, 9, false)");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isForbidden());
        db.update("UPDATE ai_workflow_analyses SET team_id=6");
        db.update("UPDATE agent_sessions SET no_push=false WHERE id=2");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isForbidden());
        db.update("UPDATE agent_sessions SET no_push=true WHERE id=2");
        var manual = mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(request().asyncStarted()).andReturn();
        mvc.perform(asyncDispatch(manual)).andExpect(status().isOk());
        db.update("UPDATE ai_workflow_analyses SET automatically_queued=true");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isForbidden());
        db.update("UPDATE ai_workflow_analyses SET automatically_queued=false");
        db.update("UPDATE agent_sessions SET created_by='logos-agent (analysis)' WHERE id=2");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isForbidden());
        db.update("UPDATE team_repositories SET automatic_workflow_analysis=true WHERE id=7");
        var automatic = mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(request().asyncStarted()).andReturn();
        mvc.perform(asyncDispatch(automatic)).andExpect(status().isOk());
        db.update("UPDATE agent_sessions SET team_repository_id=NULL, repo_slug=NULL WHERE id=2");
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isForbidden());
        assertThat(CALLS).hasValue(2);
    }

    @Test
    void closesRunningAutomaticAnalysisWhenOptOutChanges() throws Exception {
        linkRepository();
        db.update("UPDATE agent_sessions SET trigger_kind='analysis', no_push=true WHERE id=2");
        db.update("INSERT INTO ai_workflow_analyses VALUES (2, 7, 6, true)");
        assertIdleStreamClosedBy(() -> db.update("UPDATE team_repositories SET automatic_workflow_analysis=false WHERE id=7"));
    }

    @Test
    void closesAnAlreadyStartedStreamIfTheTeamRevokesRemoteProcessing() throws Exception {
        linkRepository();
        assertIdleStreamClosedBy(() -> db.update("UPDATE teams SET privacy_scope='LOCAL' WHERE id=6"));
    }

    @Test
    void relaysStreamBeforeEofAndClosesIdleReadWhenSessionPauses() throws Exception {
        assertIdleStreamClosedBy(() -> db.update("UPDATE agent_sessions SET status='paused' WHERE id=2"));
    }

    @Test
    void closesAnActiveStreamAndRejectsNewCallsIfTheRegistryBaseChanges() throws Exception {
        assertIdleStreamClosedBy(() -> db.update("UPDATE providers SET base_url=? WHERE id=3", registeredBase + "/v1"));
        mvc.perform(relayRequest(MESSAGES, JSON)).andExpect(status().isUnauthorized());
        assertThat(CALLS).hasValue(1);
    }

    private void assertIdleStreamClosedBy(Runnable revoke) throws Exception {
        MODE.set("hold");
        var input = new MockHttpServletRequest("POST", MESSAGES);
        input.addHeader("X-Logos-Agent-Inference", "cloud");
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
            var response = controller.relayMessages(input);
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
                // The watcher also runs during normal sessions and must not interrupt
                // a matching, live stream before policy actually changes.
                relay.closeInactive();
                assertThat(writing.isDone()).isFalse();
                revoke.run();
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
