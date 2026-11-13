package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

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
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.stream.Stream;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Timeout;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;
import org.springframework.core.task.TaskRejectedException;
import org.springframework.core.task.support.TaskExecutorAdapter;
import org.springframework.http.ResponseEntity;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.web.context.request.RequestAttributes;
import org.springframework.web.context.request.RequestContextHolder;
import org.springframework.web.context.request.ServletWebRequest;
import org.springframework.web.context.request.async.StandardServletAsyncWebRequest;
import org.springframework.web.context.request.async.WebAsyncUtils;
import org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;

import jakarta.servlet.AsyncEvent;

/** Provider work is accounted for even when MVC never runs the streaming body. */
class GatewayUnattachedRelayTest {

    private enum ClientEnd { REJECTED, COMPLETED, DISCONNECTED, ASYNC_TIMEOUT, ATTACH_TIMEOUT }
    private enum ProviderEnd { USAGE, ERROR_WITH_USAGE, ERROR, STALL_AFTER_CONTENT, SILENT, EMPTY_EOF, EOF }

    private static final byte[] REQUEST = "{\"model\":\"m\",\"stream\":true}".getBytes(StandardCharsets.UTF_8);
    private static final String USAGE = "{\"prompt_tokens\":37,\"completion_tokens\":19,\"total_tokens\":56}";
    private static final Map<String, Long> TOKENS =
        Map.of("prompt_tokens", 37L, "completion_tokens", 19L, "total_tokens", 56L);

    private final CountDownLatch continueProvider = new CountDownLatch(1);
    private final CountDownLatch settled = new CountDownLatch(1);
    private final AtomicInteger settlements = new AtomicInteger();
    private final AtomicReference<GatewayForwardResult> success = new AtomicReference<>();
    private final AtomicReference<GatewayForwardResult> failure = new AtomicReference<>();
    private final AtomicReference<String> error = new AtomicReference<>();
    private HttpServer upstream;

    @AfterEach
    void stopProvider() {
        continueProvider.countDown();
        if (upstream != null) {
            upstream.stop(0);
        }
        RequestContextHolder.resetRequestAttributes();
    }

    static Stream<Arguments> abandonedStreams() {
        return Stream.of(ClientEnd.values()).flatMap(client -> Stream.of(ProviderEnd.values())
            .map(provider -> Arguments.of(client, provider)));
    }

    @ParameterizedTest
    @MethodSource("abandonedStreams")
    @Timeout(10)
    void anUnstartedRelayDrainsAndSettlesOnce(ClientEnd clientEnd, ProviderEnd providerEnd) throws Exception {
        startProvider(providerEnd);
        MockHttpServletRequest servlet = new MockHttpServletRequest();
        servlet.setAsyncSupported(true);
        MockHttpServletResponse client = new MockHttpServletResponse();
        ServletWebRequest request = new ServletWebRequest(servlet, client);
        RequestContextHolder.setRequestAttributes(request);
        ResponseEntity<StreamingResponseBody> forwarded = forward(clientEnd);
        RequestContextHolder.resetRequestAttributes();
        GatewayCloudForwarder.UpstreamPump pump = (GatewayCloudForwarder.UpstreamPump)
            request.getAttribute(GatewayRelayCleanup.ATTRIBUTE, RequestAttributes.SCOPE_REQUEST);

        if (clientEnd != ClientEnd.ATTACH_TIMEOUT) {
            StandardServletAsyncWebRequest asyncRequest = new StandardServletAsyncWebRequest(servlet, client);
            var manager = WebAsyncUtils.getAsyncManager(servlet);
            manager.setAsyncWebRequest(asyncRequest);
            manager.registerCallableInterceptor("cleanup", new GatewayRelayCleanup());
            AtomicReference<Runnable> queued = new AtomicReference<>();
            manager.setTaskExecutor(new TaskExecutorAdapter(task -> {
                if (clientEnd == ClientEnd.REJECTED) {
                    throw new TaskRejectedException("Relay executor full");
                }
                queued.set(task);
            }));
            manager.startCallableProcessing(() -> {
                forwarded.getBody().writeTo(client.getOutputStream());
                return null;
            });
            switch (clientEnd) {
                case REJECTED -> {
                    assertThat(manager.getConcurrentResult()).isInstanceOf(TaskRejectedException.class);
                    servlet.getAsyncContext().complete();
                }
                case COMPLETED -> {
                    assertThat(queued.get()).isNotNull();
                    servlet.getAsyncContext().complete();
                }
                case DISCONNECTED -> asyncRequest.onError(new AsyncEvent(servlet.getAsyncContext(),
                    servlet, client, new IOException("Broken pipe")));
                case ASYNC_TIMEOUT -> asyncRequest.onTimeout(new AsyncEvent(servlet.getAsyncContext()));
                default -> throw new AssertionError(clientEnd);
            }
            // The drain owns the stream while the provider is still waiting. A
            // late executor invocation cannot consume chunks or settle failure.
            assertLateRelayRefused(forwarded);
            if (providerEnd != ProviderEnd.EMPTY_EOF) {
                assertThat(settlements.get()).isZero();
            }
        }

        if (providerEnd != ProviderEnd.SILENT && providerEnd != ProviderEnd.STALL_AFTER_CONTENT) {
            continueProvider.countDown();
        }
        assertThat(settled.await(5, TimeUnit.SECONDS)).isTrue();
        assertThat(pump.isClosed()).isTrue();
        switch (providerEnd) {
            case USAGE -> {
                assertThat(failure.get()).isNull();
                assertThat(success.get().usage()).containsExactlyInAnyOrderEntriesOf(TOKENS);
            }
            case ERROR_WITH_USAGE, ERROR -> {
                assertThat(success.get()).isNull();
                assertThat(error.get()).isEqualTo("provider failed");
                assertThat(failure.get().usage()).containsExactlyInAnyOrderEntriesOf(
                    providerEnd == ProviderEnd.ERROR_WITH_USAGE ? TOKENS : Map.of());
            }
            case STALL_AFTER_CONTENT, EOF -> {
                assertThat(failure.get()).isNull();
                assertThat(success.get().usage()).containsEntry("completion_tokens", 2L)
                    .containsEntry("prompt_tokens", (REQUEST.length + 3L) / 4)
                    .containsEntry("usage_estimated", 1L);
            }
            case SILENT -> {
                // Still silent at the drain deadline: the prompt may be processing.
                assertThat(failure.get()).isNull();
                assertThat(success.get().usage()).containsEntry("completion_tokens", 0L)
                    .containsEntry("prompt_tokens", (REQUEST.length + 3L) / 4)
                    .containsEntry("usage_estimated", 1L);
            }
            case EMPTY_EOF -> {
                assertThat(success.get()).isNull();
                assertThat(error.get()).isEqualTo("Provider delivered no response body");
                assertThat(failure.get().usage()).isEmpty();
            }
        }
        new GatewayRelayCleanup().afterCompletion(request, () -> null);
        assertLateRelayRefused(forwarded);
        assertThat(settlements.get()).isEqualTo(1);
    }

