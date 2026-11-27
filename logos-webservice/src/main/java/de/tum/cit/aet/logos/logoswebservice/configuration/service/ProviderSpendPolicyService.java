package de.tum.cit.aet.logos.logoswebservice.configuration.service;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
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
import de.tum.cit.aet.logos.logoswebservice.gateway.ProviderSpendCapService;

@Service
public class ProviderSpendPolicyService {

    public record Change(Boolean enabled, @JsonProperty("limit_usd") BigDecimal limitUsd) {}

    private final NamedParameterJdbcTemplate jdbc;
    private final AuditLogService audit;
    private final ProviderSpendCapService capService;

    public ProviderSpendPolicyService(NamedParameterJdbcTemplate jdbc, AuditLogService audit, ProviderSpendCapService capService) {
        this.jdbc = jdbc;
        this.audit = audit;
        this.capService = capService;
    }

    public Map<String, Object> get(int providerId) {
        requireCloudProvider(providerId, false);
        return snapshot(providerId, utcNow().toLocalDate().withDayOfMonth(1));
    }

    @Transactional(timeout = 5)
    public Map<String, Object> update(int providerId, Change change) {
        Long cap = parseCap(change);
        // Serialize config edits; an edit waiting across UTC midnight must use the new month.
        requireCloudProvider(providerId, true);
        OffsetDateTime now = utcNow();
        LocalDate month = now.toLocalDate().withDayOfMonth(1);
        Map<String, Object> before = readPolicy(providerId, month, false);
        writePolicy(providerId, month, cap);
        audit.record("provider.monthly_spend_cap_updated", "provider", providerId, null, before,
            readPolicy(providerId, month, true));
        return snapshot(providerId, month);
    }

    @Transactional(timeout = 5)
    public Map<String, Object> schedule(int providerId, Change change) {
        Long cap = parseCap(change);
        requireCloudProvider(providerId, true);
        OffsetDateTime now = utcNow();
        LocalDate month = now.toLocalDate().withDayOfMonth(1);
        LocalDate next = month.plusMonths(1);
        if (!now.plusMinutes(1).isBefore(next.atStartOfDay().atOffset(ZoneOffset.UTC))) {
            throw new ResponseStatusException(HttpStatus.CONFLICT,
                "Wait for UTC month rollover before scheduling a provider spend change");
        }
        Map<String, Object> before = readPolicy(providerId, next, true);
        writePolicy(providerId, next, cap);
        Map<String, Object> after = readPolicy(providerId, next, true);
        audit.record("provider.monthly_spend_cap_scheduled", "provider", providerId, null, before, after);
        return snapshot(providerId, month);
    }

    private void writePolicy(int providerId, LocalDate month, Long cap) {
        jdbc.update("""
            INSERT INTO provider_spend_policies (provider_id, effective_month, limit_micro_cents)
            VALUES (:provider, :month, :cap)
            ON CONFLICT (provider_id, effective_month) DO UPDATE
            SET limit_micro_cents = EXCLUDED.limit_micro_cents
            """, new MapSqlParameterSource().addValue("provider", providerId).addValue("month", month).addValue("cap", cap));
    }

    private Long parseCap(Change change) {
        if (change.enabled() == null) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Specify enabled explicitly");
        }
        if (Boolean.TRUE.equals(change.enabled())) {
            if (change.limitUsd() == null || change.limitUsd().signum() < 0) {
                throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Supply a nonnegative USD limit");
            }
            try {
                return change.limitUsd().movePointRight(8).longValueExact();
            } catch (ArithmeticException e) {
                throw new ResponseStatusException(HttpStatus.BAD_REQUEST,
                    "USD limit is too large or has more than eight decimal places");
            }
        }
        if (change.limitUsd() != null) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Omit the limit when disabling the cap");
        }
        return null;
    }

    private Map<String, Object> snapshot(int provider, LocalDate month) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("current", readPolicy(provider, month, false));
        result.put("scheduled", readPolicy(provider, month.plusMonths(1), true));
        result.put("charged_usd", capService.recordedCosts(provider, month).movePointLeft(8));
        return result;
    }

    private Map<String, Object> readPolicy(int provider, LocalDate month, boolean exact) {
        return jdbc.query("""
            SELECT effective_month, limit_micro_cents FROM provider_spend_policies
             WHERE provider_id = :provider AND effective_month <= :month
               AND (NOT :exact OR effective_month = :month)
             ORDER BY effective_month DESC LIMIT 1
            """, Map.of("provider", provider, "month", month, "exact", exact), rs -> {
                if (!rs.next()) {
                    return null;
                }
                Map<String, Object> policy = new LinkedHashMap<>();
                BigDecimal limit = rs.getBigDecimal("limit_micro_cents");
                policy.put("enabled", limit != null);
                policy.put("effective_month", rs.getObject("effective_month", LocalDate.class).toString());
                policy.put("limit_usd", limit == null ? null : limit.movePointLeft(8).toPlainString());
                return policy;
            });
    }

    private OffsetDateTime utcNow() {
        return jdbc.queryForObject("SELECT clock_timestamp()", Map.of(), OffsetDateTime.class)
            .withOffsetSameInstant(ZoneOffset.UTC);
    }

    private void requireCloudProvider(int providerId, boolean lock) {
        var params = Map.of("provider", providerId);
        Boolean exists = lock
            ? jdbc.query("""
                SELECT id FROM providers WHERE id = :provider AND provider_type = 'cloud' FOR UPDATE
                """, params, rs -> { return rs.next(); })
            : jdbc.queryForObject("""
                SELECT EXISTS (SELECT 1 FROM providers WHERE id = :provider AND provider_type = 'cloud')
                """, params, Boolean.class);
        if (!Boolean.TRUE.equals(exists)) {
            throw new ResponseStatusException(HttpStatus.NOT_FOUND, "Cloud provider not found");
        }
    }
}
