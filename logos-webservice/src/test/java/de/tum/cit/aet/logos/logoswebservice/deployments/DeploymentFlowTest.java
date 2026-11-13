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
import de.tum.cit.aet.logos.logoswebservice.deployments.service.DeployerService;
import de.tum.cit.aet.logos.logoswebservice.deployments.repository.DeployerRepository;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicReference;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * The whole way through the webservice: an admin makes the core deployer, the worker asks for orders with the key of its
 * provider, the admin starts a plan, the deployers ask for orders and report, and the plan moves on. The database is a
 * real PostgreSQL, so the changelog and the mapping of the entities are checked as well. This Logos is not protected
 * (like Dev and Test); {@link DeploymentProtectionTest} covers the protected one.
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
    "logos.deployments.protect=false",
})
@Sql(scripts = "/sql/seed-identity.sql", executionPhase = Sql.ExecutionPhase.BEFORE_TEST_METHOD)
@Sql(statements = {
    "DELETE FROM deployment_steps",
    "DELETE FROM deployment_plans",
    "DELETE FROM deployers",
    "DELETE FROM providers WHERE id BETWEEN 9201 AND 9209",
    "DELETE FROM notifications WHERE type IN ('deployment', 'deployment_failed')",
}, executionPhase = Sql.ExecutionPhase.AFTER_TEST_METHOD)
@Sql(scripts = "/sql/cleanup-identity.sql", executionPhase = Sql.ExecutionPhase.AFTER_TEST_METHOD)
class DeploymentFlowTest {

    /** The logosnode provider of the worker of most tests, and the worker key that its node manager presents. */
    private static final int WORKER_PROVIDER = 9201;
    private static final String WORKER_KEY = "worker-key-of-dev-worker";

    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;
    @Autowired ObjectMapper json;
    @Autowired DeploymentReportService reports;
    @Autowired DeployerService deployerService;
    @Autowired DeployerRepository deployerRepository;
    @Autowired TransactionTemplate transactions;
    @MockitoBean JwtDecoder jwtDecoder;

    // ------------------------------------------------------------------ helpers

    private JsonNode body(ResultActions result) throws Exception {
        return json.readTree(result.andReturn().getResponse().getContentAsString());
    }

