package de.tum.cit.aet.logos.logoswebservice.identity;

import static org.assertj.core.api.Assertions.assertThat;
import static org.hamcrest.Matchers.contains;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.time.Duration;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.LockSupport;

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
import org.springframework.transaction.support.TransactionTemplate;

import de.tum.cit.aet.logos.logoswebservice.TestContainersConfig;
import de.tum.cit.aet.logos.logoswebservice.TestJwt;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.ApiKeyRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.service.ApiKeyAdminService;

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
class ApiKeyLoggingAuditControllerTest {
    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;
    @Autowired TransactionTemplate transactions;
    @Autowired ApiKeyRepository keys;
    @Autowired ApiKeyAdminService adminKeys;
    @MockitoBean JwtDecoder jwtDecoder;
    private long lastAuditId;

    @BeforeEach
    void captureAuditCursor() {
        lastAuditId = jdbc.queryForObject("SELECT COALESCE(MAX(id), 0) FROM audit_log", Long.class);
    }

    @AfterEach
    void cleanAuditEvents() {
        jdbc.update("DELETE FROM audit_log WHERE team_id = 2001 AND id > ?", lastAuditId);
    }

    @Test
    void developerLegacyAndAdminChangesOnTheSameKeyFormOneOrderedHistory() throws Exception {
        changeOwnedKey("FULL");
        changeOwnedKey("FULL"); // A retry does not create another row.
        mvc.perform(post("/logosdb/set_log").with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("""
                    {"api_key_id":3001,"set_log":"BILLING"}
                    """))
            .andExpect(status().isOk());
        mvc.perform(patch("/admin/api-keys/3001").with(TestJwt.logosAdmin())
                .contentType("application/json").content("{\"log\":\"FULL\"}"))
            .andExpect(status().isOk());
        mvc.perform(patch("/admin/api-keys/3001").with(TestJwt.logosAdmin())
                .contentType("application/json").content("{\"log\":\"FULL\"}"))
            .andExpect(status().isOk());

        var rows = mvc.perform(get("/admin/audit-log").with(TestJwt.logosAdmin())
                .param("team_id", "2001").param("limit", "3"))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$[*].target_type", contains("api_key", "api_key", "api_key")))
            .andExpect(jsonPath("$[*].target_id", contains("3001", "3001", "3001")))
            .andExpect(jsonPath("$[*].actor_user_id", contains(1003, 1003, 1001)))
            .andExpect(jsonPath("$[*].action", contains(
                "api_key.updated", "api_key.log_level_changed", "api_key.log_level_changed")))
            .andExpect(jsonPath("$[*].before_state.log", contains("BILLING", "FULL", "BILLING")))
            .andExpect(jsonPath("$[*].after_state.log", contains("FULL", "BILLING", "FULL")))
            .andReturn().getResponse().getContentAsString();
        assertThat(rows).doesNotContain("dev-key-1");
        assertThat(historyCount()).isEqualTo(3);
    }

    @Test
    void onboardingAndLaterBillingSelectionBothAppearInAuditHistory() throws Exception {
        mvc.perform(post("/teams/2001/logging-onboarding").with(TestJwt.testUser())
                .contentType("application/json").content("{\"full_logging\":true}"))
            .andExpect(status().isNoContent());
        changeOwnedKey("BILLING");

        assertThat(jdbc.queryForList("""
            SELECT (before_state ->> 'log') || '→' || (after_state ->> 'log')
            FROM audit_log WHERE id > ? AND target_type = 'api_key' AND target_id = '3001'
            ORDER BY id
            """, String.class, lastAuditId)).containsExactly("BILLING→FULL", "FULL→BILLING");
    }

    @Test
    void aConcurrentLoggingChangeAuditsTheCommittedPreviousLevel() throws Exception {
        try (var executor = Executors.newSingleThreadExecutor()) {
            var pending = transactions.execute(tx -> {
                adminKeys.setLog(3001, "FULL");
                keys.flush(); // Keep FULL uncommitted while the second request starts.
                var second = executor.submit(() -> {
                    changeOwnedKey("BILLING");
                    return null;
                });
                int currentPid = jdbc.queryForObject("SELECT pg_backend_pid()", Integer.class);
                // In the regression, the second request can return a successful
                // no-op from a stale BILLING snapshot instead of waiting.
                long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
                while (!second.isDone() && System.nanoTime() < deadline) {
                    if (Boolean.TRUE.equals(jdbc.queryForObject("""
                        SELECT EXISTS (
                            SELECT FROM pg_stat_activity WHERE ? = ANY(pg_blocking_pids(pid))
                        )
                        """, Boolean.class, currentPid))) {
                        return second;
                    }
                    LockSupport.parkNanos(Duration.ofMillis(20).toNanos());
                }
                throw new AssertionError("The concurrent logging mutation did not wait for the row lock");
            });
            pending.get(10, TimeUnit.SECONDS);
        }
        assertThat(jdbc.queryForObject("SELECT log::text FROM api_keys WHERE id = 3001", String.class))
            .isEqualTo("BILLING");
        assertThat(jdbc.queryForList("""
            SELECT (before_state ->> 'log') || '→' || (after_state ->> 'log')
            FROM audit_log WHERE id > ? AND target_type = 'api_key' AND target_id = '3001'
            ORDER BY id
            """, String.class, lastAuditId)).containsExactly("BILLING→FULL", "FULL→BILLING");
    }

    @Test
    void anUnrelatedSaveFromAnOlderSnapshotCannotSilentlyRevertLogging() {
        try (var executor = Executors.newSingleThreadExecutor()) {
            transactions.executeWithoutResult(tx -> {
                var stale = keys.findById(3001).orElseThrow();
                try {
                    executor.submit(() -> { changeOwnedKey("FULL"); return null; }).get(10, TimeUnit.SECONDS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    throw new AssertionError(e);
                } catch (Exception e) {
                    throw new AssertionError(e);
                }
                stale.setIsActive(false);
                keys.saveAndFlush(stale);
            });
        }
        assertThat(jdbc.queryForObject("SELECT log::text FROM api_keys WHERE id = 3001", String.class))
            .isEqualTo("FULL");
        assertThat(historyCount()).isEqualTo(1);
    }

    private void changeOwnedKey(String level) throws Exception {
        mvc.perform(patch("/me/keys/3001/log").with(TestJwt.testUser())
                .contentType("application/json").content("{\"log\":\"" + level + "\"}"))
            .andExpect(status().isOk());
    }

    private long historyCount() {
        return jdbc.queryForObject("""
            SELECT COUNT(*) FROM audit_log
            WHERE id > ? AND target_type = 'api_key' AND target_id = '3001'
            """, Long.class, lastAuditId);
    }
}
