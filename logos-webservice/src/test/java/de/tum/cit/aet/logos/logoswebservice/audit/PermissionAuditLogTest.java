package de.tum.cit.aet.logos.logoswebservice.audit;

import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
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
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

import de.tum.cit.aet.logos.logoswebservice.TestContainersConfig;
import de.tum.cit.aet.logos.logoswebservice.TestJwt;
import de.tum.cit.aet.logos.logoswebservice.configuration.service.PermissionService;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

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
@Sql(scripts = {"/sql/seed-identity.sql", "/sql/seed-configuration.sql"},
     executionPhase = Sql.ExecutionPhase.BEFORE_TEST_METHOD)
@Sql(scripts = {"/sql/cleanup-configuration.sql", "/sql/cleanup-identity.sql"},
     executionPhase = Sql.ExecutionPhase.AFTER_TEST_METHOD)
class PermissionAuditLogTest {
    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;
    @Autowired PermissionService permissions;
    @Autowired PlatformTransactionManager transactions;
    @Autowired PermissionAuditContext context;
    @Autowired com.fasterxml.jackson.databind.ObjectMapper objectMapper;
    @MockitoBean JwtDecoder jwtDecoder;

    @BeforeEach
    void clearHistory() {
        jdbc.update("DELETE FROM permission_audit_log");
    }

    @ParameterizedTest
    @CsvSource({
        "teams,2001,model,5001,team,test-team,gpt-4",
        "teams,2001,provider,6001,team,test-team,openai-provider",
        "api-keys,3001,model,5001,api_key,dev key,gpt-4",
        "api-keys,3001,provider,6001,api_key,dev key,openai-provider"
    })
    void fullSetWritesRecordOnlyRealChanges(String resource, int scopeId, String target, int targetId,
                                           String scope, String scopeName, String targetName) throws Exception {
        String path = "/admin/" + resource + "/" + scopeId + "/" + target + "-permissions";
        putPermissions(path, target, "[" + targetId + "]");
        Map<String, Object> grant = events().getFirst();
        assertThat(grant).containsEntry("actor_user_id", 1003).containsEntry("actor_role", "logos_admin")
            .containsEntry("action", "grant").containsEntry("cause", "direct")
            .containsEntry("team_id", 2001).containsEntry("team_name", "test-team")
            .containsEntry("scope_type", scope).containsEntry("scope_id", scopeId)
            .containsEntry("scope_name", scopeName).containsEntry("target_type", target)
            .containsEntry("target_id", targetId).containsEntry("target_name", targetName);
        assertThat(grant.get("occurred_at")).isNotNull();
        putPermissions(path, target, "[" + targetId + "," + targetId + "]");
        assertThat(events()).hasSize(1);
        putPermissions(path, target, "[]");
        assertThat(events()).hasSize(2);
        assertThat(events().getLast()).containsEntry("action", "revoke").containsEntry("cause", "direct");
    }

    @Test
    void teamOwnerIsRecordedAsTheActor() throws Exception {
        mvc.perform(put("/admin/teams/2001/model-permissions").with(TestJwt.adminUser())
                .contentType("application/json").content("{\"model_ids\":[5001]}"))
            .andExpect(status().isOk());
        assertThat(events().getFirst()).containsEntry("actor_user_id", 1002).containsEntry("actor_role", "app_admin");
    }

    @ParameterizedTest
    @CsvSource({"false", "true"})
    void providerRevocationAuditsEveryCascade(boolean atomic) throws Exception {
        seedAllGrants();
        clearHistory();
        if (atomic) {
            mvc.perform(delete("/admin/teams/2001/provider-permissions/6001").with(TestJwt.logosAdmin()))
                .andExpect(status().isOk());
        } else {
            putPermissions("/admin/teams/2001/provider-permissions", "provider", "[]");
        }
        assertThat(events()).hasSize(4);
        assertThat(events().getFirst()).containsEntry("action", "revoke").containsEntry("cause", "direct")
            .containsEntry("actor_user_id", 1003);
        assertThat(events().subList(1, 4)).allSatisfy(row -> assertThat(row)
            .containsEntry("action", "revoke").containsEntry("cause", "cascade")
            .containsEntry("actor_user_id", null).containsEntry("actor_role", "system"));
        assertThat(events()).extracting(row -> row.get("scope_type") + ":" + row.get("target_type"))
            .containsExactlyInAnyOrder("team:provider", "team:model", "api_key:provider", "api_key:model");
    }

