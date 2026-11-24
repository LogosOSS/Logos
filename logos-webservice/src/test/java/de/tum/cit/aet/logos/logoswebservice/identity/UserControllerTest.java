package de.tum.cit.aet.logos.logoswebservice.identity;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.multipart;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.context.annotation.Import;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.context.jdbc.Sql;
import org.springframework.test.web.servlet.MockMvc;

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
class UserControllerTest {

    @Autowired MockMvc mvc;
    @MockitoBean JwtDecoder jwtDecoder;

    @Test
    void listUsers_requires_admin_key() throws Exception {
        mvc.perform(get("/users").with(TestJwt.testUser()))
           .andExpect(status().isForbidden());
    }

    @Test
    void listUsers_returns_list_for_admin() throws Exception {
        mvc.perform(get("/users").with(TestJwt.adminUser()))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$").isArray());
    }

    @Test
    void listUsers_excludes_inactive_users() throws Exception {
        mvc.perform(get("/users").with(TestJwt.adminUser()))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$[?(@.username == 'adminuser')]").isNotEmpty())
           .andExpect(jsonPath("$[?(@.username == 'inactiveuser')]").isEmpty());
    }

    @Test
    void listAdmins_returns_only_admins() throws Exception {
        mvc.perform(get("/users/admins").with(TestJwt.adminUser()))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$").isArray());
    }

    @Test
    void patchUserRole_requires_logos_admin() throws Exception {
        mvc.perform(patch("/users/1001/role")
                .with(TestJwt.adminUser())
                .contentType("application/json")
                .content("{\"role\":\"app_admin\"}"))
        .andExpect(status().isForbidden());
    }

    @Test
    void deleteUser_requires_logos_admin() throws Exception {
        mvc.perform(delete("/users/1001")
                .with(TestJwt.adminUser()))
        .andExpect(status().isForbidden());
    }

