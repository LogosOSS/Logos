package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.doThrow;
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
import org.springframework.dao.DataAccessResourceFailureException;
import org.springframework.web.server.ResponseStatusException;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

class ProviderSpendCapServiceTest {
    private static final String PATH = "/v1/chat/completions";
    private static final GatewayDeployment DEPLOYMENT = new GatewayDeployment(
        11, "test-model", 22, "synthetic", "cloud", "openai", "https://api.openai.com",
        null, null, null, null, null, null);
    private final ProviderSpendLedger ledger = mock(ProviderSpendLedger.class);
    private final ObjectMapper mapper = new ObjectMapper();
    private final ProviderSpendCapService service = new ProviderSpendCapService(ledger);

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
        "https://api.openai.com/v1/%72esponses,",
        "https://other.invalid/v1/messages,"
    })
    void recognizesOnlySupportedOfficialRoutes(String url, String kind) {
        assertThat(ProviderSpendCapService.kindFor(URI.create(url))).isEqualTo(kind);
    }

    @Test
    void bindsVerifiedPayloadAndReturnsOnlyConfirmedReservation() {
        var body = mapper.createObjectNode().put("model", "test-model");
        when(ledger.reserve(any(), eq(22), eq(11), eq("https://api.openai.com" + PATH), eq(body)))
            .thenReturn(ProviderSpendLedger.Decision.RESERVED);
        when(ledger.reserve(any(), eq(22), eq(11), isNull(), eq(body)))
            .thenReturn(ProviderSpendLedger.Decision.UNCAPPED);
        UUID attempt = service.reserve(DEPLOYMENT, PATH, null, body);
        assertThat(attempt).isNotNull();
        verify(ledger).reserve(eq(attempt), eq(22), eq(11), eq("https://api.openai.com" + PATH), eq(body));
        assertThat(service.reserve(DEPLOYMENT, PATH, "option=1", body)).isNull();
    }

    @ParameterizedTest
    @CsvSource({"EXHAUSTED,402", "UNBOUNDED,422", "DUPLICATE,503"})
    void deniesUnconfirmedAdmissions(ProviderSpendLedger.Decision result, int status) {
        when(ledger.reserve(any(), anyInt(), anyInt(), isNull(), isNull())).thenReturn(result);
        assertThatThrownBy(() -> service.reserve(DEPLOYMENT, PATH, null, null))
            .isInstanceOfSatisfying(ResponseStatusException.class,
                e -> assertThat(e.getStatusCode().value()).isEqualTo(status));
    }

    @Test
    void treatsUnavailableLedgerAsUnavailableAdmission() {
        when(ledger.reserve(any(), anyInt(), anyInt(), isNull(), isNull()))
            .thenThrow(new DataAccessResourceFailureException("synthetic"));
        assertThatThrownBy(() -> service.reserve(DEPLOYMENT, PATH, null, null))
            .isInstanceOfSatisfying(ResponseStatusException.class,
                e -> assertThat(e.getStatusCode().value()).isEqualTo(503));
    }

    @Test
    void settlesExplicitZeroCompletionFromTheProviderEnvelope() throws Exception {
        UUID attempt = UUID.randomUUID();
        var usage = GatewayUsageExtractor.fromEnvelope(mapper.readTree("""
            {"response":{"usage":{"input_tokens":12,"output_tokens":0}}}
            """));
        service.settle(attempt, new GatewayForwardResult(usage, null, null));
        verify(ledger).settle(attempt, usage, null, false);
    }

    @Test
    void keepsHoldsOnUnknownUsageAndSettlesReportedUsageOrProofOfNoDispatch() {
        UUID attempt = UUID.randomUUID();
        service.releaseUndispatched(null);
        service.settle(attempt, new GatewayForwardResult(Map.of("prompt_tokens", 3L), null, null));
        service.settle(attempt, new GatewayForwardResult(Map.of("prompt_tokens", 3L, "completion_tokens", -1L),
            null, null));
        service.settle(attempt, new GatewayForwardResult(Map.of("usage_estimated", 1L, "prompt_tokens", 3L,
            "completion_tokens", 4L), null, null));
        verifyNoInteractions(ledger);

        service.settle(attempt, new GatewayForwardResult(Map.of("provider_delivered_nothing", 1L,
            "prompt_tokens", 0L, "completion_tokens", 0L), null, null));
        verify(ledger).settle(attempt, Map.of("prompt_tokens", 0L, "completion_tokens", 0L), null, false);
        service.releaseUndispatched(attempt);
        verify(ledger).settle(attempt, null, null, true);
    }

    @Test
    void internalGateRequiresExactSecretAndReturnsOnlyAttemptId() {
        UUID attempt = UUID.randomUUID();
        ProviderSpendCapService cap = mock(ProviderSpendCapService.class);
        var controller = new InternalProviderSpendController(cap, mapper, "synthetic-secret");
        var request = new InternalProviderSpendController.Reserve(12, 13, "https://api.openai.com/v1/responses",
            Map.of());
        for (String bearer : new String[] {null, "synthetic-secret", "Bearer wrong"}) {
            assertThatThrownBy(() -> controller.reserve(bearer, request))
                .isInstanceOfSatisfying(ResponseStatusException.class,
                    error -> assertThat(error.getStatusCode().value()).isEqualTo(401));
        }
        verifyNoInteractions(cap);
        when(cap.reserve(eq(12), eq(13), eq("https://api.openai.com/v1/responses"), any(JsonNode.class)))
            .thenReturn(attempt);
        assertThat(controller.reserve("Bearer synthetic-secret", request).attemptId()).isEqualTo(attempt);
    }

    @Test
    void settleChecksSecretAndAllowsOnlyReportedUseOrExplicitNoDispatch() {
        var controller = new InternalProviderSpendController(service, mapper, "synthetic-secret");
        UUID attempt = UUID.randomUUID();
        String auth = "Bearer synthetic-secret";
        Map<String, Long> usage = Map.of("prompt_tokens", 2L, "completion_tokens", 4L);
        assertThatThrownBy(() -> controller.settle("Bearer wrong",
            new InternalProviderSpendController.Settle(attempt, usage, null, false)))
            .isInstanceOfSatisfying(ResponseStatusException.class, e -> assertThat(e.getStatusCode().value()).isEqualTo(401));
        assertThatThrownBy(() -> controller.settle(auth,
            new InternalProviderSpendController.Settle(null, usage, null, false)))
            .isInstanceOfSatisfying(ResponseStatusException.class, e -> assertThat(e.getStatusCode().value()).isEqualTo(400));
        assertThatThrownBy(() -> controller.reserve(auth,
            new InternalProviderSpendController.Reserve(0, 2, null, Map.of())))
            .isInstanceOfSatisfying(ResponseStatusException.class, e -> assertThat(e.getStatusCode().value()).isEqualTo(400));
        assertThatThrownBy(() -> controller.reserve(auth,
            new InternalProviderSpendController.Reserve(1, -2, null, Map.of())))
            .isInstanceOfSatisfying(ResponseStatusException.class, e -> assertThat(e.getStatusCode().value()).isEqualTo(400));
        verifyNoInteractions(ledger);

        controller.settle(auth, new InternalProviderSpendController.Settle(attempt, usage, "flex", false));
        verify(ledger).settle(attempt, usage, "flex", false);
        doThrow(new DataAccessResourceFailureException("synthetic ledger unavailable"))
            .when(ledger).settle(attempt, null, null, true);
        controller.settle(auth, new InternalProviderSpendController.Settle(attempt, null, null, true));
        verify(ledger).settle(attempt, null, null, true);
    }

}
