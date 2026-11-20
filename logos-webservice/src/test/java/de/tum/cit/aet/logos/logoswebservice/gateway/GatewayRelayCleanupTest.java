package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.ByteArrayInputStream;
import java.util.concurrent.atomic.AtomicInteger;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.web.context.request.RequestContextHolder;
import org.springframework.web.context.request.ServletWebRequest;

class GatewayRelayCleanupTest {
    @AfterEach void clear() { RequestContextHolder.resetRequestAttributes(); }

    @Test
    void synchronousAndAsyncNotificationsShareOneCloseAndPermitRelease() throws Exception {
        GatewayInFlightLimit limit = new GatewayInFlightLimit(1);
        MockHttpServletRequest request = new MockHttpServletRequest();
        ServletWebRequest webRequest = new ServletWebRequest(request);
        RequestContextHolder.setRequestAttributes(webRequest);
        GatewayRelayCleanup.begin(request, limit.tryAcquire());
        AtomicInteger closes = new AtomicInteger();
        GatewayRelayCleanup.closeWhenRequestEnds(new ByteArrayInputStream(new byte[0]) {
            @Override public void close() {
                // Release the connection before making capacity available.
                assertThat(limit.inFlight()).isEqualTo(1);
                closes.incrementAndGet();
            }
        });
        GatewayRelayCleanup cleanup = new GatewayRelayCleanup();
        cleanup.afterCompletion(request, new MockHttpServletResponse(), new Object(), null);
        cleanup.handleTimeout(webRequest, () -> null);
        cleanup.afterCompletion(webRequest, () -> null);
        assertThat(closes.get()).isEqualTo(1);
        assertThat(limit.inFlight()).isZero();
    }

    @Test
    void concurrentCompletionKeepsAClosingUpstreamCounted() throws Exception {
        GatewayInFlightLimit limit = new GatewayInFlightLimit(1);
        MockHttpServletRequest request = new MockHttpServletRequest();
        RequestContextHolder.setRequestAttributes(new ServletWebRequest(request));
        var lifecycle = GatewayRelayCleanup.begin(request, limit.tryAcquire());
        var closing = new java.util.concurrent.CountDownLatch(1);
        var finishClose = new java.util.concurrent.CountDownLatch(1);
        var action = GatewayRelayCleanup.closeWhenRequestEnds(new ByteArrayInputStream(new byte[0]) {
            @Override public void close() {
                closing.countDown();
                try {
                    finishClose.await(3, java.util.concurrent.TimeUnit.SECONDS);
                } catch (InterruptedException failure) {
                    Thread.currentThread().interrupt();
                }
            }
        });
        Thread firstClose = Thread.ofVirtual().start(action);
        try {
            assertThat(closing.await(2, java.util.concurrent.TimeUnit.SECONDS)).isTrue();
            lifecycle.close();
            assertThat(limit.inFlight()).isEqualTo(1);
        } finally {
            finishClose.countDown();
            firstClose.join();
        }
        assertThat(limit.inFlight()).isZero();
    }

    @Test
    void redispatchAndCapacityRejectionDoNotAcquireOrOverRelease() throws Exception {
        GatewayInFlightLimit limit = new GatewayInFlightLimit(1);
        try (var owner = limit.tryAcquire()) {
            GatewayRelayCleanup cleanup = new GatewayRelayCleanup();
            MockHttpServletRequest rejected = new MockHttpServletRequest();
            cleanup.preHandle(rejected, new MockHttpServletResponse(), new Object());
            cleanup.afterCompletion(rejected, new MockHttpServletResponse(), new Object(), null);
            cleanup.preHandle(rejected, new MockHttpServletResponse(), new Object());
            assertThat(limit.inFlight()).isEqualTo(1);
        }
        assertThat(limit.inFlight()).isZero();
    }

    @Test
    void resettingBenchmarkCountersLeavesAdmissionAndRejectionTotalsAlone() {
        GatewayInFlightLimit limit = new GatewayInFlightLimit(1);
        try (var owner = limit.tryAcquire()) {
            GatewayRelayStatsController stats = new GatewayRelayStatsController(limit);
            assertThat(stats.reset().getBody()).containsEntry("in_flight", 1).containsEntry("limit", 1)
                .containsEntry("executor_kind", "virtual-per-task");
            assertThat(limit.inFlight()).isEqualTo(1);
        }
    }
}
