package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.web.server.ResponseStatusException;
import org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody;

import de.tum.cit.aet.logos.logoswebservice.identity.entity.ApiKeyType;

/**
 * The Responses {@code image_generation} tool on the direct cloud forward: the
 * request stays in the gateway, the key needs the image model, and only an Azure
 * upstream gets the header that names the image deployment.
 */
class GatewayImageGenerationToolTest {

    private static final ObjectMapper JSON = new ObjectMapper();

    private HttpServer upstream;
    private final AtomicReference<String> seenImageHeader = new AtomicReference<>();
    private final AtomicReference<Boolean> upstreamCalled = new AtomicReference<>(false);

    private final GatewayAuthService authService = mock(GatewayAuthService.class);
    private final GatewayCloudAccounting cloudAccounting = mock(GatewayCloudAccounting.class);
    private final GatewayCloudRateLimiter cloudRateLimiter = mock(GatewayCloudRateLimiter.class);
    private final GatewayOrchestratorProxy orchestratorProxy = mock(GatewayOrchestratorProxy.class);
    private InferenceGatewayController controller;

    @BeforeEach
    void start() throws IOException {
        upstream = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        upstream.createContext("/", exchange -> {
            upstreamCalled.set(true);
            seenImageHeader.set(exchange.getRequestHeaders().getFirst(
                GatewayImageGenerationTool.AZURE_IMAGE_DEPLOYMENT_HEADER));
            exchange.getRequestBody().readAllBytes();
            byte[] out = "{\"id\":\"resp_1\",\"output\":[]}".getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, out.length);
            exchange.getResponseBody().write(out);
            exchange.close();
        });
        upstream.start();

