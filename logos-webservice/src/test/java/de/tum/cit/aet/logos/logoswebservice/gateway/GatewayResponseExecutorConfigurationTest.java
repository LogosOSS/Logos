package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.when;

import java.lang.reflect.Field;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.TimeUnit;

import com.sun.net.httpserver.HttpServer;
import de.tum.cit.aet.logos.logoswebservice.TestContainersConfig;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.ApiKeyType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.ApplicationContext;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.http.ResponseEntity;
import org.springframework.scheduling.annotation.Async;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.test.annotation.DirtiesContext;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
import org.springframework.web.servlet.mvc.method.annotation.RequestMappingHandlerAdapter;
import org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody;

/** Includes Boot's MVC configurer as well as WebConfig; fixture-only wiring can hide an override. */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@Import({TestContainersConfig.class, GatewayResponseExecutorConfigurationTest.ProbeConfiguration.class})
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
class GatewayResponseExecutorConfigurationTest {
    @MockitoBean JwtDecoder jwtDecoder;
    @MockitoBean GatewayAuthService auth;
    @MockitoSpyBean GatewayOrchestratorProxy proxy;
    @Autowired ApplicationContext context;
    @Autowired RequestMappingHandlerAdapter adapter;
    @Autowired AsyncProbe asyncProbe;
    private static HttpServer upstream;

    @DynamicPropertySource
    static void properties(DynamicPropertyRegistry registry) throws Exception {
        upstream = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        upstream.createContext("/", exchange -> {
            byte[] bytes = "{\"data\":[]}".getBytes(java.nio.charset.StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, bytes.length);
            exchange.getResponseBody().write(bytes);
            exchange.close();
        });
        upstream.start();
        registry.add("logos.orchestrator.url", () -> "http://127.0.0.1:" + upstream.getAddress().getPort());
    }

    @AfterAll static void stop() { if (upstream != null) upstream.stop(0); }

    @Test
    void bootUsesVirtualResponsesAndKeepsItsBackgroundExecutor() throws Exception {
        when(auth.requireActiveKey(any())).thenReturn(new GatewayKey(1, "lg-test", "k", ApiKeyType.developer,
            1, null, "test", false, null, 0, "BILLING"));
        ConcurrentLinkedQueue<Thread> callbacks = new ConcurrentLinkedQueue<>();
        doAnswer(invocation -> {
            @SuppressWarnings("unchecked")
            ResponseEntity<StreamingResponseBody> actual = (ResponseEntity<StreamingResponseBody>) invocation.callRealMethod();
            return ResponseEntity.status(actual.getStatusCode()).headers(actual.getHeaders()).body(
                (StreamingResponseBody) out -> {
                    callbacks.add(Thread.currentThread());
                    actual.getBody().writeTo(out);
                });
        }).when(proxy).proxy(any(), any(), any());

        try (HttpClient client = HttpClient.newHttpClient()) {
            int port = context.getEnvironment().getRequiredProperty("local.server.port", Integer.class);
            HttpResponse<String> response = client.send(HttpRequest.newBuilder(
                URI.create("http://127.0.0.1:" + port + "/v1/models"))
                .header("Authorization", "Bearer lg-test").GET().build(), HttpResponse.BodyHandlers.ofString());
            assertThat(response.statusCode()).isEqualTo(200);
            assertThat(response.body()).isEqualTo("{\"data\":[]}");
        }
        assertThat(callbacks).hasSize(1).allSatisfy(thread -> {
            assertThat(thread.isVirtual()).isTrue();
            assertThat(thread.getName()).startsWith("logos-response-");
        });
        // spring-webmvc 7.0.10 has no public getter. Recheck this field on an upgrade.
        Field executorField = RequestMappingHandlerAdapter.class.getDeclaredField("taskExecutor");
        executorField.setAccessible(true);
        assertThat(executorField.get(adapter)).isSameAs(context.getBean("gatewayResponseExecutor"));
        ThreadPoolTaskExecutor background = context.getBean("applicationTaskExecutor", ThreadPoolTaskExecutor.class);
        Thread backgroundThread = asyncProbe.thread().get(2, TimeUnit.SECONDS);
        assertThat(backgroundThread.isVirtual()).isFalse();
        assertThat(backgroundThread.getName()).startsWith(background.getThreadNamePrefix());
        assertThat(background).isNotSameAs(executorField.get(adapter));
    }

    @TestConfiguration(proxyBeanMethods = false)
    static class ProbeConfiguration {
        @Bean AsyncProbe asyncProbe() { return new AsyncProbe(); }
    }

    static class AsyncProbe {
        @Async public CompletableFuture<Thread> thread() {
            return CompletableFuture.completedFuture(Thread.currentThread());
        }
    }
}