    @Test
    void modelRevocationPrunesKeyGrantsWithCascadeCause() throws Exception {
        seedAllGrants();
        clearHistory();
        putPermissions("/admin/teams/2001/model-permissions", "model", "[]");
        assertThat(events()).hasSize(2);
        assertThat(events().getLast()).containsEntry("scope_type", "api_key").containsEntry("cause", "cascade");
    }

    @Test
    void keyProviderRevocationAuditsItsModelCascade() throws Exception {
        seedAllGrants();
        clearHistory();
        putPermissions("/admin/api-keys/3001/provider-permissions", "provider", "[]");
        assertThat(events()).hasSize(2);
        assertThat(events().getFirst()).containsEntry("target_type", "provider").containsEntry("cause", "direct");
        assertThat(events().getLast()).containsEntry("target_type", "model").containsEntry("cause", "cascade");
    }

    @Test
    void atomicRepairAndRemovalAreIdempotent() throws Exception {
        String path = "/admin/teams/2001/provider-permissions/6001";
        for (int i = 0; i < 2; i++) {
            mvc.perform(post(path).with(TestJwt.logosAdmin())).andExpect(status().isOk());
        }
        assertThat(events()).hasSize(1);
        assertThat(events().getFirst()).containsEntry("cause", "repair").containsEntry("actor_user_id", 1003);
        for (int i = 0; i < 2; i++) {
            mvc.perform(delete(path).with(TestJwt.logosAdmin())).andExpect(status().isOk());
        }
        assertThat(events()).hasSize(2);
    }

    @Test
    void permissionAndAuditRowsRollBackTogether() {
        new TransactionTemplate(transactions).executeWithoutResult(tx -> {
            permissions.setTeamModelPermissions(2001, List.of(5001));
            assertThat(events()).hasSize(1);
            tx.setRollbackOnly();
        });
        assertThat(events()).isEmpty();
        assertThat(permissions.getTeamModelPermissions(2001)).isEmpty();
    }

    @Test
    void failedWriteRollsBackAndDoesNotLeakContext() {
        assertThatThrownBy(() -> permissions.setTeamModelPermissions(2001, List.of(5001, -1)))
            .isInstanceOf(RuntimeException.class);
        assertThat(events()).isEmpty();
        assertThat(permissions.getTeamModelPermissions(2001)).isEmpty();
        jdbc.update("INSERT INTO team_model_permissions VALUES (2001, 5001)");
        assertThat(events().getFirst()).containsEntry("cause", "migration").containsEntry("actor_role", "system");
    }

    @Test
    void contextIsRestoredWithinTheSameTransaction() {
        new TransactionTemplate(transactions).executeWithoutResult(tx -> {
            context.withCause("repair", () -> permissions.setTeamModelPermissions(2001, List.of(5001)));
            jdbc.update("INSERT INTO team_model_permissions VALUES (2001, 5002)");
        });
        assertThat(events()).extracting(row -> row.get("cause")).containsExactly("direct", "migration");
    }

