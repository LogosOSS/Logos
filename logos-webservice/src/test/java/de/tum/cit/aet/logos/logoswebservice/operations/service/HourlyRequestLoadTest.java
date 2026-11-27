package de.tum.cit.aet.logos.logoswebservice.operations.service;

import java.sql.Timestamp;
import java.time.Instant;
import java.util.List;

import org.junit.jupiter.api.Test;

import de.tum.cit.aet.logos.logoswebservice.operations.repository.LogEntryRepository;
import de.tum.cit.aet.logos.logoswebservice.operations.repository.HourlyRequestSourceProjection;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

class HourlyRequestLoadTest {

    private final LogEntryRepository logs = mock(LogEntryRepository.class);
    private final RequestLogStatsService service = new RequestLogStatsService(logs);

    @Test
    void repeatedLocalHourCountsBothOccurrencesAndKeepsZerosSeparateFromMissingHours() {
        String now = "2026-10-25T03:26:00Z";
        // 01:00 CEST is a logged zero; 02:00 occurs twice, at 00:00 and 01:00 UTC.
        when(logs.findHourlyRequestSources(Timestamp.from(Instant.parse("2026-10-24T23:00:00Z")),
                Timestamp.from(Instant.parse("2026-10-25T03:00:00Z")),
                4, 8, 16, 32, false, List.of(10, 20)))
            .thenReturn(List.of(hour("2026-10-25T00:00:00Z", 3, 7),
                hour("2026-10-25T01:00:00Z", 5, 1), hour("2026-10-25T02:00:00Z", 2, 1)));
        var load = service.getHourlyRequestLoad("2026-10-24T22:25:00Z", "2026-10-26T00:00:00Z",
            4, 8, 16, 32, List.of(10, 20), Instant.parse(now));

        assertThat(load.timeZone()).isEqualTo("Europe/Berlin");
        assertThat(load.allDays()).hasSize(24);
        assertThat(load.allDays().get(1)).isEqualTo(new RequestLogStatsService.HourSlot(1, 1, 0, 0, 0));
        assertThat(load.allDays().get(2)).isEqualTo(new RequestLogStatsService.HourSlot(2, 2, 8, 8, 4));
        assertThat(load.weekends().get(2)).isEqualTo(load.allDays().get(2));
        assertThat(load.weekdays().get(2)).isEqualTo(new RequestLogStatsService.HourSlot(2, 0, 0, 0, 0));
        assertThat(load.allDays().get(4)).isEqualTo(new RequestLogStatsService.HourSlot(4, 0, 0, 0, 0));
    }

    @Test
    void springClockChangeDoesNotCreateAZeroForTheSkippedHour() {
        when(logs.findHourlyRequestSources(Timestamp.from(Instant.parse("2026-03-29T00:00:00Z")),
                Timestamp.from(Instant.parse("2026-03-29T02:00:00Z")),
                null, null, null, null, true, List.of(-1)))
            .thenReturn(List.of(hour("2026-03-29T00:00:00Z", 2, 8), hour("2026-03-29T01:00:00Z", 2, 5)));
        var load = service.getHourlyRequestLoad("2026-03-29T00:00:00Z", "2026-03-29T02:00:00Z",
            null, null, null, null, List.of(), Instant.parse("2026-03-29T04:00:00Z"));

        assertThat(load.allDays().get(1).completeHours()).isOne();
        assertThat(load.allDays().get(2).completeHours()).isZero();
        assertThat(load.allDays().get(3).completeHours()).isOne();
    }

    @Test
    void aRangeWithNoCompleteHoursDoesNotQueryTheRequestLog() {
        var load = service.getHourlyRequestLoad("2026-11-23T07:57:00Z", "2026-11-23T08:15:00Z",
            null, null, null, null, List.of(), Instant.parse("2026-11-23T08:15:00Z"));

        assertThat(load.allDays()).hasSize(24).allSatisfy(slot -> assertThat(slot.completeHours()).isZero());
        verifyNoInteractions(logs);
    }

    private static HourlyRequestSourceProjection hour(String start, long other, long agent) {
        return new HourlyRequestSourceProjection() {
            @Override public long getHourEpoch() { return Instant.parse(start).getEpochSecond(); }
            @Override public long getOtherRequests() { return other; }
            @Override public long getAgentRequests() { return agent; }
            @Override public long getBenchmarkRequests() { return 2; }
        };
    }
}
