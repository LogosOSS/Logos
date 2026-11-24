package de.tum.cit.aet.logos.logoswebservice.configuration;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentMatchers;
import org.mockito.Mockito;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.context.annotation.Import;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.context.jdbc.Sql;
import org.springframework.test.web.servlet.MockMvc;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import static org.assertj.core.api.Assertions.assertThat;

import de.tum.cit.aet.logos.logoswebservice.TestContainersConfig;
import de.tum.cit.aet.logos.logoswebservice.TestJwt;
import de.tum.cit.aet.logos.logoswebservice.orchestrator.OrchestratorHfLicenseClient;
import de.tum.cit.aet.logos.logoswebservice.orchestrator.OrchestratorHfReachabilityClient;
import de.tum.cit.aet.logos.logoswebservice.orchestrator.OrchestratorHfReachabilityClient.HfReachabilityResult;

/**
 * The model-request endpoints with the REQUIRE_LOGOS_ADMIN_LICENSE_APPROVAL
 * environment variable set to false: the Logos admin no longer handles the
 * license of a non-gated model — the listing reports the setting (so the
 * user interface can offer the text read-only), and recording an acceptance
 * for such a model is refused. Gated models keep their handling.
 */
@SpringBootTest
@AutoConfigureMockMvc
@Import(TestContainersConfig.class)
@TestPropertySource(properties = {
    "spring.liquibase.enabled=true",
    "spring.liquibase.change-log=classpath:liquibase/changelog/master.xml",
    "logos.auth.roles.logos-admin=itg-admin",
    "logos.auth.roles.app-admin=chair-member",
    "logos.auth.sync-debounce-minutes=5",
    "REQUIRE_LOGOS_ADMIN_LICENSE_APPROVAL=false"
})
@Sql(scripts = {"/sql/seed-identity.sql", "/sql/seed-configuration.sql"},
     executionPhase = Sql.ExecutionPhase.BEFORE_TEST_METHOD)
@Sql(statements = {
    "DELETE FROM notifications WHERE type = 'gated_model_requested' AND payload->>'model' IN (SELECT name FROM requested_models)",
    "DELETE FROM requested_models"
}, executionPhase = Sql.ExecutionPhase.AFTER_TEST_METHOD)
@Sql(scripts = {"/sql/cleanup-configuration.sql", "/sql/cleanup-identity.sql"},
     executionPhase = Sql.ExecutionPhase.AFTER_TEST_METHOD)
class ModelRequestLicenseSettingTest {

    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;
    @MockitoBean OrchestratorHfReachabilityClient hfReachabilityClient;
    @MockitoBean OrchestratorHfLicenseClient hfLicenseClient;

    @BeforeEach
    void stubHfClients() {
        // The precheck answers "still gated": the scheduled recheck runs from
        // context startup (initial delay 0) and can land on a seeded row while
        // a test runs — a reachable answer would unflag it and record an
        // acceptance out from under the test, a gated one leaves it alone.
        Mockito.lenient().when(hfReachabilityClient.check(ArgumentMatchers.anyString()))
            .thenReturn(new HfReachabilityResult(
                HfReachabilityResult.STATUS_REJECTED, HfReachabilityResult.REASON_MODEL_GATED, null));
    }

    @Test
    void list_reportsTheDisabledSetting() throws Exception {
        mvc.perform(post("/logosdb/get_model_requests")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.models").isArray())
           .andExpect(jsonPath("$.require_admin_license_approval").value(false));
    }

    @Test
    void approveLicense_isRefusedForANonGatedModel() throws Exception {
        int id = seedModelWithLicense("acme/licensed", "apache-2.0", false);

        mvc.perform(post("/logosdb/approve_model_license")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"model_id\": " + id + "}"))
           .andExpect(status().isBadRequest());

        // The refusal comes before any write: nothing was recorded.
        assertThat(jdbc.queryForObject(
            "SELECT COUNT(*) FROM model_license_approvals WHERE requested_model_id = ?",
            Integer.class, id)).isZero();
    }

    @Test
    void approveLicense_stillRecordsTheAcceptanceForAGatedModel() throws Exception {
        // The setting exempts non-gated models only; a gated row keeps its
        // acceptance handling.
        int id = seedModelWithLicense("acme/gated-licensed", "llama3", true);

        mvc.perform(post("/logosdb/approve_model_license")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"model_id\": " + id + "}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.approved").value(true))
           .andExpect(jsonPath("$.approved_at").isNotEmpty());
    }

    @Test
    void list_keepsReportingTheLicenseStateOfANonGatedModel() throws Exception {
        // The setting changes who handles the license, not what the listing
        // reports: the row's license state is unchanged.
        int id = seedModelWithLicense("acme/reference-only", "apache-2.0", false);

        mvc.perform(post("/logosdb/get_model_requests")
                .with(TestJwt.logosAdmin())
                .contentType("application/json").content("{}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.models[?(@.id == " + id + ")].license_status").value("available"))
           .andExpect(jsonPath("$.models[?(@.id == " + id + ")].license_approved").value(false));
    }

    private int seedModelWithLicense(String name, String licenseId, boolean gated) {
        jdbc.update("INSERT INTO requested_models (name, is_gated, license_id, license_text, license_fetched_date) "
            + "VALUES (?, ?, ?, ?, now())",
            name, gated, licenseId, licenseId == null ? null : "License text of " + name);
        return jdbc.queryForObject("SELECT id FROM requested_models WHERE name = ?", Integer.class, name);
    }
}
