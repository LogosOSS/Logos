package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.mock.web.MockHttpServletRequest;

/**
 * A request naming an answer-reuse context must reach the orchestrator, which
 * holds the answer cache, even when it names a cloud-only model that the
 * gateway would otherwise forward to the provider directly.
 */
class GatewayContextRoutingTest {

    private static MockHttpServletRequest request() {
        return new MockHttpServletRequest("POST", "/v1/chat/completions");
    }

    @ParameterizedTest
    @ValueSource(strings = {"logos-context", "logos_context", "Logos-Context"})
    void aContextHeaderKeepsTheRequestOnTheOrchestrator(String header) {
        MockHttpServletRequest request = request();
        request.addHeader(header, "shop-a");

        assertThat(InferenceGatewayController.requiresOrchestratorPolicy(request, "gpt-4o")).isTrue();
    }

    @Test
    void withoutAContextHeaderACloudModelMayGoDirect() {
        assertThat(InferenceGatewayController.requiresOrchestratorPolicy(request(), "gpt-4o")).isFalse();
    }

    @Test
    void aPolicyHeaderStillKeepsTheRequestOnTheOrchestrator() {
        MockHttpServletRequest request = request();
        request.addHeader("policy", "1");

        assertThat(InferenceGatewayController.requiresOrchestratorPolicy(request, "gpt-4o")).isTrue();
    }

    @Test
    void logosVModelsStillStayOnTheOrchestrator() {
        assertThat(InferenceGatewayController.requiresOrchestratorPolicy(request(), "logos-v2")).isTrue();
    }
}
