package de.tum.cit.aet.logos.logoswebservice.operations.service;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.stream.Collectors;

import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import de.tum.cit.aet.logos.logoswebservice.configuration.repository.TokenPriceRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.ApiKeyRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.ModelAccessProjection;

/** Standard token rates for an estimate, scoped to the selected key's accessible deployments. */
@Service
public class BatchPriceService {
    private final ApiKeyRepository keys;
    private final TokenPriceRepository prices;

    public BatchPriceService(ApiKeyRepository keys, TokenPriceRepository prices) {
        this.keys = keys;
        this.prices = prices;
    }

    /** Prices are micro-cents per 1000 tokens; null means a permitted deployment has no known rate. */
    public record ModelPrice(String model_name, Long input_price_per_k_tokens, Long output_price_per_k_tokens) {}

    @Transactional(readOnly = true)
    public List<ModelPrice> pricesForKey(int userId, int keyId) {
        var key = keys.findById(keyId).orElse(null);
        if (key == null || !Boolean.TRUE.equals(key.getIsActive())
                || key.getUserId() == null || key.getUserId() != userId) {
            throw new BatchService.KeyNotOwnedException("This API key does not belong to you.");
        }
        var accessible = Boolean.TRUE.equals(key.getUseCustomPermissions())
            ? keys.findAccessibleModelsByKey(keyId)
            : key.getTeamId() == null ? List.<ModelAccessProjection>of()
                : keys.findAccessibleModelsByTeam(key.getTeamId());
        var byModel = accessible.stream().collect(Collectors.groupingBy(
            ModelAccessProjection::getModelId, LinkedHashMap::new, Collectors.toList()));
        return byModel.entrySet().stream().map(entry -> {
            var deployments = entry.getValue();
            return new ModelPrice(deployments.getFirst().getModelName(),
                highestRate(deployments, "billed_input_uncached"),
                highestRate(deployments, "billed_output_text"));
        }).toList();
    }

    /** Use billing's highest context tier per deployment, then the maximum across permitted providers. */
    private Long highestRate(List<ModelAccessProjection> deployments, String quantity) {
        long highest = 0;
        for (var deployment : deployments) {
            if ("logosnode".equals(deployment.getProviderType())) continue;
            Long rate = prices.resolveCurrentDefaultUnitPrice(quantity, deployment.getModelId(),
                deployment.getProviderId(), Long.MAX_VALUE);
            if (rate == null) return null;
            highest = Math.max(highest, rate);
        }
        return highest;
    }
}
