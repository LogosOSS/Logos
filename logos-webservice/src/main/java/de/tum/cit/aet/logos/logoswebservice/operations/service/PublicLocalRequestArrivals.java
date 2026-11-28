package de.tum.cit.aet.logos.logoswebservice.operations.service;

import java.time.Duration;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;

import de.tum.cit.aet.logos.logoswebservice.operations.repository.PublicLocalRequestArrivalsProjection;

/** Approximation of local demand as recorded arrivals, not GPU utilization. */
public record PublicLocalRequestArrivals(Instant from, Instant until, List<Hour> hours) {

    private static final int WINDOW_DAYS = 7;
    private static final int PUBLICATION_DELAY_HOURS = 24;

    public record Hour(Instant start, long regular, long agent, long benchmark) {}

    record Window(Instant from, Instant until) {}

    static Window window(Instant now) {
        Instant end = now.truncatedTo(ChronoUnit.HOURS).minus(Duration.ofHours(PUBLICATION_DELAY_HOURS));
        return new Window(end.minus(Duration.ofDays(WINDOW_DAYS)), end);
    }

    static PublicLocalRequestArrivals summarize(Window window, List<PublicLocalRequestArrivalsProjection> rows) {
        var counts = new HashMap<Instant, PublicLocalRequestArrivalsProjection>();
        for (var row : rows) counts.put(row.getBucket(), row);

        var hours = new ArrayList<Hour>();
        for (Instant bucket = window.from(); bucket.isBefore(window.until()); bucket = bucket.plus(1, ChronoUnit.HOURS)) {
            var row = counts.get(bucket);
            hours.add(row == null ? new Hour(bucket, 0, 0, 0)
                : new Hour(bucket, row.getRegular(), row.getAgent(), row.getBenchmark()));
        }
        return new PublicLocalRequestArrivals(window.from(), window.until(), List.copyOf(hours));
    }
}