    private JsonNode createDeployer(String name, String kind) throws Exception {
        return body(mvc.perform(post("/deployments/deployers")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content(json.writeValueAsString(Map.of("name", name, "kind", kind))))
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

    private Map<String, Object> snapshot(String id, String phase, String outcome, String message) {
        Map<String, Object> item = new LinkedHashMap<>();
        item.put("id", id);
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

    private ResultActions rawStart(String tag) throws Exception {
        return mvc.perform(post("/deployments/plans")
            .with(TestJwt.logosAdmin())
            .contentType("application/json")
            .content(json.writeValueAsString(Map.of("tag", tag))));
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

    /** A provider the way a worker registers: its key is the worker key that the node manager presents as LOGOS_API_KEY. */
    private void provider(int id, String name, String type, String key) {
        jdbc.update("INSERT INTO providers (id, name, base_url, provider_type, auth_name, auth_format, api_key) "
            + "VALUES (?, ?, '', CAST(? AS provider_type_enum), '', '{}', ?)", id, name, type, key);
    }

    /** An online core deployer and one worker. Returns {coreKey, workerKey}. */
    private String[] coreAndWorker() throws Exception {
        String coreKey = createDeployer("dev-core", "core").get("key").asText();
        provider(WORKER_PROVIDER, "dev-worker", "logosnode", WORKER_KEY);
        poll(coreKey);
        poll(WORKER_KEY);
        return new String[] {coreKey, WORKER_KEY};
    }

    // ------------------------------------------------------------------ deployers

    @Test
    void aDeployerKeyIsShownOnceAndOnlyItsHashIsStored() throws Exception {
        JsonNode created = createDeployer("dev-core", "core");
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
    void aDeployerNeedsAKnownKeyAndAKnownKind() throws Exception {
        rawPoll("not-a-key").andExpect(status().isUnauthorized());
        mvc.perform(post("/deployments/deployer/poll")).andExpect(status().isUnauthorized());
        mvc.perform(post("/deployments/deployers")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"name\":\"x\",\"kind\":\"banana\"}"))
            .andExpect(status().isBadRequest());
    }

    @Test
    void aNameIsUsedOnce() throws Exception {
        createDeployer("dev-core", "core");

        mvc.perform(post("/deployments/deployers")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"name\":\"DEV-CORE\",\"kind\":\"core\"}"))
            .andExpect(status().isConflict());
    }

    @Test
    void aNewKeyReplacesTheOldOneAndARemovedDeployerLosesItsKey() throws Exception {
        JsonNode created = createDeployer("dev-core", "core");
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

    @Test
    void aHeartbeatThatStartedBeforeANewKeyDoesNotBringTheOldKeyBack() throws Exception {
        JsonNode created = createDeployer("dev-core", "core");
        String oldKey = created.get("key").asText();
        UUID id = UUID.fromString(created.get("deployer").get("id").asText());
        AtomicReference<String> newKey = new AtomicReference<>();

        transactions.executeWithoutResult(status -> {
            deployerRepository.findById(id).orElseThrow(); // the heartbeat has read the row with the old key
            Thread other = new Thread(() -> newKey.set(deployerService.replaceKey(id).key()));
            other.start();
            try {
                other.join();
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            deployerService.touch(id, "0.2.0");
        });

        rawPoll(oldKey).andExpect(status().isUnauthorized());
        poll(newKey.get());
    }

    // ------------------------------------------------------------------ workers

    @Test
    void aWorkerNeedsNoKeyOfItsOwnAndItsRowIsMadeAtTheFirstContact() throws Exception {
        provider(WORKER_PROVIDER, "dev-worker", "logosnode", WORKER_KEY);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM deployers", Integer.class)).isZero();

        poll(WORKER_KEY);
        poll(WORKER_KEY);

        Map<String, Object> row = jdbc.queryForMap("SELECT name, kind, key_hash, provider_id FROM deployers");
        assertThat(row.get("name")).isEqualTo("dev-worker");
        assertThat(row.get("kind")).isEqualTo("worker");
        assertThat(row.get("key_hash")).isNull();
        assertThat(row.get("provider_id")).isEqualTo(WORKER_PROVIDER);
        mvc.perform(get("/deployments/deployers").with(TestJwt.logosAdmin()))
            .andExpect(jsonPath("$[0].name").value("dev-worker"))
            .andExpect(jsonPath("$[0].kind").value("worker"))
            .andExpect(jsonPath("$[0].online").value(true));
        // Two first contacts at the same moment make one row: the second insert is a no-op.
        Integer again = transactions.execute(status ->
            deployerRepository.insertWorkerIfAbsent(UUID.randomUUID(), "other", WORKER_PROVIDER));
        assertThat(again).isZero();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM deployers", Integer.class)).isEqualTo(1);
    }

    @Test
    void aKeyThatIsNotAWorkerKeyIsRefused() throws Exception {
        provider(9202, "azure", "cloud", "key-of-another-service");
        provider(9203, "twin-a", "logosnode", "key-of-two-providers");
        provider(9204, "twin-b", "logosnode", "key-of-two-providers");

        // The key of a cloud provider belongs to another service. A key of two providers names no worker.
        rawPoll("key-of-another-service").andExpect(status().isUnauthorized());
        rawPoll("key-of-two-providers").andExpect(status().isUnauthorized());
        assertThat(jdbc.queryForObject("SELECT count(*) FROM deployers", Integer.class)).isZero();
    }

    @Test
    void aWorkerIsNotMadeOnThePageAndHasNoKeyToReplace() throws Exception {
        JsonNode refused = body(mvc.perform(post("/deployments/deployers")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"name\":\"deioma\",\"kind\":\"worker\"}"))
            .andExpect(status().isBadRequest()));
        assertThat(refused.get("error").asText()).contains("worker key");
        // A request without a kind makes a core deployer.
        mvc.perform(post("/deployments/deployers")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"name\":\"dev-core\"}"))
            .andExpect(status().isCreated())
            .andExpect(jsonPath("$.deployer.kind").value("core"));

        provider(WORKER_PROVIDER, "dev-worker", "logosnode", WORKER_KEY);
        poll(WORKER_KEY);
        String workerId = jdbc.queryForObject("SELECT id::text FROM deployers WHERE kind = 'worker'", String.class);
        mvc.perform(post("/deployments/deployers/" + workerId + "/key").with(TestJwt.logosAdmin()))
            .andExpect(status().isBadRequest());
        poll(WORKER_KEY);
    }

    @Test
    void aWorkerFollowsTheNameOfItsProvider() throws Exception {
        provider(WORKER_PROVIDER, "deioma", "logosnode", WORKER_KEY);
        poll(WORKER_KEY);

        jdbc.update("UPDATE providers SET name = 'deioma-gpu' WHERE id = ?", WORKER_PROVIDER);
        poll(WORKER_KEY);

        assertThat(jdbc.queryForObject("SELECT name FROM deployers", String.class)).isEqualTo("deioma-gpu");
        assertThat(jdbc.queryForObject("SELECT count(*) FROM deployers", Integer.class)).isEqualTo(1);
    }

    @Test
    void removingAProviderRemovesItsWorkerButNotTheHistory() throws Exception {
        String[] keys = coreAndWorker();
        JsonNode plan = body(rawStart("pr-93").andExpect(status().isCreated()));

        jdbc.update("DELETE FROM providers WHERE id = ?", WORKER_PROVIDER);

        assertThat(jdbc.queryForObject("SELECT count(*) FROM deployers WHERE kind = 'worker'", Integer.class)).isZero();
        rawPoll(keys[1]).andExpect(status().isUnauthorized());
        JsonNode kept = plan(plan.get("id").asText());
        assertThat(step(kept, "dev-worker").get("status").asText()).isEqualTo("waiting");
    }

    // ------------------------------------------------------------------ starting

    @Test
    void workersAloneDoNotMakeADeploymentPossible() throws Exception {
        provider(WORKER_PROVIDER, "dev-worker", "logosnode", WORKER_KEY);
        poll(WORKER_KEY);

        // The core goes first. A worker makes its own row, so it can be there before the core deployer is.
        mvc.perform(get("/deployments/overview").with(TestJwt.logosAdmin()))
            .andExpect(jsonPath("$.can_start").value(false))
            .andExpect(jsonPath("$.blocked_reason").value("No core deployer is set up yet. The core deployer goes first."));
        rawStart("pr-93").andExpect(status().isConflict());
    }

    @Test
    void theOverviewSaysWhyADeploymentCannotStartYet() throws Exception {
        mvc.perform(get("/deployments/overview").with(TestJwt.logosAdmin()))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.requires_confirmation").value(false))
            .andExpect(jsonPath("$.deployers").isEmpty())
            .andExpect(jsonPath("$.can_start").value(false))
            .andExpect(jsonPath("$.blocked_reason").value("No deployer is set up yet."));
    }

    @Test
    void anOfflineCoreDeployerBlocksTheStart() throws Exception {
        createDeployer("dev-core", "core");

        rawStart("pr-93")
            .andExpect(status().isConflict())
            .andExpect(jsonPath("$.detail").value("The core deployer dev-core has not connected yet."));
    }

    @Test
    void aPlanIsTheCoreFirstAndThenTheWorkers() throws Exception {
        String[] keys = coreAndWorker();

        JsonNode plan = body(rawStart("pr-93").andExpect(status().isCreated()));

        assertThat(plan.get("status").asText()).isEqualTo("running");
        assertThat(plan.get("initiator").asText()).isNotBlank();
        assertThat(step(plan, "dev-core").get("status").asText()).isEqualTo("ordered");
        assertThat(step(plan, "dev-worker").get("status").asText()).isEqualTo("waiting");

        // The worker has nothing to do yet. The core has one order: the id of the step, the tag and the kind.
        assertThat(poll(keys[1]).get("orders")).isEmpty();
        JsonNode orders = poll(keys[0]).get("orders");
        assertThat(orders).hasSize(1);
        assertThat(orders.get(0).get("id").asText()).isEqualTo(step(plan, "dev-core").get("id").asText());
        assertThat(orders.get(0).get("tag").asText()).isEqualTo("pr-93");
        assertThat(orders.get(0).get("kind").asText()).isEqualTo("core");
        // The order is handed out again until the deployer reports on it.
        assertThat(poll(keys[0]).get("orders")).hasSize(1);
    }

    @Test
    void theWorkersStartWhenTheCoreSucceededAndThePlanEndsWithTheLastStep() throws Exception {
        String[] keys = coreAndWorker();
        JsonNode plan = body(rawStart("pr-93").andExpect(status().isCreated()));
        String planId = plan.get("id").asText();
        String coreId = step(plan, "dev-core").get("id").asText();
        String workerId = step(plan, "dev-worker").get("id").asText();

        JsonNode acked = report(keys[0], snapshot(coreId, "pulling", null, ""));
        assertThat(acked.get("acked")).hasSize(1);
        assertThat(poll(keys[0]).get("orders")).isEmpty();
        assertThat(step(plan(planId), "dev-core").get("status").asText()).isEqualTo("running");
        assertThat(poll(keys[1]).get("orders")).isEmpty();

        report(keys[0], snapshot(coreId, "done", "succeeded", "now running tag pr-93"));

        JsonNode afterCore = plan(planId);
        assertThat(step(afterCore, "dev-core").get("status").asText()).isEqualTo("succeeded");
        assertThat(step(afterCore, "dev-worker").get("status").asText()).isEqualTo("ordered");
        assertThat(afterCore.get("status").asText()).isEqualTo("running");
        assertThat(poll(keys[1]).get("orders").get(0).get("id").asText()).isEqualTo(workerId);

        report(keys[1], snapshot(workerId, "done", "succeeded", "now running pr-93"));

        JsonNode ended = plan(planId);
        assertThat(ended.get("status").asText()).isEqualTo("succeeded");
        assertThat(ended.get("finished_at").isNull()).isFalse();
        assertThat(step(ended, "dev-core").get("events")).hasSize(1);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM notifications WHERE type = 'deployment'", Integer.class))
            .isEqualTo(2);
    }

    @Test
    void aFailedCoreKeepsTheWorkersFromStartingAndSaysWhy() throws Exception {
        String[] keys = coreAndWorker();
        JsonNode plan = body(rawStart("pr-93").andExpect(status().isCreated()));
        String coreId = step(plan, "dev-core").get("id").asText();

        report(keys[0], snapshot(coreId, "done", "failed", "docker compose up failed: port is already allocated"));

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
        coreAndWorker();
        JsonNode first = body(rawStart("pr-93").andExpect(status().isCreated()));
        mvc.perform(post("/deployments/plans/" + first.get("id").asText() + "/abandon").with(TestJwt.logosAdmin()));
        JsonNode second = body(rawStart("pr-94").andExpect(status().isCreated()));

        assertThat(second.get("number").asLong()).isEqualTo(first.get("number").asLong() + 1);
        assertThat(plan(first.get("id").asText()).get("number").asLong()).isEqualTo(first.get("number").asLong());
    }

    @Test
    void aSecondDeploymentIsRefusedWhileOneRuns() throws Exception {
        coreAndWorker();
        rawStart("pr-93").andExpect(status().isCreated());

        rawStart("pr-94")
            .andExpect(status().isConflict())
            .andExpect(jsonPath("$.detail").value("A deployment is running."));
        mvc.perform(get("/deployments/overview").with(TestJwt.logosAdmin()))
            .andExpect(jsonPath("$.can_start").value(false))
            .andExpect(jsonPath("$.blocked_reason").value("A deployment is running."));
    }

    @Test
    void anAdminAbandonsAPlanAndCanStartAnother() throws Exception {
        String[] keys = coreAndWorker();
        JsonNode plan = body(rawStart("pr-93").andExpect(status().isCreated()));
        String planId = plan.get("id").asText();

        JsonNode abandoned = body(mvc.perform(post("/deployments/plans/" + planId + "/abandon").with(TestJwt.logosAdmin()))
            .andExpect(status().isOk()));

        assertThat(abandoned.get("status").asText()).isEqualTo("failed");
        assertThat(abandoned.get("abandoned_by").asText()).isNotBlank();
        assertThat(step(abandoned, "dev-core").get("status").asText()).isEqualTo("cancelled");
        assertThat(poll(keys[0]).get("orders")).isEmpty();
        mvc.perform(post("/deployments/plans/" + planId + "/abandon").with(TestJwt.logosAdmin()))
            .andExpect(status().isConflict());
        rawStart("pr-94").andExpect(status().isCreated());
    }

    @Test
    void theTagMustBeADockerTag() throws Exception {
        coreAndWorker();

        rawStart("a b").andExpect(status().isBadRequest());
        rawStart("previous").andExpect(status().isBadRequest());
    }

    // ------------------------------------------------------------------ what a deployer may do

    @Test
    void aDeployerCannotReportOnTheStepOfAnotherDeployer() throws Exception {
        String[] keys = coreAndWorker();
        JsonNode plan = body(rawStart("pr-93").andExpect(status().isCreated()));
        String coreStepId = step(plan, "dev-core").get("id").asText();

        // The worker names the step of the core and says it succeeded. That would release the workers.
        report(keys[1], snapshot(coreStepId, "done", "succeeded", "faked"));

        JsonNode after = plan(plan.get("id").asText());
        assertThat(step(after, "dev-core").get("status").asText()).isEqualTo("ordered");
        assertThat(step(after, "dev-worker").get("status").asText()).isEqualTo("waiting");
    }

    @Test
    void aReportAboutAStepThatDoesNotExistIsTakenAndMakesNothing() throws Exception {
        String key = createDeployer("dev-core", "core").get("key").asText();
        String unknown = "3f2b6c1e-8d4a-4b7e-9c11-0a1b2c3d4e5f";

        JsonNode answer = report(key, snapshot(unknown, "pulling", null, ""));

        assertThat(answer.get("acked")).hasSize(1);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM deployment_plans", Integer.class)).isZero();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM deployment_steps", Integer.class)).isZero();
    }

    // ------------------------------------------------------------------ silence

    @Test
    void aStepThatStaysSilentEndsAsTimedOutAndTheLateReportShowsTheRealResult() throws Exception {
        String[] keys = coreAndWorker();
        JsonNode plan = body(rawStart("pr-93").andExpect(status().isCreated()));
        String planId = plan.get("id").asText();
        String coreId = step(plan, "dev-core").get("id").asText();
        report(keys[0], snapshot(coreId, "swapping", null, ""));
        jdbc.update("UPDATE deployment_steps SET last_activity_at = now() - interval '2 hours' WHERE id = ?::uuid", coreId);

        int ended = reports.closeSilentSteps();

        assertThat(ended).isEqualTo(1);
        JsonNode timedOut = plan(planId);
        assertThat(timedOut.get("status").asText()).isEqualTo("failed");
        assertThat(step(timedOut, "dev-core").get("failure").asText()).isEqualTo("timed_out");
        assertThat(step(timedOut, "dev-worker").get("status").asText()).isEqualTo("cancelled");
        rawStart("pr-94").andExpect(status().isCreated());

        // The host did finish after all. The step shows it. The plan never released the workers.
        report(keys[0], snapshot(coreId, "done", "succeeded", "now running tag pr-93"));

        JsonNode corrected = plan(planId);
        assertThat(step(corrected, "dev-core").get("status").asText()).isEqualTo("succeeded");
        assertThat(step(corrected, "dev-worker").get("status").asText()).isEqualTo("cancelled");
        assertThat(corrected.get("status").asText()).isEqualTo("failed");
    }

    @Test
    void anOrderThatNobodyTakesEndsAsTimedOutToo() throws Exception {
        coreAndWorker();
        JsonNode plan = body(rawStart("pr-93").andExpect(status().isCreated()));
        jdbc.update("UPDATE deployment_steps SET last_activity_at = now() - interval '2 hours' WHERE status = 'ordered'");

        reports.closeSilentSteps();

        JsonNode ended = plan(plan.get("id").asText());
        assertThat(step(ended, "dev-core").get("message").asText()).contains("did not take the order");
    }

    @Test
    void anOrderThatIsTooOldIsNotHandedOutBeforeTheSweepEndsIt() throws Exception {
        String[] keys = coreAndWorker();
        body(rawStart("pr-93").andExpect(status().isCreated()));
        jdbc.update("UPDATE deployment_steps SET last_activity_at = now() - interval '2 hours' WHERE status = 'ordered'");

        assertThat(poll(keys[0]).get("orders")).isEmpty();
    }

    @Test
    void aReportAfterALongOutageClosesTheStalePlanBeforeItCanReleaseTheWorkers() throws Exception {
        String[] keys = coreAndWorker();
        JsonNode plan = body(rawStart("pr-93").andExpect(status().isCreated()));
        String coreId = step(plan, "dev-core").get("id").asText();
        jdbc.update("UPDATE deployment_steps SET last_activity_at = now() - interval '2 hours' WHERE id = ?::uuid", coreId);

        report(keys[0], snapshot(coreId, "done", "succeeded", "now running tag pr-93"));

        JsonNode ended = plan(plan.get("id").asText());
        assertThat(step(ended, "dev-worker").get("status").asText()).isEqualTo("cancelled");
        assertThat(poll(keys[1]).get("orders")).isEmpty();
    }

    // ------------------------------------------------------------------ history

    @Test
    void theHistoryListsTheLatestPlans() throws Exception {
        coreAndWorker();
        JsonNode first = body(rawStart("pr-93").andExpect(status().isCreated()));
        mvc.perform(post("/deployments/plans/" + first.get("id").asText() + "/abandon").with(TestJwt.logosAdmin()));
        rawStart("pr-94").andExpect(status().isCreated());

        List<String> tags = new ArrayList<>();
        for (JsonNode plan : body(mvc.perform(get("/deployments/plans").with(TestJwt.logosAdmin()))
                .andExpect(status().isOk()))) {
            tags.add(plan.get("tag").asText());
        }

        assertThat(tags).containsExactly("pr-94", "pr-93");
        mvc.perform(get("/deployments/overview").with(TestJwt.logosAdmin()))
            .andExpect(jsonPath("$.suggested_tags[0]").value("latest"))
            .andExpect(jsonPath("$.suggested_tags[1]").value("pr-94"));
    }
}
