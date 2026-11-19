package de.tum.cit.aet.logos.logoswebservice.configuration;

import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentMatchers;
import org.mockito.Mockito;
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
import org.springframework.test.web.servlet.request.RequestPostProcessor;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import static org.assertj.core.api.Assertions.assertThat;

import de.tum.cit.aet.logos.logoswebservice.TestContainersConfig;
import de.tum.cit.aet.logos.logoswebservice.TestJwt;
import de.tum.cit.aet.logos.logoswebservice.configuration.service.RequestedModelHfRechecker;
import de.tum.cit.aet.logos.logoswebservice.orchestrator.OrchestratorHfLicenseClient;
import de.tum.cit.aet.logos.logoswebservice.orchestrator.OrchestratorHfLicenseClient.HfLicenseResult;
import de.tum.cit.aet.logos.logoswebservice.orchestrator.OrchestratorHfReachabilityClient;
import de.tum.cit.aet.logos.logoswebservice.orchestrator.OrchestratorHfReachabilityClient.HfReachabilityResult;

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
@Sql(scripts = {"/sql/cleanup-requested-models.sql", "/sql/cleanup-configuration.sql", "/sql/cleanup-identity.sql"},
     executionPhase = Sql.ExecutionPhase.AFTER_TEST_METHOD)
class ModelRequestControllerTest {

    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;
    @Autowired RequestedModelHfRechecker hfStatusRechecker;
    @MockitoBean JwtDecoder jwtDecoder;
    @MockitoBean OrchestratorHfReachabilityClient hfReachabilityClient;
    @MockitoBean OrchestratorHfLicenseClient hfLicenseClient;

    /**
     * The Hugging Face stubs: the precheck answers "reachable" and the license
     * fetch answers with a license and a use policy, unless a test says
     * otherwise.
     */
    @BeforeEach
    void stubHfClients() {
        Mockito.lenient().when(hfReachabilityClient.check(ArgumentMatchers.anyString()))
            .thenReturn(new HfReachabilityResult(HfReachabilityResult.STATUS_REACHABLE, null, null));
        Mockito.lenient().when(hfLicenseClient.fetch(ArgumentMatchers.anyString()))
            .thenAnswer(invocation -> new HfLicenseResult(
                "apache-2.0", "LICENSE.md", "Apache License 2.0 — " + invocation.getArgument(0),
                "USE_POLICY.md", "Use policy of " + invocation.getArgument(0), null));
    }

    /** Vote for a model and return the id of its registry row. */
    private int voteFor(String name, RequestPostProcessor jwt) throws Exception {
        mvc.perform(post("/logosdb/add_model_request")
                .with(jwt)
                .contentType("application/json")
                .content("{\"name\": \"" + name + "\", \"team_id\": 2001}"))
           .andExpect(status().isOk());
        return jdbc.queryForObject(
            "SELECT id FROM requested_models WHERE name = ?", Integer.class, name);
    }

