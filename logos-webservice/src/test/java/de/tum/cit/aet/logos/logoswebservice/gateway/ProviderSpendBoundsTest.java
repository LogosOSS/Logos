package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.stream.Stream;

import com.fasterxml.jackson.databind.ObjectMapper;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;
import org.junit.jupiter.params.provider.NullSource;
import org.junit.jupiter.params.provider.ValueSource;

class ProviderSpendBoundsTest {
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String OPENAI = "https://api.openai.com/v1/";
    private static final String MESSAGES = "https://api.anthropic.com/v1/messages";
    private static final String OPENAI_DOC = "https://developers.openai.com/api/docs/models";
    private static final String ANTHROPIC_DOC = "https://platform.claude.com/docs/en/models/overview";

    @Test
    void approvesOnlyExactOperationAndAuthorityForReviewEvidence() {
        assertThat(ProviderSpendBounds.trustedEvidence(OPENAI + "chat/completions", "snapshot-2026-11-01",
            "max_completion_tokens", OPENAI_DOC)).isTrue();
        assertThat(ProviderSpendBounds.trustedEvidence(OPENAI + "chat/completions", "snapshot-2026-11-01",
            "max_tokens", OPENAI_DOC)).isTrue();
        assertThat(ProviderSpendBounds.trustedEvidence(OPENAI + "responses", "snapshot-2026-11-01",
            "max_output_tokens", OPENAI_DOC)).isTrue();
        assertThat(ProviderSpendBounds.trustedEvidence(MESSAGES, "snapshot-20261101",
            "max_tokens", ANTHROPIC_DOC)).isTrue();

        assertThat(ProviderSpendBounds.trustedEvidence(OPENAI + "responses", "snapshot-2026-11-01",
            "max_tokens", OPENAI_DOC)).isFalse();
        assertThat(ProviderSpendBounds.trustedEvidence(MESSAGES, "snapshot-20261101",
            "max_tokens", OPENAI_DOC)).isFalse();
        assertThat(ProviderSpendBounds.trustedEvidence(OPENAI + "chat/completions", "snapshot-2026-11-01",
            "max_completion_tokens", "https://example.test/model")).isFalse();
        assertThat(ProviderSpendBounds.trustedEvidence(MESSAGES, "snapshot-20261101",
            "max_completion_tokens", ANTHROPIC_DOC)).isFalse();
        assertThat(ProviderSpendBounds.trustedEvidence(MESSAGES, "latest",
            "max_tokens", ANTHROPIC_DOC)).isFalse();
        assertThat(ProviderSpendBounds.trustedEvidence(OPENAI + "responses", null,
            "max_output_tokens", OPENAI_DOC)).isFalse();
    }

    @ParameterizedTest
    @NullSource
    @ValueSource(strings = {"https://developers.openai.com", "https://developers.openai.com/",
        "https://developers.openai.com/docs?preview=1", "not a URL", "http://developers.openai.com/docs"})
    void rejectsMissingOrUnsuitableDocumentation(String source) {
        assertThat(ProviderSpendBounds.trustedEvidence(OPENAI + "responses", "snapshot-2026-11-01",
            "max_output_tokens", source)).isFalse();
    }

    @Test
    void canonicalizesOnlyWellFormedOfficialTargetsAndUsesStandardAuth() {
        assertThat(ProviderSpendBounds.officialUrl("https://API.OPENAI.COM:443/v1/responses"))
            .isEqualTo(OPENAI + "responses");
        assertThat(ProviderSpendBounds.officialUrl("not a URL")).isNull();
        assertThat(ProviderSpendBounds.officialUrl(OPENAI + "responses?version=preview")).isNull();
        assertThat(ProviderSpendBounds.officialUrl("https://api.openai.com/v1/models")).isNull();
        assertThat(ProviderSpendBounds.officialUrl("https://example.test/v1/responses")).isNull();
        assertThat(ProviderSpendBounds.standardAuth("openai", null)).isTrue();
        assertThat(ProviderSpendBounds.standardAuth("openai", " ")).isTrue();
        assertThat(ProviderSpendBounds.standardAuth("openai", " Authorization ")).isTrue();
        assertThat(ProviderSpendBounds.standardAuth("anthropic", "X-API-Key")).isTrue();
        assertThat(ProviderSpendBounds.standardAuth("anthropic", "Authorization")).isFalse();
    }

