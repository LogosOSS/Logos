package de.tum.cit.aet.logos.logoswebservice.gateway;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody;

/**
 * Process-local count of direct-cloud {@link StreamingResponseBody} callbacks
 * currently running; not total gateway I/O or detached usage drains.
 *
 * <p>Opening the upstream connection happens earlier, on the servlet thread,
 * so an upstream-side overlap counter can over-count relative to these
 * callbacks. {@link GatewayInFlightLimit} separately bounds owned I/O.
 */
public final class GatewayRelayOccupancy {

    private static final AtomicInteger ACTIVE = new AtomicInteger();
    private static final AtomicInteger PEAK = new AtomicInteger();
    private static final AtomicInteger ADMITTED = new AtomicInteger();

    private GatewayRelayOccupancy() {
    }

    static void enter() {
        ADMITTED.incrementAndGet();
        int now = ACTIVE.incrementAndGet();
        PEAK.accumulateAndGet(now, Math::max);
    }

    static void leave() {
        ACTIVE.decrementAndGet();
    }

    /**
     * Wrap a streaming body so enter/leave bracket the executor task itself.
     */
    public static StreamingResponseBody track(StreamingResponseBody inner) {
        return outputStream -> {
            enter();
            try {
                inner.writeTo(outputStream);
            } finally {
                leave();
            }
        };
    }

    /** Reset peak/admitted for the next concurrency step; leave {@code active} alone. */
    public static void resetStep() {
        PEAK.set(ACTIVE.get());
        ADMITTED.set(0);
    }

    public static Map<String, Object> snapshot() {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("active", ACTIVE.get());
        out.put("peak_concurrent_relays", PEAK.get());
        out.put("admitted", ADMITTED.get());
        return out;
    }
}