    @Test
    void getModelRequests_emptyReturnsEmptyArray() throws Exception {
        mvc.perform(post("/logosdb/get_model_requests")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$").isArray())
           .andExpect(jsonPath("$.length()").value(0));
    }

    @Test
    void vote_createsModelAndCountsOne() throws Exception {
        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"name\": \"openai/gpt-5\", \"team_id\": 2001}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.name").value("openai/gpt-5"))
           .andExpect(jsonPath("$.request_count").value(1))
           .andExpect(jsonPath("$.has_voted").value(true))
           // The precheck answered reachable: the repository is not gated.
           .andExpect(jsonPath("$.is_gated").value(false))
           // The calling user's team is the first one to request the model.
           .andExpect(jsonPath("$.first_requesting_team").value(2001))
           .andExpect(jsonPath("$.id").isNumber());
    }

    @Test
    void vote_acceptsAGatedModelAndFlagsIt() throws Exception {
        // A gated repository is a legitimate request: it only needs a license
        // acceptance before it can be pulled, so the vote goes through and the
        // row is flagged as gated.
        Mockito.when(hfReachabilityClient.check("meta-llama/llama-3.1-8b-instruct"))
            .thenReturn(new HfReachabilityResult(
                HfReachabilityResult.STATUS_REJECTED, HfReachabilityResult.REASON_MODEL_GATED,
                "Repository access requires an authorized HF_TOKEN."));

        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"name\": \"meta-llama/llama-3.1-8b-instruct\", \"team_id\": 2001}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.name").value("meta-llama/llama-3.1-8b-instruct"))
           .andExpect(jsonPath("$.request_count").value(1))
           .andExpect(jsonPath("$.has_voted").value(true))
           .andExpect(jsonPath("$.is_gated").value(true));

        // The gating verdict is stored on the registry row.
        assertThat(jdbc.queryForObject(
            "SELECT is_gated FROM requested_models WHERE name = 'meta-llama/llama-3.1-8b-instruct'",
            Boolean.class)).isTrue();
    }

    @Test
    void vote_notifiesLogosAdminsWhenAGatedModelIsFirstRequested() throws Exception {
        // A gated repository needs a license acceptance by a Logos admin, so
        // the vote that first flags the row notifies that role.
        Mockito.when(hfReachabilityClient.check("meta-llama/llama-3.1-8b-instruct"))
            .thenReturn(new HfReachabilityResult(
                HfReachabilityResult.STATUS_REJECTED, HfReachabilityResult.REASON_MODEL_GATED,
                "Repository access requires an authorized HF_TOKEN."));

        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"name\": \"meta-llama/llama-3.1-8b-instruct\", \"team_id\": 2001}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.is_gated").value(true));

        assertThat(jdbc.queryForObject(
            "SELECT COUNT(*) FROM notifications WHERE type = 'gated_model_requested'",
            Integer.class)).isEqualTo(1);
        assertThat(jdbc.queryForList(
            "SELECT title, body, source FROM notifications WHERE type = 'gated_model_requested'"))
           .containsExactly(Map.of(
               "title", "Gated model requested",
               "body", "Gated model meta-llama/llama-3.1-8b-instruct has been requested.",
               "source", "webservice"));
        // The fan-out targets the Logos admin role, not any individual user.
        assertThat(jdbc.queryForObject(
            "SELECT COUNT(*) FROM notification_recipients r "
                + "JOIN notifications n ON n.id = r.notification_id "
                + "WHERE n.type = 'gated_model_requested' "
                + "AND r.recipient_kind = 'role' AND r.recipient_role = 'logos_admin'",
            Integer.class)).isEqualTo(1);
    }

    @Test
    void vote_notifiesOnlyOnceForAGatedModel() throws Exception {
        // The notification goes out with the vote that first flags the row;
        // later votes for the same model must not notify again.
        Mockito.when(hfReachabilityClient.check("meta-llama/llama-3.1-8b-instruct"))
            .thenReturn(new HfReachabilityResult(
                HfReachabilityResult.STATUS_REJECTED, HfReachabilityResult.REASON_MODEL_GATED,
                "Repository access requires an authorized HF_TOKEN."));

        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"name\": \"meta-llama/llama-3.1-8b-instruct\", \"team_id\": 2001}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.is_gated").value(true));

        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.adminUser())
                .contentType("application/json")
                .content("{\"name\": \"meta-llama/llama-3.1-8b-instruct\", \"team_id\": 2001}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.request_count").value(2));

        assertThat(jdbc.queryForObject(
            "SELECT COUNT(*) FROM notifications WHERE type = 'gated_model_requested'",
            Integer.class)).isEqualTo(1);
    }

    @Test
    void vote_notifiesNothingForANonGatedModel() throws Exception {
        // A reachable repository needs no license acceptance, so there is
        // nothing to notify the Logos admins about.
        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"name\": \"openai/gpt-5\", \"team_id\": 2001}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.is_gated").value(false));

        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM notifications", Integer.class)).isZero();
    }

    @Test
    void recheck_clearsTheFlagWhenTheLicenseHasBeenAccepted() throws Exception {
        jdbc.update("INSERT INTO requested_models (name, is_gated) VALUES ('acme/license-accepted', true)");
        Mockito.when(hfReachabilityClient.check("acme/license-accepted"))
            .thenReturn(new HfReachabilityResult(HfReachabilityResult.STATUS_REACHABLE, null, null));

        mvc.perform(post("/logosdb/recheck_model_requests").with(TestJwt.testUser()))
           .andExpect(status().isOk());

        assertThat(jdbc.queryForObject(
            "SELECT is_gated FROM requested_models WHERE name = 'acme/license-accepted'",
            Boolean.class)).isFalse();
    }

    @Test
    void recheck_keepsTheFlagWhileTheHubStillReportsGated() throws Exception {
        jdbc.update("INSERT INTO requested_models (name, is_gated) VALUES ('acme/still-gated', true)");
        Mockito.when(hfReachabilityClient.check("acme/still-gated"))
            .thenReturn(new HfReachabilityResult(
                HfReachabilityResult.STATUS_REJECTED, HfReachabilityResult.REASON_MODEL_GATED,
                "Repository access requires an authorized HF_TOKEN."));

        mvc.perform(post("/logosdb/recheck_model_requests").with(TestJwt.testUser()))
           .andExpect(status().isOk());

        assertThat(jdbc.queryForObject(
            "SELECT is_gated FROM requested_models WHERE name = 'acme/still-gated'",
            Boolean.class)).isTrue();
    }

    @Test
    void recheck_leavesTheFlagAloneWithoutADecisiveAnswer() throws Exception {
        // A timeout (or an unknown repository) is no answer about the license,
        // so the flag must stay until a reachable verdict comes in.
        jdbc.update("INSERT INTO requested_models (name, is_gated) VALUES ('acme/unknown', true)");
        Mockito.when(hfReachabilityClient.check("acme/unknown"))
            .thenReturn(new HfReachabilityResult(HfReachabilityResult.STATUS_UNKNOWN, null, null));

        mvc.perform(post("/logosdb/recheck_model_requests").with(TestJwt.testUser()))
           .andExpect(status().isOk());

        assertThat(jdbc.queryForObject(
            "SELECT is_gated FROM requested_models WHERE name = 'acme/unknown'",
            Boolean.class)).isTrue();
    }

    @Test
    void recheck_checksNoRepositoryWhenNothingIsFlagged() throws Exception {
        mvc.perform(post("/logosdb/recheck_model_requests").with(TestJwt.testUser()))
           .andExpect(status().isOk());

        Mockito.verify(hfReachabilityClient, Mockito.never())
            .check(ArgumentMatchers.anyString());
    }

    @Test
    void vote_requiresATeam() throws Exception {
        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"name\": \"openai/gpt-5\"}"))
           .andExpect(status().isBadRequest());

        assertThat(jdbc.queryForObject(
            "SELECT COUNT(*) FROM requested_models WHERE name = 'openai/gpt-5'", Integer.class)).isZero();
    }

    @Test
    void vote_rejectsATeamTheCallerIsNotAMemberOf() throws Exception {
        // Team 2002 exists in the seed, but the test user is not a member of it.
        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"name\": \"openai/gpt-5\", \"team_id\": 2002}"))
           .andExpect(status().isBadRequest())
           .andExpect(jsonPath("$.error").value("You can only request a model for one of your own teams."));

        assertThat(jdbc.queryForObject(
            "SELECT COUNT(*) FROM requested_models WHERE name = 'openai/gpt-5'", Integer.class)).isZero();
    }

    @Test
    void vote_keepsTheFirstRequestingTeamForLaterTeams() throws Exception {
        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"name\": \"acme/first-team\", \"team_id\": 2001}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.first_requesting_team").value(2001));

        // A second team votes for the very same model: the vote counts ...
        jdbc.update("INSERT INTO team_members (user_id, team_id, is_owner) VALUES (1002, 2002, false)");
        try {
            mvc.perform(post("/logosdb/add_model_request")
                    .with(TestJwt.adminUser())
                    .contentType("application/json")
                    .content("{\"name\": \"acme/first-team\", \"team_id\": 2002}"))
               .andExpect(status().isOk())
               .andExpect(jsonPath("$.request_count").value(2))
               // ... but the first requesting team is not overwritten.
               .andExpect(jsonPath("$.first_requesting_team").value(2001));
        } finally {
            jdbc.update("DELETE FROM team_members WHERE user_id = 1002 AND team_id = 2002");
        }
    }

    @Test
    void vote_isOnePerUserPerModel() throws Exception {
        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"name\": \"openai/gpt-5\", \"team_id\": 2001}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.request_count").value(1));

        // A second vote by the same user for the same model is a no-op: everyone
        // gets exactly one vote, so the count must not rise to two.
        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"name\": \"openai/gpt-5\", \"team_id\": 2001}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.request_count").value(1));
    }

    @Test
    void vote_secondUserIncrementsTheCount() throws Exception {
        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json").content("{\"name\": \"openai/gpt-5\", \"team_id\": 2001}"))
           .andExpect(status().isOk());
        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.adminUser())
                .contentType("application/json").content("{\"name\": \"openai/gpt-5\", \"team_id\": 2001}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.request_count").value(2));
    }

    @Test
    void vote_rejectsAModelThatIsAlreadyServed() throws Exception {
        // "openai/gpt-4" is added to the models table (simulated directly), so it
        // is already available and must not be requestable.
        jdbc.update("INSERT INTO models (id, name) VALUES (5098, 'openai/gpt-4')");
        try {
            mvc.perform(post("/logosdb/add_model_request")
                    .with(TestJwt.testUser())
                    .contentType("application/json")
                    .content("{\"name\": \"openai/gpt-4\", \"team_id\": 2001}"))
               .andExpect(status().isConflict())
               .andExpect(jsonPath("$.error").exists());
        } finally {
            jdbc.update("DELETE FROM models WHERE id = 5098");
        }
        // The local availability check runs before the Hugging Face precheck.
        Mockito.verify(hfReachabilityClient, Mockito.never()).check(ArgumentMatchers.anyString());
    }

    @Test
    void vote_rejectsAModelServedDuringThePrecheck() throws Exception {
        // An admin adds the model while the precheck is in flight: the outer
        // availability check already answered "not served", so only the
        // re-check under the shared model-name/alias namespace lock can catch
        // it and refuse the vote.
        Mockito.when(hfReachabilityClient.check("openai/raced"))
            .thenAnswer(invocation -> {
                jdbc.update("INSERT INTO models (id, name) VALUES (5100, 'openai/raced')");
                return new HfReachabilityResult(HfReachabilityResult.STATUS_REACHABLE, null, null);
            });
        try {
            mvc.perform(post("/logosdb/add_model_request")
                    .with(TestJwt.testUser())
                    .contentType("application/json")
                    .content("{\"name\": \"openai/raced\", \"team_id\": 2001}"))
               .andExpect(status().isConflict())
               .andExpect(jsonPath("$.error").value("This model is already available in Logos."));
            assertThat(jdbc.queryForObject(
                "SELECT COUNT(*) FROM requested_models WHERE name = 'openai/raced'", Integer.class)).isZero();
        } finally {
            jdbc.update("DELETE FROM models WHERE id = 5100");
        }
    }

    @Test
    void vote_rejectedByTheHfPrecheckIsBadRequest() throws Exception {
        Mockito.when(hfReachabilityClient.check("openai/does-not-exist"))
            .thenReturn(new HfReachabilityResult(
                HfReachabilityResult.STATUS_REJECTED, "model-not-found-or-unauthorized", null));

        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"name\": \"openai/does-not-exist\", \"team_id\": 2001}"))
           .andExpect(status().isBadRequest())
           .andExpect(jsonPath("$.error").value(
               "Model not found on HuggingFace. Maybe it is a cloud model (unsupported)?"));

        // The precheck runs before the registry row is created: nothing was stored.
        assertThat(jdbc.queryForObject(
            "SELECT COUNT(*) FROM requested_models WHERE name = 'openai/does-not-exist'", Integer.class)).isZero();
    }

    @Test
    void vote_unknownHfPrecheckResultShowsATimeout() throws Exception {
        Mockito.when(hfReachabilityClient.check("openai/slow-hub"))
            .thenReturn(HfReachabilityResult.unknown("Hugging Face Hub did not answer within 15s."));

        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"name\": \"openai/slow-hub\", \"team_id\": 2001}"))
           .andExpect(status().isServiceUnavailable())
           .andExpect(jsonPath("$.error").value(
               "Hugging Face did not respond in time. Please try again later."));
    }

    @Test
    void vote_checksTheNormalizedNameAgainstHuggingFace() throws Exception {
        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"name\": \"https://huggingface.co/openai/gpt-oss-120b\", \"team_id\": 2001}"))
           .andExpect(status().isOk());

        // The URL prefix is stripped before the precheck sees the name.
        Mockito.verify(hfReachabilityClient).check("openai/gpt-oss-120b");
    }

    @Test
    void undo_takesTheVoteBackAndAllowsRevoting() throws Exception {
        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json").content("{\"name\": \"anthropic/claude\", \"team_id\": 2001}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.request_count").value(1));

        // Undo: the (only) vote is removed, the model's registry row goes with it.
        mvc.perform(post("/logosdb/remove_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json").content("{\"name\": \"anthropic/claude\", \"team_id\": 2001}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.request_count").value(0))
           .andExpect(jsonPath("$.has_voted").value(false));

        mvc.perform(post("/logosdb/get_model_requests")
                .with(TestJwt.testUser())
                .contentType("application/json").content("{}"))
           .andExpect(jsonPath("$.length()").value(0));

        // Re-vote: the model is requestable again and counts one.
        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json").content("{\"name\": \"anthropic/claude\", \"team_id\": 2001}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.request_count").value(1));
    }

    @Test
    void list_reportsHasVotedPerVoter() throws Exception {
        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json").content("{\"name\": \"meta-llama/llama-3\", \"team_id\": 2001}"))
           .andExpect(status().isOk());

        // The voter sees has_voted=true ...
        mvc.perform(post("/logosdb/get_model_requests")
                .with(TestJwt.testUser())
                .contentType("application/json").content("{}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.length()").value(1))
           .andExpect(jsonPath("$[0].name").value("meta-llama/llama-3"))
           .andExpect(jsonPath("$[0].request_count").value(1))
           .andExpect(jsonPath("$[0].has_voted").value(true));

        // ... a user who did not vote sees has_voted=false for the same model.
        mvc.perform(post("/logosdb/get_model_requests")
                .with(TestJwt.adminUser())
                .contentType("application/json").content("{}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.length()").value(1))
           .andExpect(jsonPath("$[0].name").value("meta-llama/llama-3"))
           .andExpect(jsonPath("$[0].request_count").value(1))
           .andExpect(jsonPath("$[0].has_voted").value(false));
    }

    @Test
    void list_excludesModelsThatBecameServed() throws Exception {
        // First request "openai/gpt-5" (not yet served) ...
        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json").content("{\"name\": \"openai/gpt-5\", \"team_id\": 2001}"))
           .andExpect(status().isOk());
        mvc.perform(post("/logosdb/get_model_requests")
                .with(TestJwt.testUser())
                .contentType("application/json").content("{}"))
           .andExpect(jsonPath("$.length()").value(1));

        // ... then an admin adds it to the models table (simulated directly).
        jdbc.update("INSERT INTO models (id, name) VALUES (5099, 'openai/gpt-5')");
        try {
            // It is served now, so it drops out of the requests list.
            mvc.perform(post("/logosdb/get_model_requests")
                    .with(TestJwt.testUser())
                    .contentType("application/json").content("{}"))
               .andExpect(status().isOk())
               .andExpect(jsonPath("$.length()").value(0));
        } finally {
            jdbc.update("DELETE FROM models WHERE id = 5099");
        }
    }

    @Test
    void list_ordersByVoteCountDescending() throws Exception {
        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json").content("{\"name\": \"acme/alpha\", \"team_id\": 2001}"))
           .andExpect(status().isOk());
        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.adminUser())
                .contentType("application/json").content("{\"name\": \"acme/alpha\", \"team_id\": 2001}"))
           .andExpect(status().isOk());
        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json").content("{\"name\": \"acme/beta\", \"team_id\": 2001}"))
           .andExpect(status().isOk());

        mvc.perform(post("/logosdb/get_model_requests")
                .with(TestJwt.testUser())
                .contentType("application/json").content("{}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.length()").value(2))
           // "acme/alpha" (2 votes) ranks above "acme/beta" (1 vote).
           .andExpect(jsonPath("$[0].name").value("acme/alpha"))
           .andExpect(jsonPath("$[0].request_count").value(2))
           .andExpect(jsonPath("$[1].name").value("acme/beta"))
           .andExpect(jsonPath("$[1].request_count").value(1));
    }

    @Test
    void vote_blankNameIsBadRequest() throws Exception {
        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"name\": \"   \"}"))
           .andExpect(status().isBadRequest());

        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{}"))
           .andExpect(status().isBadRequest());
    }

    @Test
    void recheck_recordsOnlyDecisiveGatingVerdictsOnUnverifiedRows() {
        // Rows that predate the precheck (no verdict yet) plus one row that
        // already carries one.
        jdbc.update("INSERT INTO requested_models (name) VALUES ('acme/legacy-a'), ('acme/legacy-b'), ('acme/legacy-c'), ('acme/legacy-d')");
        jdbc.update("INSERT INTO requested_models (name, is_gated) VALUES ('acme/legacy-e', true)");

        Mockito.when(hfReachabilityClient.check("acme/legacy-a"))
            .thenReturn(new HfReachabilityResult(HfReachabilityResult.STATUS_REACHABLE, null, null));
        Mockito.when(hfReachabilityClient.check("acme/legacy-b"))
            .thenReturn(new HfReachabilityResult(HfReachabilityResult.STATUS_REJECTED, HfReachabilityResult.REASON_MODEL_GATED, null));
        Mockito.when(hfReachabilityClient.check("acme/legacy-c"))
            .thenReturn(new HfReachabilityResult(HfReachabilityResult.STATUS_REJECTED, "model-not-found-or-unauthorized", null));
        // A timeout is no verdict: the row must stay empty.
        Mockito.when(hfReachabilityClient.check("acme/legacy-d"))
            .thenReturn(HfReachabilityResult.unknown("Hugging Face Hub did not answer within 15s."));

        hfStatusRechecker.recheckUnverified();

        // Reachable and non-gated rejections are not gated ...
        assertThat(isGated("acme/legacy-a")).isFalse();
        assertThat(isGated("acme/legacy-c")).isFalse();
        // ... a model-gated rejection is gated ...
        assertThat(isGated("acme/legacy-b")).isTrue();
        // ... a timeout leaves no verdict ...
        assertThat(isGated("acme/legacy-d")).isNull();
        // ... and an existing verdict is never clobbered by the sweep.
        assertThat(isGated("acme/legacy-e")).isTrue();
    }

    @Test
    void recheck_unflagsAGatedModelWhoseLicenseWasAccepted() {
        // One pass does both jobs: the flagged row's license was accepted
        // since it was flagged, and the unverified row gets its verdict.
        jdbc.update("INSERT INTO requested_models (name, is_gated) VALUES ('acme/license-accepted', true)");
        jdbc.update("INSERT INTO requested_models (name) VALUES ('acme/unverified')");

        Mockito.when(hfReachabilityClient.check("acme/license-accepted"))
            .thenReturn(new HfReachabilityResult(HfReachabilityResult.STATUS_REACHABLE, null, null));
        Mockito.when(hfReachabilityClient.check("acme/unverified"))
            .thenReturn(new HfReachabilityResult(HfReachabilityResult.STATUS_REACHABLE, null, null));

        hfStatusRechecker.recheck();

        assertThat(isGated("acme/license-accepted")).isFalse();
        assertThat(isGated("acme/unverified")).isFalse();
    }

    // ── License fetch and acceptance ─────────────────────────────────────────

    @Test
    void vote_fetchesTheLicenseOfANonGatedModel() throws Exception {
        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"name\": \"openai/gpt-5\", \"team_id\": 2001}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.license_status").value("available"))
           .andExpect(jsonPath("$.license_approved").value(false));

        // The license is stored on the registry row.
        assertThat(jdbc.queryForObject(
            "SELECT license_id FROM requested_models WHERE name = 'openai/gpt-5'", String.class))
           .isEqualTo("apache-2.0");
        assertThat(jdbc.queryForObject(
            "SELECT license_text FROM requested_models WHERE name = 'openai/gpt-5'", String.class))
           .contains("Apache License 2.0");
        assertThat(jdbc.queryForObject(
            "SELECT license_fetched_date IS NOT NULL FROM requested_models WHERE name = 'openai/gpt-5'",
            Boolean.class)).isTrue();
    }

    @Test
    void vote_storesALicenseExpressionLongerThanSixtyFourCharacters() throws Exception {
        // The Hub's card can declare OR-joined expressions of any length; the
        // column must store the expression intact, not truncate or reject it.
        String expression = "mit OR apache-2.0 OR bsl-1.0 OR ";
        while (expression.length() < 80) {
            expression += "mit OR ";
        }
        Mockito.when(hfLicenseClient.fetch("openai/gpt-5"))
            .thenReturn(new HfLicenseResult(expression, null, null, null, null, null));

        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"name\": \"openai/gpt-5\", \"team_id\": 2001}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.license_status").value("available"));

        assertThat(jdbc.queryForObject(
            "SELECT license_id FROM requested_models WHERE name = 'openai/gpt-5'", String.class))
           .isEqualTo(expression);
    }

    @Test
    void vote_leavesTheLicensePendingForAGatedModel() throws Exception {
        // The token cannot read a gated repository yet, so there is nothing to
        // fetch: the row stays pending until it unflags.
        Mockito.when(hfReachabilityClient.check("meta-llama/llama-3.1-8b-instruct"))
            .thenReturn(new HfReachabilityResult(
                HfReachabilityResult.STATUS_REJECTED, HfReachabilityResult.REASON_MODEL_GATED,
                "Repository access requires an authorized HF_TOKEN."));

        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"name\": \"meta-llama/llama-3.1-8b-instruct\", \"team_id\": 2001}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.license_status").value("pending"));

        Mockito.verify(hfLicenseClient, Mockito.never()).fetch(ArgumentMatchers.anyString());
        assertThat(jdbc.queryForObject(
            "SELECT license_fetched_date IS NULL FROM requested_models WHERE name = 'meta-llama/llama-3.1-8b-instruct'",
            Boolean.class)).isTrue();
    }

    @Test
    void vote_succeedsWhenTheLicenseFetchFails() throws Exception {
        // A failed license fetch is a problem for the next recheck, not for
        // the vote.
        Mockito.when(hfLicenseClient.fetch("openai/gpt-5"))
            .thenReturn(HfLicenseResult.failure("The license could not be fetched right now."));

        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"name\": \"openai/gpt-5\", \"team_id\": 2001}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.request_count").value(1))
           .andExpect(jsonPath("$.license_status").value("pending"));
    }

    @Test
    void unflaggingFetchesTheLicenseForTheFirstTime() {
        // The token can read the repository once the license was accepted, so
        // the unflag and the first license fetch happen together.
        jdbc.update("INSERT INTO requested_models (name, is_gated) VALUES ('acme/unlocked', true)");

        hfStatusRechecker.recheck();

        assertThat(isGated("acme/unlocked")).isFalse();
        assertThat(jdbc.queryForObject(
            "SELECT license_fetched_date IS NOT NULL FROM requested_models WHERE name = 'acme/unlocked'",
            Boolean.class)).isTrue();
        Mockito.verify(hfLicenseClient).fetch("acme/unlocked");
    }

    @Test
    void recheck_unflaggingRecordsTheGlobalAcceptanceWithoutAUser() throws Exception {
        // The moment the flag clears, the acceptance has happened on Hugging
        // Face — the recheck records it, and the list reports the row as
        // approved (the tick in the web application), so the Logos admin does
        // not accept the same agreement a second time.
        jdbc.update("INSERT INTO requested_models (name, is_gated) VALUES ('acme/auto-accepted', true)");

        hfStatusRechecker.recheck();

        int id = jdbc.queryForObject(
            "SELECT id FROM requested_models WHERE name = 'acme/auto-accepted'", Integer.class);
        assertThat(isGated("acme/auto-accepted")).isFalse();
        // The system-recorded row: no team, no user, the database's timestamp.
        assertThat(jdbc.queryForObject(
            "SELECT COUNT(*) FROM model_license_approvals WHERE requested_model_id = ?",
            Integer.class, id)).isEqualTo(1);
        assertThat(jdbc.queryForObject(
            "SELECT user_id IS NULL FROM model_license_approvals WHERE requested_model_id = ? AND team_id IS NULL",
            Boolean.class, id)).isTrue();
        assertThat(jdbc.queryForObject(
            "SELECT approved_at IS NOT NULL FROM model_license_approvals WHERE requested_model_id = ?",
            Boolean.class, id)).isTrue();

        mvc.perform(post("/logosdb/get_model_requests")
                .with(TestJwt.testUser())
                .contentType("application/json").content("{}"))
           .andExpect(jsonPath("$[0].license_approved").value(true));
    }

    @Test
    void recheck_unflaggingKeepsAnEarlierAcceptance() {
        // A named acceptance predates the unflag: the recheck must not
        // replace it with the user-less row — the agreement is accepted
        // either way, and the named user stays the audit record.
        jdbc.update("INSERT INTO requested_models (name, is_gated) VALUES ('acme/early-accepted', true)");
        int id = jdbc.queryForObject(
            "SELECT id FROM requested_models WHERE name = 'acme/early-accepted'", Integer.class);
        jdbc.update("INSERT INTO model_license_approvals (requested_model_id, team_id, user_id, approved_at) "
            + "VALUES (?, NULL, 1003, now())", id);

        hfStatusRechecker.recheck();

        assertThat(jdbc.queryForObject(
            "SELECT COUNT(*) FROM model_license_approvals WHERE requested_model_id = ?",
            Integer.class, id)).isEqualTo(1);
        assertThat(jdbc.queryForObject(
            "SELECT user_id FROM model_license_approvals WHERE requested_model_id = ? AND team_id IS NULL",
            Integer.class, id)).isEqualTo(1003);
    }

    @Test
    void vote_clearingTheFlagRecordsTheGlobalAcceptance() throws Exception {
        // The vote's fresh precheck finds the repository reachable: access
        // was granted on Hugging Face, which is the license acceptance. The
        // vote moves the row out of the recheck's flagged set, so it must
        // record the global acceptance itself — otherwise the row would
        // never get one and the tick would stay red.
        jdbc.update("INSERT INTO requested_models (name, is_gated) VALUES ('acme/vote-unflag', true)");

        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"name\": \"acme/vote-unflag\", \"team_id\": 2001}"))
           .andExpect(status().isOk());

        int id = jdbc.queryForObject(
            "SELECT id FROM requested_models WHERE name = 'acme/vote-unflag'", Integer.class);
        assertThat(isGated("acme/vote-unflag")).isFalse();
        // The system-recorded row: no team, no user, the database's timestamp.
        assertThat(jdbc.queryForObject(
            "SELECT COUNT(*) FROM model_license_approvals WHERE requested_model_id = ?",
            Integer.class, id)).isEqualTo(1);
        assertThat(jdbc.queryForObject(
            "SELECT user_id IS NULL FROM model_license_approvals WHERE requested_model_id = ? AND team_id IS NULL",
            Boolean.class, id)).isTrue();
        assertThat(jdbc.queryForObject(
            "SELECT approved_at IS NOT NULL FROM model_license_approvals WHERE requested_model_id = ?",
            Boolean.class, id)).isTrue();
        // The license the precheck fetched is stored with the vote.
        assertThat(jdbc.queryForObject(
            "SELECT license_fetched_date IS NOT NULL FROM requested_models WHERE id = ?",
            Boolean.class, id)).isTrue();

        mvc.perform(post("/logosdb/get_model_requests")
                .with(TestJwt.testUser())
                .contentType("application/json").content("{}"))
           .andExpect(jsonPath("$[0].license_approved").value(true));
    }

    @Test
    void recheck_concurrentCallersReuseTheInFlightPass() throws Exception {
        // The hourly schedule and a manual refresh enter the same
        // coordinator: the second caller must reuse the in-flight pass, not
        // run a second one alongside it — so while the first pass is still
        // blocked on the Hub, the second caller may not ask the Hub itself.
        jdbc.update("INSERT INTO requested_models (name, is_gated) VALUES ('acme/slow-recheck', true)");
        AtomicInteger checkCalls = new AtomicInteger();
        CountDownLatch firstCheckEntered = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        Mockito.when(hfReachabilityClient.check("acme/slow-recheck"))
            .thenAnswer(invocation -> {
                checkCalls.incrementAndGet();
                firstCheckEntered.countDown();
                release.await(10, TimeUnit.SECONDS);
                return new HfReachabilityResult(HfReachabilityResult.STATUS_REACHABLE, null, null);
            });

        ExecutorService executor = Executors.newFixedThreadPool(2);
        try {
            Future<Void> firstPass = executor.submit(() -> {
                hfStatusRechecker.recheck();
                return null;
            });
            // The first pass is in flight, blocked on the Hub answer.
            assertThat(firstCheckEntered.await(10, TimeUnit.SECONDS)).isTrue();
            // A second caller (the manual refresh) arrives during the pass.
            Future<Void> secondPass = executor.submit(() -> {
                hfStatusRechecker.recheck();
                return null;
            });
            release.countDown();
            // Both callers come back — the second one only after the pass
            // it reused has finished.
            firstPass.get(15, TimeUnit.SECONDS);
            secondPass.get(15, TimeUnit.SECONDS);
            // Exactly one pass ran: the flagged model's repository was asked
            // once, not once per caller.
            assertThat(checkCalls.get()).isEqualTo(1);
            // The pass is fully done, so the slot must be free again: the
            // next caller starts its own pass. If a reusing caller had left
            // a never-completed future in the slot, this call would hang —
            // the timeout turns that hang into a fast test failure.
            Future<Void> nextPass = executor.submit(() -> {
                hfStatusRechecker.recheck();
                return null;
            });
            nextPass.get(10, TimeUnit.SECONDS);
        } finally {
            executor.shutdownNow();
        }
    }

    @Test
    void recheckFetchBackfillsReadableRowsWithoutALicense() {
        jdbc.update("INSERT INTO requested_models (name, is_gated) VALUES ('acme/backfill', false)");
        jdbc.update("INSERT INTO requested_models (name, is_gated) VALUES ('acme/still-gated', true)");
        // Keep the second row gated against the default "reachable" stub, so
        // it stays unreadable and must not be fetched.
        Mockito.when(hfReachabilityClient.check("acme/still-gated"))
            .thenReturn(new HfReachabilityResult(
                HfReachabilityResult.STATUS_REJECTED, HfReachabilityResult.REASON_MODEL_GATED,
                "Repository access requires an authorized HF_TOKEN."));

        hfStatusRechecker.recheck();

        assertThat(jdbc.queryForObject(
            "SELECT license_fetched_date IS NOT NULL FROM requested_models WHERE name = 'acme/backfill'",
            Boolean.class)).isTrue();
        // The gated row is not readable, so it is not fetched.
        assertThat(jdbc.queryForObject(
            "SELECT license_fetched_date IS NULL FROM requested_models WHERE name = 'acme/still-gated'",
            Boolean.class)).isTrue();
    }

    @Test
    void vote_storesTheUsePolicyFileWithTheLicense() throws Exception {
        // The use policy comes back in the same fetch as the license and is
        // stored with it — the team's app admin accepts it together with the
        // license agreement.
        mvc.perform(post("/logosdb/add_model_request")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"name\": \"openai/gpt-5\", \"team_id\": 2001}"))
           .andExpect(status().isOk());

        assertThat(jdbc.queryForObject(
            "SELECT use_policy_file FROM requested_models WHERE name = 'openai/gpt-5'", String.class))
           .isEqualTo("USE_POLICY.md");
        assertThat(jdbc.queryForObject(
            "SELECT use_policy_text FROM requested_models WHERE name = 'openai/gpt-5'", String.class))
           .contains("Use policy of");
        assertThat(jdbc.queryForObject(
            "SELECT use_policy_fetched_date IS NOT NULL FROM requested_models WHERE name = 'openai/gpt-5'",
            Boolean.class)).isTrue();
    }

    @Test
    void getLicense_reportsTheStoredUsePolicy() throws Exception {
        int id = seedModelWithLicense("acme/policy", "apache-2.0");
        jdbc.update("UPDATE requested_models SET use_policy_file = 'USE_POLICY.md', "
            + "use_policy_text = 'Use policy of acme/policy', use_policy_fetched_date = now() WHERE id = ?", id);

        mvc.perform(post("/logosdb/get_model_license")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"model_id\": " + id + "}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.license_id").value("apache-2.0"))
           .andExpect(jsonPath("$.use_policy_file").value("USE_POLICY.md"))
           .andExpect(jsonPath("$.use_policy_text").value("Use policy of acme/policy"));
    }

    @Test
    void getLicense_reportsNoUsePolicyWhenNoneWasStored() throws Exception {
        // A repository without a use policy file stores nothing for it — the
        // dialog shows the license only, which the null fields express.
        int id = seedModelWithLicense("acme/nopolicy", "apache-2.0");

        mvc.perform(post("/logosdb/get_model_license")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"model_id\": " + id + "}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.license_id").value("apache-2.0"))
           .andExpect(jsonPath("$.use_policy_file", org.hamcrest.Matchers.nullValue()))
           .andExpect(jsonPath("$.use_policy_text", org.hamcrest.Matchers.nullValue()));
    }

    @Test
    void getLicense_reportsTheStoredLicense() throws Exception {
        int id = seedModelWithLicense("acme/licensed", "bllm");

        mvc.perform(post("/logosdb/get_model_license")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"model_id\": " + id + "}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.status").value("available"))
           .andExpect(jsonPath("$.license_id").value("bllm"))
           .andExpect(jsonPath("$.license_text").value("License text of acme/licensed"))
           .andExpect(jsonPath("$.approved").value(false));
    }

    @Test
    void getLicense_reportsPendingWhileNoFetchHasRun() throws Exception {
        int id = voteFor("acme/pending", TestJwt.testUser());
        jdbc.update("UPDATE requested_models SET license_id = NULL, license_text = NULL, "
            + "license_fetched_date = NULL WHERE id = ?", id);

        mvc.perform(post("/logosdb/get_model_license")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"model_id\": " + id + "}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.status").value("pending"))
           .andExpect(jsonPath("$.license_id", org.hamcrest.Matchers.nullValue()))
           .andExpect(jsonPath("$.license_text", org.hamcrest.Matchers.nullValue()));
    }

    @Test
    void approveLicense_recordsTheLogosAdminAcceptance() throws Exception {
        int id = seedModelWithLicense("acme/licensed", "apache-2.0");

        mvc.perform(post("/logosdb/approve_model_license")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"model_id\": " + id + "}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.approved").value(true))
           .andExpect(jsonPath("$.approved_at").isNotEmpty());

        // The global row: no team, the accepting Logos admin, a timestamp.
        assertThat(jdbc.queryForObject(
            "SELECT user_id FROM model_license_approvals WHERE requested_model_id = ? AND team_id IS NULL",
            Integer.class, id)).isEqualTo(1003);
        assertThat(jdbc.queryForObject(
            "SELECT approved_at IS NOT NULL FROM model_license_approvals WHERE requested_model_id = ?",
            Boolean.class, id)).isTrue();
    }

    @Test
    void approveLicense_isRefusedWithoutALicense() throws Exception {
        int id = seedModelWithLicense("acme/unlicensed", null);

        mvc.perform(post("/logosdb/approve_model_license")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"model_id\": " + id + "}"))
           .andExpect(status().isBadRequest());
    }

    @Test
    void approveLicense_isRefusedForANonAdmin() throws Exception {
        int id = seedModelWithLicense("acme/licensed", "apache-2.0");

        mvc.perform(post("/logosdb/approve_model_license")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{\"model_id\": " + id + "}"))
           .andExpect(status().isForbidden());
    }

    @Test
    void revokeLicense_clearsTheGlobalAcceptanceAndTheTeamRows() throws Exception {
        int id = seedModelWithLicense("acme/licensed", "apache-2.0");
        mvc.perform(post("/logosdb/approve_model_license")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"model_id\": " + id + "}"))
           .andExpect(status().isOk());
        // The team-level acceptance that rests on the global one.
        jdbc.update("INSERT INTO model_license_approvals (requested_model_id, team_id, user_id, approved_at) "
            + "VALUES (?, 2002, 1002, now())", id);

        mvc.perform(post("/logosdb/revoke_model_license")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"model_id\": " + id + "}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.approved").value(false));

        assertThat(jdbc.queryForObject(
            "SELECT COUNT(*) FROM model_license_approvals WHERE requested_model_id = ?", Integer.class, id))
           .isZero();
    }

    @Test
    void revokeLicense_isIdempotent() throws Exception {
        int id = seedModelWithLicense("acme/licensed", "apache-2.0");

        mvc.perform(post("/logosdb/revoke_model_license")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"model_id\": " + id + "}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.approved").value(false));
    }

    @Test
    void list_reportsTheLicenseState() throws Exception {
        voteFor("openai/gpt-5", TestJwt.testUser());

        mvc.perform(post("/logosdb/get_model_requests")
                .with(TestJwt.testUser())
                .contentType("application/json")
                .content("{}"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$[0].name").value("openai/gpt-5"))
           .andExpect(jsonPath("$[0].license_status").value("available"))
           .andExpect(jsonPath("$[0].license_approved").value(false));
    }

    private int seedModelWithLicense(String name, String licenseId) {
        jdbc.update("INSERT INTO requested_models (name, is_gated, license_id, license_text, license_fetched_date) "
            + "VALUES (?, false, ?, ?, now())",
            name, licenseId, licenseId == null ? null : "License text of " + name);
        return jdbc.queryForObject("SELECT id FROM requested_models WHERE name = ?", Integer.class, name);
    }

    private Boolean isGated(String name) {
        return jdbc.queryForObject("SELECT is_gated FROM requested_models WHERE name = ?",
            Boolean.class, name);
    }

    @Test
    void addModelRequest_withoutTokenIsUnauthorized() throws Exception {
        mvc.perform(post("/logosdb/add_model_request")
                .contentType("application/json")
                .content("{\"name\": \"openai/gpt-5\", \"team_id\": 2001}"))
           .andExpect(status().isUnauthorized());
    }
    @Test
    void modelRequestsRejectControlsBeforeNormalization() throws Exception {
        for (char control : new char[] {0, '\r', '\n', '\t', 27, 127, 133, 159}) {
            for (String name : java.util.List.of(control + "org/model", "org/model" + control)) {
                mvc.perform(post("/logosdb/add_model_request")
                        .with(TestJwt.testUser())
                        .contentType("application/json")
                        .content(new com.fasterxml.jackson.databind.ObjectMapper().writeValueAsString(
                            java.util.Map.of("name", name, "team_id", 2001))))
                   .andExpect(status().isBadRequest());
            }
        }
    }

}
