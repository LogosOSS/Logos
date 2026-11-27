package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.math.BigDecimal;
import java.sql.DriverManager;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.http.converter.json.JacksonJsonHttpMessageConverter;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.server.ResponseStatusException;

import tools.jackson.databind.PropertyNamingStrategies;
import tools.jackson.databind.json.JsonMapper;

import de.tum.cit.aet.logos.logoswebservice.audit.AuditLogService;
import de.tum.cit.aet.logos.logoswebservice.configuration.controller.ProviderSpendPolicyController;
import de.tum.cit.aet.logos.logoswebservice.configuration.service.ProviderSpendPolicyService;
import de.tum.cit.aet.logos.logoswebservice.configuration.service.ProviderSpendPolicyService.Change;
import org.testcontainers.containers.PostgreSQLContainer;

/** Real PostgreSQL row locks are the shared contract between the two relays. */
class ProviderSpendCapDatabaseTest {

    private static final String MODEL = "synthetic-20261101";
    private static final String PAYLOAD = """
        {"model":"synthetic-20261101","messages":[{"role":"user","content":"hi"}],"max_tokens":100}
        """;

    @Test
    void concurrentRelayAdmissionAndSettlementShareOneMonthlyLedger() throws Exception {
        String local = System.getProperty("logos.test.providerSpendDatabase");
        if (local != null) {
            run(local, System.getProperty("user.name"), "");
            return;
        }
        try (var postgres = new PostgreSQLContainer<>("postgres:17").withDatabaseName("logosdb")
                .withUsername("postgres").withPassword("root")) {
            postgres.start();
            run(postgres.getJdbcUrl(), postgres.getUsername(), postgres.getPassword());
        }
    }

