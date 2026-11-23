package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import org.junit.jupiter.api.Test;
import org.springframework.web.server.ResponseStatusException;

class GatewayInFlightLimitTest {
    @Test
    void detachedProviderOwnershipKeepsCapacityUntilSettlementAndRejectsLateRetain() {
        GatewayInFlightLimit limit = new GatewayInFlightLimit(1);
        var request = limit.tryAcquire();
        var provider = request.retain();
        request.close();
        request.close();
        assertThat(limit.inFlight()).isEqualTo(1);
        assertThatThrownBy(limit::tryAcquire).isInstanceOfSatisfying(ResponseStatusException.class,
            failure -> assertThat(failure.getStatusCode().value()).isEqualTo(503));
        assertThat(limit.rejected()).isEqualTo(1);
        provider.close();
        assertThat(limit.inFlight()).isZero();
        assertThatThrownBy(provider::retain).isInstanceOf(IllegalStateException.class);
        limit.tryAcquire().close();
        assertThat(limit.inFlight()).isZero();
    }

    @Test
    void concurrentOwnersAndDuplicateCleanupCannotReleaseCapacityEarly() throws Exception {
        GatewayInFlightLimit limit = new GatewayInFlightLimit(1);
        var request = limit.tryAcquire();
        CountDownLatch release = new CountDownLatch(1);
        List<Future<?>> futures = new ArrayList<>();
        try (var executor = Executors.newVirtualThreadPerTaskExecutor()) {
            for (int i = 0; i < 256; i++) {
                var owner = request.retain();
                futures.add(executor.submit(() -> {
                    release.await();
                    owner.close();
                    owner.close();
                    return null;
                }));
            }
            request.close();
            assertThat(limit.inFlight()).isEqualTo(1);
            release.countDown();
            for (var future : futures) future.get(2, TimeUnit.SECONDS);
        }
        assertThat(limit.inFlight()).isZero();
    }

    @Test
    void disabledLimitAdmitsMoreThanTheOldCeilingAndStillTracksOwnership() {
        GatewayInFlightLimit limit = new GatewayInFlightLimit(0);
        var owners = new ArrayList<GatewayInFlightLimit.Owner>();
        for (int i = 0; i < 1024; i++) owners.add(limit.tryAcquire());
        assertThat(limit.inFlight()).isEqualTo(1024);
        assertThat(limit.rejected()).isZero();
        owners.forEach(GatewayInFlightLimit.Owner::close);
        assertThat(limit.inFlight()).isZero();
    }

    @Test
    void rejectsNegativeCapacity() {
        assertThatThrownBy(() -> new GatewayInFlightLimit(-1)).isInstanceOf(IllegalArgumentException.class);
    }
}
