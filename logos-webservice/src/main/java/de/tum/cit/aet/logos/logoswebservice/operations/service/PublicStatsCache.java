package de.tum.cit.aet.logos.logoswebservice.operations.service;

import java.time.Duration;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.function.LongSupplier;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

/**
 * Warms every public stats window at startup and refreshes expired figures in
 * the background. Requests keep reading the previous batch during a refresh,
 * including when a refresh fails. Only requests before the first batch is ready
 * wait for computation. A zero TTL disables caching for callers needing live data.
 */
@Service
public class PublicStatsCache {

    private static final Logger log = LoggerFactory.getLogger(PublicStatsCache.class);

    private record Entry(long computedAtNanos, Map<String, Map<String, Object>> windows) {
    }

    private final StatsService statsService;
    private final long ttlNanos;
    private final LongSupplier nanoClock;
    private final Object refreshLock = new Object();
    private volatile Entry entry;

    @Autowired
    public PublicStatsCache(StatsService statsService,
                            @Value("${logos.public-stats.cache-ttl:PT30M}") Duration ttl) {
        this(statsService, ttl, System::nanoTime);
    }

    PublicStatsCache(StatsService statsService, Duration ttl, LongSupplier nanoClock) {
        this.statsService = statsService;
        this.ttlNanos = ttl.toNanos();
        this.nanoClock = nanoClock;
    }

    /** Unknown windows are rejected before accessing the cache. */
    public Map<String, Object> get(String days) {
        String window = StatsService.normalizePublicStatsDays(days);
        if (ttlNanos <= 0) {
            return statsService.publicStats(window);
        }
        Entry current = entry;
        if (current == null) {
            synchronized (refreshLock) {
                if (entry == null) computeAllWindows();
                current = entry;
            }
        }
        return current.windows().get(window);
    }

    // Check expiration independently of requests, including windows without traffic.
    @Scheduled(initialDelay = 0, fixedDelay = 1000)
    public void refreshExpired() {
        if (ttlNanos <= 0) return;
        synchronized (refreshLock) {
            if (entry != null && nanoClock.getAsLong() - entry.computedAtNanos() < ttlNanos) return;
            try {
                computeAllWindows();
            } catch (RuntimeException ex) {
                log.warn("Could not refresh public stats; retaining the previous figures", ex);
            }
        }
    }

    private void computeAllWindows() {
        Map<String, Map<String, Object>> windows = new LinkedHashMap<>();
        for (String window : StatsService.PUBLIC_STATS_DAY_OPTIONS) {
            windows.put(window, Collections.unmodifiableMap(new LinkedHashMap<>(statsService.publicStats(window))));
        }
        entry = new Entry(nanoClock.getAsLong(), Collections.unmodifiableMap(windows));
    }
}
