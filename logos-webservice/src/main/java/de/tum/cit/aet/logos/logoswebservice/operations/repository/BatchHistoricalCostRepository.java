package de.tum.cit.aet.logos.logoswebservice.operations.repository;

import java.math.BigDecimal;
import java.sql.Types;
import java.util.List;

import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;

@Repository
public class BatchHistoricalCostRepository {
    private final NamedParameterJdbcTemplate jdbc;
    public BatchHistoricalCostRepository(NamedParameterJdbcTemplate jdbc) { this.jdbc = jdbc; }

    public record CostAggregate(int modelId, int providerId, boolean nativeBatch, long count, BigDecimal sum) {}

    public List<CostAggregate> readAggregates(int userId, int keyId, Integer teamId) {
        var params = new MapSqlParameterSource().addValue("userId", userId).addValue("keyId", keyId)
            .addValue("teamId", teamId, Types.INTEGER);
        // Native batch settlement writes both the tier and a batch-scoped
        // request id. Ordinary calls are a proxy for Logos replay pricing.
        // Managed local workers have no metered price; do not infer zero for missing cloud costs.
        return jdbc.query("""
            WITH sample AS (
                SELECT le.model_id, le.provider_id, COALESCE(le.settled_cost_micro_cents, 0) AS settled_cost_micro_cents,
                       CASE WHEN le.service_tier = 'batch' AND le.request_id LIKE 'batch-%'
                            THEN true ELSE false END AS native_batch
                FROM log_entry le
                JOIN api_keys ak ON ak.id = le.api_key_id
                JOIN providers p ON p.id = le.provider_id
                WHERE le.api_key_id = :keyId AND ak.is_active AND ak.user_id = :userId AND le.user_id = :userId
                  AND ak.team_id IS NOT DISTINCT FROM :teamId
                  AND le.team_id IS NOT DISTINCT FROM :teamId
                  AND le.timestamp_request >= CURRENT_TIMESTAMP - INTERVAL '30 days'
                  AND le.timestamp_response IS NOT NULL AND le.cost_finalized
                  AND le.result_status = 'success'
                  AND (le.settled_cost_micro_cents >= 0
                       OR (le.settled_cost_micro_cents IS NULL AND p.provider_type = 'logosnode'))
                  AND le.model_id IS NOT NULL AND le.provider_id IS NOT NULL
                ORDER BY le.timestamp_request DESC, le.id DESC
                LIMIT 20000
            )
            SELECT model_id, provider_id, native_batch, count(*) AS sample_count,
                   sum(settled_cost_micro_cents) AS cost_sum
            FROM sample GROUP BY model_id, provider_id, native_batch
            """, params, (rs, rowNum) -> new CostAggregate(rs.getInt("model_id"), rs.getInt("provider_id"),
                rs.getBoolean("native_batch"), rs.getLong("sample_count"), rs.getBigDecimal("cost_sum")));
    }
}
