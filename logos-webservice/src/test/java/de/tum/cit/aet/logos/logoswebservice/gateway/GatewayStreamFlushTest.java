package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
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
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

import com.sun.net.httpserver.HttpServer;

import org.apache.catalina.Context;
import org.apache.catalina.Wrapper;
import org.apache.catalina.startup.Tomcat;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.context.support.AnnotationConfigWebApplicationContext;
import org.springframework.web.servlet.DispatcherServlet;
import org.springframework.web.servlet.config.annotation.EnableWebMvc;

import com.fasterxml.jackson.databind.ObjectMapper;

import de.tum.cit.aet.logos.logoswebservice.identity.entity.ApiKeyType;

/**
 * A streamed answer must reach the client event by event, through the real
 * servlet container and Spring MVC: both hold back what a relay writes until
 * it is flushed to the network.
 *
 * <p>The upstream sends each event only after the client has read the one
 * before. A gateway that holds events back makes the upstream give up
 * waiting, which the test reports.
 */
class GatewayStreamFlushTest {

    private static final Duration UPSTREAM_WAIT = Duration.ofSeconds(5);

    private static InferenceGatewayController controller;

    @TempDir
    Path tomcatBase;

    private HttpServer upstream;
    private Tomcat tomcat;
    private AnnotationConfigWebApplicationContext mvc;
    private final CountDownLatch clientReadFirstEvent = new CountDownLatch(1);
    private final CountDownLatch clientReadSecondEvent = new CountDownLatch(1);
    private final AtomicBoolean upstreamSawClientReadFirstEvent = new AtomicBoolean();
    private final AtomicBoolean upstreamSawClientReadSecondEvent = new AtomicBoolean();

    private final GatewayAuthService authService = mock(GatewayAuthService.class);
    private final GatewayCloudAccounting cloudAccounting = mock(GatewayCloudAccounting.class);
    private final GatewayCloudRateLimiter cloudRateLimiter = mock(GatewayCloudRateLimiter.class);

    @BeforeEach
    void start() throws Exception {
        upstream = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        upstream.createContext("/", exchange -> {
            exchange.getRequestBody().readAllBytes();
            exchange.getResponseHeaders().add("Content-Type", "text/event-stream");
            exchange.sendResponseHeaders(200, 0);
            try (OutputStream body = exchange.getResponseBody()) {
                sendThenAwait(body, "data: first\n\n", clientReadFirstEvent, upstreamSawClientReadFirstEvent);
                sendThenAwait(body, "data: second\n\n", clientReadSecondEvent, upstreamSawClientReadSecondEvent);
                body.write("data: [DONE]\n\n".getBytes(StandardCharsets.UTF_8));
            }
        });
        upstream.start();

        GatewayKey key = new GatewayKey(1, "lg-test", "k", ApiKeyType.developer, 1, null, "test",
            false, null, 0, "BILLING");
        when(authService.requireActiveKey(any())).thenReturn(key);
        when(cloudAccounting.admitAndReserve(any(), any(), any(), any(), any(), any())).thenReturn(1);
        controller = new InferenceGatewayController(true, authService, cloudAccounting,
            new GatewayCloudForwarder(new ObjectMapper()), cloudRateLimiter,
            new GatewayOrchestratorProxy(upstreamBaseUrl()), new ObjectMapper());

        mvc = new AnnotationConfigWebApplicationContext();
        mvc.register(GatewayMvc.class);
        tomcat = new Tomcat();
        tomcat.setBaseDir(tomcatBase.toString());
        tomcat.setPort(0);
        tomcat.getConnector();
        Context context = tomcat.addContext("", null);
        Wrapper dispatcher = Tomcat.addServlet(context, "dispatcher", new DispatcherServlet(mvc));
        dispatcher.setAsyncSupported(true);
        dispatcher.setLoadOnStartup(1);
        context.addServletMappingDecoded("/", "dispatcher");
        tomcat.start();
    }

    @AfterEach
    void stop() throws Exception {
        if (tomcat != null) {
            tomcat.stop();
            tomcat.destroy();
        }
        if (mvc != null) {
            mvc.close();
        }
        if (upstream != null) {
            upstream.stop(0);
        }
        controller = null;
    }

    @Test
    void anOrchestratorStream_reachesTheClientEventByEvent() throws Exception {
        when(authService.deploymentsForModel(any(), any())).thenReturn(List.of(new GatewayDeployment(
            1, "m", 20, "Worker", "logosnode", null, "", "", "", "", "", "LOCAL", null)));

        List<String> lines = streamChatCompletion();

        assertThat(upstreamSawClientReadFirstEvent).isTrue();
        assertThat(upstreamSawClientReadSecondEvent).isTrue();
        assertThat(lines).containsSubsequence("data: first", "data: second", "data: [DONE]");
    }

    @Test
    void aDirectCloudStream_reachesTheClientEventByEvent() throws Exception {
        when(authService.deploymentsForModel(any(), any())).thenReturn(List.of(new GatewayDeployment(
            1, "m", 10, "Cloud", "cloud", "openai", upstreamBaseUrl(), null, "Authorization", "Bearer {}",
            "sk", "CLOUD_NOT_IN_EU_BY_US_PROVIDER", null)));

        List<String> lines = streamChatCompletion();

        assertThat(upstreamSawClientReadFirstEvent).isTrue();
        assertThat(upstreamSawClientReadSecondEvent).isTrue();
        assertThat(lines).containsSubsequence("data: first", "data: second", "data: [DONE]");
    }

    /** Sends one event, then waits until the client has read it, and records whether it did in time. */
    private static void sendThenAwait(OutputStream body, String event, CountDownLatch clientRead,
                                      AtomicBoolean clientReadInTime) throws IOException {
        body.write(event.getBytes(StandardCharsets.UTF_8));
        body.flush();
        try {
            clientReadInTime.set(clientRead.await(UPSTREAM_WAIT.toMillis(), TimeUnit.MILLISECONDS));
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    /** Posts a streamed chat completion and reads the answer line by line as it arrives. */
    private List<String> streamChatCompletion() throws IOException, InterruptedException {
        int port = tomcat.getConnector().getLocalPort();
        HttpRequest request = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + "/v1/chat/completions"))
            .header("Authorization", "Bearer lg-test")
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(
                "{\"model\":\"m\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}"))
            .timeout(Duration.ofSeconds(30))
            .build();
        HttpResponse<InputStream> response = HttpClient.newBuilder()
            .version(HttpClient.Version.HTTP_1_1)
            .build()
            .send(request, HttpResponse.BodyHandlers.ofInputStream());
        assertThat(response.statusCode()).isEqualTo(200);

        List<String> lines = new ArrayList<>();
        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(response.body(), StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                lines.add(line);
                if (line.equals("data: first")) {
                    clientReadFirstEvent.countDown();
                } else if (line.equals("data: second")) {
                    clientReadSecondEvent.countDown();
                }
            }
        }
        return lines;
    }

    private String upstreamBaseUrl() {
        return "http://127.0.0.1:" + upstream.getAddress().getPort();
    }

    @Configuration
    @EnableWebMvc
    static class GatewayMvc {

        @Bean
        InferenceGatewayController inferenceGatewayController() {
            return controller;
        }
    }
}