    @Test
    void migrationsCaptureInsertUpdateDeleteAndTruncate() {
        jdbc.update("INSERT INTO team_model_permissions VALUES (2001, 5001)");
        jdbc.update("UPDATE team_model_permissions SET model_id = model_id");
        assertThat(events()).hasSize(1);
        jdbc.update("UPDATE team_model_permissions SET model_id = 5002 WHERE team_id = 2001");
        jdbc.update("DELETE FROM team_model_permissions WHERE team_id = 2001");
        jdbc.update("INSERT INTO api_key_model_permissions VALUES (3001, 5001)");
        jdbc.execute("TRUNCATE api_key_model_permissions");
        assertThat(events()).extracting(row -> row.get("action"))
            .containsExactly("grant", "revoke", "grant", "revoke", "grant", "revoke");
        assertThat(events()).allSatisfy(row -> assertThat(row)
            .containsEntry("cause", "migration").containsEntry("actor_user_id", null)
            .containsEntry("actor_role", "system"));
        assertThat(events().getLast()).containsEntry("scope_name", "dev key").containsEntry("team_id", 2001);
    }

    @ParameterizedTest
    @CsvSource({"api_keys,3001,2", "teams,2001,4", "models,5001,2", "providers,6001,2"})
    void parentDeletionKeepsNamesAndOwnership(String table, int id, int expected) {
        seedAllGrants();
        clearHistory();
        // Table comes from the fixed test data above.
        jdbc.update("DELETE FROM " + table + " WHERE id = ?", id);
        assertThat(events()).hasSize(expected).allSatisfy(row -> {
            assertThat(row).containsEntry("team_id", 2001).containsEntry("team_name", "test-team")
                .containsEntry("action", "revoke").containsEntry("cause", "cascade");
            assertThat(row.get("scope_name")).isNotNull();
            assertThat(row.get("target_name")).isNotNull();
        });
    }

    @Test
    void adminsCanFilterAndPageWhileOwnersAreIsolated() throws Exception {
        seedAllGrants();
        jdbc.update("INSERT INTO team_model_permissions VALUES (2002, 5002)");
        long lastId = ((Number) events().getLast().get("id")).longValue();
        mvc.perform(get("/admin/permission-audit-log").with(TestJwt.logosAdmin()))
            .andExpect(status().isOk()).andExpect(jsonPath("$.length()").value(5));
        mvc.perform(get("/admin/permission-audit-log").with(TestJwt.logosAdmin())
                .param("before_id", Long.toString(lastId)).param("limit", "1"))
            .andExpect(status().isOk()).andExpect(jsonPath("$.length()").value(1))
            .andExpect(jsonPath("$[0].api_key_id").value(3001));
        mvc.perform(get("/admin/permission-audit-log").with(TestJwt.adminUser()).param("team_id", "2001")
                .param("api_key_id", "3001").param("model_id", "5001")
                .param("from", "2000-01-01T00:00:00Z").param("to", "2100-01-01T00:00:00Z"))
            .andExpect(status().isOk()).andExpect(jsonPath("$.length()").value(1))
            .andExpect(jsonPath("$[0].scope_name").value("dev key"));
        mvc.perform(get("/admin/permission-audit-log").with(TestJwt.testUser()).param("team_id", "2001"))
            .andExpect(status().isOk()).andExpect(jsonPath("$.length()").value(4));
        mvc.perform(get("/admin/permission-audit-log").with(TestJwt.adminUser()).param("team_id", "2002"))
            .andExpect(status().isForbidden());
        mvc.perform(get("/admin/permission-audit-log").with(TestJwt.adminUser()))
            .andExpect(status().isForbidden());
        mvc.perform(get("/admin/permission-audit-log").with(TestJwt.forSeededUser(1006, "kcmember", "chair-member"))
                .param("team_id", "2002"))
            .andExpect(status().isForbidden());
        mvc.perform(get("/admin/permission-audit-log")).andExpect(status().isUnauthorized());
        mvc.perform(get("/admin/permission-audit-log").with(TestJwt.logosAdmin()).param("provider_id", "6001"))
            .andExpect(status().isOk()).andExpect(jsonPath("$.length()").value(2));
        mvc.perform(get("/admin/permission-audit-log").with(TestJwt.logosAdmin()).param("to", "2000-01-01T00:00:00Z"))
            .andExpect(status().isOk()).andExpect(jsonPath("$.length()").value(0));
    }

