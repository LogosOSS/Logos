package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.argThat;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import java.net.URI;
import java.util.Map;
import java.util.UUID;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.NullSource;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.dao.DataAccessResourceFailureException;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.jdbc.core.namedparam.SqlParameterSource;
import org.springframework.web.server.ResponseStatusException;

import com.fasterxml.jackson.databind.ObjectMapper;

class ProviderSpendCapServiceTest {

    private static final String PATH = "/v1/chat/completions";
    private static final GatewayDeployment DEPLOYMENT = new GatewayDeployment(
        11, "test-model", 22, "synthetic", "cloud", "openai", "https://api.openai.com",
        null, null, null, null, null, null);
    private final NamedParameterJdbcTemplate jdbc = mock(NamedParameterJdbcTemplate.class);
    private final ObjectMapper mapper = new ObjectMapper();
    private final ProviderSpendCapService service = new ProviderSpendCapService(jdbc, mapper);

    @ParameterizedTest
    @CsvSource({
        "https://api.openai.com/v1/chat/completions, chat",
        "https://api.openai.com:443/v1/responses, responses",
        "https://api.anthropic.com/v1/messages, messages",
        "https://api.openai.com/v1/embeddings,",
        "http://api.openai.com/v1/responses,",
        "https://api.openai.com:444/v1/responses,",
        "https://api.openai.com/v1/responses?option=1,",
        "https://api.anthropic.com/v1/messages#fragment,",
        "https://other.invalid/v1/messages,"
    })
    void recognizesOnlySupportedOfficialRoutes(String url, String kind) {
        assertThat(ProviderSpendCapService.kindFor(URI.create(url))).isEqualTo(kind);
    }

    @Test
    void bindsVerifiedPayloadAndReturnsOnlyConfirmedReservation() {
        var body = mapper.createObjectNode().put("model", "test-model");
        when(jdbc.queryForObject(anyString(), any(SqlParameterSource.class),
            eq(String.class))).thenReturn("reserved", "uncapped");
        UUID attempt = service.reserve(DEPLOYMENT, PATH, null, body);
        assertThat(attempt).isNotNull();
        verify(jdbc).queryForObject(anyString(), argThat((SqlParameterSource p) ->
            attempt.equals(p.getValue("attempt")) && Integer.valueOf(22).equals(p.getValue("provider"))
                && Integer.valueOf(11).equals(p.getValue("model")) && "chat".equals(p.getValue("kind"))
                && body.toString().equals(p.getValue("payload"))), eq(String.class));

        assertThat(service.reserve(DEPLOYMENT, PATH, "option=1", body)).isNull();
        verify(jdbc).queryForObject(anyString(), argThat((SqlParameterSource p) ->
            p.getValue("kind") == null && p.getValue("payload") == null), eq(String.class));
    }

    @ParameterizedTest
    @NullSource
    @ValueSource(strings = {"exhausted", "unbounded", "unexpected"})
    void deniesUnconfirmedAdmissions(String result) {
        when(jdbc.queryForObject(anyString(), any(SqlParameterSource.class),
            eq(String.class))).thenReturn(result);
        int status = "exhausted".equals(result) ? 402 : "unbounded".equals(result) ? 422 : 503;
        assertThatThrownBy(() -> service.reserve(DEPLOYMENT, PATH, null, null))
            .isInstanceOfSatisfying(ResponseStatusException.class, e -> assertThat(e.getStatusCode().value())
                .isEqualTo(status));
    }

    @Test
    void treatsUnavailableLedgerAsUnavailableAdmission() {
        when(jdbc.queryForObject(anyString(), any(SqlParameterSource.class),
            eq(String.class))).thenThrow(new DataAccessResourceFailureException("synthetic"));
        assertThatThrownBy(() -> service.reserve(DEPLOYMENT, PATH, null, null))
            .isInstanceOfSatisfying(ResponseStatusException.class, e -> assertThat(e.getStatusCode().value())
                .isEqualTo(503));
    }

    @Test
    void settlesExplicitZeroCompletionFromTheProviderEnvelope() throws Exception {
        UUID attempt = UUID.randomUUID();
        var usage = GatewayUsageExtractor.fromEnvelope(mapper.readTree("""
            {"response":{"usage":{"input_tokens":12,"output_tokens":0}}}
            """));
        service.settle(attempt, new GatewayForwardResult(usage, null, null));
        verify(jdbc).queryForObject(anyString(), argThat((SqlParameterSource p) ->
            attempt.equals(p.getValue("attempt"))
                && p.getValue("usage").toString().contains("\"completion_tokens\":0")
                && p.getValue("usage").toString().contains("\"prompt_tokens\":12")
                && Boolean.FALSE.equals(p.getValue("notDispatched"))), eq(Boolean.class));
    }

    @Test
    void settlesZeroCountsRetainedAfterClientDisconnect() {
        UUID attempt = UUID.randomUUID();
        service.settle(attempt, new GatewayForwardResult(Map.of(
            "provider_delivered_nothing", 1L, "prompt_tokens", 0L, "completion_tokens", 0L), null, null));
        verify(jdbc).queryForObject(anyString(), argThat((SqlParameterSource p) ->
            attempt.equals(p.getValue("attempt")) && Boolean.FALSE.equals(p.getValue("notDispatched"))
                && !p.getValue("usage").toString().contains("provider_delivered_nothing")),
            eq(Boolean.class));
    }

    @Test
    void keepsHoldsOnUnknownUsageAndSettlesReportedUsageOrProofOfNoDispatch() {
        UUID attempt = UUID.randomUUID();
        service.releaseUndispatched(null);
        service.settle(null, new GatewayForwardResult(Map.of(), null, null));
        service.settle(attempt, null);
        service.settle(attempt, new GatewayForwardResult(null, null, null));
        service.settle(attempt, new GatewayForwardResult(Map.of("prompt_tokens", 3L), null, null));
        service.settle(attempt, new GatewayForwardResult(Map.of("prompt_tokens", 3L, "completion_tokens", -1L),
            null, null));
        service.settle(attempt, new GatewayForwardResult(
            Map.of("provider_delivered_nothing", 1L), null, null));
        service.settle(attempt, new GatewayForwardResult(
            Map.of("usage_estimated", 1L, "prompt_tokens", 3L, "completion_tokens", 4L), null, null));
        verifyNoInteractions(jdbc);

        service.settle(attempt, new GatewayForwardResult(Map.of("prompt_tokens", 3L, "completion_tokens", 4L),
            null, "flex"));
        verify(jdbc).queryForObject(anyString(), argThat((SqlParameterSource p) ->
            attempt.equals(p.getValue("attempt")) && "flex".equals(p.getValue("tier"))
                && p.getValue("usage").toString().contains("\"completion_tokens\":4")
                && Boolean.FALSE.equals(p.getValue("notDispatched"))), eq(Boolean.class));

        service.releaseUndispatched(attempt);
        verify(jdbc).queryForObject(anyString(), argThat((SqlParameterSource p) ->
            attempt.equals(p.getValue("attempt")) && p.getValue("usage") == null
                && Boolean.TRUE.equals(p.getValue("notDispatched"))), eq(Boolean.class));
    }
}
