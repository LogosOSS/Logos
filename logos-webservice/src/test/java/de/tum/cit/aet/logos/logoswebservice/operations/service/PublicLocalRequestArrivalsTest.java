package de.tum.cit.aet.logos.logoswebservice.operations.service;

import java.time.Duration;
import java.time.Instant;
import java.util.List;

import org.junit.jupiter.api.Test;

import de.tum.cit.aet.logos.logoswebservice.operations.repository.PublicLocalRequestArrivalsProjection;
import static org.assertj.core.api.Assertions.assertThat;

class PublicLocalRequestArrivalsTest {

    @Test
    void fillsQuietHoursAndKeepsTheTwoAutumnClockChangeHoursDistinct() {
        var window = PublicLocalRequestArrivals.window(Instant.parse("2026-10-26T10:37:00Z"));
        assertThat(window.until()).isEqualTo(Instant.parse("2026-10-25T10:00:00Z"));
        assertThat(Duration.between(window.from(), window.until())).isEqualTo(Duration.ofDays(7));
        var earlier = Instant.parse("2026-10-25T00:00:00Z");
        var later = Instant.parse("2026-10-25T01:00:00Z");
        var chart = PublicLocalRequestArrivals.summarize(window, List.of(row(earlier, 1, 0, 0), row(later, 2, 3, 4)));
        assertThat(chart.hours()).hasSize(168).containsSequence(
            new PublicLocalRequestArrivals.Hour(earlier, 1, 0, 0),
            new PublicLocalRequestArrivals.Hour(later, 2, 3, 4),
            new PublicLocalRequestArrivals.Hour(later.plusSeconds(3600), 0, 0, 0));
    }

    private static PublicLocalRequestArrivalsProjection row(Instant bucket, long regular, long agent, long benchmark) {
        return new PublicLocalRequestArrivalsProjection() {
            @Override public Instant getBucket() { return bucket; }
            @Override public long getRegular() { return regular; }
            @Override public long getAgent() { return agent; }
            @Override public long getBenchmark() { return benchmark; }
        };
    }
}
