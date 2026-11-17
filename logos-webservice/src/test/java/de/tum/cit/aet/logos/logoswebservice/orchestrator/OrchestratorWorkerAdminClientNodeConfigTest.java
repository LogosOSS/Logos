package de.tum.cit.aet.logos.logoswebservice.orchestrator;

import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.http.HttpEntity;
import org.springframework.http.ResponseEntity;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.web.client.RestTemplate;
import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/** The node configuration calls: what the orchestrator receives, secret included. */
class OrchestratorWorkerAdminClientNodeConfigTest {

    private RestTemplate restTemplate;
    private OrchestratorWorkerAdminClient client;

    @BeforeEach
    void setUp() {
        restTemplate = mock(RestTemplate.class);
        client = new OrchestratorWorkerAdminClient(restTemplate);
        ReflectionTestUtils.setField(client, "orchestratorUrl", "http://orchestrator");
        ReflectionTestUtils.setField(client, "internalSecret", "secret");
        when(restTemplate.postForEntity(any(String.class), any(HttpEntity.class), eq(Map.class)))
            .thenReturn(ResponseEntity.ok(Map.of()));
    }

    @SuppressWarnings({"unchecked", "rawtypes"})
    private HttpEntity<Map<String, Object>> sentTo(String url) {
        ArgumentCaptor<HttpEntity> sent = ArgumentCaptor.forClass(HttpEntity.class);
        verify(restTemplate).postForEntity(eq(url), sent.capture(), eq(Map.class));
        return sent.getValue();
    }

    @Test
    void readingAndInsightsSendOnlyTheProvider() {
        client.getNodeConfig(7);
        client.nodeConfigInsights(7);

        assertThat(sentTo("http://orchestrator/internal/logosnode/node_config/get").getBody())
            .isEqualTo(Map.of("provider_id", 7));
        assertThat(sentTo("http://orchestrator/internal/logosnode/node_config/insights").getBody())
            .isEqualTo(Map.of("provider_id", 7));
    }

    @Test
    void savingSendsTheListTheRevisionAndTheAuthor() {
        List<Map<String, Object>> entries = List.of(Map.of("model_name", "org/a", "capability", Map.of(), "overrides", Map.of()));

        client.replaceNodeConfig(7, 42, 3, entries);

        HttpEntity<Map<String, Object>> request = sentTo("http://orchestrator/internal/logosnode/node_config/replace");
        assertThat(request.getBody())
            .containsEntry("provider_id", 7)
            .containsEntry("user_id", 42)
            .containsEntry("expected_revision", 3)
            .containsEntry("entries", entries);
        assertThat(request.getHeaders().getFirst("Authorization")).isEqualTo("Bearer secret");
    }

    @Test
    void savingWithoutAnAuthorLeavesTheUserOut() {
        client.replaceNodeConfig(7, null, 0, List.of());

        assertThat(sentTo("http://orchestrator/internal/logosnode/node_config/replace").getBody())
            .doesNotContainKey("user_id");
    }

    @Test
    void calibratingAllModelsSendsOnlyTheProvider() {
        client.calibrateUncalibrated(7, null);

        assertThat(sentTo("http://orchestrator/internal/logosnode/calibrate_uncalibrated").getBody())
            .isEqualTo(Map.of("provider_id", 7));
    }

    @Test
    void calibratingOneModelNamesIt() {
        client.calibrateUncalibrated(7, "org/a");

        assertThat(sentTo("http://orchestrator/internal/logosnode/calibrate_uncalibrated").getBody())
            .isEqualTo(Map.of("provider_id", 7, "model", "org/a"));
    }

    @Test
    void aBlankModelCalibratesEverything() {
        client.calibrateUncalibrated(7, " ");

        assertThat(sentTo("http://orchestrator/internal/logosnode/calibrate_uncalibrated").getBody())
            .isEqualTo(Map.of("provider_id", 7));
    }

    @Test
    void readingTheSettingsSendsOnlyTheProvider() {
        client.getNodeSettings(7);

        assertThat(sentTo("http://orchestrator/internal/logosnode/node_settings/get").getBody())
            .isEqualTo(Map.of("provider_id", 7));
    }

    @Test
    void savingTheSettingsSendsThemWithTheRevisionAndTheAuthor() {
        client.replaceNodeSettings(7, 42, 3, Map.of("gpu_devices", "0"));

        HttpEntity<Map<String, Object>> request = sentTo("http://orchestrator/internal/logosnode/node_settings/replace");
        assertThat(request.getBody())
            .containsEntry("provider_id", 7)
            .containsEntry("user_id", 42)
            .containsEntry("expected_revision", 3)
            .containsEntry("settings", Map.of("gpu_devices", "0"));
        assertThat(request.getHeaders().getFirst("Authorization")).isEqualTo("Bearer secret");
    }

    @Test
    void savingTheSettingsWithoutAnAuthorLeavesTheUserOut() {
        client.replaceNodeSettings(7, null, 0, Map.of());

        assertThat(sentTo("http://orchestrator/internal/logosnode/node_settings/replace").getBody())
            .doesNotContainKey("user_id");
    }
}