    private void assertLateRelayRefused(ResponseEntity<StreamingResponseBody> forwarded) {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        assertThatThrownBy(() -> forwarded.getBody().writeTo(output)).isInstanceOf(IOException.class)
            .hasMessage("The upstream body is no longer available to the relay");
        assertThat(output.size()).isZero();
    }

    private ResponseEntity<StreamingResponseBody> forward(ClientEnd clientEnd) throws IOException {
        GatewayCloudForwarder forwarder = new GatewayCloudForwarder(new ObjectMapper(), null);
        forwarder.setStreamDrainTimeout(Duration.ofMillis(250));
        if (clientEnd == ClientEnd.ATTACH_TIMEOUT) {
            forwarder.setRelayAttachTimeout(Duration.ofMillis(50));
        }
        GatewayDeployment deployment = new GatewayDeployment(
            1, "m", 10, "Cloud", "cloud", "openai", "http://127.0.0.1:" + upstream.getAddress().getPort(), null,
            "Authorization", "Bearer {}", "sk", "CLOUD_NOT_IN_EU_BY_US_PROVIDER", null);
        return forwarder.forward(deployment, "/v1/chat/completions", null, "POST", REQUEST,
            Map.of("content-type", List.of("application/json")), false,
            result -> {
                success.set(result);
                settlements.incrementAndGet();
                settled.countDown();
            },
            (message, result) -> {
                error.set(message);
                failure.set(result);
                settlements.incrementAndGet();
                settled.countDown();
            });
    }

    private void startProvider(ProviderEnd providerEnd) throws IOException {
        upstream = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        upstream.createContext("/", exchange -> {
            exchange.getRequestBody().readAllBytes();
            exchange.getResponseHeaders().add("Content-Type", "text/event-stream");
            exchange.sendResponseHeaders(200, 0);
            try (OutputStream body = exchange.getResponseBody()) {
                if (providerEnd != ProviderEnd.SILENT && providerEnd != ProviderEnd.EMPTY_EOF) {
                    body.write("data: {\"choices\":[{\"delta\":{\"content\":\"hello\"}}]}\n\n"
                        .getBytes(StandardCharsets.UTF_8));
                    body.flush();
                }
                body.flush();
                if (providerEnd != ProviderEnd.EMPTY_EOF) {
                    continueProvider.await();
                }
                String tail = switch (providerEnd) {
                    case USAGE -> "data: {\"usage\":" + USAGE + "}\n\n";
                    case ERROR_WITH_USAGE ->
                        "data: {\"error\":{\"message\":\"provider failed\"},\"usage\":" + USAGE + "}\n\n";
                    case ERROR -> "data: {\"error\":{\"message\":\"provider failed\"}}\n\n";
                    default -> "";
                };
                body.write(tail.getBytes(StandardCharsets.UTF_8));
                body.flush();
            } catch (IOException ignored) {
                // A completed drain closes its provider connection.
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            } finally {
                exchange.close();
            }
        });
        upstream.start();
    }
}