    private void run(String url, String user, String password) throws Exception {
        try (var connection = DriverManager.getConnection(url, user, password);
                var resource = new ClassLoaderResourceAccessor();
                var liquibase = new Liquibase("liquibase/changelog/master.xml", resource, new JdbcConnection(connection))) {
            liquibase.update(new Contexts(), new LabelExpression());
        }
        var dataSource = new DriverManagerDataSource(url, user, password);
        var jdbc = new JdbcTemplate(dataSource);
        var named = new NamedParameterJdbcTemplate(jdbc);
        var transactions = new DataSourceTransactionManager(dataSource);
        var ledger = new ProviderSpendLedger(named, transactions, new ProviderSpendPricing(named));
        var mapper = new ObjectMapper();
        int provider = jdbc.queryForObject("""
            INSERT INTO providers (name, base_url, provider_type, cloud_provider_type, auth_name, auth_format)
            VALUES ('Synthetic Anthropic', 'https://api.anthropic.com', 'cloud', 'anthropic', 'x-api-key', '{}')
            RETURNING id
            """, Integer.class);
        int model = jdbc.queryForObject("""
            INSERT INTO models (name) VALUES (?) RETURNING id
            """, Integer.class, MODEL);
        jdbc.update("INSERT INTO model_provider (model_id, provider_id) VALUES (?, ?)", model, provider);
        jdbc.update("""
            INSERT INTO token_prices (type_id, model_id, provider_id, valid_from, price_per_k_unit)
            SELECT id, ?, ?, clock_timestamp() - interval '1 minute',
                   CASE name WHEN 'billed_input_uncached' THEN 1000 ELSE 2000 END
              FROM token_types WHERE name IN ('billed_input_uncached','billed_output_text')
            """, model, provider);
        jdbc.update("""
            INSERT INTO provider_spend_policies (provider_id, effective_month, limit_micro_cents)
            VALUES (?, date_trunc('month', current_timestamp AT TIME ZONE 'UTC'), 1500000)
            """, provider);
        var request = mapper.readTree(PAYLOAD);
        var messages = "https://api.anthropic.com/v1/messages";
        assertThat(ledger.reserve(UUID.randomUUID(), provider, model, messages, request))
            .isEqualTo(ProviderSpendLedger.Decision.UNBOUNDED);
        ledger.recordReviewedBound(provider, model, new ProviderSpendLedger.BoundReview(messages, MODEL, "max_tokens",
            1000000, 64000, "https://platform.claude.com/docs/en/models/haiku-4-5/overview"));
        assertThat(ledger.coverage(provider)).isEqualTo(1);
        assertThat(ledger.reserve(UUID.randomUUID(), provider, model, messages, mapper.readTree(PAYLOAD
            .replace(MODEL, "synthetic-latest")))).isEqualTo(ProviderSpendLedger.Decision.UNBOUNDED);
        assertThat(ledger.reserve(UUID.randomUUID(), provider, model, messages, mapper.readTree(PAYLOAD
            .replace(":100", ":64001")))).isEqualTo(ProviderSpendLedger.Decision.UNBOUNDED);
        var tools = (com.fasterxml.jackson.databind.node.ObjectNode) request.deepCopy();
        tools.set("tools", mapper.createArrayNode());
        assertThat(ledger.reserve(UUID.randomUUID(), provider, model, messages, tools))
            .isEqualTo(ProviderSpendLedger.Decision.UNBOUNDED);
        jdbc.update("UPDATE providers SET auth_name = 'anthropic-beta' WHERE id = ?", provider);
        assertThat(ledger.coverage(provider)).isZero();
        assertThat(ledger.reserve(UUID.randomUUID(), provider, model, messages, request))
            .isEqualTo(ProviderSpendLedger.Decision.UNBOUNDED);
        jdbc.update("UPDATE providers SET auth_name = 'x-api-key' WHERE id = ?", provider);
        // Independent application replicas, same store.
        var other = new ProviderSpendLedger(named, new DataSourceTransactionManager(dataSource), new ProviderSpendPricing(named));
        java.util.concurrent.atomic.AtomicInteger replicas = new java.util.concurrent.atomic.AtomicInteger();

        List<UUID> attempts = java.util.stream.IntStream.range(0, 12).mapToObj(i -> UUID.randomUUID()).toList();
        List<Callable<ProviderSpendLedger.Decision>> admit = attempts.stream()
            .<Callable<ProviderSpendLedger.Decision>>map(attempt -> () -> (replicas.getAndIncrement() % 2 == 0 ? ledger : other)
                .reserve(attempt, provider, model, messages, request)).toList();
        List<ProviderSpendLedger.Decision> results;
        try (var exec = Executors.newFixedThreadPool(12)) {
            var futures = exec.invokeAll(admit);
            var decisions = new java.util.ArrayList<ProviderSpendLedger.Decision>();
            for (var future : futures) {
                decisions.add(future.get());
            }
            results = decisions;
        }
        var reserved = ProviderSpendLedger.Decision.RESERVED;
        var exhausted = ProviderSpendLedger.Decision.EXHAUSTED;
        assertThat(results).containsOnly(reserved, exhausted);
        assertThat(results.stream().filter(reserved::equals)).hasSize(1);
        UUID first = attempts.get(results.indexOf(reserved));
        assertThat(ledger.reserve(first, provider, model, messages, request)).isEqualTo(ProviderSpendLedger.Decision.DUPLICATE);
        assertThat(ledger.reserve(UUID.randomUUID(), provider, model, messages, mapper.readTree(
            PAYLOAD.replace(":100", ":\"unbounded\"")))).isEqualTo(ProviderSpendLedger.Decision.UNBOUNDED);

        assertThat(ledger.settle(first, Map.of("prompt_tokens", 10L, "completion_tokens", 4L, "usage_estimated", 1L),
            null, false)).isFalse();
        // This exact shared fixture is produced by finalize_billing_inputs in the Python suite.
        Map<String, Long> orchestratorUsage;
        try (var fixture = getClass().getResourceAsStream("/fixtures/provider-spend-orchestrator-usage.json")) {
            orchestratorUsage = mapper.convertValue(mapper.readTree(fixture).path("usage"),
                new com.fasterxml.jackson.core.type.TypeReference<>() {});
        }
        var withCharacters = ProviderSpendPricing.quantities(orchestratorUsage, true);
        var tokenRates = new ProviderSpendPricing(named).rates(provider, model, java.time.OffsetDateTime.now());
        assertThat(ProviderSpendPricing.charge(tokenRates.stream().filter(rate ->
            !"billed_input_uncached".equals(rate.quantity())).toList(), withCharacters, null)).isNull();
        assertThat(ProviderSpendPricing.charge(tokenRates.stream().filter(rate ->
            !"billed_output_text".equals(rate.quantity())).toList(), withCharacters, null)).isNull();
        assertThat(ledger.settle(first, orchestratorUsage, null, false)).isTrue();
        assertThat(ledger.settle(first, orchestratorUsage, null, false)).isFalse();
        assertThat(jdbc.queryForMap("""
            SELECT charged_micro_cents, held_micro_cents, bound_violation FROM provider_spend_months WHERE provider_id = ?
            """, provider)).containsEntry("charged_micro_cents", new BigDecimal("18"))
            .containsEntry("held_micro_cents", BigDecimal.ZERO).containsEntry("bound_violation", false);

        checkZeroMetering(jdbc, ledger, mapper, provider, model, messages, request);

        UUID previous = UUID.randomUUID();
        jdbc.update("""
            INSERT INTO provider_spend_months (provider_id, month_start, held_micro_cents)
            VALUES (?, date_trunc('month', current_timestamp AT TIME ZONE 'UTC') - interval '1 month', 1000)
            """, provider);
        jdbc.update("""
            INSERT INTO provider_spend_attempts
                (attempt_id, provider_id, month_start, model_id, priced_at, reserved_micro_cents, anthropic_usage, max_input_tokens, max_output_tokens)
            VALUES (?, ?, date_trunc('month', current_timestamp AT TIME ZONE 'UTC') - interval '1 month',
                ?, current_timestamp, 1000, true, 1000000, 100)
            """, previous, provider, model);
        assertThat(ledger.settle(previous, Map.of("prompt_tokens", 20L, "completion_tokens", 30L), null, false)).isTrue();
        assertThat(jdbc.queryForList("""
            SELECT charged_micro_cents FROM provider_spend_months WHERE provider_id = ? ORDER BY month_start
            """, Long.class, provider)).containsExactly(80L, 18L);

        UUID wrongBound = UUID.randomUUID();
        assertThat(ledger.reserve(wrongBound, provider, model, messages, request)).isEqualTo(reserved);
        assertThat(ledger.settle(wrongBound, Map.of("prompt_tokens", 5000000L, "completion_tokens", 4L), null, false)).isTrue();
        assertThat(jdbc.queryForObject("""
            SELECT BOOL_OR(bound_violation) FROM provider_spend_months WHERE provider_id = ?
            """, Boolean.class, provider)).isTrue();
        assertThat(ledger.reserve(UUID.randomUUID(), provider, model, messages, request)).isEqualTo(exhausted);

        jdbc.update("""
            UPDATE provider_spend_months SET bound_violation = false, charged_micro_cents = 0
             WHERE provider_id = ? AND month_start = date_trunc('month', current_timestamp AT TIME ZONE 'UTC')::date
            """, provider);
        UUID extraBilled = UUID.randomUUID();
        assertThat(ledger.reserve(extraBilled, provider, model, messages, request)).isEqualTo(reserved);
        assertThat(ledger.settle(extraBilled, Map.of("prompt_tokens", 10L, "completion_tokens", 4L,
            "billed_search_queries", 1L), null, false)).isFalse();
        assertThat(jdbc.queryForObject("SELECT charged_micro_cents FROM provider_spend_attempts WHERE attempt_id = ?",
            Long.class, extraBilled)).isNull();
        assertThat(ledger.reserve(UUID.randomUUID(), provider, model, messages, request)).isEqualTo(exhausted);

        checkAdminPolicy(jdbc, provider, model, ledger);
        checkImmediateCurrentPolicy(jdbc, new TransactionTemplate(transactions), model, provider, ledger, request, messages);
    }

