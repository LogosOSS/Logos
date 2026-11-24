package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.clearInvocations;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.lang.management.ManagementFactory;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;

import de.tum.cit.aet.logos.logoswebservice.auth.JwtAuthInterceptor;
import de.tum.cit.aet.logos.logoswebservice.auth.WebConfig;
import de.tum.cit.aet.logos.logoswebservice.auth.ServletExecutorConfiguration;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.ApiKeyType;
import org.apache.catalina.Context;
import org.apache.catalina.Wrapper;
import org.apache.catalina.startup.Tomcat;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.beans.factory.config.BeanPostProcessor;
import org.springframework.core.MethodParameter;
import org.springframework.http.ResponseEntity;
import org.springframework.web.context.request.NativeWebRequest;
import org.springframework.web.method.support.HandlerMethodReturnValueHandler;
import org.springframework.web.method.support.ModelAndViewContainer;
import org.springframework.web.servlet.mvc.method.annotation.RequestMappingHandlerAdapter;
import org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;
import org.springframework.web.context.support.AnnotationConfigWebApplicationContext;
import org.springframework.web.servlet.DispatcherServlet;
import org.springframework.web.servlet.config.annotation.EnableWebMvc;

/** Real HTTP and production MVC configuration, with an independently concurrent fake provider. */
class GatewayVirtualResponseMvcTest {

    private static InferenceGatewayController controller;
    @TempDir Path tomcatBase;
    private volatile CountDownLatch release = new CountDownLatch(1);
    private final List<InputStream> clients = new ArrayList<>();
    private final ExecutorService upstreamExecutor = Executors.newVirtualThreadPerTaskExecutor();
    private HttpServer upstream;
    private Tomcat tomcat;
    private AnnotationConfigWebApplicationContext mvc;
    private HttpClient client;
    private final ExecutorService clientExecutor = Executors.newVirtualThreadPerTaskExecutor();
    private final CountDownLatch releaseHeaders = new CountDownLatch(1);
    private CountDownLatch headersReached = new CountDownLatch(0);
    private final ConcurrentLinkedQueue<Thread> requestThreads = new ConcurrentLinkedQueue<>();
    private GatewayInFlightLimit limit;
    private final GatewayAuthService auth = mock(GatewayAuthService.class);
    private final GatewayCloudAccounting accounting = mock(GatewayCloudAccounting.class);
    private final ConcurrentLinkedQueue<Thread> callbacks = new ConcurrentLinkedQueue<>();
    private final AtomicInteger upstreamCalls = new AtomicInteger();
    private final AtomicInteger synchronousCloses = new AtomicInteger();
    private String providerBase;
    private boolean pulseProvider;


