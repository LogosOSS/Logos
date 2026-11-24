package de.tum.cit.aet.logos.logoswebservice.operations.service;

import java.math.BigDecimal;
import java.math.MathContext;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.TreeMap;

import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.annotation.Isolation;

import de.tum.cit.aet.logos.logoswebservice.identity.repository.ApiKeyRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.ModelAccessProjection;
import de.tum.cit.aet.logos.logoswebservice.operations.repository.BatchHistoricalCostRepository;

@Service
public class BatchHistoricalCostService {
    static final int MINIMUM_SAMPLES = 3;
    private final ApiKeyRepository keys;
    private final BatchHistoricalCostRepository costs;

    public BatchHistoricalCostService(ApiKeyRepository keys, BatchHistoricalCostRepository costs) {
        this.keys = keys;
        this.costs = costs;
    }

    public record ModelCost(String model_name, BigDecimal provider_average_usd, BigDecimal logos_average_usd) {}
    private record Deployment(int modelId, int providerId) {}
    private record Group(int modelId, boolean nativeBatch) {}
    private record Sample(long count, BigDecimal sum) {
        Sample plus(Sample other) { return new Sample(count + other.count, sum.add(other.sum)); }
        BigDecimal averageUsd() {
            if (count < MINIMUM_SAMPLES) return null;
            return sum.divide(BigDecimal.valueOf(count), MathContext.DECIMAL64).movePointLeft(8);
        }
    }

    @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
    public List<ModelCost> historicalCostsForKey(int userId, int keyId) {
        var key = keys.findById(keyId).orElse(null);
        if (key == null || !Boolean.TRUE.equals(key.getIsActive())
                || key.getUserId() == null || key.getUserId() != userId) {
            throw new BatchService.KeyNotOwnedException("This API key does not belong to you.");
        }
        var accessible = Boolean.TRUE.equals(key.getUseCustomPermissions()) ? keys.findAccessibleModelsByKey(keyId)
            : key.getTeamId() == null ? List.<ModelAccessProjection>of() : keys.findAccessibleModelsByTeam(key.getTeamId());
        if (accessible.isEmpty()) return List.of();

        var deployments = new HashSet<Deployment>();
        var names = new TreeMap<Integer, String>();
        for (var row : accessible) {
            names.put(row.getModelId(), row.getModelName());
            deployments.add(new Deployment(row.getModelId(), row.getProviderId()));
        }
        var groups = new HashMap<Group, Sample>();
        for (var row : costs.readAggregates(userId, keyId, key.getTeamId())) {
            if (!deployments.contains(new Deployment(row.modelId(), row.providerId()))) continue;
            groups.merge(new Group(row.modelId(), row.nativeBatch()), new Sample(row.count(), row.sum()), Sample::plus);
        }
        var result = new ArrayList<ModelCost>();
        names.forEach((id, name) -> result.add(new ModelCost(name, average(groups.get(new Group(id, true))),
            average(groups.get(new Group(id, false))))));
        return List.copyOf(result);
    }

    private static BigDecimal average(Sample sample) {
        return sample == null ? null : sample.averageUsd();
    }
}
