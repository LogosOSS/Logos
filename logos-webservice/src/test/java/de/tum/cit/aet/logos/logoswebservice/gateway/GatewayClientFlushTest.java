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
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

import com.sun.net.httpserver.HttpServer;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.ResponseEntity;
import org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody;

import com.fasterxml.jackson.databind.ObjectMapper;

import jakarta.servlet.http.HttpServletResponse;

/**
 * A client that is gone shows up when the relay flushes to it. The direct-cloud
 * relay must then end like any other failed relay: the request is settled as a
 * failure, the upstream connection is released, and the relay no longer counts
 * as active.
 */
class GatewayClientFlushTest {

    private HttpServer upstream;
    private final CountDownLatch upstreamReleased = new CountDownLatch(1);

    @BeforeEach
    void startUpstream() throws IOException {
        upstream = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        upstream.createContext("/", exchange -> {
            exchange.getRequestBody().readAllBytes();
            exchange.getResponseHeaders().add("Content-Type", "text/event-stream");
            exchange.sendResponseHeaders(200, 0);
            OutputStream body = exchange.getResponseBody();
            try {
                // Keeps streaming until the gateway closes the connection.
                for (int i = 0; i < 200; i++) {
                    body.write("data: {\"choices\":[{\"delta\":{\"content\":\"x\"}}]}\n\n"
                        .getBytes(StandardCharsets.UTF_8));
                    body.flush();
                    Thread.sleep(25);
                }
            } catch (IOException e) {
                upstreamReleased.countDown();
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
        if (upstream != null) {
            upstream.stop(0);
        }
    }

    @Test
    void aFailedFlushToTheClient_settlesTheRequestAsFailedAndReleasesTheUpstream() throws Exception {
        GatewayDeployment deployment = new GatewayDeployment(
            1, "m", 10, "Cloud", "cloud", "openai", "http://127.0.0.1:" + upstream.getAddress().getPort(), null,
            "Authorization", "Bearer {}", "sk", "CLOUD_NOT_IN_EU_BY_US_PROVIDER", null);
        AtomicBoolean settledSuccess = new AtomicBoolean();
        AtomicReference<String> settledFailure = new AtomicReference<>();
        int activeBefore = activeRelays();

        ResponseEntity<StreamingResponseBody> forwarded = new GatewayCloudForwarder(new ObjectMapper()).forward(
            deployment, "/v1/chat/completions", null, "POST",
            "{\"model\":\"m\",\"stream\":true}".getBytes(StandardCharsets.UTF_8),
            Map.of("content-type", List.of("application/json")), false,
            result -> settledSuccess.set(true),
            (error, timings) -> settledFailure.set(error));
        HttpServletResponse goneClient = mock(HttpServletResponse.class);
        doThrow(new IOException("Broken pipe")).when(goneClient).flushBuffer();
        ResponseEntity<StreamingResponseBody> response = GatewayClientFlush.flushingTo(goneClient, forwarded);

        assertThatThrownBy(() -> response.getBody().writeTo(new ByteArrayOutputStream()))
            .isInstanceOf(IOException.class)
            .hasMessage("Broken pipe");

        assertThat(settledFailure.get()).isEqualTo("Broken pipe");
        assertThat(settledSuccess).isFalse();
        assertThat(activeRelays()).isEqualTo(activeBefore);
        assertThat(upstreamReleased.await(5, TimeUnit.SECONDS)).isTrue();
    }

    private static int activeRelays() {
        return (int) GatewayRelayOccupancy.snapshot().get("active");
    }
}