    private static void checkZeroMetering(JdbcTemplate jdbc, ProviderSpendLedger ledger, ObjectMapper mapper,
            int provider, int model, String messages, JsonNode request) throws Exception {
        var controller = new InternalProviderSpendController(new ProviderSpendCapService(ledger), mapper, "synthetic-secret");
        var json = JsonMapper.builder().propertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE).build();
        var mvc = MockMvcBuilders.standaloneSetup(controller)
            .setMessageConverters(new JacksonJsonHttpMessageConverter(json)).build();
        UUID attempt = UUID.randomUUID();
        assertThat(ledger.reserve(attempt, provider, model, messages, request))
            .isEqualTo(ProviderSpendLedger.Decision.RESERVED);

        var body = mapper.createObjectNode().put("attempt_id", attempt.toString()).put("undispatched", false).putNull("tier");
        try (var fixture = ProviderSpendCapDatabaseTest.class.getResourceAsStream(
                "/fixtures/provider-spend-orchestrator-zero-usage.json")) {
            body.set("usage", mapper.readTree(fixture).path("usage"));
        }
        var incomplete = body.deepCopy();
        ((com.fasterxml.jackson.databind.node.ObjectNode) incomplete.path("usage")).remove("completion_tokens");
        mvc.perform(post("/internal/provider-spend/settle").header("Authorization", "Bearer synthetic-secret")
            .contentType(MediaType.APPLICATION_JSON).content(incomplete.toString())).andExpect(status().isOk());
        assertThat(jdbc.queryForObject("SELECT charged_micro_cents FROM provider_spend_attempts WHERE attempt_id = ?",
            Long.class, attempt)).isNull();

