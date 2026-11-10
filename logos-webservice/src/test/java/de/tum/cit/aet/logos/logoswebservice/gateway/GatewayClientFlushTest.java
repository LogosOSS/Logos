package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import com.sun.net.httpserver.HttpServer;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.ResponseEntity;
import org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody;

import com.fasterxml.jackson.databind.ObjectMapper;

import jakarta.servlet.http.HttpServletResponse;

/** Client failures release the relay while provider usage is collected independently. */
class GatewayClientFlushTest {

    private HttpServer upstream;
    private final CountDownLatch continueUpstream = new CountDownLatch(1);
    private final CountDownLatch settled = new CountDownLatch(1);
    private final AtomicReference<GatewayForwardResult> success = new AtomicReference<>();
    private final AtomicReference<String> failure = new AtomicReference<>();
    private String tail = "data: {\"usage\":{\"prompt_tokens\":37,\"completion_tokens\":19,\"total_tokens\":56}}\n\n";
    private static final byte[] REQUEST = "{\"model\":\"m\",\"stream\":true}".getBytes(StandardCharsets.UTF_8);

    @BeforeEach
    void startUpstream() throws IOException {
        upstream = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        upstream.createContext("/", exchange -> {
            exchange.getRequestBody().readAllBytes();
            exchange.getResponseHeaders().add("Content-Type", "text/event-stream");
            exchange.sendResponseHeaders(200, 0);
            try (OutputStream body = exchange.getResponseBody()) {
                body.write("data: {\"choices\":[{\"delta\":{\"content\":\"hello\"}}]}\n\n"
                    .getBytes(StandardCharsets.UTF_8));
                body.flush();
                continueUpstream.await();
                body.write(tail.getBytes(StandardCharsets.UTF_8));
                body.flush();
            } catch (IOException ignored) {
                // A completed or timed-out drain closes the provider connection.
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            } finally {
                exchange.close();
            }
        });
        upstream.start();
    }

    @AfterEach
    void stopUpstream() {
        continueUpstream.countDown();
        upstream.stop(0);
    }

    private ResponseEntity<StreamingResponseBody> forward(Duration timeout) throws Exception {
        GatewayDeployment deployment = new GatewayDeployment(
            1, "m", 10, "Cloud", "cloud", "openai", "http://127.0.0.1:" + upstream.getAddress().getPort(), null,
            "Authorization", "Bearer {}", "sk", "CLOUD_NOT_IN_EU_BY_US_PROVIDER", null);
        GatewayCloudForwarder forwarder = new GatewayCloudForwarder(new ObjectMapper());
        forwarder.setStreamDrainTimeout(timeout);
        return forwarder.forward(deployment, "/v1/chat/completions", null, "POST", REQUEST,
            Map.of("content-type", List.of("application/json")), false,
            result -> { success.set(result); settled.countDown(); },
            (error, timings) -> { failure.set(error); settled.countDown(); });
    }

    private void disconnectOnFlush(ResponseEntity<StreamingResponseBody> forwarded) throws Exception {
        HttpServletResponse goneClient = mock(HttpServletResponse.class);
        doThrow(new IOException("Broken pipe")).when(goneClient).flushBuffer();
        ResponseEntity<StreamingResponseBody> response = GatewayClientFlush.flushingTo(goneClient, forwarded);
        int activeBefore = (int) GatewayRelayOccupancy.snapshot().get("active");
        assertThatThrownBy(() -> response.getBody().writeTo(new ByteArrayOutputStream()))
            .isInstanceOf(IOException.class).hasMessage("Broken pipe");
        assertThat(GatewayRelayOccupancy.snapshot().get("active")).isEqualTo(activeBefore);
        assertThat(failure.get()).isNull();
    }

