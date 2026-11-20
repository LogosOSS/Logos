package de.tum.cit.aet.logos.logoswebservice.operations;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.stream.Stream;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.context.annotation.Import;
import org.springframework.http.HttpMethod;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.context.jdbc.Sql;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;

import de.tum.cit.aet.logos.logoswebservice.TestContainersConfig;
import de.tum.cit.aet.logos.logoswebservice.TestJwt;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.options;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.request;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT, properties = {
    "spring.liquibase.enabled=true",
    "spring.liquibase.change-log=classpath:liquibase/changelog/master.xml",
    "logos.auth.roles.logos-admin=itg-admin",
    "logos.auth.roles.app-admin=chair-member",
    "logos.cors.allowed-origins=https://app.example",
    "logos.notifications.fanout=local"
})
@AutoConfigureMockMvc
@Import(TestContainersConfig.class)
@Sql(scripts = {"/sql/seed-identity.sql", "/sql/seed-configuration.sql", "/sql/seed-operations.sql"},
     executionPhase = Sql.ExecutionPhase.BEFORE_TEST_METHOD)
@Sql(scripts = {"/sql/cleanup-operations.sql", "/sql/cleanup-configuration.sql", "/sql/cleanup-identity.sql"},
     executionPhase = Sql.ExecutionPhase.AFTER_TEST_METHOD)
class HttpQueryIntegrationTest {

    private static final String HISTORY = """
        {"start_iso":"2020-01-01T00:00:00Z","end_iso":"2030-01-01T00:00:00Z"}
        """;
    @Autowired MockMvc mvc;
    @MockitoBean JwtDecoder jwtDecoder;
    @LocalServerPort int port;

    static Stream<Arguments> searches() {
        return Stream.of(
            Arguments.of("/logosdb/latest_requests", "{\"api_key_id\":3001}"),
            Arguments.of("/logosdb/request_payloads", "{\"request_id\":\"req-aaa-111\"}"),
            Arguments.of("/logosdb/request_routing", "{\"request_id\":\"req-aaa-111\"}"),
            Arguments.of("/logosdb/request_logs", "{\"request_ids\":[\"req-aaa-111\"]}"),
            Arguments.of("/logosdb/request_log_stats", "{\"start_date\":\"2025-01-01T00:00:00Z\",\"end_date\":\"2025-01-02T00:00:00Z\"}"),
            Arguments.of("/logosdb/request_latency_stats", "{\"start_date\":\"2025-01-01T00:00:00Z\",\"end_date\":\"2025-01-02T00:00:00Z\"}"),
            Arguments.of("/logosdb/request_log_scope_options", "{}"),
            Arguments.of("/logosdb/billing/team_budget_history", HISTORY),
            Arguments.of("/logosdb/billing/key_budget_history/2001", HISTORY)
        );
    }

    @ParameterizedTest
    @MethodSource("searches")
    void queryPreservesPostSearchResultsAndJsonBodies(String path, String body) throws Exception {
        String expected = mvc.perform(post(path).with(TestJwt.logosAdmin())
                .contentType("application/json").content(body))
            .andExpect(status().isOk()).andReturn().getResponse().getContentAsString();
        String actual = mvc.perform(query(path).with(TestJwt.logosAdmin()).content(body))
            .andExpect(status().isOk())
            .andExpect(header().string("Accept-Query", "application/json"))
            .andExpect(header().string("Cache-Control", org.hamcrest.Matchers.containsString("no-store")))
            .andReturn().getResponse().getContentAsString();
        ObjectMapper json = new ObjectMapper();
        assertThat(json.readTree(actual)).isEqualTo(json.readTree(expected));
    }

    @Test
    void queryPreservesAuthenticationAndAdminRestrictions() throws Exception {
        mvc.perform(query("/logosdb/latest_requests").content("{}"))
            .andExpect(status().isUnauthorized());
        mvc.perform(query("/logosdb/latest_requests").with(TestJwt.testUser()).content("{}"))
            .andExpect(status().isForbidden());
        mvc.perform(query("/logosdb/billing/key_budget_history/2001")
                .with(TestJwt.adminUser()).content(HISTORY))
            .andExpect(status().isOk());
        mvc.perform(query("/logosdb/billing/key_budget_history/2002")
                .with(TestJwt.adminUser()).content(HISTORY))
            .andExpect(status().isForbidden());
    }

