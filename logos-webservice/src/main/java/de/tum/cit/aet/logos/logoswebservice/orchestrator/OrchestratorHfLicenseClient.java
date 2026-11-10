package de.tum.cit.aet.logos.logoswebservice.orchestrator;

import java.util.Map;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestTemplate;

/**
 * Asks the orchestrator's secret-gated /internal/hf_license for a model
 * repository's license, read with the central HF_TOKEN: the SPDX expression
 * the model card declares plus the license file's text when the repository
 * carries one. The token lives in the orchestrator's environment, so this
 * service never sees it.
 *
 * Like the reachability client, a transport failure on this side (orchestrator
 * not configured or unreachable, timeout, unparsable answer) is reported as an
 * {@code error} rather than thrown, so callers branch on exactly one outcome
 * and the round trip never fails the request itself. A non-null error is not
 * a statement that the model is unlicensed — the row stays pending for the
 * next pass.
 */
@Service
public class OrchestratorHfLicenseClient {

    private static final Logger log = LoggerFactory.getLogger(OrchestratorHfLicenseClient.class);
    private static final ObjectMapper JSON = new ObjectMapper();

    private final RestTemplate restTemplate;

    @Value("${logos.orchestrator.url:}")
    private String orchestratorUrl;

    @Value("${logos.orchestrator.internal-secret:}")
    private String internalSecret;

    public OrchestratorHfLicenseClient(@Qualifier("hfLicenseRestTemplate") RestTemplate restTemplate) {
        this.restTemplate = restTemplate;
    }

    /** The repository's license. Never null; {@code error} is null on success. */
    public record HfLicenseResult(String licenseId, String licenseFile, String licenseText, String error) {
        public static HfLicenseResult failure(String error) {
            return new HfLicenseResult(null, null, null, error);
        }
    }

    /**
     * The license of one repository id. Any failure short of the Hub's answer
     * is an {@code error} — callers leave the row pending and retry later.
     */
    public HfLicenseResult fetch(String hfRepoId) {
        if (orchestratorUrl.isBlank() || internalSecret == null || internalSecret.isBlank()) {
            return HfLicenseResult.failure("The orchestrator is not configured.");
        }
        try {
            HttpHeaders headers = new HttpHeaders();
            headers.setContentType(MediaType.APPLICATION_JSON);
            headers.set("Authorization", "Bearer " + internalSecret);
            var response = restTemplate.exchange(
                orchestratorUrl + "/internal/hf_license",
                HttpMethod.POST,
                new HttpEntity<>(JSON.writeValueAsString(Map.of("hf_repo_id", hfRepoId)), headers),
                Map.class);
            return toResult(response.getBody());
        } catch (Exception e) {
            // Library/network errors can name internal hosts or proxies; log only.
            log.warn("HF license fetch for {} failed: {}", hfRepoId, e.getMessage());
            return HfLicenseResult.failure("The license could not be fetched right now.");
        }
    }

    /** Reduce the answer to the public fields; a missing envelope is a failure. */
    private static HfLicenseResult toResult(Object body) {
        if (!(body instanceof Map<?, ?> map)) {
            return HfLicenseResult.failure("The license answer was malformed.");
        }
        if (map.get("error") instanceof String error && !error.isBlank()) {
            return HfLicenseResult.failure(error);
        }
        return new HfLicenseResult(
            map.get("license_id") instanceof String s ? s : null,
            map.get("license_file") instanceof String f ? f : null,
            map.get("license_text") instanceof String t ? t : null,
            null);
    }
}
