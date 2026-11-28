package de.tum.cit.aet.logos.logoswebservice.operations.service;

import java.time.Instant;
import java.time.LocalDate;
import java.util.List;

import org.junit.jupiter.api.Test;

import de.tum.cit.aet.logos.logoswebservice.operations.repository.PublicHourlyRequestsProjection;
import static org.assertj.core.api.Assertions.assertThat;

class PublicHourlyRequestLoadTest {
    @Test
    void releasesAdjacentNonOverlappingBlocksOnlyAfterAFullWeek() {
        var beforeRelease = PublicHourlyRequestLoad.block(Instant.parse("2026-11-29T22:59:59Z"));
        var atRelease = PublicHourlyRequestLoad.block(Instant.parse("2026-11-29T23:00:00Z"));
        assertThat(beforeRelease.end()).isEqualTo(atRelease.start());
        assertThat(atRelease.end()).isEqualTo(LocalDate.of(2026, 11, 23));
        assertThat(atRelease.start()).isEqualTo(LocalDate.of(2026, 10, 26));
        assertThat(PublicHourlyRequestLoad.block(Instant.parse("2026-12-19T12:00:00Z"))).isEqualTo(atRelease);
    }

    @Test
    void disclosesOnlyBandsAndDifferentiatesSuppressedAndZeroHours() {
        var block = new PublicHourlyRequestLoad.PublicationBlock(LocalDate.of(2026, 10, 5), LocalDate.of(2026, 11, 2));
        var chart = PublicHourlyRequestLoad.summarize(block, List.of(
            row(-1, 500, 3, 0), row(2, 140, 3, 70), row(3, 140, 3, 70),
            row(4, 40, 2, 20), row(5, 100, 3, 81), row(6, 29, 3, 12)));
        assertThat(chart.hours()).hasSize(24);
        // 02:00 occurs 29 times after the autumn clock change, not 28.
        assertThat(chart.hours().get(2)).isEqualTo(new PublicHourlyRequestLoad.Hour(2, 0L, false));
        assertThat(chart.hours().get(3)).isEqualTo(new PublicHourlyRequestLoad.Hour(3, 5L, false));
        for (int h : List.of(4, 5, 6)) assertThat(chart.hours().get(h))
            .isEqualTo(new PublicHourlyRequestLoad.Hour(h, null, false));
        assertThat(chart.hours().get(7)).isEqualTo(new PublicHourlyRequestLoad.Hour(7, null, true));
        assertThat(PublicHourlyRequestLoad.summarize(block, List.of(row(-1, 1000, 2, 0)))).isNull();
    }

    @Test
    void poolsSparseHoursWithoutPublishingOverlappingBandsOrChangingThresholds() {
        var block = new PublicHourlyRequestLoad.PublicationBlock(LocalDate.of(2026, 10, 5), LocalDate.of(2026, 11, 2));
        var chart = PublicHourlyRequestLoad.summarize(block, List.of(
            row(-1, 1000, 3, 0), row(0, 100, 2, 50), row(3, 465, 3, 200), group(0, 565, 3, 250),
            row(4, 600, 1, 600), row(5, 25, 2, 14), group(4, 625, 3, 600)));
        // The first group has 113 hour occurrences at the autumn clock change.
        assertThat(chart.hours().getFirst()).isEqualTo(new PublicHourlyRequestLoad.Hour(0, 5L, false, 4));
        assertThat(chart.hours()).hasSize(21).noneMatch(hour -> hour.hour() > 0 && hour.hour() < 4);
        for (int h : List.of(4, 5)) assertThat(chart.hours()).contains(new PublicHourlyRequestLoad.Hour(h, null, false));
        assertThat(chart.hours()).contains(new PublicHourlyRequestLoad.Hour(6, null, true));
    }

    @Test
    void doesNotPoolAnAlreadyPublishableGroupOrBypassTheOverallCohort() {
        var block = PublicHourlyRequestLoad.block(Instant.parse("2026-11-29T23:00:00Z"));
        var rows = List.of(row(-1, 150, 3, 0), row(10, 150, 3, 50), group(8, 150, 3, 50));
        assertThat(PublicHourlyRequestLoad.summarize(block, rows).hours()).hasSize(24)
            .contains(new PublicHourlyRequestLoad.Hour(10, 5L, false));
        assertThat(PublicHourlyRequestLoad.summarize(block, List.of(row(-1, 150, 2, 0), group(8, 150, 2, 75)))).isNull();
    }

    private static PublicHourlyRequestsProjection group(int hour, long requests, long teams, long largest) {
        return row(hour, 4, requests, teams, largest);
    }

    private static PublicHourlyRequestsProjection row(int hour, long requests, long teams, long largest) {
        return row(hour, 1, requests, teams, largest);
    }

    private static PublicHourlyRequestsProjection row(int hour, int span, long requests, long teams, long largest) {
        return new PublicHourlyRequestsProjection() {
            @Override public int getHour() { return hour; }
            @Override public int getSpan() { return span; }
            @Override public long getRequests() { return requests; }
            @Override public long getTeams() { return teams; }
            @Override public long getLargestTeamRequests() { return largest; }
        };
    }
}
