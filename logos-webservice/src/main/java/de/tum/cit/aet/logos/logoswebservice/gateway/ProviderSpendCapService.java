package de.tum.cit.aet.logos.logoswebservice.gateway;

import java.math.BigDecimal;
import java.time.Duration;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.TimeUnit;
import java.util.function.LongSupplier;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.dao.DataAccessException;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import org.springframework.web.server.ResponseStatusException;

/** Admit against recorded monthly spend. Healthy request paths read one immutable snapshot. */
@Service
public class ProviderSpendCapService {
    private static final Logger log = LoggerFactory.getLogger(ProviderSpendCapService.class);
    private static final long FRESHNESS = TimeUnit.SECONDS.toNanos(5);

    private record Limit(int provider, long microCents) {}
    private record Snapshot(Set<Integer> providers, long expiresAt) {}
    record Status(Set<Integer> blockedProviderIds, long validForMs) {}

    private final NamedParameterJdbcTemplate jdbc;
    private final LongSupplier nanoTime;
    private volatile Snapshot last;
    private volatile Snapshot knownActive;

    @Autowired
    public ProviderSpendCapService(NamedParameterJdbcTemplate jdbc) {
        this(jdbc, System::nanoTime);
    }

    ProviderSpendCapService(NamedParameterJdbcTemplate jdbc, LongSupplier nanoTime) {
        this.jdbc = jdbc;
        this.nanoTime = nanoTime;
    }

    @EventListener(ApplicationReadyEvent.class)
    @Scheduled(scheduler = "providerSpendScheduler", initialDelay = 2000, fixedDelay = 2000)
    public synchronized void refresh() {
        long start = nanoTime.getAsLong();
        try {
            OffsetDateTime now = jdbc.queryForObject("SELECT statement_timestamp()", Map.of(), OffsetDateTime.class)
                .withOffsetSameInstant(ZoneOffset.UTC);
            LocalDate month = now.toLocalDate().withDayOfMonth(1);
            var limits = jdbc.query("""
                SELECT provider_id, limit_micro_cents FROM (
                    SELECT DISTINCT ON (provider_id) provider_id, limit_micro_cents
                      FROM provider_spend_policies WHERE effective_month <= :month
                     ORDER BY provider_id, effective_month DESC
                ) current_policy WHERE limit_micro_cents IS NOT NULL
                """, Map.of("month", month), (rs, row) -> new Limit(rs.getInt(1), rs.getLong(2)));
            long untilRollover = Duration.between(now,
                month.plusMonths(1).atStartOfDay().atOffset(ZoneOffset.UTC)).toNanos();
            long expiresAt = start + Math.min(FRESHNESS, untilRollover);
            var active = new HashSet<Integer>();
            limits.forEach(limit -> active.add(limit.provider()));
            knownActive = new Snapshot(Set.copyOf(active), expiresAt);
            Set<Integer> blocked = new HashSet<>();
            for (var limit : limits) {
                if (limit.microCents() == 0 || recordedCosts(limit.provider(), month)
                        .compareTo(BigDecimal.valueOf(limit.microCents())) >= 0) {
                    blocked.add(limit.provider());
                }
            }
            last = new Snapshot(Set.copyOf(blocked), expiresAt);
        } catch (DataAccessException e) {
            log.warn("Could not refresh provider spend limits", e);
        }
    }

    public void check(int provider) {
        Snapshot snapshot = last;
        Snapshot fallback = knownActive;
        long now = nanoTime.getAsLong();
        if (snapshot != null && snapshot.expiresAt() - now > 0) {
            if (snapshot.providers().contains(provider)) {
                throw new ResponseStatusException(HttpStatus.PAYMENT_REQUIRED, "Provider monthly spend limit reached");
            }
        } else if (fallback == null || fallback.expiresAt() - now <= 0 || fallback.providers().contains(provider)) {
            // When only policy is known, uncapped providers can continue
            // without silently bypassing a cap whose total could not be read.
            throw unavailable();
        }
    }

    Status status() {
        Snapshot snapshot = last;
        long remaining = snapshot == null ? 0 : snapshot.expiresAt() - nanoTime.getAsLong();
        long validForMs = TimeUnit.NANOSECONDS.toMillis(remaining);
        if (validForMs <= 0) {
            throw unavailable();
        }
        return new Status(snapshot.providers(), validForMs);
    }

    private static ResponseStatusException unavailable() {
        return new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE, "Provider monthly spend limit is unavailable");
    }

    public BigDecimal recordedCosts(int provider, LocalDate month) {
        return jdbc.queryForObject("""
            SELECT COALESCE(SUM(settled_cost_micro_cents), 0)
              FROM log_entry
             WHERE provider_id = :provider AND timestamp_request >= :start AND timestamp_request < :end
               AND cost_finalized AND settled_cost_micro_cents > 0
               AND result_status IN ('success', 'error', 'timeout')
            """, Map.of("provider", provider, "start", month.atStartOfDay().atOffset(ZoneOffset.UTC),
                "end", month.plusMonths(1).atStartOfDay().atOffset(ZoneOffset.UTC)), BigDecimal.class);
    }
}
