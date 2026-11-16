package de.tum.cit.aet.logos.logoswebservice.gateway;

import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.web.server.ResponseStatusException;

/** Bounds owned gateway I/O, including provider work after a client disconnects. */
@Service
public final class GatewayInFlightLimit {
    private static final Logger log = LoggerFactory.getLogger(GatewayInFlightLimit.class);
    private final int limit;
    private final AtomicInteger inFlight = new AtomicInteger();
    private final AtomicLong rejected = new AtomicLong();

    public GatewayInFlightLimit(@Value("${logos.gateway.max-in-flight:0}") int limit) {
        if (limit < 0) throw new IllegalArgumentException("Gateway in-flight limit must not be negative");
        this.limit = limit;
    }

    Owner tryAcquire() {
        while (true) {
            int current = inFlight.get();
            if (limit > 0 && current >= limit) {
                rejected.incrementAndGet();
                log.debug("Gateway capacity exhausted: in_flight={} limit={}", current, limit);
                throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE, "Gateway capacity exhausted");
            }
            if (inFlight.compareAndSet(current, current + 1)) {
                return new Owner(new Lease());
            }
        }
    }

    int inFlight() { return inFlight.get(); }
    int limit() { return limit; }
    long rejected() { return rejected.get(); }

    private final class Lease {
        private final AtomicInteger owners = new AtomicInteger(1);

        Owner retain() {
            while (true) {
                int count = owners.get();
                if (count == 0) throw new IllegalStateException("Gateway lease already ended");
                if (owners.compareAndSet(count, count + 1)) return new Owner(this);
            }
        }

        void release() {
            if (owners.decrementAndGet() == 0) inFlight.decrementAndGet();
        }
    }

    static final class Owner implements AutoCloseable {
        private final Lease lease;
        private final AtomicBoolean closed = new AtomicBoolean();

        private Owner(Lease lease) { this.lease = lease; }
        static Owner noop() { return new Owner(null); }

        Owner retain() {
            if (closed.get()) throw new IllegalStateException("Gateway owner already ended");
            return lease == null ? noop() : lease.retain();
        }

        @Override public void close() {
            if (closed.compareAndSet(false, true) && lease != null) lease.release();
        }
    }
}
