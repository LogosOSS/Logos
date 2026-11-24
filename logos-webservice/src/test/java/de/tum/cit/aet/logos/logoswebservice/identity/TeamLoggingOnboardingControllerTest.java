package de.tum.cit.aet.logos.logoswebservice.identity;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
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

import de.tum.cit.aet.logos.logoswebservice.TestContainersConfig;
import de.tum.cit.aet.logos.logoswebservice.TestJwt;

import static org.assertj.core.api.Assertions.assertThat;
import static org.hamcrest.Matchers.contains;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

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
class TeamLoggingOnboardingControllerTest {

    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;
    @MockitoBean JwtDecoder jwtDecoder;

    private long lastAuditId;

    @BeforeEach
    void rememberAuditStart() {
        lastAuditId = jdbc.queryForObject("SELECT COALESCE(MAX(id), 0) FROM audit_log", Long.class);
    }

    @AfterEach
    void cleanAuditEvents() {
        jdbc.update("DELETE FROM audit_log WHERE id > ? AND team_id IN (2001, 2002)", lastAuditId);
    }

    @Test
    void listsOnlyTeamsWithAnActiveBillingDeveloperKeyAndMembership() throws Exception {
        addKey(3005, "developer", 1001, 2002, true);
        addKey(3006, "application", 1001, 2002, true);

        mvc.perform(get("/me").with(TestJwt.testUser()))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.logging_onboarding_teams[*].id", contains(2001)))
            .andExpect(jsonPath("$.logging_onboarding_teams[0].name").value("test-team"));

        submit(2002, true).andExpect(status().isForbidden());
        jdbc.update("UPDATE api_keys SET is_active = false WHERE id = 3001");
        mvc.perform(get("/me").with(TestJwt.testUser()))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.logging_onboarding_teams").isEmpty());
        submit(2001, true).andExpect(status().isForbidden());
    }

    @Test
    void optInOnlyUpdatesTheCallersActiveDeveloperKeysForTheChosenTeam() throws Exception {
        jdbc.update("INSERT INTO team_members (user_id, team_id, is_owner) VALUES (1001, 2002, false)");
        addKey(3005, "developer", 1001, 2002, true);
        addKey(3006, "application", null, 2001, true);
        addKey(3007, "developer", 1001, 2001, false);

        submit(2001, true).andExpect(status().isNoContent());
        submit(2001, true).andExpect(status().isNoContent());
        submit(2001, false).andExpect(status().isConflict())
            .andExpect(jsonPath("$.full_logging").value(true));

        assertThat(keyLevel(3001)).isEqualTo("FULL");
        assertThat(keyLevel(3003)).isEqualTo("BILLING");
        assertThat(keyLevel(3005)).isEqualTo("BILLING");
        assertThat(keyLevel(3006)).isEqualTo("BILLING");
        assertThat(keyLevel(3007)).isEqualTo("BILLING");
        assertChoice(2001, "2001/1001", true);

        mvc.perform(get("/me").with(TestJwt.testUser()))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.logging_onboarding_teams[*].id", contains(2002)));
        mvc.perform(get("/me").with(TestJwt.adminUser()))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.logging_onboarding_teams[*].id", contains(2001)));
    }

    @Test
    void declineRecordsOneChoiceAndDoesNotSilentlyAcceptAConflictingRetry() throws Exception {
        submit(2001, false).andExpect(status().isNoContent());
        submit(2001, false).andExpect(status().isNoContent());
        submit(2001, true).andExpect(status().isConflict())
            .andExpect(jsonPath("$.full_logging").value(false));

        assertThat(keyLevel(3001)).isEqualTo("BILLING");
        assertChoice(2001, "2001/1001", false);
        mvc.perform(get("/me").with(TestJwt.testUser()))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.logging_onboarding_teams").isEmpty());
    }

    @Test
    void fullLoggingAlreadyEnabledNeedsNoOnboardingChoice() throws Exception {
        jdbc.update("UPDATE api_keys SET log = 'FULL' WHERE id = 3001");

        mvc.perform(get("/me").with(TestJwt.testUser()))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.logging_onboarding_teams").isEmpty());
        submit(2001, false).andExpect(status().isForbidden());
        assertThat(keyLevel(3001)).isEqualTo("FULL");
    }

    @Test
    void rejectsAnUnknownTeamAndRequiresAnExplicitAuthenticatedChoice() throws Exception {
        submit(2999, true).andExpect(status().isNotFound());
        mvc.perform(post("/teams/2001/logging-onboarding").with(TestJwt.testUser())
                .contentType("application/json").content("{}"))
            .andExpect(status().isBadRequest());
        mvc.perform(post("/teams/2001/logging-onboarding").contentType("application/json")
                .content("{\"full_logging\":true}"))
            .andExpect(status().isUnauthorized());
    }

    private ResultActions submit(int teamId, boolean fullLogging) throws Exception {
        return mvc.perform(post("/teams/{teamId}/logging-onboarding", teamId).with(TestJwt.testUser())
            .contentType("application/json").content("{\"full_logging\":" + fullLogging + "}"));
    }

    private void addKey(int id, String type, Integer userId, int teamId, boolean active) {
        jdbc.update("""
            INSERT INTO api_keys (id, key_value, name, key_type, user_id, team_id, is_active)
            VALUES (?, ?, ?, CAST(? AS api_key_type_enum), ?, ?, ?)
            """, id, "team-test-" + id, "team-test-" + id, type, userId, teamId, active);
    }

    private String keyLevel(int keyId) {
        return jdbc.queryForObject("SELECT log::text FROM api_keys WHERE id = ?", String.class, keyId);
    }

    private void assertChoice(int teamId, String target, boolean fullLogging) {
        assertThat(jdbc.queryForList("""
            SELECT (after_state ->> 'full_logging')::boolean
            FROM audit_log
            WHERE action = 'team_member.logging_onboarding_completed' AND target_type = 'team_member'
              AND team_id = ? AND target_id = ? AND actor_user_id = 1001
            """, Boolean.class, teamId, target)).containsExactly(fullLogging);
    }
}
