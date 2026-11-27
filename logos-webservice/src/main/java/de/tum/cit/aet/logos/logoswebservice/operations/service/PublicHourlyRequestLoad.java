package de.tum.cit.aet.logos.logoswebservice.operations.service;

import java.time.DayOfWeek;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.time.temporal.ChronoUnit;
import java.time.temporal.TemporalAdjusters;
import java.util.ArrayList;
import java.util.List;

import de.tum.cit.aet.logos.logoswebservice.operations.repository.PublicHourlyRequestsProjection;

/** One release, independent of the query's rolling duration. */
public record PublicHourlyRequestLoad(
        String timeZone, LocalDate from, LocalDate until, int bandSize, List<Hour> hours) {

    private static final ZoneId ZONE = ZoneId.of("Europe/Berlin");
    private static final LocalDate MONDAY_EPOCH = LocalDate.of(1970, 1, 5);
    private static final int WEEKS = 4;
    private static final int RELEASE_DELAY_WEEKS = 1;
    private static final int MIN_TEAMS = 3;
    private static final int MIN_REQUESTS = 30;
    private static final int BAND_WIDTH = 5;

    public record Hour(int hour, Long bandStart, boolean zero) {}

    record PublicationBlock(LocalDate start, LocalDate end) {
        Instant startTime() { return start.atStartOfDay(ZONE).toInstant(); }
        Instant endTime() { return end.atStartOfDay(ZONE).toInstant(); }
    }

    static PublicationBlock block(Instant now) {
        var cutOff = now.atZone(ZONE).toLocalDate()
            .with(TemporalAdjusters.previousOrSame(DayOfWeek.MONDAY)).minusWeeks(RELEASE_DELAY_WEEKS);
        long weeks = ChronoUnit.WEEKS.between(MONDAY_EPOCH, cutOff);
        var end = MONDAY_EPOCH.plusWeeks(Math.floorDiv(weeks, WEEKS) * WEEKS);
        return new PublicationBlock(end.minusWeeks(WEEKS), end);
    }

    static PublicHourlyRequestLoad summarize(PublicationBlock block, List<PublicHourlyRequestsProjection> rows) {
        var cohort = rows.stream().filter(r -> r.getHour() == -1).findFirst().orElse(null);
        if (cohort == null || cohort.getTeams() < MIN_TEAMS || cohort.getRequests() < MIN_REQUESTS) return null;

        var perHour = new PublicHourlyRequestsProjection[24];
        for (var row : rows) {
            if (row.getHour() >= 0 && row.getHour() < 24) perHour[row.getHour()] = row;
        }
        var occurrences = new long[24];
        // Count absolute hours first so a Berlin clock change contributes only
        // the occurrences that actually exist: repeated twice, skipped zero times.
        for (var ts = block.startTime(); ts.isBefore(block.endTime()); ts = ts.plus(1, ChronoUnit.HOURS)) {
            occurrences[ts.atZone(ZONE).getHour()]++;
        }

        var hours = new ArrayList<Hour>(24);
        for (int hour = 0; hour < 24; hour++) {
            var row = perHour[hour];
            if (row == null) {
                // No matching traffic across a published cohort, not an
                // inference about missing or other deployments' requests.
                hours.add(new Hour(hour, null, true));
            } else if (row.getTeams() < MIN_TEAMS || row.getRequests() < MIN_REQUESTS
                    || row.getLargestTeamRequests() / (double) row.getRequests() > 0.8) {
                hours.add(new Hour(hour, null, false));
            } else {
                long bandStart = row.getRequests() / (occurrences[hour] * BAND_WIDTH) * BAND_WIDTH;
                hours.add(new Hour(hour, bandStart, false));
            }
        }
        return new PublicHourlyRequestLoad(ZONE.getId(), block.start(), block.end(), BAND_WIDTH, List.copyOf(hours));
    }
}