    @Test
    void createUser_returns_created_user() throws Exception {
        mvc.perform(post("/users")
                .with(TestJwt.adminUser())
                .contentType("application/json")
                .content("{\"username\":\"newuser\",\"prename\":\"N\",\"name\":\"User\",\"email\":\"n@n.com\",\"role\":\"app_developer\",\"team_ids\":[]}"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.username").value("nuser"));
    }

    @Test
    void createUser_appAdminCanAddToOwnedTeam() throws Exception {
        mvc.perform(post("/users")
                .with(TestJwt.adminUser())
                .contentType("application/json")
                .content("{\"username\":\"u2\",\"prename\":\"A\",\"name\":\"B\",\"email\":\"a@b.com\",\"role\":\"app_developer\",\"team_ids\":[2001]}"))
        .andExpect(status().isOk());
    }

    @Test
    void createUser_appAdminCannotAddToNonOwnedTeam() throws Exception {
        mvc.perform(post("/users")
                .with(TestJwt.adminUser())
                .contentType("application/json")
                .content("{\"username\":\"u3\",\"prename\":\"A\",\"name\":\"B\",\"email\":\"c@d.com\",\"role\":\"app_developer\",\"team_ids\":[9999]}"))
        .andExpect(status().isForbidden());
    }

    @Test
    void patchUserInfo_forbidden_for_app_admin() throws Exception {
        // The email of an unlinked account decides which Keycloak login adopts it.
        mvc.perform(patch("/users/1005")
                .with(TestJwt.adminUser())
                .contentType("application/json")
                .content("{\"email\":\"takeover@test.com\"}"))
        .andExpect(status().isForbidden());
    }

    @Test
    void patchUserInfo_succeeds_for_logos_admin() throws Exception {
        // 1005 is a manually created (non-Keycloak) user, so its info is editable.
        mvc.perform(patch("/users/1005")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"prename\":\"Updated\",\"name\":\"Name\",\"email\":\"upd@test.com\"}"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.prename").value("Updated"))
        .andExpect(jsonPath("$.managed").value(false));
    }

    @Test
    void patchUserInfo_conflict_for_keycloak_user() throws Exception {
        // 1001 is provisioned from Keycloak; identity is Keycloak-owned and re-synced.
        mvc.perform(patch("/users/1001")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"prename\":\"Updated\",\"name\":\"Name\",\"email\":\"upd@test.com\"}"))
        .andExpect(status().isConflict());
    }

    @Test
    void patchUserRole_succeeds_for_keycloak_user() throws Exception {
        mvc.perform(patch("/users/1001/role")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"role\":\"app_admin\"}"))
        .andExpect(status().isOk());
    }

    @Test
    void patchUserRole_succeeds_for_manual_user() throws Exception {
        mvc.perform(patch("/users/1005/role")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"role\":\"app_admin\"}"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.role").value("app_admin"));
    }

    @Test
    void patchUserRole_conflict_when_demoting_team_owner() throws Exception {
        // Team owners must stay app_admin/logos_admin; demoting one to
        // app_developer would leave an owner who cannot manage their team.
        mvc.perform(patch("/users/1005/role")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"role\":\"app_admin\"}"))
        .andExpect(status().isOk());
        mvc.perform(post("/teams/2001/members")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"user_id\":1005,\"is_owner\":true}"))
        .andExpect(status().isOk());
        mvc.perform(patch("/users/1005/role")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"role\":\"app_developer\"}"))
        .andExpect(status().isConflict());
    }

    @Test
    void deleteUser_conflict_for_keycloak_user() throws Exception {
        mvc.perform(delete("/users/1001")
                .with(TestJwt.logosAdmin()))
        .andExpect(status().isConflict());
    }

    @Test
    void deleteUser_succeeds_for_manual_user() throws Exception {
        mvc.perform(delete("/users/1005")
                .with(TestJwt.logosAdmin()))
        .andExpect(status().isOk());
    }

    @Test
    void patchUserInfo_forbidden_for_developer() throws Exception {
        mvc.perform(patch("/users/1001")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"prename\":\"X\"}"))
        .andExpect(status().isForbidden());
    }

    @Test
    void importUsers_returns_summary() throws Exception {
        mvc.perform(post("/users/import")
                .with(TestJwt.adminUser())
                .contentType("application/json")
                .content("{\"rows\":[{\"prename\":\"Alice\",\"name\":\"Smith\",\"email\":\"alice@import.com\"}]}"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.summary.created").value(1))
        .andExpect(jsonPath("$.rows[0].username").value("asmith"));
    }

    @Test
    void importUsers_marks_existing_email() throws Exception {
        mvc.perform(post("/users/import")
                .with(TestJwt.adminUser())
                .contentType("application/json")
                .content("{\"rows\":[{\"prename\":\"Test\",\"name\":\"User\",\"email\":\"test@test.com\"}]}"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.summary.existing").value(1))
        .andExpect(jsonPath("$.rows[0].status").value("existing"))
        .andExpect(jsonPath("$.rows[0].username").value("testuser"));
    }

    @Autowired JdbcTemplate jdbc;

    @Test
    void importExistingOwnerIntoTwoTeamsIsIdempotentAndReportsUnknownTeamPerRow() throws Exception {
        String csvRows = """
            {"rows":[
              {"email":"admin@test.com","team":"test-team"},
              {"email":"admin@test.com","team":"kc-team"},
              {"email":"admin@test.com","team":"kc-team"},
              {"email":"admin@test.com","team":"absent-team"}
            ]}
            """;
        for (int attempt = 0; attempt < 2; attempt++) {
            mvc.perform(post("/users/import").with(TestJwt.logosAdmin()).contentType("application/json").content(csvRows))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.summary.existing").value(3))
                .andExpect(jsonPath("$.summary.created").value(0))
                .andExpect(jsonPath("$.summary.failed").value(1))
                .andExpect(jsonPath("$.rows[3].error").value("Unknown team name"));
        }
        assertThat(jdbc.queryForList(
            "SELECT is_owner FROM team_members WHERE user_id = 1002 ORDER BY team_id", Boolean.class))
            .containsExactly(true, false);
        assertThat(jdbc.queryForObject(
            "SELECT role FROM users WHERE id = 1002", String.class)).isEqualTo("app_admin");
        assertThat(jdbc.queryForObject(
            "SELECT count(*) FROM api_keys WHERE user_id = 1002 AND team_id = 2002", Integer.class)).isEqualTo(1);
    }

    @Test
    void appAdminCannotAssignByNameOrIdOutsideOwnedTeams() throws Exception {
        mvc.perform(post("/users/import").with(TestJwt.adminUser()).contentType("application/json")
                .content("{\"team_id\":2002,\"rows\":[{\"email\":\"manual@test.com\"}]}"))
            .andExpect(status().isForbidden());
        mvc.perform(post("/users/import").with(TestJwt.adminUser()).contentType("application/json")
                .content("{\"rows\":[{\"team\":\"kc-team\",\"email\":\"manual@test.com\"}]}"))
            .andExpect(status().isOk()).andExpect(jsonPath("$.summary.failed").value(1));
    }

    @Test
    void importUsers_ignores_missing_rows() throws Exception {
        mvc.perform(post("/users/import")
                .with(TestJwt.adminUser())
                .contentType("application/json")
                .content("{}"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.summary.created").value(0))
        .andExpect(jsonPath("$.rows").isEmpty());
    }

    @Test
    void importUsers_forbidden_for_developer() throws Exception {
        mvc.perform(post("/users/import")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"rows\":[]}"))
        .andExpect(status().isForbidden());
    }

    @Test
    void importPreview_returns_columns_and_rows() throws Exception {
        String csv = "First Name,Last Name,Email,Matriculation Number,Pass Status\n"
            + "Tobias,Wasner,tobias.wasner@tum.de,984734,passed\n"
            + "Jane,Doe,jane@example.com,123456,failed\n";
        mvc.perform(multipart("/users/import/preview")
                .file(new MockMultipartFile("file", "people.csv", "text/csv", csv.getBytes()))
                .with(TestJwt.adminUser()))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.columns[0]").value("First Name"))
        .andExpect(jsonPath("$.columns[4]").value("Pass Status"))
        .andExpect(jsonPath("$.rows[0][0]").value("Tobias"))
        .andExpect(jsonPath("$.rows[1][2]").value("jane@example.com"))
        .andExpect(jsonPath("$.rows.length()").value(2));
    }

    @Test
    void importPreview_rejects_non_csv() throws Exception {
        mvc.perform(multipart("/users/import/preview")
                .file(new MockMultipartFile("file", "people.txt", "text/plain", "data".getBytes()))
                .with(TestJwt.adminUser()))
        .andExpect(status().isBadRequest());
    }

    @Test
    void importPreview_forbidden_for_developer() throws Exception {
        String csv = "First Name,Last Name,Email\nTobias,Wasner,t@tum.de\n";
        mvc.perform(multipart("/users/import/preview")
                .file(new MockMultipartFile("file", "people.csv", "text/csv", csv.getBytes()))
                .with(TestJwt.testUser()))
        .andExpect(status().isForbidden());
    }
}
