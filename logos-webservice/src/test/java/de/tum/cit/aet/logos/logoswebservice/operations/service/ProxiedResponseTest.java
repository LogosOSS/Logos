package de.tum.cit.aet.logos.logoswebservice.operations.service;

import static org.assertj.core.api.Assertions.assertThat;

import java.nio.charset.StandardCharsets;

import org.junit.jupiter.api.Test;

import de.tum.cit.aet.logos.logoswebservice.operations.service.BatchService.ProxiedResponse;

class ProxiedResponseTest {

    private static ProxiedResponse response(int status, String body) {
        return new ProxiedResponse(status, "application/json",
            body == null ? null : body.getBytes(StandardCharsets.UTF_8));
    }

    @Test
    void equalBodiesMakeEqualResponses() {
        assertThat(response(200, "{}")).isEqualTo(response(200, "{}")).hasSameHashCodeAs(response(200, "{}"));
    }

    @Test
    void differentFieldsMakeDifferentResponses() {
        ProxiedResponse base = response(200, "{}");

        assertThat(base).isNotEqualTo(response(201, "{}"));
        assertThat(base).isNotEqualTo(response(200, "[]"));
        assertThat(base).isNotEqualTo(response(200, null));
        assertThat(base).isNotEqualTo(new ProxiedResponse(200, "text/plain", "{}".getBytes(StandardCharsets.UTF_8)));
        assertThat(base).isNotEqualTo("{}");
    }

    @Test
    void toStringNamesTheBodySizeNotItsContent() {
        assertThat(response(200, "secret").toString()).contains("body=6 bytes").doesNotContain("secret");
        assertThat(response(204, null).toString()).contains("body=null");
    }
}
