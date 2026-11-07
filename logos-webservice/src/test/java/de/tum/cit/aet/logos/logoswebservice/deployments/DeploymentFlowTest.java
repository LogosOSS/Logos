package de.tum.cit.aet.logos.logoswebservice.deployments;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

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

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import de.tum.cit.aet.logos.logoswebservice.TestContainersConfig;
import de.tum.cit.aet.logos.logoswebservice.TestJwt;
import de.tum.cit.aet.logos.logoswebservice.deployments.service.DeploymentReportService;

/**
 * The whole way through the webservice: an admin makes deployers, starts a plan, the deployers ask for orders and report,
 * and the plan moves on. The database is a real PostgreSQL, so the changelog and the mapping of the entities are checked
 * as well.
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
    "logos.deployments.environment=Prod",
    "logos.deployments.orderable-environments=Prod,Dev,Test",
})
@Sql(scripts = "/sql/seed-identity.sql", executionPhase = Sql.ExecutionPhase.BEFORE_TEST_METHOD)
@Sql(statements = {
    "DELETE FROM deployment_steps",
    "DELETE FROM deployment_plans",
    "DELETE FROM deployers",
    "DELETE FROM notifications WHERE type IN ('deployment', 'deployment_failed')",
}, executionPhase = Sql.ExecutionPhase.AFTER_TEST_METHOD)
@Sql(scripts = "/sql/cleanup-identity.sql", executionPhase = Sql.ExecutionPhase.AFTER_TEST_METHOD)
class DeploymentFlowTest {

    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;
    @Autowired ObjectMapper json;
    @Autowired DeploymentReportService reports;
    @MockitoBean JwtDecoder jwtDecoder;

    // ------------------------------------------------------------------ helpers

    private JsonNode body(ResultActions result) throws Exception {
        return json.readTree(result.andReturn().getResponse().getContentAsString());
    }

    private JsonNode createDeployer(String name, String environment, String kind) throws Exception {
        return body(mvc.perform(post("/deployments/deployers")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content(json.writeValueAsString(Map.of("name", name, "environment", environment, "kind", kind))))
            .andExpect(status().isCreated()));
    }

    private ResultActions rawPoll(String key) throws Exception {
        return mvc.perform(post("/deployments/deployer/poll")
            .header("Authorization", "Bearer " + key)
            .contentType("application/json")
            .content("{\"wait_s\":0,\"kind\":\"core\",\"version\":\"0.1.0\"}"));
    }

    private JsonNode poll(String key) throws Exception {
        return body(rawPoll(key).andExpect(status().isOk()));
    }

    private Map<String, Object> snapshot(String id, String tag, String phase, String outcome, String message) {
        Map<String, Object> item = new LinkedHashMap<>();
        item.put("id", id);
        item.put("tag", tag);
        item.put("phase", phase);
        item.put("outcome", outcome);
        item.put("message", message);
        item.put("requested_by", "Test User");
        item.put("started_at", "2026-11-07T10:00:00Z");
        item.put("finished_at", outcome == null ? null : "2026-11-07T10:03:00Z");
        item.put("events", List.of(Map.of("at", "2026-11-07T10:00:01Z", "phase", phase, "note", "a note")));
        return item;
    }

    private JsonNode report(String key, Map<String, Object>... items) throws Exception {
        return body(mvc.perform(post("/deployments/deployer/report")
                .header("Authorization", "Bearer " + key)
                .contentType("application/json")
                .content(json.writeValueAsString(Map.of("kind", "core", "version", "0.1.0", "deployments", List.of(items)))))
            .andExpect(status().isOk()));
    }

    private ResultActions rawStart(String environment, String tag, String confirmation) throws Exception {
        Map<String, Object> request = new LinkedHashMap<>();
        request.put("environment", environment);
        request.put("tag", tag);
        request.put("confirmation", confirmation);
        return mvc.perform(post("/deployments/plans")
            .with(TestJwt.logosAdmin())
            .contentType("application/json")
            .content(json.writeValueAsString(request)));
    }

    private JsonNode plan(String id) throws Exception {
        return body(mvc.perform(get("/deployments/plans/" + id).with(TestJwt.logosAdmin())).andExpect(status().isOk()));
    }

    private JsonNode step(JsonNode plan, String deployerName) {
        for (JsonNode step : plan.get("step_details")) {
            if (deployerName.equals(step.get("deployer_name").asText())) {
                return step;
            }
        }
        throw new AssertionError("no step for " + deployerName + " in " + plan);
    }

    /** A Dev environment with an online core deployer and one worker. Returns {coreKey, workerKey}. */
    private String[] devEnvironment() throws Exception {
        String coreKey = createDeployer("dev-core", "Dev", "core").get("key").asText();
        String workerKey = createDeployer("dev-worker", "Dev", "worker").get("key").asText();
        poll(coreKey);
        poll(workerKey);
        return new String[] {coreKey, workerKey};
    }

    // ------------------------------------------------------------------ deployers

    @Test
    void aDeployerKeyIsShownOnceAndOnlyItsHashIsStored() throws Exception {
        JsonNode created = createDeployer("dev-core", "Dev", "core");
        String key = created.get("key").asText();

        assertThat(key).hasSize(64);
        String stored = jdbc.queryForObject("SELECT key_hash FROM deployers", String.class);
        assertThat(stored).hasSize(64).isNotEqualTo(key);

        String listing = mvc.perform(get("/deployments/deployers").with(TestJwt.logosAdmin()))
            .andExpect(status().isOk())
            .andReturn().getResponse().getContentAsString();
        assertThat(listing).contains("dev-core").doesNotContain(key).doesNotContain(stored);
    }

    @Test
    void onlyLogosAdminsUseThePage() throws Exception {
        mvc.perform(get("/deployments/overview").with(TestJwt.adminUser())).andExpect(status().isForbidden());
        mvc.perform(get("/deployments/plans").with(TestJwt.testUser())).andExpect(status().isForbidden());
    }

    @Test
    void aDeployerNeedsAKnownKeyAndAKnownEnvironment() throws Exception {
        rawPoll("not-a-key").andExpect(status().isUnauthorized());
        mvc.perform(post("/deployments/deployer/poll")).andExpect(status().isUnauthorized());
        mvc.perform(post("/deployments/deployers")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"name\":\"x\",\"environment\":\"Staging\",\"kind\":\"core\"}"))
            .andExpect(status().isBadRequest());
        mvc.perform(post("/deployments/deployers")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"name\":\"x\",\"environment\":\"Dev\",\"kind\":\"banana\"}"))
            .andExpect(status().isBadRequest());
    }

    @Test
    void aNameIsUsedOnceForAnEnvironment() throws Exception {
        createDeployer("dev-core", "Dev", "core");

        mvc.perform(post("/deployments/deployers")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"name\":\"DEV-CORE\",\"environment\":\"dev\",\"kind\":\"core\"}"))
            .andExpect(status().isConflict());
    }

    @Test
    void aNewKeyReplacesTheOldOneAndARemovedDeployerLosesItsKey() throws Exception {
        JsonNode created = createDeployer("dev-core", "Dev", "core");
        String oldKey = created.get("key").asText();
        String id = created.get("deployer").get("id").asText();
        poll(oldKey);

        String newKey = body(mvc.perform(post("/deployments/deployers/" + id + "/key").with(TestJwt.logosAdmin()))
            .andExpect(status().isOk())).get("key").asText();

        rawPoll(oldKey).andExpect(status().isUnauthorized());
        poll(newKey);
        mvc.perform(delete("/deployments/deployers/" + id).with(TestJwt.logosAdmin())).andExpect(status().isNoContent());
        rawPoll(newKey).andExpect(status().isUnauthorized());
    }

    // ------------------------------------------------------------------ starting

    @Test
    void theOverviewSaysWhyAnEnvironmentCannotStartYet() throws Exception {
        mvc.perform(get("/deployments/overview").with(TestJwt.logosAdmin()))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.enabled").value(true))
            .andExpect(jsonPath("$.environment").value("Prod"))
            .andExpect(jsonPath("$.targets[0].name").value("Prod"))
            .andExpect(jsonPath("$.targets[0].own").value(true))
            .andExpect(jsonPath("$.targets[0].requires_confirmation").value(true))
            .andExpect(jsonPath("$.targets[1].name").value("Dev"))
            .andExpect(jsonPath("$.targets[1].can_start").value(false))
            .andExpect(jsonPath("$.targets[1].blocked_reason").value("No deployer is set up for Dev yet."));
    }

    @Test
    void anOfflineCoreDeployerBlocksTheStart() throws Exception {
        createDeployer("dev-core", "Dev", "core");

        rawStart("Dev", "pr-93", null)
            .andExpect(status().isConflict())
            .andExpect(jsonPath("$.detail").value("The core deployer dev-core has not connected yet."));
    }

    @Test
    void aPlanIsTheCoreFirstAndThenTheWorkers() throws Exception {
        String[] keys = devEnvironment();

        JsonNode plan = body(rawStart("Dev", "pr-93", null).andExpect(status().isCreated()));

        assertThat(plan.get("status").asText()).isEqualTo("running");
        assertThat(plan.get("origin").asText()).isEqualTo("local");
        assertThat(plan.get("initiator").asText()).isNotBlank();
        assertThat(step(plan, "dev-core").get("status").asText()).isEqualTo("ordered");
        assertThat(step(plan, "dev-worker").get("status").asText()).isEqualTo("waiting");

        // The worker has nothing to do yet. The core has one order: the id of the step, the tag and the plan.
        assertThat(poll(keys[1]).get("orders")).isEmpty();
        JsonNode orders = poll(keys[0]).get("orders");
        assertThat(orders).hasSize(1);
        assertThat(orders.get(0).get("id").asText()).isEqualTo(step(plan, "dev-core").get("id").asText());
        assertThat(orders.get(0).get("tag").asText()).isEqualTo("pr-93");
        assertThat(orders.get(0).get("plan").get("environment").asText()).isEqualTo("Dev");
        assertThat(orders.get(0).get("plan").get("kind").asText()).isEqualTo("core");
        // The order is handed out again until the deployer reports on it.
        assertThat(poll(keys[0]).get("orders")).hasSize(1);
    }

    @Test
    void theWorkersStartWhenTheCoreSucceededAndThePlanEndsWithTheLastStep() throws Exception {
        String[] keys = devEnvironment();
        JsonNode plan = body(rawStart("Dev", "pr-93", null).andExpect(status().isCreated()));
        String planId = plan.get("id").asText();
        String coreId = step(plan, "dev-core").get("id").asText();
        String workerId = step(plan, "dev-worker").get("id").asText();

        JsonNode acked = report(keys[0], snapshot(coreId, "pr-93", "pulling", null, ""));
        assertThat(acked.get("acked")).hasSize(1);
        assertThat(poll(keys[0]).get("orders")).isEmpty();
        assertThat(step(plan(planId), "dev-core").get("status").asText()).isEqualTo("running");
        assertThat(poll(keys[1]).get("orders")).isEmpty();

        report(keys[0], snapshot(coreId, "pr-93", "done", "succeeded", "now running tag pr-93"));

        JsonNode afterCore = plan(planId);
        assertThat(step(afterCore, "dev-core").get("status").asText()).isEqualTo("succeeded");
        assertThat(step(afterCore, "dev-worker").get("status").asText()).isEqualTo("ordered");
        assertThat(afterCore.get("status").asText()).isEqualTo("running");
        assertThat(poll(keys[1]).get("orders").get(0).get("id").asText()).isEqualTo(workerId);

        report(keys[1], snapshot(workerId, "pr-93", "done", "succeeded", "now running pr-93"));

        JsonNode ended = plan(planId);
        assertThat(ended.get("status").asText()).isEqualTo("succeeded");
        assertThat(ended.get("finished_at").isNull()).isFalse();
        assertThat(step(ended, "dev-core").get("events")).hasSize(1);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM notifications WHERE type = 'deployment'", Integer.class))
            .isEqualTo(2);
    }

    @Test
    void aFailedCoreKeepsTheWorkersFromStartingAndSaysWhy() throws Exception {
        String[] keys = devEnvironment();
        JsonNode plan = body(rawStart("Dev", "pr-93", null).andExpect(status().isCreated()));
        String coreId = step(plan, "dev-core").get("id").asText();

        report(keys[0], snapshot(coreId, "pr-93", "done", "failed", "docker compose up failed: port is already allocated"));

        JsonNode ended = plan(plan.get("id").asText());
        assertThat(ended.get("status").asText()).isEqualTo("failed");
        assertThat(step(ended, "dev-worker").get("status").asText()).isEqualTo("cancelled");
        assertThat(ended.get("reason").asText()).isEqualTo("dev-core: docker compose up failed: port is already allocated");
        assertThat(poll(keys[1]).get("orders")).isEmpty();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM notifications WHERE type = 'deployment_failed'", Integer.class))
            .isEqualTo(1);
    }

    @Test
    void aDeploymentHasANumberThatGrows() throws Exception {
        devEnvironment();
        JsonNode first = body(rawStart("Dev", "pr-93", null).andExpect(status().isCreated()));
        mvc.perform(post("/deployments/plans/" + first.get("id").asText() + "/abandon").with(TestJwt.logosAdmin()));
        JsonNode second = body(rawStart("Dev", "pr-94", null).andExpect(status().isCreated()));

        assertThat(second.get("number").asLong()).isEqualTo(first.get("number").asLong() + 1);
        assertThat(plan(first.get("id").asText()).get("number").asLong()).isEqualTo(first.get("number").asLong());
    }

    @Test
    void theOverviewShowsHowTheLatestDeploymentOfEachEnvironmentEnded() throws Exception {
        String[] keys = devEnvironment();
        mvc.perform(get("/deployments/overview").with(TestJwt.logosAdmin()))
            .andExpect(jsonPath("$.targets[1].last_status").doesNotExist());
        JsonNode plan = body(rawStart("Dev", "pr-93", null).andExpect(status().isCreated()));
        mvc.perform(get("/deployments/overview").with(TestJwt.logosAdmin()))
            .andExpect(jsonPath("$.targets[1].last_status").value("running"));

        report(keys[0], snapshot(step(plan, "dev-core").get("id").asText(), "pr-93", "done", "failed", "boom"));

        mvc.perform(get("/deployments/overview").with(TestJwt.logosAdmin()))
            .andExpect(jsonPath("$.targets[1].last_status").value("failed"));
    }

    @Test
    void aSecondDeploymentOfTheSameEnvironmentIsRefusedWhileOneRuns() throws Exception {
        devEnvironment();
        rawStart("Dev", "pr-93", null).andExpect(status().isCreated());

        rawStart("Dev", "pr-94", null)
            .andExpect(status().isConflict())
            .andExpect(jsonPath("$.detail").value("A deployment of Dev is running."));
    }

    @Test
    void anAdminAbandonsAPlanAndCanStartAnother() throws Exception {
        String[] keys = devEnvironment();
        JsonNode plan = body(rawStart("Dev", "pr-93", null).andExpect(status().isCreated()));
        String planId = plan.get("id").asText();

        JsonNode abandoned = body(mvc.perform(post("/deployments/plans/" + planId + "/abandon").with(TestJwt.logosAdmin()))
            .andExpect(status().isOk()));

        assertThat(abandoned.get("status").asText()).isEqualTo("failed");
        assertThat(abandoned.get("abandoned_by").asText()).isNotBlank();
        assertThat(step(abandoned, "dev-core").get("status").asText()).isEqualTo("cancelled");
        assertThat(poll(keys[0]).get("orders")).isEmpty();
        mvc.perform(post("/deployments/plans/" + planId + "/abandon").with(TestJwt.logosAdmin()))
            .andExpect(status().isConflict());
        rawStart("Dev", "pr-94", null).andExpect(status().isCreated());
    }

    @Test
    void aProtectedEnvironmentNeedsItsNameAndAReleaseTag() throws Exception {
        String coreKey = createDeployer("prod-core", "Prod", "core").get("key").asText();
        poll(coreKey);

        rawStart("Prod", "latest", null).andExpect(status().isBadRequest());
        rawStart("Prod", "latest", "dev").andExpect(status().isBadRequest());
        rawStart("Prod", "pr-93", "prod")
            .andExpect(status().isBadRequest())
            .andExpect(jsonPath("$.error").value("Prod accepts only 'latest' and release tags, not 'pr-93'."));
        rawStart("Prod", "latest", "PROD").andExpect(status().isCreated());
    }

    @Test
    void theTagMustBeADockerTag() throws Exception {
        devEnvironment();

        rawStart("Dev", "a b", null).andExpect(status().isBadRequest());
        rawStart("Dev", "previous", null).andExpect(status().isBadRequest());
        rawStart("Staging", "latest", null).andExpect(status().isBadRequest());
    }

    // ------------------------------------------------------------------ silence

    @Test
    void aStepThatStaysSilentEndsAsTimedOutAndTheLateReportShowsTheRealResult() throws Exception {
        String[] keys = devEnvironment();
        JsonNode plan = body(rawStart("Dev", "pr-93", null).andExpect(status().isCreated()));
        String planId = plan.get("id").asText();
        String coreId = step(plan, "dev-core").get("id").asText();
        report(keys[0], snapshot(coreId, "pr-93", "swapping", null, ""));
        jdbc.update("UPDATE deployment_steps SET last_activity_at = now() - interval '2 hours' WHERE id = ?::uuid", coreId);

        int ended = reports.closeSilentSteps();

        assertThat(ended).isEqualTo(1);
        JsonNode timedOut = plan(planId);
        assertThat(timedOut.get("status").asText()).isEqualTo("failed");
        assertThat(step(timedOut, "dev-core").get("failure").asText()).isEqualTo("timed_out");
        assertThat(step(timedOut, "dev-worker").get("status").asText()).isEqualTo("cancelled");
        rawStart("Dev", "pr-94", null).andExpect(status().isCreated());

        // The host did finish after all. The step shows it. The plan never released the workers.
        report(keys[0], snapshot(coreId, "pr-93", "done", "succeeded", "now running tag pr-93"));

        JsonNode corrected = plan(planId);
        assertThat(step(corrected, "dev-core").get("status").asText()).isEqualTo("succeeded");
        assertThat(step(corrected, "dev-worker").get("status").asText()).isEqualTo("cancelled");
        assertThat(corrected.get("status").asText()).isEqualTo("failed");
    }

    @Test
    void anOrderThatNobodyTakesEndsAsTimedOutToo() throws Exception {
        devEnvironment();
        JsonNode plan = body(rawStart("Dev", "pr-93", null).andExpect(status().isCreated()));
        jdbc.update("UPDATE deployment_steps SET last_activity_at = now() - interval '2 hours' WHERE status = 'ordered'");

        reports.closeSilentSteps();

        JsonNode ended = plan(plan.get("id").asText());
        assertThat(step(ended, "dev-core").get("message").asText()).contains("did not take the order");
    }

    @Test
    void aReportAfterALongOutageClosesTheStalePlanBeforeItCanReleaseTheWorkers() throws Exception {
        String[] keys = devEnvironment();
        JsonNode plan = body(rawStart("Dev", "pr-93", null).andExpect(status().isCreated()));
        String coreId = step(plan, "dev-core").get("id").asText();
        jdbc.update("UPDATE deployment_steps SET last_activity_at = now() - interval '2 hours' WHERE id = ?::uuid", coreId);

        report(keys[0], snapshot(coreId, "pr-93", "done", "succeeded", "now running tag pr-93"));

        JsonNode ended = plan(plan.get("id").asText());
        assertThat(step(ended, "dev-worker").get("status").asText()).isEqualTo("cancelled");
        assertThat(poll(keys[1]).get("orders")).isEmpty();
    }

    // ------------------------------------------------------------------ observed plans

    @Test
    void aDeploymentThatAnotherLogosOrderedIsShownAsObserved() throws Exception {
        String devKey = createDeployer("dev-core", "Dev", "core").get("key").asText();
        String planId = "3f2b6c1e-8d4a-4b7e-9c11-0a1b2c3d4e5f";
        String stepId = "0e9d8c7b-6a5f-4e3d-8c2b-1a0f9e8d7c6b";
        Map<String, Object> item = snapshot(stepId, "pr-93", "pulling", null, "");
        item.put("plan", Map.of("id", planId, "environment", "Dev", "position", "0", "kind", "core", "initiator", "Jane Doe"));

        report(devKey, item);

        JsonNode observed = plan(planId);
        assertThat(observed.get("origin").asText()).isEqualTo("observed");
        assertThat(observed.get("environment").asText()).isEqualTo("Dev");
        assertThat(observed.get("initiator").asText()).isEqualTo("Jane Doe");
        assertThat(observed.get("status").asText()).isEqualTo("running");
        // It cannot be abandoned here, and it does not block a deployment of this Logos.
        mvc.perform(post("/deployments/plans/" + planId + "/abandon").with(TestJwt.logosAdmin()))
            .andExpect(status().isConflict());

        item.put("outcome", "succeeded");
        item.put("phase", "done");
        report(devKey, item);
        assertThat(plan(planId).get("status").asText()).isEqualTo("succeeded");
        assertThat(jdbc.queryForObject("SELECT count(*) FROM notifications WHERE type = 'deployment'", Integer.class))
            .isZero();
    }

    @Test
    void aDeployerCannotReportOnAPlanOfAnotherEnvironmentOrOfThisLogos() throws Exception {
        String devKey = createDeployer("dev-core", "Dev", "core").get("key").asText();
        String testKey = createDeployer("test-core", "Test", "core").get("key").asText();
        poll(devKey);
        poll(testKey);
        JsonNode plan = body(rawStart("Dev", "pr-93", null).andExpect(status().isCreated()));
        String devStepId = step(plan, "dev-core").get("id").asText();

        // A deployer of Test names the step of a deployer of Dev.
        report(testKey, snapshot(devStepId, "pr-93", "done", "succeeded", "faked"));

        assertThat(step(plan(plan.get("id").asText()), "dev-core").get("status").asText()).isEqualTo("ordered");

        // A deployer of Test names the plan of Dev for a step of its own.
        Map<String, Object> item = snapshot("11111111-2222-4333-8444-555555555555", "pr-93", "pulling", null, "");
        item.put("plan", Map.of("id", plan.get("id").asText(), "environment", "Dev", "position", "0", "kind", "core"));
        report(testKey, item);

        // The plan still has its one step: a deployer cannot add a step to a plan of this Logos.
        assertThat(plan(plan.get("id").asText()).get("step_details")).hasSize(1);
    }

    @Test
    void theHistoryListsTheLatestPlansOfAnEnvironment() throws Exception {
        devEnvironment();
        JsonNode first = body(rawStart("Dev", "pr-93", null).andExpect(status().isCreated()));
        mvc.perform(post("/deployments/plans/" + first.get("id").asText() + "/abandon").with(TestJwt.logosAdmin()));
        rawStart("Dev", "pr-94", null).andExpect(status().isCreated());

        List<String> tags = new ArrayList<>();
        for (JsonNode plan : body(mvc.perform(get("/deployments/plans").param("environment", "dev").with(TestJwt.logosAdmin()))
                .andExpect(status().isOk()))) {
            tags.add(plan.get("tag").asText());
        }

        assertThat(tags).containsExactly("pr-94", "pr-93");
        mvc.perform(get("/deployments/overview").with(TestJwt.logosAdmin()))
            .andExpect(jsonPath("$.suggested_tags[0]").value("latest"))
            .andExpect(jsonPath("$.suggested_tags[1]").value("pr-94"));
    }
}
