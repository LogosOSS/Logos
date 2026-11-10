package de.tum.cit.aet.logos.logoswebservice.operations.service;

import java.time.Instant;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import de.tum.cit.aet.logos.logoswebservice.configuration.entity.TokenPrice;
import de.tum.cit.aet.logos.logoswebservice.configuration.entity.TokenType;
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.TokenPriceRepository;
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.TokenTypeRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.ApiKeyRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.ModelAccessProjection;

/** Standard token rates for an estimate, scoped to the selected key's accessible deployments. */
@Service
public class BatchPriceService {
    private final ApiKeyRepository keys;
    private final TokenPriceRepository prices;
    private final TokenTypeRepository types;

    public BatchPriceService(ApiKeyRepository keys, TokenPriceRepository prices, TokenTypeRepository types) {
        this.keys = keys;
        this.prices = prices;
        this.types = types;
    }

    /** Prices are micro-cents per 1000 tokens; null means a permitted deployment has no known rate. */
    public record ModelPrice(String model_name, Long input_price_per_k_tokens, Long output_price_per_k_tokens) {}

    private record Dimension(Integer providerId, Integer typeId, Long contextTokens) {}

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
        Map<String, Integer> typeIds = types.findAll().stream()
            .collect(Collectors.toMap(TokenType::getName, TokenType::getId));
        var byModel = accessible.stream().collect(Collectors.groupingBy(
            ModelAccessProjection::getModelId, LinkedHashMap::new, Collectors.toList()));
        Instant now = Instant.now();
        return byModel.entrySet().stream().map(entry -> {
            Map<Dimension, TokenPrice> current = new HashMap<>();
            for (TokenPrice price : prices.findByModelId(entry.getKey())) {
                if (!"token".equals(price.getUnit()) || !"default".equals(price.getServiceTier())
                        || price.getValidFrom().isAfter(now)) continue;
                var dimension = new Dimension(price.getProviderId(), price.getTypeId(), price.getMinContextTokens());
                current.merge(dimension, price,
                    (a, b) -> a.getValidFrom().isAfter(b.getValidFrom()) ? a : b);
            }
            var deployments = entry.getValue();
            return new ModelPrice(deployments.getFirst().getModelName(),
                highestRate(deployments, current, typeIds.get("billed_input_uncached")),
                highestRate(deployments, current, typeIds.get("billed_output_text")));
        }).toList();
    }

    /** Use the highest current standard rate across permitted providers and context tiers. */
    private Long highestRate(List<ModelAccessProjection> deployments, Map<Dimension, TokenPrice> current,
                             Integer typeId) {
        long highest = 0;
        for (var deployment : deployments) {
            if ("logosnode".equals(deployment.getProviderType())) continue;
            var rate = current.entrySet().stream()
                .filter(entry -> java.util.Objects.equals(entry.getKey().providerId(), deployment.getProviderId())
                    && entry.getKey().typeId().equals(typeId))
                .mapToLong(entry -> entry.getValue().getPricePerKUnit()).max();
            if (rate.isEmpty()) return null;
            highest = Math.max(highest, rate.getAsLong());
        }
        return highest;
    }
}