    static Stream<Arguments> requestShapes() {
        return Stream.of(
            Arguments.of("responses", "max_output_tokens",
                "{\"input\":\"hi\",\"instructions\":\"one answer\",\"max_output_tokens\":8}", 8),
            Arguments.of("responses", "max_output_tokens", "{\"input\":\"hi\",\"max_output_tokens\":10}", 10),
            Arguments.of("responses", "max_output_tokens", "{\"input\":[{}],\"max_output_tokens\":8}", null),
            Arguments.of("responses", "max_output_tokens", "{\"input\":\"hi\",\"instructions\":[],\"max_output_tokens\":8}", null),
            Arguments.of("responses", "max_output_tokens", "{\"input\":\"hi\",\"tools\":[],\"max_output_tokens\":8}", null),
            Arguments.of("chat", "max_completion_tokens",
                "{\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"n\":1,\"max_completion_tokens\":8}", 8),
            Arguments.of("chat", "max_tokens", "{\"messages\":[],\"n\":2,\"max_tokens\":8}", null),
            Arguments.of("chat", "max_tokens", "{\"messages\":[],\"n\":1.5,\"max_tokens\":8}", null),
            Arguments.of("chat", "max_tokens", "{\"messages\":[],\"n\":2147483648,\"max_tokens\":8}", null),
            Arguments.of("chat", "max_tokens", "{\"messages\":[],\"max_tokens\":8,\"max_completion_tokens\":8}", null),
            Arguments.of("chat", "max_tokens", "{\"messages\":[null],\"max_tokens\":8}", null),
            Arguments.of("chat", "max_tokens", "{\"messages\":[{\"content\":\"hi\"}],\"max_tokens\":8}", null),
            Arguments.of("chat", "max_tokens", "{\"messages\":[{\"role\":\"user\",\"content\":[]}],\"max_tokens\":8}", null),
            Arguments.of("chat", "max_tokens", "{\"messages\":[{\"role\":\"user\",\"content\":\"hi\",\"extra\":1}],\"max_tokens\":8}", null),
            Arguments.of("chat", "max_tokens", "{\"messages\":{},\"max_tokens\":8}", null),
            Arguments.of("chat", "max_tokens", "{\"messages\":[],\"max_tokens\":2147483648}", null),
            Arguments.of("chat", "max_tokens", "{\"messages\":[],\"max_tokens\":0}", null),
            Arguments.of("chat", "max_tokens", "{\"messages\":[],\"max_tokens\":17}", null),
            Arguments.of("messages", "max_tokens", "{\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"hi\"}]}],\"system\":\"a role\",\"max_tokens\":8}", 8),
            Arguments.of("messages", "max_tokens", "{\"messages\":[],\"system\":[],\"max_tokens\":8}", null),
            Arguments.of("messages", "max_tokens", "{\"messages\":[{\"role\":\"user\",\"content\":[1]}],\"max_tokens\":8}", null),
            Arguments.of("messages", "max_tokens", "{\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"image\"}]}],\"max_tokens\":8}", null),
            Arguments.of("messages", "max_tokens", "{\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":5}]}],\"max_tokens\":8}", null),
            Arguments.of("messages", "max_tokens", "{\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"hi\",\"extra\":1}]}],\"max_tokens\":8}", null),
            Arguments.of("messages", "max_tokens", "{\"messages\":[{\"role\":\"user\",\"content\":42}],\"max_tokens\":8}", null),
            Arguments.of("responses", "max_output_tokens", "[]", null)
        );
    }

    @ParameterizedTest
    @MethodSource("requestShapes")
    void boundsOnlyProvenTextShapesAndOutputLimits(String operation, String field, String json, Integer expected)
            throws Exception {
        assertThat(ProviderSpendBounds.outputLimit(operation, field, MAPPER.readTree(json), 16))
            .isEqualTo(expected);
    }
}