    @Test
    void failedClientFlushReleasesTheRelayBeforeSettlingFromLateProviderUsage() throws Exception {
        org.springframework.web.context.request.ServletWebRequest request =
            new org.springframework.web.context.request.ServletWebRequest(
                new org.springframework.mock.web.MockHttpServletRequest());
        org.springframework.web.context.request.RequestContextHolder.setRequestAttributes(request);
        ResponseEntity<StreamingResponseBody> forwarded;
        try {
            forwarded = forward(Duration.ofSeconds(5));
        } finally {
            org.springframework.web.context.request.RequestContextHolder.resetRequestAttributes();
        }
        disconnectOnFlush(forwarded);
        new GatewayRelayCleanup().afterCompletion(request, () -> null);
        assertThat(settled.getCount()).isEqualTo(1);
        continueUpstream.countDown();
        assertThat(settled.await(5, TimeUnit.SECONDS)).isTrue();
        assertThat(failure.get()).isNull();
        assertThat(success.get().usage()).containsExactlyInAnyOrderEntriesOf(
            Map.of("prompt_tokens", 37L, "completion_tokens", 19L, "total_tokens", 56L));
    }

    @Test
    void requestCompletionDuringProviderSilenceDetachesTheRelayAndKeepsReadingUsage() throws Exception {
        org.springframework.web.context.request.ServletWebRequest request =
            new org.springframework.web.context.request.ServletWebRequest(
                new org.springframework.mock.web.MockHttpServletRequest());
        org.springframework.web.context.request.RequestContextHolder.setRequestAttributes(request);
        ResponseEntity<StreamingResponseBody> forwarded;
        try {
            forwarded = forward(Duration.ofSeconds(5));
        } finally {
            org.springframework.web.context.request.RequestContextHolder.resetRequestAttributes();
        }
        CountDownLatch firstWrite = new CountDownLatch(1);
        CountDownLatch relayReleased = new CountDownLatch(1);
        Thread.ofVirtual().start(() -> {
            try {
                forwarded.getBody().writeTo(new ByteArrayOutputStream() {
                    @Override
                    public void flush() {
                        firstWrite.countDown();
                    }
                });
            } catch (IOException ignored) {
                // The client-side relay ends independently of provider completion.
            } finally {
                relayReleased.countDown();
            }
        });
        assertThat(firstWrite.await(5, TimeUnit.SECONDS)).isTrue();
        new GatewayRelayCleanup().afterCompletion(request, () -> null);
        assertThat(relayReleased.await(1, TimeUnit.SECONDS)).isTrue();
        assertThat(settled.getCount()).isEqualTo(1);
        continueUpstream.countDown();
        assertThat(settled.await(5, TimeUnit.SECONDS)).isTrue();
        assertThat(failure.get()).isNull();
        assertThat(success.get().usage()).containsEntry("completion_tokens", 19L);
    }

    @Test
    void failedClientWriteStillCapturesTheProviderChunkAndEstimatesAtEnd() throws Exception {
        tail = "data: {\"choices\":[{\"delta\":{\"content\":\" world!\"}}]}\n\n";
        ResponseEntity<StreamingResponseBody> forwarded = forward(Duration.ofSeconds(5));
        OutputStream gone = new OutputStream() {
            @Override
            public void write(int b) throws IOException {
                throw new IOException("Broken pipe");
            }
        };
        assertThatThrownBy(() -> forwarded.getBody().writeTo(gone)).isInstanceOf(IOException.class);
        continueUpstream.countDown();
        assertThat(settled.await(5, TimeUnit.SECONDS)).isTrue();
        assertThat(failure.get()).isNull();
        assertThat(success.get().usage()).containsEntry("completion_tokens", 3L)
            .containsEntry("prompt_tokens", (REQUEST.length + 3L) / 4).containsEntry("usage_estimated", 1L);
    }

    @Test
    void silentProviderIsClosedAndEstimatedAtTheDrainDeadline() throws Exception {
        disconnectOnFlush(forward(Duration.ofMillis(100)));
        assertThat(settled.await(5, TimeUnit.SECONDS)).isTrue();
        assertThat(failure.get()).isNull();
        assertThat(success.get().usage()).containsEntry("completion_tokens", 2L)
            .containsEntry("prompt_tokens", (REQUEST.length + 3L) / 4).containsEntry("usage_estimated", 1L);
    }
}
