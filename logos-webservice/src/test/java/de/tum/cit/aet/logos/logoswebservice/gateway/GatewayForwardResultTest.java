package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThat;

import java.nio.charset.StandardCharsets;
import java.util.Map;

import org.junit.jupiter.api.Test;

class GatewayForwardResultTest {

    private static GatewayForwardResult result(String body) {
        return new GatewayForwardResult(Map.of("input_tokens", 3L),
            body == null ? null : body.getBytes(StandardCharsets.UTF_8), "flex");
    }

    @Test
    void equalBodiesMakeEqualResults() {
        GatewayForwardResult left = result("{\"ok\":true}");
        GatewayForwardResult right = result("{\"ok\":true}");

        assertThat(left).isEqualTo(right).hasSameHashCodeAs(right);
    }

    @Test
    void differentBodiesOrFieldsMakeDifferentResults() {
        GatewayForwardResult base = result("a");

        assertThat(base).isNotEqualTo(result("b"));
        assertThat(base).isNotEqualTo(result(null));
        assertThat(base).isNotEqualTo(new GatewayForwardResult(Map.of(), "a".getBytes(StandardCharsets.UTF_8), "flex"));
        assertThat(base).isNotEqualTo(new GatewayForwardResult(Map.of("input_tokens", 3L),
            "a".getBytes(StandardCharsets.UTF_8), null));
        assertThat(base).isNotEqualTo("a");
    }

    @Test
    void toStringNamesTheBodySizeNotItsContent() {
        assertThat(result("secret").toString()).contains("responseBody=6 bytes").doesNotContain("secret");
        assertThat(GatewayForwardResult.none().toString()).contains("responseBody=null");
    }
}
