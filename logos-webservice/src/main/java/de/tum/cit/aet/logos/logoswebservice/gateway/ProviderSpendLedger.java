package de.tum.cit.aet.logos.logoswebservice.gateway;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.server.ResponseStatusException;

/** Cross-replica accounting. PostgreSQL supplies locks and persistence, not admission policy. */
@Service
public class ProviderSpendLedger {
    public enum Decision { UNCAPPED, RESERVED, EXHAUSTED, UNBOUNDED, DUPLICATE }
    public record BoundReview(String upstreamUrl, String revision, String outputLimitField,
                              int maxInputTokens, int maxOutputTokens, String evidenceUrl) {}
    private record Deployment(int id, String revision, String baseUrl, String endpoint, String cloudType) {}
    private record Attempt(int provider, int model, LocalDate month, OffsetDateTime pricedAt, long held, boolean closed,
                           boolean anthropic) {}

    private final NamedParameterJdbcTemplate jdbc;
    private final TransactionTemplate transaction;
    private final ProviderSpendPricing pricing;

    ProviderSpendLedger(NamedParameterJdbcTemplate jdbc, PlatformTransactionManager manager, ProviderSpendPricing pricing) {
        this.jdbc = jdbc;
        this.transaction = new TransactionTemplate(manager);
        this.transaction.setTimeout(5);
        this.pricing = pricing;
    }

    public Decision reserve(UUID attemptId, int provider, Integer model, String upstreamUrl, JsonNode payload) {
        return transaction.execute(status -> {
            // An admin update/withdrawal or provider reconfiguration cannot race with the selected policy.
            if (!lockProvider(provider)) {
                return Decision.UNBOUNDED;
            }
            OffsetDateTime now = jdbc.queryForObject("SELECT clock_timestamp()", Map.of(), OffsetDateTime.class);
            LocalDate month = now.withOffsetSameInstant(java.time.ZoneOffset.UTC).toLocalDate().withDayOfMonth(1);
            Long limit = jdbc.query("""
                SELECT limit_micro_cents FROM provider_spend_policies WHERE provider_id = :provider
                  AND effective_month <= :month ORDER BY effective_month DESC LIMIT 1
                """, Map.of("provider", provider, "month", month),
                rs -> rs.next() ? rs.getObject(1, Long.class) : null);
            if (limit == null) {
                return Decision.UNCAPPED;
            }
            if (limit == 0) {
                return Decision.EXHAUSTED;
            }
            BoundReview bound = findBound(provider, model, upstreamUrl,
                payload == null ? null : payload.path("model").asText(null));
            if (bound == null || attemptId == null) {
                return Decision.UNBOUNDED;
            }
            String kind = ProviderSpendCapService.kindFor(java.net.URI.create(bound.upstreamUrl()));
            Integer output = ProviderSpendBounds.outputLimit(kind, bound.outputLimitField(), payload, bound.maxOutputTokens());
            if (output == null) {
                return Decision.UNBOUNDED;
            }
            List<ProviderSpendPricing.Rate> rates = pricing.rates(provider, model, now);
            BigDecimal required = ProviderSpendPricing.ceiling(rates, bound.maxInputTokens(), output);
            if (required == null) {
                return Decision.UNBOUNDED;
            }
            if (required.compareTo(BigDecimal.valueOf(limit)) > 0) {
                return Decision.EXHAUSTED;
            }

            var params = new MapSqlParameterSource().addValue("provider", provider).addValue("month", month)
                .addValue("model", model).addValue("attempt", attemptId).addValue("pricedAt", now)
                .addValue("held", required.longValueExact()).addValue("limit", limit)
                .addValue("anthropic", "messages".equals(kind));
            jdbc.update("""
                INSERT INTO provider_spend_months (provider_id, month_start)
                VALUES (:provider, :month) ON CONFLICT DO NOTHING
                """, params);
            lockMonth(provider, month);
            if (readAttempt(attemptId) != null) {
                return Decision.DUPLICATE;
            }
            if (jdbc.update("""
                UPDATE provider_spend_months SET held_micro_cents = held_micro_cents + :held
                 WHERE provider_id = :provider AND month_start = :month AND NOT bound_violation
                   AND charged_micro_cents + held_micro_cents + :held <= :limit
                """, params) == 0) {
                return Decision.EXHAUSTED;
            }
            jdbc.update("""
                INSERT INTO provider_spend_attempts (attempt_id, provider_id, month_start, model_id,
                    priced_at, reserved_micro_cents, anthropic_usage)
                VALUES (:attempt, :provider, :month, :model, :pricedAt, :held, :anthropic)
                """, params);
            return Decision.RESERVED;
        });
    }