        mvc.perform(post("/internal/provider-spend/settle").header("Authorization", "Bearer synthetic-secret")
            .contentType(MediaType.APPLICATION_JSON).content(body.toString())).andExpect(status().isOk());
        assertThat(jdbc.queryForObject("SELECT charged_micro_cents FROM provider_spend_attempts WHERE attempt_id = ?",
            Long.class, attempt)).isZero();
        assertThat(jdbc.queryForMap("""
            SELECT charged_micro_cents, held_micro_cents, bound_violation FROM provider_spend_months WHERE provider_id = ?
            """, provider)).containsEntry("charged_micro_cents", new BigDecimal("18"))
            .containsEntry("held_micro_cents", BigDecimal.ZERO).containsEntry("bound_violation", false);
    }

    private static void checkImmediateCurrentPolicy(JdbcTemplate jdbc, TransactionTemplate tx, int model,
            int otherProvider, ProviderSpendLedger ledger, JsonNode request, String messages) throws Exception {
        int provider = jdbc.queryForObject("""
            INSERT INTO providers (name, base_url, provider_type, cloud_provider_type, auth_name, auth_format)
            VALUES ('Synthetic immediate cap', 'https://api.anthropic.com', 'cloud', 'anthropic', 'x-api-key', '{}')
            RETURNING id
            """, Integer.class);
        jdbc.update("INSERT INTO model_provider (model_id, provider_id) VALUES (?, ?)", model, provider);
        jdbc.update("""
            INSERT INTO token_prices (type_id, model_id, provider_id, valid_from, price_per_k_unit)
            SELECT type_id, model_id, ?, valid_from, price_per_k_unit
              FROM token_prices WHERE provider_id = ? AND model_id = ?
            """, provider, otherProvider, model);
        ledger.recordReviewedBound(provider, model, new ProviderSpendLedger.BoundReview(messages, MODEL, "max_tokens",
            1000000, 64000, "https://platform.claude.com/docs/en/models/overview"));
        assertThat(ledger.reserve(UUID.randomUUID(), provider, model, messages, request))
            .isEqualTo(ProviderSpendLedger.Decision.UNCAPPED);

        var now = jdbc.queryForObject("SELECT clock_timestamp()", OffsetDateTime.class).withOffsetSameInstant(ZoneOffset.UTC);
        var month = now.toLocalDate().withDayOfMonth(1);
        var firstInstant = month.atStartOfDay().atOffset(ZoneOffset.UTC);
        recordCost(jdbc, provider, firstInstant, "success", true, 350L);
        recordCost(jdbc, provider, firstInstant, "error", true, 50L);
        recordCost(jdbc, provider, firstInstant.minusNanos(1000), "success", true, 700L);
        recordCost(jdbc, provider, now.plusDays(1), "success", true, 800L);
        recordCost(jdbc, otherProvider, firstInstant, "success", true, 900L);
        recordCost(jdbc, provider, firstInstant, null, true, 1000L);
        recordCost(jdbc, provider, firstInstant, "success", false, 2000L);
        recordCost(jdbc, provider, firstInstant, "success", true, null);

        var audit = mock(AuditLogService.class);
        var controller = new ProviderSpendPolicyController(
            new ProviderSpendPolicyService(new NamedParameterJdbcTemplate(jdbc), audit, ledger));
        jdbc.update("""
            INSERT INTO provider_spend_policies (provider_id, effective_month, limit_micro_cents)
            VALUES (?, ?, 4000000)
            """, provider, month.plusMonths(1));
        Map<String, Object> scheduled = Map.of(
            "enabled", true, "limit_usd", "0.04000000", "effective_month", month.plusMonths(1).toString());
        var original = new Change(true, new BigDecimal("0.010004"));
        Map<String, Object> initial = tx.execute(state -> {
            jdbc.execute("SET LOCAL TIME ZONE 'Pacific/Kiritimati'");
            return controller.update(provider, original);
        });
        Map<String, Object> immediate = Map.of(
            "enabled", true, "limit_usd", "0.01000400", "effective_month", month.toString());
        assertThat(initial).containsEntry("current", immediate).containsEntry("scheduled", scheduled)
            .containsEntry("charged_usd", new BigDecimal("0.00000400"));
        verify(audit).record("provider.monthly_spend_cap_updated", "provider", provider, null, null, immediate);
        // A request that fits before accounting history no longer fits once
        // that history is included; nothing goes upstream under the new cap.
        assertThat(ledger.reserve(UUID.randomUUID(), provider, model, messages, request))
            .isEqualTo(ProviderSpendLedger.Decision.EXHAUSTED);

        var high = new Change(true, new BigDecimal("0.03"));
        Map<String, Object> raised = tx.execute(state -> controller.update(provider, high));
        assertThat(raised).containsEntry("scheduled", scheduled);
        UUID held = UUID.randomUUID();
        assertThat(ledger.reserve(held, provider, model, messages, request)).isEqualTo(ProviderSpendLedger.Decision.RESERVED);
        assertThat(controller.get(provider)).containsEntry("reserved_usd", new BigDecimal("0.01000200"));

        // Hold the exact DB row lock shared with admission. The waiting replica
        // must see the newly lowered cap after this transaction commits.
        var starting = new CountDownLatch(1);
        try (var executor = Executors.newVirtualThreadPerTaskExecutor()) {
            var pending = tx.execute(state -> {
                jdbc.queryForObject("SELECT id FROM providers WHERE id = ? FOR UPDATE", Integer.class, provider);
                var result = executor.submit(() -> {
                    starting.countDown();
                    return ledger.reserve(UUID.randomUUID(), provider, model, messages, request);
                });
                try {
                    assertThat(starting.await(1, TimeUnit.SECONDS)).isTrue();
                } catch (InterruptedException ex) {
                    Thread.currentThread().interrupt();
                    throw new IllegalStateException(ex);
                }
                assertThat(result).isNotDone();
                assertThat(controller.update(provider, original)).containsEntry("scheduled", scheduled)
                    .containsEntry("reserved_usd", new BigDecimal("0.01000200"));
                return result;
            });
            assertThat(pending.get(5, TimeUnit.SECONDS)).isEqualTo(ProviderSpendLedger.Decision.EXHAUSTED);
        }

        var removed = tx.execute(state -> controller.update(provider, new Change(false, null)));
        assertThat(removed).containsEntry("scheduled", scheduled).containsEntry("reserved_usd", new BigDecimal("0.01000200"));
        assertThat(((Map<?, ?>) removed.get("current")).get("enabled")).isEqualTo(false);
        assertThat(ledger.reserve(UUID.randomUUID(), provider, model, messages, request)).isEqualTo(ProviderSpendLedger.Decision.UNCAPPED);
        assertThat(ledger.settle(held, Map.of("prompt_tokens", 10L, "completion_tokens", 4L), null, false)).isTrue();
        // Settled capped attempts and later uncapped logs are never merged by
        // guessing which historical rows are duplicates when re-enabling.
        recordCost(jdbc, provider, now, "success", true, 18L);
        recordCost(jdbc, provider, now, "success", true, 900L);
        var restarted = tx.execute(state -> controller.update(provider, high));
        assertThat(restarted).containsEntry("scheduled", scheduled).containsEntry("charged_usd", new BigDecimal("0.00000418"))
            .containsEntry("reserved_usd", new BigDecimal("0.00000000"));
    }

    private static void recordCost(JdbcTemplate jdbc, int provider, OffsetDateTime at, String status, boolean finalized,
            Long amount) {
        jdbc.update("""
            INSERT INTO log_entry (provider_id, timestamp_request, result_status, cost_finalized, settled_cost_micro_cents)
            VALUES (?, ?, CAST(? AS result_status_enum), ?, ?)
            """, provider, at, status, finalized, amount);
    }

    private static void checkAdminPolicy(JdbcTemplate jdbc, int provider, int model, ProviderSpendLedger ledger) {
        var audit = mock(AuditLogService.class);
        var service = new ProviderSpendPolicyService(new NamedParameterJdbcTemplate(jdbc), audit, ledger);
        var controller = new ProviderSpendPolicyController(service);
        var currentMonth = jdbc.queryForObject("""
            SELECT date_trunc('month', CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date
            """, LocalDate.class);
        Map<String, Object> current = Map.of(
            "enabled", true, "effective_month", currentMonth.toString(), "limit_usd", "0.01500000");
        assertThat(controller.get(provider)).containsEntry("current", current)
            .containsEntry("scheduled", null).containsEntry("bound_violation", true);

        assertThatThrownBy(() -> controller.get(-1)).isInstanceOf(ResponseStatusException.class)
            .hasMessageContaining("404");
        for (Change invalid : List.of(new Change(null, null), new Change(true, null),
                new Change(true, new BigDecimal("-1")), new Change(true, new BigDecimal("0.000000001")),
                new Change(false, BigDecimal.ONE))) {
            assertThatThrownBy(() -> controller.update(provider, invalid))
                .isInstanceOf(ResponseStatusException.class).hasMessageContaining("400");
        }

        String official = "https://api.anthropic.com/v1/messages";
        var evidence = new ProviderSpendLedger.BoundReview(official, MODEL, "max_tokens",
            1000000, 64000, "https://platform.claude.com/docs/en/models/overview");
        assertThat(controller.removeReviewedBound(provider, model, official)).containsEntry("bounded_deployments", 0);
        assertThatThrownBy(() -> controller.removeReviewedBound(provider, model, "https://example.test/messages"))
            .isInstanceOf(ResponseStatusException.class).hasMessageContaining("400");
        assertThatThrownBy(() -> controller.recordReviewedBound(provider, model,
            new ProviderSpendLedger.BoundReview(official, MODEL, "max_tokens", 0, 0, evidence.evidenceUrl())))
            .isInstanceOf(ResponseStatusException.class).hasMessageContaining("400");
        assertThatThrownBy(() -> controller.recordReviewedBound(Integer.MAX_VALUE, model, evidence))
            .isInstanceOf(ResponseStatusException.class).hasMessageContaining("404");
        assertThat(controller.recordReviewedBound(provider, model, evidence)).containsEntry("bounded_deployments", 1);
        assertThatThrownBy(() -> controller.update(Integer.MAX_VALUE, new Change(false, null)))
            .isInstanceOf(ResponseStatusException.class).hasMessageContaining("404");

        boolean nearRollover = Boolean.TRUE.equals(jdbc.queryForObject("""
            SELECT clock_timestamp() AT TIME ZONE 'UTC' + interval '1 minute'
                >= date_trunc('month', CURRENT_TIMESTAMP AT TIME ZONE 'UTC') + interval '1 month'
            """, Boolean.class));
        if (nearRollover) {
            assertThatThrownBy(() -> controller.schedule(provider, new Change(true, BigDecimal.ONE)))
                .isInstanceOf(ResponseStatusException.class).hasMessageContaining("409");
            return;
        }

        Map<String, Object> next = Map.of(
            "enabled", true, "effective_month", currentMonth.plusMonths(1).toString(),
            "limit_usd", "123.00000001");
        assertThat(controller.schedule(provider, new Change(true, new BigDecimal("123.00000001"))))
            .containsEntry("current", current).containsEntry("scheduled", next);
        Map<String, Object> disabled = new LinkedHashMap<>();
        disabled.put("enabled", false);
        disabled.put("effective_month", currentMonth.plusMonths(1).toString());
        disabled.put("limit_usd", null);
        assertThat(controller.schedule(provider, new Change(false, null)))
            .containsEntry("current", current).containsEntry("scheduled", disabled);
        verify(audit).record("provider.monthly_spend_cap_scheduled", "provider", provider, null, null, next);
        verify(audit).record("provider.monthly_spend_cap_scheduled", "provider", provider, null, next, disabled);
    }

}