    @Test
    void deletedKeyHistoryRemainsVisibleToItsTeamOwner() throws Exception {
        seedAllGrants();
        jdbc.update("DELETE FROM api_keys WHERE id = 3001");
        mvc.perform(get("/admin/permission-audit-log").with(TestJwt.adminUser())
                .param("team_id", "2001").param("api_key_id", "3001"))
            .andExpect(status().isOk()).andExpect(jsonPath("$.length()").value(4))
            .andExpect(jsonPath("$[0].scope_name").value("dev key"));
    }

    @Test
    void configurationImportPreservesHistoryAndRecordsItsActor() throws Exception {
        seedAllGrants();
        long lastId = ((Number) events().getLast().get("id")).longValue();
        String exported = mvc.perform(post("/logosdb/export").with(TestJwt.logosAdmin())
                .contentType("application/json").content("{}"))
            .andExpect(status().isOk()).andReturn().getResponse().getContentAsString();
        Object data = objectMapper.readValue(exported, Map.class).get("result");
        mvc.perform(post("/logosdb/import").with(TestJwt.logosAdmin()).contentType("application/json")
                .content(objectMapper.writeValueAsString(Map.of("json_data", data))))
            .andExpect(status().isOk());
        assertThat(events().stream().filter(row -> ((Number) row.get("id")).longValue() <= lastId)).hasSize(4);
        List<Map<String, Object>> imported = events().stream()
            .filter(row -> ((Number) row.get("id")).longValue() > lastId).toList();
        assertThat(imported).hasSize(8).allSatisfy(row -> assertThat(row)
            .containsEntry("actor_user_id", 1003).containsEntry("actor_role", "logos_admin")
            .containsEntry("cause", "direct").containsEntry("team_id", 2001)
            .containsEntry("team_name", "test-team"));
        assertThat(imported.stream().filter(row -> "revoke".equals(row.get("action")))).hasSize(4);
        assertThat(imported.stream().filter(row -> "grant".equals(row.get("action")))).hasSize(4);
    }

    @Test
    void auditEndpointRejectsInvalidQueriesAndAllWrites() throws Exception {
        mvc.perform(get("/admin/permission-audit-log").with(TestJwt.logosAdmin()).param("limit", "201"))
            .andExpect(status().isBadRequest());
        mvc.perform(get("/admin/permission-audit-log").with(TestJwt.logosAdmin()).param("before_id", "0"))
            .andExpect(status().isBadRequest());
        mvc.perform(get("/admin/permission-audit-log").with(TestJwt.logosAdmin())
                .param("from", "2026-01-01T00:00:00Z").param("to", "2025-01-01T00:00:00Z"))
            .andExpect(status().isBadRequest());
        mvc.perform(post("/admin/permission-audit-log").with(TestJwt.logosAdmin()))
            .andExpect(status().isMethodNotAllowed());
        mvc.perform(put("/admin/permission-audit-log").with(TestJwt.logosAdmin()))
            .andExpect(status().isMethodNotAllowed());
        mvc.perform(delete("/admin/permission-audit-log").with(TestJwt.logosAdmin()))
            .andExpect(status().isMethodNotAllowed());
    }

    private void putPermissions(String path, String target, String ids) throws Exception {
        mvc.perform(put(path).with(TestJwt.logosAdmin()).contentType("application/json")
                .content("{\"" + target + "_ids\":" + ids + "}"))
            .andExpect(status().isOk());
    }

    private List<Map<String, Object>> events() {
        return jdbc.queryForList("SELECT * FROM permission_audit_log ORDER BY id");
    }

    private void seedAllGrants() {
        jdbc.update("INSERT INTO team_provider_permissions VALUES (2001, 6001)");
        jdbc.update("INSERT INTO team_model_permissions VALUES (2001, 5001)");
        jdbc.update("INSERT INTO api_key_provider_permissions VALUES (3001, 6001)");
        jdbc.update("INSERT INTO api_key_model_permissions VALUES (3001, 5001)");
    }
}
