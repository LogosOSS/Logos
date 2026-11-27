package de.tum.cit.aet.logos.logoswebservice.configuration.service;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.LinkedHashMap;
import java.util.Map;

import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.server.ResponseStatusException;

import com.fasterxml.jackson.annotation.JsonProperty;

import de.tum.cit.aet.logos.logoswebservice.audit.AuditLogService;

@Service
public class ProviderSpendPolicyService {

    public record Change(Boolean enabled, @JsonProperty("limit_usd") BigDecimal limitUsd) {}

    private final NamedParameterJdbcTemplate jdbc;
    private final AuditLogService audit;

    public ProviderSpendPolicyService(NamedParameterJdbcTemplate jdbc, AuditLogService audit) {
        this.jdbc = jdbc;
        this.audit = audit;
    }

    public Map<String, Object> get(int providerId) {
        requireCloudProvider(providerId);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("current", readPolicy(providerId, false));
        result.put("scheduled", readPolicy(providerId, true));
        Map<String, Object> totals = jdbc.queryForMap("""
            SELECT COALESCE(SUM(charged_micro_cents), 0) / 100000000 AS charged_usd,
                   COALESCE(SUM(held_micro_cents), 0) / 100000000 AS reserved_usd,
                   COALESCE(BOOL_OR(bound_violation), false) AS bound_violation
              FROM provider_spend_months
             WHERE provider_id = :provider AND month_start = date_trunc('month', CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date
            """, Map.of("provider", providerId));
        result.putAll(totals);
        return result;
    }

    @Transactional(timeout = 5)
    public Map<String, Object> schedule(int providerId, Change change) {
        requireCloudProvider(providerId);
        if (change.enabled() == null) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Specify enabled explicitly");
        }
        Long cap = null;
        if (Boolean.TRUE.equals(change.enabled())) {
            if (change.limitUsd() == null || change.limitUsd().signum() < 0) {
                throw new ResponseStatusException(HttpStatus.BAD_REQUEST,
                    "Supply a nonnegative USD limit");
            }
            try {
                cap = change.limitUsd().movePointRight(8).longValueExact();
            } catch (ArithmeticException e) {
                throw new ResponseStatusException(HttpStatus.BAD_REQUEST,
                    "USD limit is too large or has more than eight decimal places");
            }
        } else if (change.limitUsd() != null) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Omit the limit when disabling the cap");
        }
        // Never retroactively mark prior uncapped usage as a tracked month.
        // Serialize simultaneous admin changes to keep the audit prior value exact.
        jdbc.queryForObject("SELECT id FROM providers WHERE id = :provider FOR UPDATE",
            Map.of("provider", providerId), Integer.class);
        Boolean nearRollover = jdbc.queryForObject("""
            SELECT clock_timestamp() AT TIME ZONE 'UTC' + interval '1 minute'
                >= date_trunc('month', CURRENT_TIMESTAMP AT TIME ZONE 'UTC') + interval '1 month'
            """, Map.of(), Boolean.class);
        if (Boolean.TRUE.equals(nearRollover)) {
            throw new ResponseStatusException(HttpStatus.CONFLICT,
                "Wait for UTC month rollover before scheduling a provider spend change");
        }
        Map<String, Object> before = readPolicy(providerId, true);
        jdbc.update("""
            INSERT INTO provider_spend_policies (provider_id, effective_month, limit_micro_cents)
            VALUES (:provider, (date_trunc('month', CURRENT_TIMESTAMP AT TIME ZONE 'UTC') + interval '1 month')::date, :cap)
            ON CONFLICT (provider_id, effective_month) DO UPDATE
            SET limit_micro_cents = EXCLUDED.limit_micro_cents
            """, new MapSqlParameterSource()
                .addValue("provider", providerId).addValue("cap", cap));
        Map<String, Object> after = readPolicy(providerId, true);
        audit.record("provider.monthly_spend_cap_scheduled", "provider", providerId, null, before, after);
        return get(providerId);
    }

    private Map<String, Object> readPolicy(int providerId, boolean scheduled) {
        return jdbc.query("""
            SELECT effective_month, limit_micro_cents
              FROM provider_spend_policies
             WHERE provider_id = :provider
               AND CASE WHEN :scheduled THEN effective_month =
                   (date_trunc('month', CURRENT_TIMESTAMP AT TIME ZONE 'UTC') + interval '1 month')::date
                   ELSE effective_month <= date_trunc('month', CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date
                   END
             ORDER BY effective_month DESC LIMIT 1
            """, Map.of("provider", providerId, "scheduled", scheduled), rs -> {
                if (!rs.next()) {
                    return null;
                }
                Map<String, Object> policy = new LinkedHashMap<>();
                BigDecimal limit = rs.getBigDecimal("limit_micro_cents");
                policy.put("enabled", limit != null);
                policy.put("effective_month", rs.getObject("effective_month", LocalDate.class).toString());
                policy.put("limit_usd", limit == null ? null : limit.movePointLeft(8));
                return policy;
            });
    }

    private void requireCloudProvider(int providerId) {
        boolean exists = Boolean.TRUE.equals(jdbc.queryForObject("""
            SELECT EXISTS (SELECT 1 FROM providers WHERE id = :provider AND provider_type = 'cloud')
            """, Map.of("provider", providerId), Boolean.class));
        if (!exists) {
            throw new ResponseStatusException(HttpStatus.NOT_FOUND, "Cloud provider not found");
        }
    }
}