    void start(int capacity, boolean enabled) throws Exception {
        limit = new GatewayInFlightLimit(capacity);
        upstream = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 4096);
        upstream.setExecutor(upstreamExecutor);
        upstream.createContext("/", exchange -> {
            upstreamCalls.incrementAndGet();
            String request = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            if (request.contains("\"hold_headers\":true")) {
                headersReached.countDown();
                try {
                    releaseHeaders.await(45, TimeUnit.SECONDS);
                } catch (InterruptedException failure) {
                    Thread.currentThread().interrupt();
                }
            }
            boolean chat = exchange.getRequestURI().getPath().endsWith("chat/completions");
            exchange.getResponseHeaders().add("Content-Type", chat ? "text/event-stream" : "application/json");
            exchange.getResponseHeaders().add("X-Upstream", "first");
            exchange.getResponseHeaders().add("X-Upstream", "second");
            exchange.sendResponseHeaders(200, 0);
            try (OutputStream out = exchange.getResponseBody()) {
                out.write((chat ? "data: first\n\n" : "{\"data\":[1],\"raw\":true}")
                    .getBytes(StandardCharsets.UTF_8));
                out.flush();
                if (request.contains("\"hold\":true")) {
                    try {
                        if (request.contains("\"pulse\":true")) {
                            // Keep exercising the real socket after client close, so
                            // Tomcat observes the disconnect without a synthetic signal.
                            while (release.getCount() != 0) {
                                out.write((":" + "x".repeat(4096) + "\n\n").getBytes(StandardCharsets.UTF_8));
                                out.flush();
                                Thread.sleep(10);
                            }
                        } else {
                            release.await(30, TimeUnit.SECONDS);
                        }
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                    }
                }
                if (chat) {
                    out.write("data: [DONE]\n\n".getBytes(StandardCharsets.UTF_8));
                }
            }
        });
        upstream.start();
        providerBase = "http://127.0.0.1:" + upstream.getAddress().getPort();
        when(auth.requireActiveKey(any())).thenAnswer(invocation -> {
            requestThreads.add(Thread.currentThread());
            return new GatewayKey(1, "lg-test", "k", ApiKeyType.developer,
                1, null, "test", false, null, 0, "BILLING");
        });
        when(auth.deploymentsForModel(any(), any())).thenReturn(List.of(new GatewayDeployment(1, "local", 10,
            "Local worker", "logosnode", null, null, null, null, null, null, "LOCAL", null)));
        GatewayCloudForwarder cloud = new GatewayCloudForwarder(new ObjectMapper(), accounting) {
            @Override public ResponseEntity<StreamingResponseBody> forward(GatewayDeployment deployment,
                    String path, String query, String method, byte[] body, Map<String, List<String>> headers,
                    Map<String, String> extra, boolean retain,
                    java.util.function.Consumer<GatewayForwardResult> success,
                    java.util.function.BiConsumer<String, GatewayForwardResult> failure) throws java.io.IOException {
                return observe(super.forward(deployment, path, query, method, body, headers, extra, retain, success, failure));
            }
        };
        GatewayOrchestratorProxy proxy = new GatewayOrchestratorProxy(providerBase) {
            @Override public ResponseEntity<StreamingResponseBody> proxy(jakarta.servlet.http.HttpServletRequest request,
                    String path, byte[] body) throws java.io.IOException {
                if (path.equals("/v1/null") || path.equals("/v1/return-failure")) {
                    GatewayRelayCleanup.closeWhenRequestEnds(new java.io.ByteArrayInputStream(new byte[0]) {
                        @Override public void close() { synchronousCloses.incrementAndGet(); }
                    });
                    return ResponseEntity.ok().body(path.equals("/v1/null") ? null : out -> out.write(1));
                }
                if (path.equals("/v1/route-failure")) throw new java.io.IOException("fake upstream failed");
                return observe(super.proxy(request, path, body));
            }
        };
        controller = new InferenceGatewayController(enabled, auth, accounting, cloud,
            mock(GatewayCloudRateLimiter.class), proxy, new ObjectMapper(), limit);
        mvc = new AnnotationConfigWebApplicationContext();
        mvc.register(GatewayMvc.class, WebConfig.class, GatewayResponseExecutorConfiguration.class, ServletExecutorConfiguration.class);
        tomcat = new Tomcat();
        tomcat.setBaseDir(tomcatBase.toString());
        tomcat.setPort(0);
        tomcat.getConnector().setProperty("acceptCount", "4096");
        Context context = tomcat.addContext("", null);
        Wrapper servlet = Tomcat.addServlet(context, "dispatcher", new DispatcherServlet(mvc));
        servlet.setAsyncSupported(true);
        servlet.setLoadOnStartup(1);
        context.addServletMappingDecoded("/", "dispatcher");
        mvc.setServletContext(context.getServletContext());
        mvc.refresh();
        if (!Boolean.getBoolean("logos.test.bounded-servlet")) {
            @SuppressWarnings("unchecked")
            var customizer = (org.springframework.boot.tomcat.TomcatProtocolHandlerCustomizer<org.apache.coyote.ProtocolHandler>)
                mvc.getBean("servletVirtualThreadCustomizer");
            customizer.customize(tomcat.getConnector().getProtocolHandler());
        }
        tomcat.start();
        client = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).executor(clientExecutor).build();
    }

    @AfterEach
    void stop() throws Exception {
        release.countDown();
        releaseHeaders.countDown();
        for (InputStream body : clients) {
            body.close();
        }
        if (client != null) client.close();
        clientExecutor.close();
        if (tomcat != null) {
            tomcat.stop();
            tomcat.destroy();
        }
        if (mvc != null) mvc.close();
        if (upstream != null) upstream.stop(0);
        upstreamExecutor.close();
        controller = null;
    }

    @Test
    void responsesProgressWithEightHeldChats() throws Exception {
        start(0, true);
        assertProgress(8);
    }

    private void openHeld(int held) throws Exception {
        for (int i = 0; i < held; i++) {
            HttpResponse<InputStream> response = client.send(request("/v1/chat/completions", true),
                HttpResponse.BodyHandlers.ofInputStream());
            clients.add(response.body());
            assertThat(response.statusCode()).isEqualTo(200);
            BufferedReader reader = new BufferedReader(new InputStreamReader(response.body(), StandardCharsets.UTF_8));
            assertThat(reader.readLine()).isEqualTo("data: first");
        }
    }

    private void assertProgress(int held) throws Exception {
        requireFileDescriptors(held);
        openHeld(held);
        long before = System.nanoTime();
        for (String path : List.of("/v1/chat/completions", "/v1/embeddings", "/v1/rerank", "/openai/embeddings", "/v1/embeddings/", "/v1/responses")) {
            HttpResponse<String> response = client.sendAsync(request(path, false),
                HttpResponse.BodyHandlers.ofString()).get(2, TimeUnit.SECONDS);
            assertThat(response.statusCode()).isEqualTo(200);
            assertThat(response.headers().allValues("X-Upstream")).containsExactly("first", "second");
            assertThat(response.body()).isEqualTo(path.endsWith("chat/completions")
                ? "data: first\n\ndata: [DONE]\n\n" : "{\"data\":[1],\"raw\":true}");
        }
        assertThat(release.getCount()).isEqualTo(1);
        assertVirtualRequests();
        assertThat(callbacks).hasSize(held + 6).allSatisfy(thread -> {
            assertThat(thread.isVirtual()).isTrue();
            assertThat(thread.getName()).startsWith("logos-response-");
        });
        System.out.printf("GATEWAY_LOAD held=%d quick_total_ms=%.1f heap_bytes=%d platform_threads=%d%n", held,
            (System.nanoTime() - before) / 1_000_000.0,
            ManagementFactory.getMemoryMXBean().getHeapMemoryUsage().getUsed(),
            ManagementFactory.getThreadMXBean().getThreadCount());
        snapshot("held-" + held);
        release.countDown();
        for (InputStream body : clients) body.readAllBytes();
        awaitReleased();
        snapshot("released-" + held);
    }

    private ResponseEntity<StreamingResponseBody> observe(ResponseEntity<StreamingResponseBody> response) {
        return ResponseEntity.status(response.getStatusCode()).headers(response.getHeaders()).body(out -> {
            callbacks.add(Thread.currentThread());
            response.getBody().writeTo(out);
        });
    }

    @ParameterizedTest
    @ValueSource(ints = {32, 64, 256, 512, 1024})
    void responsesProgressUnderHeldStreamLoad(int held) throws Exception {
        start(0, true);
        assertProgress(held);
    }

    @Test
    void directCloudResponsesAreVirtualUnderLoad() throws Exception {
        start(0, true);
        useCloud();
        assertProgress(64);
    }

    @Test
    void disabledGatewayStillDeliversResponsesOnVirtualThreads() throws Exception {
        start(0, false);
        assertProgress(8);
    }

    @Test
    void configuredCapacityRejectsPromptlyBeforeAuthOrUpstreamAndRecovers() throws Exception {
        start(264, true);
        openHeld(264);
        clearInvocations(auth, accounting);
        int calls = upstreamCalls.get();
        HttpResponse<String> rejected = client.sendAsync(request("/v1/embeddings", false),
            HttpResponse.BodyHandlers.ofString()).get(1, TimeUnit.SECONDS);
        assertThat(rejected.statusCode()).isEqualTo(503);
        assertThat(rejected.body()).isEqualTo("{\"detail\":\"Gateway capacity exhausted\"}");
        assertThat(upstreamCalls.get()).isEqualTo(calls);
        verifyNoInteractions(auth, accounting);
        assertThat(limit.inFlight()).isEqualTo(264);
        assertThat(limit.rejected()).isEqualTo(1);
        snapshot("capacity-264");
        release.countDown();
        for (InputStream body : clients) body.readAllBytes();
        awaitReleased();
        assertThat(client.send(request("/v1/embeddings", false), HttpResponse.BodyHandlers.ofString()).statusCode())
            .isEqualTo(200);
        awaitReleased();
    }

    @Test
    void disconnectedCloudClientsKeepCapacityUntilProviderSettlement() throws Exception {
        start(2, true);
        useCloud();
        pulseProvider = true;
        openHeld(2);
        for (InputStream body : clients) body.close();
        org.awaitility.Awaitility.await().atMost(Duration.ofSeconds(2)).untilAsserted(() ->
            assertThat(GatewayRelayOccupancy.snapshot().get("active")).isEqualTo(0));
        clearInvocations(auth, accounting);
        int calls = upstreamCalls.get();
        HttpResponse<String> rejected = client.sendAsync(request("/v1/chat/completions", false),
            HttpResponse.BodyHandlers.ofString()).get(1, TimeUnit.SECONDS);
        assertThat(rejected.statusCode()).isEqualTo(503);
        assertThat(upstreamCalls.get()).isEqualTo(calls);
        verifyNoInteractions(auth, accounting);
        assertThat(limit.inFlight()).isEqualTo(2);
        release.countDown();
        awaitReleased();
        org.mockito.Mockito.verify(accounting, org.mockito.Mockito.times(2))
            .settleSuccess(any(), any(), any(), any(), any());
        assertThat(client.send(request("/v1/embeddings", false), HttpResponse.BodyHandlers.ofString()).statusCode())
            .isEqualTo(200);
        awaitReleased();
    }

    @Test
    void noAsyncBodyAndReturnHandlerFailureReleaseCapacityAndUpstream() throws Exception {
        start(0, true);
        for (String path : List.of("/v1/null", "/v1/return-failure", "/v1/route-failure")) {
            HttpResponse<String> response = client.send(request(path, false), HttpResponse.BodyHandlers.ofString());
            assertThat(response.statusCode()).isEqualTo(path.equals("/v1/null") ? 200 : 500);
            awaitReleased();
        }
        assertThat(synchronousCloses.get()).isEqualTo(2);
    }

    @Test
    void closingExecutorInterruptsHeldLocalResponsesAndReleasesCapacity() throws Exception {
        start(0, true);
        openHeld(8);
        mvc.getBean("gatewayResponseExecutor", org.springframework.core.task.SimpleAsyncTaskExecutor.class).close();
        awaitReleased();
    }

    @Test
    void closingExecutorKeepsCloudDrainsCountedUntilSettlement() throws Exception {
        start(2, true);
        useCloud();
        openHeld(2);
        mvc.getBean("gatewayResponseExecutor", org.springframework.core.task.SimpleAsyncTaskExecutor.class).close();
        org.awaitility.Awaitility.await().atMost(Duration.ofSeconds(2)).untilAsserted(() ->
            assertThat(GatewayRelayOccupancy.snapshot().get("active")).isEqualTo(0));
        assertThat(limit.inFlight()).isEqualTo(2);
        release.countDown();
        awaitReleased();
        org.mockito.Mockito.verify(accounting, org.mockito.Mockito.times(2))
            .settleSuccess(any(), any(), any(), any(), any());
    }

    @Test
    void repeatedCompletedCloudCallsDoNotRetainCapacity() throws Exception {
        start(0, true);
        useCloud();
        for (int cycle = 0; cycle < 10; cycle++) {
            for (int i = 0; i < 32; i++) {
                assertThat(client.send(request("/v1/chat/completions", false), HttpResponse.BodyHandlers.ofString())
                    .body()).contains("data: [DONE]");
            }
            awaitReleased();
            snapshot("cloud-cycle-" + cycle);
        }
    }

    @Test
    void repeatedDisconnectsReleaseSocketsThreadsMemoryAndCapacity() throws Exception {
        start(8, true);
        useCloud();
        pulseProvider = true;
        Resources warm = null;
        for (int cycle = 0; cycle < 12; cycle++) {
            release = new CountDownLatch(1);
            openHeld(8);
            for (InputStream body : clients) body.close();
            org.awaitility.Awaitility.await().atMost(Duration.ofSeconds(3)).untilAsserted(() ->
                assertThat(GatewayRelayOccupancy.snapshot().get("active")).isEqualTo(0));
            assertThat(limit.inFlight()).isEqualTo(8);
            assertThat(client.sendAsync(request("/v1/embeddings", false), HttpResponse.BodyHandlers.ofString())
                .get(1, TimeUnit.SECONDS).statusCode()).isEqualTo(503);
            release.countDown();
            awaitReleased();
            org.mockito.Mockito.verify(accounting, org.mockito.Mockito.times((cycle + 1) * 8))
                .settleSuccess(any(), any(), any(), any(), any());
            clients.clear();
            callbacks.clear();
            requestThreads.clear();
            Resources now = snapshot("disconnect-cycle-" + cycle);
            if (cycle == 2) warm = now;
            if (cycle > 2) {
                assertThat(now.fds()).isLessThanOrEqualTo(warm.fds() + 32);
                assertThat(now.threads()).isLessThanOrEqualTo(warm.threads() + 32);
                assertThat(now.heap()).isLessThanOrEqualTo(warm.heap() + 128L * 1024 * 1024);
                if (now.rssKiB() >= 0 && warm.rssKiB() >= 0) {
                    assertThat(now.rssKiB()).isLessThanOrEqualTo(warm.rssKiB() + 256L * 1024);
                }
            }
        }
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void requestsProgressWhileManyCallsWaitForUpstreamHeaders(boolean cloud) throws Exception {
        int held = 1024;
        requireFileDescriptors(held);
        start(0, true);
        if (cloud) useCloud();
        headersReached = new CountDownLatch(held);
        var waiting = new ArrayList<java.util.concurrent.CompletableFuture<HttpResponse<String>>>();
        long arrivalDeadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20);
        try {
            for (int i = 0; i < held; i++) {
                waiting.add(client.sendAsync(headerRequest(), HttpResponse.BodyHandlers.ofString()));
                // Keep connection bursts below small OS listen backlogs. Every admitted
                // request stays held, so the final simultaneous load is still 1024.
                if ((i + 1) % 32 == 0) {
                    long expectedRemaining = held - i - 1L;
                    long remainingNanos = arrivalDeadline - System.nanoTime();
                    assertThat(remainingNanos).as("provider admission deadline").isPositive();
                    org.awaitility.Awaitility.await().pollInterval(Duration.ofMillis(10))
                        .atMost(Duration.ofNanos(remainingNanos))
                        .until(() -> headersReached.getCount() <= expectedRemaining);
                }
            }
            boolean allArrived = headersReached.getCount() == 0;
            System.out.printf("GATEWAY_HEADER_WAIT target=%d arrived=%d cloud=%s bounded_control=%s%n",
                held, held - headersReached.getCount(), cloud, Boolean.getBoolean("logos.test.bounded-servlet"));
            assertThat(allArrived).as("all requests must reach the fake provider before headers are released").isTrue();
            assertThat(limit.inFlight()).isEqualTo(held);
            for (String path : List.of("/v1/chat/completions", "/v1/embeddings")) {
                var probe = client.sendAsync(request(path, false), HttpResponse.BodyHandlers.ofString())
                    .get(2, TimeUnit.SECONDS);
                assertThat(probe.statusCode()).isEqualTo(200);
            }
            assertThat(releaseHeaders.getCount()).isEqualTo(1);
            assertVirtualRequests();
            snapshot("headers-held-" + held + "-cloud-" + cloud);
        } finally {
            releaseHeaders.countDown();
            java.util.concurrent.CompletableFuture.allOf(waiting.toArray(java.util.concurrent.CompletableFuture[]::new))
                .get(30, TimeUnit.SECONDS);
        }
        assertThat(waiting).allSatisfy(future -> assertThat(future.join().statusCode()).isEqualTo(200));
        awaitReleased();
    }

    @Test
    void closingTheContextInterruptsHeaderWaitsAndReleasesLocalCapacity() throws Exception {
        start(0, true);
        headersReached = new CountDownLatch(8);
        var waiting = new ArrayList<java.util.concurrent.CompletableFuture<HttpResponse<String>>>();
        for (int i = 0; i < 8; i++) waiting.add(client.sendAsync(headerRequest(), HttpResponse.BodyHandlers.ofString()));
        assertThat(headersReached.await(5, TimeUnit.SECONDS)).isTrue();
        mvc.close();
        awaitReleased();
        assertThat(requestThreads).allSatisfy(thread -> assertThat(thread.isAlive()).isFalse());
        releaseHeaders.countDown();
        for (var future : waiting) future.handle((response, error) -> null).get(3, TimeUnit.SECONDS);
    }

    private HttpRequest headerRequest() {
        return HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + tomcat.getConnector().getLocalPort() + "/v1/embeddings"))
            .header("Authorization", "Bearer lg-test").header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString("{\"model\":\"local\",\"hold_headers\":true}"))
            .build();
    }

    private void assertVirtualRequests() {
        assertThat(requestThreads).isNotEmpty().allSatisfy(thread -> {
            assertThat(thread.isVirtual()).isTrue();
            assertThat(thread.getName()).startsWith("logos-request-");
        });
    }

    private static void requireFileDescriptors(int held) {
        if (ManagementFactory.getOperatingSystemMXBean() instanceof com.sun.management.UnixOperatingSystemMXBean unix) {
            assertThat(unix.getMaxFileDescriptorCount() - unix.getOpenFileDescriptorCount())
                .as("need at least four descriptors per held HTTP call; raise ulimit -n for this load test")
                .isGreaterThan(4L * held + 256);
        }
    }

    private void useCloud() {
        when(auth.deploymentsForModel(any(), any())).thenReturn(List.of(new GatewayDeployment(1, "local", 10,
            "Cloud", "cloud", "openai", providerBase, null, "Authorization", "Bearer {}", "fake",
            "CLOUD_NOT_IN_EU_BY_US_PROVIDER", null)));
    }

    private void awaitReleased() {
        try {
            org.awaitility.Awaitility.await().atMost(Duration.ofSeconds(3))
                .untilAsserted(() -> assertThat(limit.inFlight()).isZero());
        } catch (org.awaitility.core.ConditionTimeoutException failure) {
            dumpOwnedThreads(failure);
            throw failure;
        }
    }

    private void dumpOwnedThreads(Throwable failure) {
        // ManagementFactory's platform-thread dump omits the virtual owners we need here.
        try {
            Path dump = Path.of("target", "surefire-reports", "gateway-owned-timeout-" + System.nanoTime() + ".txt")
                .toAbsolutePath();
            Process command = new ProcessBuilder(Path.of(System.getProperty("java.home"), "bin", "jcmd").toString(),
                Long.toString(ProcessHandle.current().pid()), "Thread.dump_to_file", dump.toString())
                .redirectErrorStream(true).start();
            if (!command.waitFor(5, TimeUnit.SECONDS)) {
                command.destroyForcibly().waitFor();
                throw new IllegalStateException("Gateway thread dump timed out");
            }
            if (command.exitValue() != 0) {
                throw new IllegalStateException(new String(command.getInputStream().readAllBytes(), StandardCharsets.UTF_8));
            }
            for (String thread : java.nio.file.Files.readString(dump).split("\\R\\R")) {
                if (thread.contains("gateway-") || thread.contains("logos-response-") || thread.contains("logos-request-")) {
                    System.err.println(thread);
                }
            }
        } catch (Exception diagnosticFailure) {
            failure.addSuppressed(diagnosticFailure);
        }
    }

    private record Resources(long fds, int threads, long heap, long rssKiB) {}

    private Resources snapshot(String phase) throws Exception {
        long fds = ManagementFactory.getOperatingSystemMXBean() instanceof com.sun.management.UnixOperatingSystemMXBean unix
            ? unix.getOpenFileDescriptorCount() : -1;
        String rss = "unavailable";
        if (java.nio.file.Files.isExecutable(Path.of("/bin/ps"))) {
            Process ps = new ProcessBuilder("/bin/ps", "-o", "rss=", "-p", Long.toString(ProcessHandle.current().pid())).start();
            rss = new String(ps.getInputStream().readAllBytes(), StandardCharsets.UTF_8).strip();
            ps.waitFor();
        }
        System.out.printf("GATEWAY_RESOURCES phase=%s in_flight=%d fd=%d rss_kib=%s heap_bytes=%d platform_threads=%d%n",
            phase, limit.inFlight(), fds, rss, ManagementFactory.getMemoryMXBean().getHeapMemoryUsage().getUsed(),
            ManagementFactory.getThreadMXBean().getThreadCount());
        return new Resources(fds, ManagementFactory.getThreadMXBean().getThreadCount(),
            ManagementFactory.getMemoryMXBean().getHeapMemoryUsage().getUsed(),
            rss.equals("unavailable") ? -1 : Long.parseLong(rss));
    }

    private HttpRequest request(String path, boolean held) {
        return HttpRequest.newBuilder(URI.create("http://127.0.0.1:"
                + tomcat.getConnector().getLocalPort() + path))
            .header("Authorization", "Bearer lg-test").header("Content-Type", "application/json")
            .timeout(Duration.ofSeconds(10))
            .POST(HttpRequest.BodyPublishers.ofString("{\"model\":\"local\",\"stream\":true,\"hold\":" + held
                + ",\"pulse\":" + pulseProvider + "}"))
            .build();
    }

    @Configuration
    @EnableWebMvc
    static class GatewayMvc {
        @Bean InferenceGatewayController inferenceGatewayController() { return controller; }
        @Bean GatewayExceptionHandler gatewayExceptionHandler() { return new GatewayExceptionHandler(); }
        @Bean static BeanPostProcessor failBeforeAsyncStart() {
            return new BeanPostProcessor() {
                @Override public Object postProcessAfterInitialization(Object bean, String name) {
                    if (bean instanceof RequestMappingHandlerAdapter adapter) {
                        List<HandlerMethodReturnValueHandler> handlers = new ArrayList<>();
                        for (var handler : adapter.getReturnValueHandlers()) {
                            handlers.add(new HandlerMethodReturnValueHandler() {
                                @Override public boolean supportsReturnType(MethodParameter type) {
                                    return handler.supportsReturnType(type);
                                }
                                @Override public void handleReturnValue(Object value, MethodParameter type,
                                        ModelAndViewContainer container, NativeWebRequest request) throws Exception {
                                    if (request.getNativeRequest(jakarta.servlet.http.HttpServletRequest.class)
                                            .getRequestURI().equals("/v1/return-failure")) {
                                        throw new IllegalStateException("Injected return handler failure before async start");
                                    }
                                    handler.handleReturnValue(value, type, container, request);
                                }
                            });
                        }
                        adapter.setReturnValueHandlers(handlers);
                    }
                    return bean;
                }
            };
        }
        @Bean JwtAuthInterceptor jwtAuthInterceptor() { return mock(JwtAuthInterceptor.class); }
        @Bean ThreadPoolTaskExecutor applicationTaskExecutor() {
            ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
            executor.setCorePoolSize(8);
            executor.setMaxPoolSize(64);
            executor.setQueueCapacity(200);
            executor.setThreadNamePrefix("logos-gateway-");
            return executor;
        }
    }
}