    public boolean settle(UUID attemptId, Map<String, Long> usage, String tier, boolean undispatched) {
        return Boolean.TRUE.equals(transaction.execute(status -> {
            Attempt original = readAttempt(attemptId);
            if (original == null || original.closed()) {
                return false;
            }
            lockMonth(original.provider(), original.month());
            Attempt attempt = readAttempt(attemptId);
            if (attempt == null || attempt.closed()) {
                return false;
            }
            Long charge = undispatched ? Long.valueOf(0) : ProviderSpendPricing.charge(
                pricing.rates(attempt.provider(), attempt.model(), attempt.pricedAt()), usage, tier, attempt.anthropic());
            if (charge == null) {
                return false;
            }
            var params = Map.of("attempt", attemptId, "charge", charge,
                "provider", attempt.provider(), "month", attempt.month(), "held", attempt.held(),
                "violation", charge > attempt.held());
            jdbc.update("UPDATE provider_spend_attempts SET charged_micro_cents = :charge WHERE attempt_id = :attempt", params);
            jdbc.update("""
                UPDATE provider_spend_months
                   SET held_micro_cents = held_micro_cents - :held,
                       charged_micro_cents = charged_micro_cents + :charge,
                       bound_violation = bound_violation OR :violation
                 WHERE provider_id = :provider AND month_start = :month
                """, params);
            return true;
        }));
    }

    public int coverage(int provider) {
        var rows = jdbc.query("""
            SELECT mp.id, m.name, p.base_url, mp.endpoint, p.cloud_provider_type,
                   b.upstream_url, b.model_revision
              FROM provider_spend_bounds b JOIN model_provider mp ON mp.id = b.model_provider_id
              JOIN models m ON m.id = mp.model_id JOIN providers p ON p.id = mp.provider_id
             WHERE p.id = :provider AND p.provider_type = 'cloud'
            """, Map.of("provider", provider), (rs, index) -> {
                var deployment = new Deployment(rs.getInt("id"), rs.getString("name"), rs.getString("base_url"),
                    rs.getString("endpoint"), rs.getString("cloud_provider_type"));
                return matches(deployment, rs.getString("upstream_url"), rs.getString("model_revision"))
                    ? deployment.id() : null;
            });
        return (int) rows.stream().filter(java.util.Objects::nonNull).distinct().count();
    }

    public void recordReviewedBound(int provider, int model, BoundReview review) {
        transaction.executeWithoutResult(status -> {
            requireExclusiveProvider(provider);
            String target = ProviderSpendBounds.officialUrl(review.upstreamUrl());
            Deployment deployed = deployment(provider, model);
            if (target == null || deployed == null || !matches(deployed, target, review.revision())
                    || !ProviderSpendBounds.trustedEvidence(target, review.revision(), review.outputLimitField(), review.evidenceUrl())
                    || review.maxInputTokens() < 1 || review.maxOutputTokens() < 1) {
                throw new ResponseStatusException(HttpStatus.BAD_REQUEST,
                    "Supply reviewed limits for the exact configured official deployment and immutable revision");
            }
            jdbc.update("""
                INSERT INTO provider_spend_bounds (model_provider_id, upstream_url, model_revision, output_limit_field,
                    max_input_tokens, max_output_tokens, evidence_url, verified_at)
                VALUES (:deployment, :target, :revision, :field, :input, :output, :evidence, clock_timestamp())
                ON CONFLICT (model_provider_id, upstream_url) DO UPDATE SET model_revision = EXCLUDED.model_revision,
                    output_limit_field = EXCLUDED.output_limit_field, max_input_tokens = EXCLUDED.max_input_tokens,
                    max_output_tokens = EXCLUDED.max_output_tokens, evidence_url = EXCLUDED.evidence_url,
                    verified_at = EXCLUDED.verified_at
                """, Map.of("deployment", deployed.id(), "target", target, "revision", review.revision(),
                "field", review.outputLimitField(), "input", review.maxInputTokens(), "output", review.maxOutputTokens(),
                "evidence", review.evidenceUrl()));
        });
    }