    @ParameterizedTest
    @ValueSource(strings = {"/logosdb/add_billing", "/logosdb/delete_model", "/me/notifications/read-all",
        "/v1/chat/completions", "/openai/v1/responses", "/jobs", "/info"})
    void queryCannotDispatchMutationsOrInference(String path) throws Exception {
        mvc.perform(query(path).with(TestJwt.logosAdmin()).content("{}"))
            .andExpect(status().isMethodNotAllowed())
            .andExpect(header().string("Allow", org.hamcrest.Matchers.not(org.hamcrest.Matchers.containsString("QUERY"))));
    }

    @Test
    void rejectsInvalidBodiesAndUnsupportedMediaTypes() throws Exception {
        mvc.perform(query("/logosdb/request_logs").with(TestJwt.logosAdmin()).content("{"))
            .andExpect(status().isBadRequest());
        mvc.perform(query("/logosdb/request_logs").with(TestJwt.logosAdmin()).content("{}"))
            .andExpect(status().isBadRequest());
        mvc.perform(query("/logosdb/request_logs").with(TestJwt.logosAdmin())
                .contentType("text/plain").content("{}"))
            .andExpect(status().isUnsupportedMediaType())
            .andExpect(header().string("Accept-Query", "application/json"));
    }

    @Test
    void optionsAdvertisesOnlyOptedInQueryHandlers() throws Exception {
        mvc.perform(options("/logosdb/request_logs"))
            .andExpect(status().isNoContent())
            .andExpect(header().string("Allow", "POST, QUERY, OPTIONS"))
            .andExpect(header().string("Accept-Query", "application/json"));
        mvc.perform(options("/logosdb/batches"))
            .andExpect(header().doesNotExist("Accept-Query"));
        mvc.perform(options("/logosdb/add_billing"))
            .andExpect(header().doesNotExist("Accept-Query"));
    }

    @Test
    void corsAllowsQueryFromConfiguredOriginsOnly() throws Exception {
        mvc.perform(options("/logosdb/latest_requests")
                .header("Origin", "https://app.example")
                .header("Access-Control-Request-Method", "QUERY")
                .header("Access-Control-Request-Headers", "Authorization, Content-Type"))
            .andExpect(status().isOk())
            .andExpect(header().string("Access-Control-Allow-Methods", org.hamcrest.Matchers.containsString("QUERY")));
        mvc.perform(query("/logosdb/latest_requests").with(TestJwt.logosAdmin()).content("{}")
                .header("Origin", "https://app.example"))
            .andExpect(status().isOk())
            .andExpect(header().string("Access-Control-Allow-Origin", "https://app.example"));
        mvc.perform(query("/logosdb/latest_requests").with(TestJwt.logosAdmin()).content("{}")
                .header("Origin", "https://untrusted.example"))
            .andExpect(status().isForbidden());
        mvc.perform(options("/v1/chat/completions")
                .header("Origin", "https://app.example")
                .header("Access-Control-Request-Method", "QUERY"))
            .andExpect(status().isForbidden());
    }

    @Test
    void embeddedServletContainerAcceptsQueryOverHttp() throws Exception {
        when(jwtDecoder.decode("query-test-token")).thenReturn(TestJwt.logosAdminJwt());
        try (HttpClient client = HttpClient.newHttpClient()) {
            HttpRequest request = HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/logosdb/latest_requests"))
                .header("Authorization", "Bearer query-test-token")
                .header("Content-Type", "application/json")
                .method("QUERY", HttpRequest.BodyPublishers.ofString("{\"api_key_id\":3001}"))
                .build();
            HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
            assertThat(response.statusCode()).isEqualTo(200);
            assertThat(new ObjectMapper().readTree(response.body()).path("total").asInt()).isEqualTo(2);
        }
    }

    private static MockHttpServletRequestBuilder query(String path) {
        return request(HttpMethod.valueOf("QUERY"), path).contentType("application/json");
    }
}
