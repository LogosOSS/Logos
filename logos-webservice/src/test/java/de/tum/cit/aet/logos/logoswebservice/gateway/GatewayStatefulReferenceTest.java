package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.junit.jupiter.api.Test;
import org.springframework.web.server.ResponseStatusException;

import com.fasterxml.jackson.databind.ObjectMapper;

/** Provider-side conversation state is shared across teams, so references to it are refused. */
class GatewayStatefulReferenceTest {

    private final ObjectMapper mapper = new ObjectMapper();

    @Test
    void previousResponseIdIsRejected() throws Exception {
        var root = mapper.readTree("{\"model\":\"gpt-5\",\"previous_response_id\":\"resp_other_team\",\"input\":\"repeat\"}");
        assertThatThrownBy(() -> InferenceGatewayController.rejectStatefulReferences(root))
            .isInstanceOf(ResponseStatusException.class)
            .hasMessageContaining("previous_response_id");
    }

    @Test
    void conversationIsRejected() throws Exception {
        var root = mapper.readTree("{\"model\":\"gpt-5\",\"conversation\":\"conv_123\",\"input\":\"hi\"}");
        assertThatThrownBy(() -> InferenceGatewayController.rejectStatefulReferences(root))
            .isInstanceOf(ResponseStatusException.class);
    }

    @Test
    void statelessAndExplicitNullReferencesPass() throws Exception {
        var root = mapper.readTree("{\"model\":\"gpt-5\",\"previous_response_id\":null,\"input\":\"hi\"}");
        assertThatCode(() -> InferenceGatewayController.rejectStatefulReferences(root)).doesNotThrowAnyException();
    }
}