    public void removeReviewedBound(int provider, int model, String upstreamUrl) {
        transaction.executeWithoutResult(status -> {
            requireExclusiveProvider(provider);
            String target = ProviderSpendBounds.officialUrl(upstreamUrl);
            Deployment deployed = deployment(provider, model);
            if (target == null || deployed == null) {
                throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Unknown deployment or official upstream URL");
            }
            jdbc.update("""
                DELETE FROM provider_spend_bounds WHERE model_provider_id = :deployment AND upstream_url = :target
                """, Map.of("deployment", deployed.id(), "target", target));
        });
    }

    private void requireExclusiveProvider(int provider) {
        boolean found = Boolean.TRUE.equals(jdbc.query("""
            SELECT id FROM providers WHERE id = :provider AND provider_type = 'cloud' FOR UPDATE
            """, Map.of("provider", provider), rs -> { return rs.next(); }));
        if (!found) {
            throw new ResponseStatusException(HttpStatus.NOT_FOUND, "Cloud provider not found");
        }
    }

    private boolean lockProvider(int provider) {
        return Boolean.TRUE.equals(jdbc.query("""
            SELECT id FROM providers WHERE id = :provider AND provider_type = 'cloud' FOR SHARE
            """, Map.of("provider", provider), rs -> { return rs.next(); }));
    }

    private void lockMonth(int provider, LocalDate month) {
        jdbc.queryForObject("""
            SELECT provider_id FROM provider_spend_months
             WHERE provider_id = :provider AND month_start = :month FOR UPDATE
            """, Map.of("provider", provider, "month", month), Integer.class);
    }

    private Attempt readAttempt(UUID attemptId) {
        if (attemptId == null) {
            return null;
        }
        return jdbc.query("""
            SELECT provider_id, model_id, month_start, priced_at, reserved_micro_cents, charged_micro_cents, anthropic_usage
              FROM provider_spend_attempts WHERE attempt_id = :attempt
            """, Map.of("attempt", attemptId), rs -> rs.next() ? new Attempt(rs.getInt("provider_id"),
                rs.getInt("model_id"), rs.getObject("month_start", LocalDate.class),
                rs.getObject("priced_at", OffsetDateTime.class), rs.getLong("reserved_micro_cents"),
                rs.getObject("charged_micro_cents") != null, rs.getBoolean("anthropic_usage")) : null);
    }

    private Deployment deployment(int provider, int model) {
        return jdbc.query("""
            SELECT mp.id, m.name, p.base_url, mp.endpoint, p.cloud_provider_type
              FROM model_provider mp JOIN models m ON m.id = mp.model_id JOIN providers p ON p.id = mp.provider_id
             WHERE p.id = :provider AND m.id = :model AND p.provider_type = 'cloud'
            """, Map.of("provider", provider, "model", model),
            rs -> rs.next() ? new Deployment(rs.getInt("id"), rs.getString("name"), rs.getString("base_url"),
                rs.getString("endpoint"), rs.getString("cloud_provider_type")) : null);
    }

    private BoundReview findBound(int provider, Integer model, String url, String revision) {
        String target = ProviderSpendBounds.officialUrl(url);
        if (target == null || model == null || revision == null) {
            return null;
        }
        Deployment deployed = deployment(provider, model);
        if (deployed == null || !matches(deployed, target, revision)) {
            return null;
        }
        return jdbc.query("""
            SELECT output_limit_field, max_input_tokens, max_output_tokens, evidence_url
              FROM provider_spend_bounds
             WHERE model_provider_id = :deployment AND upstream_url = :target AND model_revision = :revision
            """, Map.of("deployment", deployed.id(), "target", target, "revision", revision),
            rs -> rs.next() ? new BoundReview(target, revision, rs.getString("output_limit_field"),
                rs.getInt("max_input_tokens"), rs.getInt("max_output_tokens"), rs.getString("evidence_url")) : null);
    }

    private static boolean matches(Deployment deployed, String target, String revision) {
        String official = ProviderSpendBounds.officialUrl(target);
        if (official == null || !deployed.revision().equals(revision)) {
            return false;
        }
        java.net.URI uri = java.net.URI.create(official);
        String host = uri.getHost();
        String cloudType = "api.openai.com".equals(host) ? "openai" : "anthropic";
        return cloudType.equals(deployed.cloudType()) && official.equals(ProviderSpendBounds.officialUrl(
            CloudForwardUrlBuilder.build(deployed.baseUrl(), uri.getRawPath(), deployed.endpoint())));
    }
}
