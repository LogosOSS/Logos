package de.tum.cit.aet.logos.logoswebservice.gateway;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.web.server.ResponseStatusException;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.asyncDispatch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.request;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

class AgentLocalInferenceControllerTest {
    private static final String SECRET = "synthetic-service-secret";
    private static final String TOKEN = "Bearer ac1.test-only-token";
    private static final String TAG = "agent:172.19.0.2";
    private final GatewayAuthService publicAuth = mock(GatewayAuthService.class);
    private final AgentInferenceRelay cloudRelay = mock(AgentInferenceRelay.class);
    private final GatewayInFlightLimit capacity = new GatewayInFlightLimit(1);
    private final AtomicReference<Headers> forwarded = new AtomicReference<>();
    private final AtomicReference<String> forwardedPath = new AtomicReference<>();
    private final AtomicReference<String> forwardedBody = new AtomicReference<>();
    private HttpServer server;
    private MockMvc mvc;

    @BeforeEach
    void start() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            forwarded.set(exchange.getRequestHeaders());
            forwardedPath.set(exchange.getRequestURI().toString());
            forwardedBody.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            exchange.getResponseHeaders().set(AgentGatewayHeaders.SECRET, "not exposed");
            exchange.getResponseHeaders().set(AgentGatewayHeaders.MODE, "not exposed");
            exchange.getResponseHeaders().set("X-Logos-Agent-Token", "not exposed");
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        server.start();
        var proxy = new GatewayOrchestratorProxy("http://127.0.0.1:" + server.getAddress().getPort());
        var external = new InferenceGatewayController(true, publicAuth, mock(GatewayCloudAccounting.class),
            mock(GatewayCloudForwarder.class), mock(GatewayCloudRateLimiter.class), proxy, new ObjectMapper(),
            capacity, mock(ProviderSpendCapService.class));
        var cloud = new AgentInferenceRelayController(cloudRelay, capacity, SECRET);
        mvc = MockMvcBuilders.standaloneSetup(new AgentLocalInferenceController(proxy, capacity, SECRET), cloud, external)
            .addInterceptors(new GatewayRelayCleanup())
            .build();
    }

    @AfterEach
    void stop() {
        if (server != null) server.stop(0);
    }

    @Test
    void trustedLocalModeUsesTheSharedGatewayPathAndKeepsCredentialInternal() throws Exception {
        MvcResult result = mvc.perform(post("/v1/messages?beta=true").contentType("application/json")
            .content("{\"model\":\"local-model\"}")
            .header(AgentGatewayHeaders.MODE, "local")
            .header(AgentGatewayHeaders.SECRET, SECRET)
            .header(AgentGatewayHeaders.CLIENT_TAG, TAG)
            .header("Authorization", TOKEN)).andExpect(request().asyncStarted()).andReturn();
        mvc.perform(asyncDispatch(result)).andExpect(status().isOk())
            .andExpect(header().doesNotExist(AgentGatewayHeaders.SECRET))
            .andExpect(header().doesNotExist(AgentGatewayHeaders.MODE))
            .andExpect(header().doesNotExist("X-Logos-Agent-Token"));
        assertThat(forwardedPath.get()).isEqualTo("/v1/messages?beta=true");
        assertThat(forwardedBody.get()).isEqualTo("{\"model\":\"local-model\"}");
        assertThat(forwarded.get().getFirst("Authorization")).isEqualTo(TOKEN);
        assertThat(forwarded.get().getFirst(AgentGatewayHeaders.CLIENT_TAG)).isEqualTo(TAG);
        assertThat(forwarded.get().getFirst(AgentGatewayHeaders.MODE)).isNull();
        assertThat(forwarded.get().getFirst(AgentGatewayHeaders.SECRET)).isNull();
        assertThat(capacity.inFlight()).isZero();
        verifyNoInteractions(publicAuth);
    }

    @Test
    void agentModeCannotBeSpoofedOrOverrideTheManagedToken() throws Exception {
        mvc.perform(get("/v1/models").header(AgentGatewayHeaders.MODE, "local")
            .header(AgentGatewayHeaders.CLIENT_TAG, TAG).header("Authorization", TOKEN))
            .andExpect(status().isUnauthorized());
        mvc.perform(get("/v1/models").header(AgentGatewayHeaders.MODE, "local")
            .header(AgentGatewayHeaders.SECRET, SECRET, "other")
            .header(AgentGatewayHeaders.CLIENT_TAG, TAG).header("Authorization", TOKEN))
            .andExpect(status().isUnauthorized());
        mvc.perform(get("/v1/models").header(AgentGatewayHeaders.MODE, "local")
            .header(AgentGatewayHeaders.SECRET, SECRET)
            .header(AgentGatewayHeaders.CLIENT_TAG, TAG).header("Authorization", "Bearer regular-key"))
            .andExpect(status().isUnauthorized());
        mvc.perform(get("/v1/models").header(AgentGatewayHeaders.MODE, "local")
            .header(AgentGatewayHeaders.SECRET, SECRET)
            .header(AgentGatewayHeaders.CLIENT_TAG, TAG).header("Authorization", TOKEN)
            .header("logos-key", "regular-key"))
            .andExpect(status().isUnauthorized());
        assertThat(forwarded.get()).isNull();
        verifyNoInteractions(publicAuth);
    }

    @Test
    void aCredentialForOneModeCannotBeUsedForTheOther() throws Exception {
        mvc.perform(post("/v1/messages").contentType("application/json").content("{}")
            .header(AgentGatewayHeaders.MODE, "cloud")
            .header(AgentGatewayHeaders.SECRET, SECRET)
            .header(AgentGatewayHeaders.CLIENT_TAG, TAG)
            .header("Authorization", TOKEN))
            .andExpect(status().isUnauthorized());
        mvc.perform(post("/v1/messages").contentType("application/json").content("{}")
            .header(AgentGatewayHeaders.MODE, "local")
            .header(AgentGatewayHeaders.SECRET, SECRET)
            .header(AgentGatewayHeaders.CLIENT_TAG, TAG)
            .header("Authorization", "Bearer " + "x".repeat(43)))
            .andExpect(status().isUnauthorized());
        assertThat(forwarded.get()).isNull();
        verifyNoInteractions(publicAuth, cloudRelay);
    }

    @Test
    void ordinaryExternalMessagesDoNotEnterEitherAgentRoute() throws Exception {
        when(publicAuth.requireActiveKey("regular-key"))
            .thenThrow(new ResponseStatusException(HttpStatus.FORBIDDEN, "public key route"));
        mvc.perform(post("/v1/messages").contentType("application/json").content("{}")
            .header("Authorization", "Bearer regular-key")).andExpect(status().isForbidden());
        verify(publicAuth).requireActiveKey("regular-key");
        assertThat(forwarded.get()).isNull();
    }
}
