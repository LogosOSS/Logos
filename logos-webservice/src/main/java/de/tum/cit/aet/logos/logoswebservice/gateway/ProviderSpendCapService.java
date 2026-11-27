package de.tum.cit.aet.logos.logoswebservice.gateway;

import java.net.URI;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.web.server.ResponseStatusException;

import com.fasterxml.jackson.databind.JsonNode;

/** Budget one upstream attempt through the ledger shared with the orchestrator. */
@Service
public class ProviderSpendCapService {

    private static final Logger log = LoggerFactory.getLogger(ProviderSpendCapService.class);

    private final ProviderSpendLedger ledger;

    public ProviderSpendCapService(ProviderSpendLedger ledger) {
        this.ledger = ledger;
    }

    UUID reserve(GatewayDeployment deployment, String inferencePath, String query, JsonNode payload) {
        String target = null;
        if ((query == null || query.isEmpty()) && payload != null
                && ProviderSpendBounds.standardAuth(deployment.cloudProviderType(), deployment.authName())
                && deployment.modelName().equals(payload.path("model").asText(null))) {
            target = CloudForwardUrlBuilder.build(deployment.baseUrl(), inferencePath, deployment.endpoint());
        }
        return reserve(deployment.providerId(), deployment.modelId(), target, payload);
    }

    public UUID reserve(int provider, Integer model, String target, JsonNode payload) {
        UUID attempt = UUID.randomUUID();
        ProviderSpendLedger.Decision decision;
        try {
            decision = ledger.reserve(attempt, provider, model, target, payload);
        } catch (org.springframework.dao.DataAccessException | org.springframework.transaction.TransactionException e) {
            log.warn("Could not check provider spend limit for provider {}", provider, e);
            throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE,
                "Provider monthly spend limit is unavailable");
        }
        return switch (decision) {
            case UNCAPPED -> null;
            case RESERVED -> attempt;
            case EXHAUSTED -> throw new ResponseStatusException(HttpStatus.PAYMENT_REQUIRED,
                "Provider monthly spend limit reached");
            case UNBOUNDED -> throw new ResponseStatusException(HttpStatus.UNPROCESSABLE_ENTITY,
                "Request cost cannot be bounded under this provider's monthly spend limit");
            case DUPLICATE -> throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE,
                "Provider monthly spend reservation could not be confirmed");
        };
    }

    public void releaseUndispatched(UUID attempt) {
        if (attempt != null) {
            settle(attempt, null, null, true);
        }
    }

    void settle(UUID attempt, GatewayForwardResult result) {
        if (attempt == null || result == null) {
            return;
        }
        Map<String, Long> usage = result.usage();
        if (usage == null || usage.containsKey("usage_estimated")) {
            return;
        }
        Long prompt = usage.get("prompt_tokens");
        Long completion = usage.get("completion_tokens");
        if (!reportedCount(prompt) || !reportedCount(completion)
                || (usage.containsKey("provider_delivered_nothing") && (prompt != 0L || completion != 0L))) {
            return;
        }
        if (usage.containsKey("provider_delivered_nothing")) {
            usage = new LinkedHashMap<>(usage);
            usage.remove("provider_delivered_nothing");
        }
        settle(attempt, usage, result.serviceTier(), false);
    }

    public void settle(UUID attempt, Map<String, Long> usage, String tier) {
        settle(attempt, new GatewayForwardResult(usage, null, tier));
    }

    private static boolean reportedCount(Long count) {
        return count != null && count >= 0;
    }

    private void settle(UUID attempt, Map<String, Long> usage, String tier, boolean notDispatched) {
        try {
            ledger.settle(attempt, usage, tier, notDispatched);
        } catch (org.springframework.dao.DataAccessException | org.springframework.transaction.TransactionException e) {
            // An unknown outcome keeps its full hold.
            log.warn("Could not settle provider spend attempt {}; keeping reservation", attempt, e);
        }
    }

    static String kindFor(URI target) {
        return ProviderSpendBounds.kind(target);
    }
}
