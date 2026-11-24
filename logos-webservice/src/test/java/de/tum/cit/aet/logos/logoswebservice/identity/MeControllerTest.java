package de.tum.cit.aet.logos.logoswebservice.identity;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.context.annotation.Import;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.context.jdbc.Sql;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.ResultActions;
import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import de.tum.cit.aet.logos.logoswebservice.TestContainersConfig;
import de.tum.cit.aet.logos.logoswebservice.TestJwt;

@SpringBootTest
@AutoConfigureMockMvc
@Import(TestContainersConfig.class)
@TestPropertySource(properties = {
        "spring.liquibase.enabled=true",
        "spring.liquibase.change-log=classpath:liquibase/changelog/master.xml",
        "logos.auth.roles.logos-admin=itg-admin",
        "logos.auth.roles.app-admin=chair-member",
        "logos.auth.sync-debounce-minutes=5"
})
@Sql(scripts = "/sql/seed-identity.sql", executionPhase = Sql.ExecutionPhase.BEFORE_TEST_METHOD)
@Sql(scripts = "/sql/cleanup-identity.sql", executionPhase = Sql.ExecutionPhase.AFTER_TEST_METHOD)
class MeControllerTest {

    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;
    @MockitoBean JwtDecoder jwtDecoder;

    @Test
    void returns401WithNoToken() throws Exception {
        mvc.perform(get("/me"))
           .andExpect(status().isUnauthorized());
    }

    @Test
    void returnsUserDataForValidToken() throws Exception {
        mvc.perform(get("/me").with(TestJwt.testUser()))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.username").value("testuser"))
           .andExpect(jsonPath("$.role").value("app_developer"))
           .andExpect(jsonPath("$.logging_onboarding_pending").value(true))
           .andExpect(jsonPath("$.user_id").isNumber());
    }

    @Test
    void optInOnlyChangesActiveDeveloperKeysInTheUsersTeams() throws Exception {
        jdbc.update("""
            INSERT INTO api_keys (id, key_value, name, key_type, user_id, team_id, is_active, log) VALUES
                (3010, 'test-app-key', 'app key', 'application', 1001, 2001, true, 'BILLING'),
                (3011, 'test-disabled-key', 'disabled key', 'developer', 1001, 2001, false, 'BILLING'),
                (3012, 'test-orphan-key', 'orphan key', 'developer', 1001, 2002, true, 'BILLING')
            """);

        submitLoggingChoice(true).andExpect(status().isNoContent());

        assertThat(keyLog(3001)).isEqualTo("FULL");
        for (int untouchedKey : new int[] {3003, 3010, 3011, 3012}) {
            assertThat(keyLog(untouchedKey)).isEqualTo("BILLING");
        }
        assertThat(jdbc.queryForObject(
            "SELECT full_logging FROM user_logging_onboarding WHERE user_id = 1001", Boolean.class)).isTrue();
        mvc.perform(get("/me").with(TestJwt.testUser()))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.logging_onboarding_pending").value(false));

        jdbc.update("UPDATE api_keys SET log = 'BILLING' WHERE id = 3001");
        submitLoggingChoice(true).andExpect(status().isNoContent());
        assertThat(keyLog(3001)).isEqualTo("BILLING");
    }

    @Test
    void declineKeepsBillingAndReportsAConflictingOptIn() throws Exception {
        submitLoggingChoice(false).andExpect(status().isNoContent());
        submitLoggingChoice(false).andExpect(status().isNoContent());
        submitLoggingChoice(true)
           .andExpect(status().isConflict())
           .andExpect(jsonPath("$.full_logging").value(false));

        assertThat(keyLog(3001)).isEqualTo("BILLING");
        assertThat(jdbc.queryForObject(
            "SELECT full_logging FROM user_logging_onboarding WHERE user_id = 1001", Boolean.class)).isFalse();
        mvc.perform(get("/me").with(TestJwt.testUser()))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.logging_onboarding_pending").value(false));
    }

    @Test
    void reportsPriorOptInWhenBillingIsSubmittedAfterALostResponse() throws Exception {
        submitLoggingChoice(true).andExpect(status().isNoContent());
        submitLoggingChoice(false)
           .andExpect(status().isConflict())
           .andExpect(jsonPath("$.full_logging").value(true));

        assertThat(keyLog(3001)).isEqualTo("FULL");
    }

    @Test
    void requiresAuthenticationAndAnExplicitChoice() throws Exception {
        mvc.perform(post("/me/logging-onboarding")
                .contentType("application/json")
                .content("{\"full_logging\":true}"))
           .andExpect(status().isUnauthorized());
        mvc.perform(post("/me/logging-onboarding").with(TestJwt.testUser())
                .contentType("application/json")
                .content("{}"))
           .andExpect(status().isBadRequest());
        assertThat(keyLog(3001)).isEqualTo("BILLING");
    }

    @Test
    void doesNotOfferOnboardingWithoutAnEligibleKey() throws Exception {
        jdbc.update("UPDATE api_keys SET is_active = false WHERE id = 3001");
        mvc.perform(get("/me").with(TestJwt.testUser()))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.logging_onboarding_pending").value(false));
    }

    private ResultActions submitLoggingChoice(boolean fullLogging) throws Exception {
        return mvc.perform(post("/me/logging-onboarding").with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"full_logging\":" + fullLogging + "}"));
    }

    private String keyLog(int keyId) {
        return jdbc.queryForObject("SELECT log::text FROM api_keys WHERE id = ?", String.class, keyId);
    }

    @Test
    void getRole_logosAdminReturnsRoot() throws Exception {
        mvc.perform(post("/logosdb/get_role")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.role").value("root"));
    }

    @Test
    void getRole_developerReturnsEntity() throws Exception {
        mvc.perform(post("/logosdb/get_role")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.role").value("entity"));
    }

    @Test
    void getApiKeyId_returnsFirstActiveKeyId() throws Exception {
        mvc.perform(post("/logosdb/get_api_key_id")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.result").value(3001));
    }

    @Test
    @Sql(statements = "UPDATE api_keys SET is_active = false WHERE id = 3003",
         executionPhase = Sql.ExecutionPhase.BEFORE_TEST_METHOD)
    void getApiKeyId_noActiveKey_returns404() throws Exception {
        mvc.perform(post("/logosdb/get_api_key_id")
                .with(TestJwt.adminUser())
                .contentType("application/json")
                .content("{}"))
           .andExpect(status().isNotFound())
           .andExpect(jsonPath("$.detail").isString());
    }

    @Test
    void setLog_adminCanChangeOtherKey() throws Exception {
        mvc.perform(post("/logosdb/set_log")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"api_key_id\": 3001, \"set_log\": \"FULL\"}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.result").isString());
    }

    @Test
    void setLog_developerCannotChangeOtherUsersKey() throws Exception {
        mvc.perform(post("/logosdb/set_log")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"api_key_id\": 3004, \"set_log\": \"BILLING\"}"))
           .andExpect(status().isForbidden());
    }

    @Test
    void logosKeys_returnsActiveKeysWithTeamContext() throws Exception {
        mvc.perform(get("/me/logos-keys").with(TestJwt.testUser()))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$[0].id").value(3001))
           .andExpect(jsonPath("$[0].key_value").value("dev-key-1"))
           .andExpect(jsonPath("$[0].team_id").value(2001))
           .andExpect(jsonPath("$[0].team_name").value("test-team"));
    }
}
