package de.tum.cit.aet.logos.logoswebservice.operations.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.doReturn;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import java.time.Duration;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

import org.junit.jupiter.api.Test;

class PublicStatsCacheTest {

    private final StatsService statsService = mock(StatsService.class);
    private final AtomicLong now = new AtomicLong();
    private final List<String> windows = List.of("7", "30", "90", "365", "all");

    {
        when(statsService.publicStats(anyString())).thenAnswer(inv -> new HashMap<>(Map.of("days", inv.getArgument(0))));
    }

    @Test
    void scheduledRefreshWarmsEveryWindowAndRecomputesThemAfterExpiry() {
        PublicStatsCache cache = new PublicStatsCache(statsService, Duration.ofMinutes(30), now::get);
        cache.refreshExpired();
        cache.refreshExpired();
        for (String window : windows) {
            assertThat(cache.get(window)).containsEntry("days", window);
            verify(statsService, times(1)).publicStats(window);
        }

        now.addAndGet(Duration.ofMinutes(30).toNanos());
        cache.get(null);
        verify(statsService, times(1)).publicStats("30");
        cache.refreshExpired();
        for (String window : windows) verify(statsService, times(2)).publicStats(window);
    }

    @Test
    void firstRequestWarmsAllWindowsWhenStartupRefreshHasNotRun() {
        PublicStatsCache cache = new PublicStatsCache(statsService, Duration.ofMinutes(30), now::get);
        cache.get(null);
        for (String window : windows) {
            cache.get(window);
            verify(statsService, times(1)).publicStats(window);
        }
    }

    @Test
    void requestsServePreviousValuesWhileBackgroundRefreshIsBlocked() throws Exception {
        PublicStatsCache cache = new PublicStatsCache(statsService, Duration.ofMinutes(30), now::get);
        cache.refreshExpired();
        Map<String, Object> previous = cache.get("30");
        now.addAndGet(Duration.ofMinutes(30).toNanos());
        CountDownLatch started = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        doAnswer(inv -> {
            started.countDown();
            if (!release.await(5, TimeUnit.SECONDS)) throw new IllegalStateException("Refresh was not released");
            return Map.of("days", "7", "updated", true);
        }).when(statsService).publicStats("7");

        try (var executor = Executors.newFixedThreadPool(2)) {
            var refresh = executor.submit(cache::refreshExpired);
            try {
                assertThat(started.await(5, TimeUnit.SECONDS)).isTrue();
                assertThat(executor.submit(() -> cache.get("30")).get(1, TimeUnit.SECONDS)).isSameAs(previous);
                assertThat(executor.submit(() -> cache.get("7")).get(1, TimeUnit.SECONDS)).doesNotContainKey("updated");
            } finally {
                release.countDown();
            }
            refresh.get(5, TimeUnit.SECONDS);
        }
        assertThat(cache.get("7")).containsEntry("updated", true);
    }

    @Test
    void failedRefreshKeepsThePreviousBatchAndCanBeRetried() {
        PublicStatsCache cache = new PublicStatsCache(statsService, Duration.ofMinutes(30), now::get);
        cache.refreshExpired();
        Map<String, Object> previous = cache.get("30");
        now.addAndGet(Duration.ofMinutes(30).toNanos());
        when(statsService.publicStats("30")).thenThrow(new IllegalStateException("Database unavailable"));
        cache.refreshExpired();
        assertThat(cache.get("30")).isSameAs(previous);

        doReturn(Map.of("days", "30", "updated", true)).when(statsService).publicStats("30");
        cache.refreshExpired();
        assertThat(cache.get("30")).containsEntry("updated", true);
    }

    @Test
    void aZeroTtlComputesEveryTimeWithoutBackgroundQueries() {
        PublicStatsCache cache = new PublicStatsCache(statsService, Duration.ZERO, now::get);
        cache.refreshExpired();
        verifyNoInteractions(statsService);
        cache.get("30");
        cache.get("30");
        verify(statsService, times(2)).publicStats("30");
    }

    @Test
    void rejectsUnknownWindowsWithoutComputing() {
        PublicStatsCache cache = new PublicStatsCache(statsService, Duration.ofMinutes(30), now::get);
        assertThatThrownBy(() -> cache.get("14")).isInstanceOf(IllegalArgumentException.class);
        verifyNoInteractions(statsService);
    }

    @Test
    void concurrentColdRequestsShareTheSameWarmup() throws Exception {
        PublicStatsCache cache = new PublicStatsCache(statsService, Duration.ofMinutes(30), now::get);
        CountDownLatch started = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        doAnswer(inv -> {
            started.countDown();
            if (!release.await(5, TimeUnit.SECONDS)) throw new IllegalStateException("Warmup was not released");
            return Map.of("days", "7");
        }).when(statsService).publicStats("7");

        try (var executor = Executors.newFixedThreadPool(2)) {
            var first = executor.submit(() -> cache.get("30"));
            try {
                assertThat(started.await(5, TimeUnit.SECONDS)).isTrue();
                var second = executor.submit(() -> cache.get("all"));
                release.countDown();
                assertThat(first.get(5, TimeUnit.SECONDS)).containsEntry("days", "30");
                assertThat(second.get(5, TimeUnit.SECONDS)).containsEntry("days", "all");
            } finally {
                release.countDown();
            }
        }
        for (String window : windows) verify(statsService, times(1)).publicStats(window);
    }
}