        GatewayKey key = new GatewayKey(1, "lg-test", "k", ApiKeyType.developer, 1, null, "test",
            false, null, 0, "BILLING");
        when(authService.requireActiveKey(any())).thenReturn(key);
        when(cloudAccounting.admitAndReserve(any(), any(), any(), any(), any(), any())).thenReturn(1);
        controller = new InferenceGatewayController(true, authService, cloudAccounting,
            new GatewayCloudForwarder(new ObjectMapper(), cloudAccounting), cloudRateLimiter,
            orchestratorProxy, new ObjectMapper());
    }

    @AfterEach
    void stop() {
        if (upstream != null) {
            upstream.stop(0);
        }
    }

    private String base() {
        return "http://127.0.0.1:" + upstream.getAddress().getPort();
    }

    private GatewayDeployment azure(int modelId, String model, String deployment, String operation) {
        return new GatewayDeployment(modelId, model, 8, "Azure", "cloud", "azure", base() + "/openai/deployments/",
            base() + "/openai/deployments/" + deployment + "/" + operation + "?api-version=x",
            "api-key", "{}", "az", "CLOUD_IN_EU_BY_US_PROVIDER", null);
    }

    private GatewayDeployment openai(int modelId, String model) {
        return new GatewayDeployment(modelId, model, 7, "OpenAI", "cloud", "openai", base() + "/v1", null,
            "Authorization", "Bearer {}", "sk", "CLOUD_NOT_IN_EU_BY_US_PROVIDER", null);
    }

    private ResponseEntity<StreamingResponseBody> post(String body) throws IOException {
        MockHttpServletRequest request = new MockHttpServletRequest("POST", "/v1/responses");
        request.setServletPath("/v1/responses");
        request.addHeader("Authorization", "Bearer lg-test");
        request.setContentType("application/json");
        request.setContent(body.getBytes(StandardCharsets.UTF_8));
        ResponseEntity<StreamingResponseBody> response = controller.handle(request, new MockHttpServletResponse());
        if (response.getBody() != null) {
            response.getBody().writeTo(new ByteArrayOutputStream());
        }
        return response;
    }

    private static final String IMAGE_REQUEST = "{\"model\":\"gpt-5.6-luna\",\"input\":\"draw\","
        + "\"tools\":[{\"type\":\"image_generation\",\"model\":\"gpt-image-1.5\"}]}";

    @Test
    void anAzureRequestWithTheImageToolNamesTheImageDeploymentAndStaysInTheGateway() throws Exception {
        when(authService.deploymentsForModel(any(), eq("gpt-5.6-luna")))
            .thenReturn(List.of(azure(1, "gpt-5.6-luna", "gpt-5.6-luna", "responses")));
        when(authService.deploymentsForModel(any(), eq("gpt-image-1.5")))
            .thenReturn(List.of(azure(2, "gpt-image-1.5", "img-15", "images/generations")));

        ResponseEntity<StreamingResponseBody> response = post(IMAGE_REQUEST);

        assertThat(response.getStatusCode().value()).isEqualTo(200);
        assertThat(seenImageHeader.get()).isEqualTo("img-15");
        verify(orchestratorProxy, never()).proxy(any(), any(), any());
    }

    @Test
    void anAzureRequestWithoutTheImageToolGetsNoHeaderAndNoImageLookup() throws Exception {
        when(authService.deploymentsForModel(any(), eq("gpt-5.6-luna")))
            .thenReturn(List.of(azure(1, "gpt-5.6-luna", "gpt-5.6-luna", "responses")));

        post("{\"model\":\"gpt-5.6-luna\",\"input\":\"hi\"}");

        assertThat(upstreamCalled.get()).isTrue();
        assertThat(seenImageHeader.get()).isNull();
        verify(authService, never()).deploymentsForModel(any(), eq(GatewayImageGenerationTool.DEFAULT_IMAGE_MODEL));
    }

    @Test
    void aNonAzureUpstreamGetsNoImageHeader() throws Exception {
        when(authService.deploymentsForModel(any(), eq("gpt-5.6-luna"))).thenReturn(List.of(openai(1, "gpt-5.6-luna")));
        when(authService.deploymentsForModel(any(), eq("gpt-image-1.5"))).thenReturn(List.of(openai(2, "gpt-image-1.5")));

        post(IMAGE_REQUEST);

        assertThat(upstreamCalled.get()).isTrue();
        assertThat(seenImageHeader.get()).isNull();
    }

    @Test
    void anImageModelTheKeyMayNotUseIsRefusedBeforeAdmission() {
        when(authService.deploymentsForModel(any(), eq("gpt-5.6-luna")))
            .thenReturn(List.of(azure(1, "gpt-5.6-luna", "gpt-5.6-luna", "responses")));
        when(authService.deploymentsForModel(any(), eq("gpt-image-1.5"))).thenReturn(List.of());

        assertThatThrownBy(() -> post(IMAGE_REQUEST))
            .isInstanceOfSatisfying(ResponseStatusException.class,
                e -> assertThat(e.getStatusCode()).isEqualTo(HttpStatus.FORBIDDEN));
        assertThat(upstreamCalled.get()).isFalse();
        verify(cloudAccounting, never()).admitAndReserve(any(), any(), any(), any(), any(), any());
    }

    @Test
    void theToolModelDefaultsAndDuplicatesCollapse() throws Exception {
        assertThat(GatewayImageGenerationTool.requestedImageModels(JSON.readTree(
            "{\"tools\":[{\"type\":\"image_generation\"},{\"type\":\"image_generation\",\"model\":\"GPT-IMAGE-1\"},"
                + "{\"type\":\"function\",\"name\":\"image_generation\"}]}")))
            .containsExactly("gpt-image-1");
        assertThat(GatewayImageGenerationTool.requestedImageModels(JSON.readTree("{\"model\":\"x\"}"))).isEmpty();
        assertThat(GatewayImageGenerationTool.requestedImageModels(null)).isEmpty();
    }

    @Test
    void theHeaderOnlyNamesAnImageDeploymentOnTheSameResource() {
        GatewayDeployment chat = azure(1, "gpt-5.6-luna", "gpt-5.6-luna", "responses");
        GatewayDeployment elsewhere = new GatewayDeployment(2, "gpt-image-1.5", 9, "Other", "cloud", "azure",
            "https://other/openai/deployments/", "https://other/openai/deployments/img/images/generations",
            "api-key", "{}", "az", "CLOUD_IN_EU_BY_US_PROVIDER", null);

        assertThat(GatewayImageGenerationTool.azureHeaders(chat, List.of(elsewhere))).isEmpty();
        assertThat(GatewayImageGenerationTool.azureHeaders(chat,
            List.of(elsewhere, azure(2, "gpt-image-1.5", "img-15", "images/generations"))))
            .isEqualTo(Map.of(GatewayImageGenerationTool.AZURE_IMAGE_DEPLOYMENT_HEADER, "img-15"));
    }
}
