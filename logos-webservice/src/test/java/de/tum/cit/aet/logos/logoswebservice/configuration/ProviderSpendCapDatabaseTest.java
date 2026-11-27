package de.tum.cit.aet.logos.logoswebservice.configuration;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;

import java.math.BigDecimal;
import java.sql.DriverManager;
import java.time.LocalDate;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.concurrent.Executors;

import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.web.server.ResponseStatusException;

import de.tum.cit.aet.logos.logoswebservice.audit.AuditLogService;
import de.tum.cit.aet.logos.logoswebservice.configuration.controller.ProviderSpendPolicyController;
import de.tum.cit.aet.logos.logoswebservice.configuration.service.ProviderSpendPolicyService;
import de.tum.cit.aet.logos.logoswebservice.configuration.service.ProviderSpendPolicyService.Change;
import org.testcontainers.containers.PostgreSQLContainer;

/** Real PostgreSQL row locks are the shared contract between the two relays. */
class ProviderSpendCapDatabaseTest {

    private static final String MODEL = "claude-haiku-4-5-20251001";
    private static final String PAYLOAD = """
        {"model":"claude-haiku-4-5-20251001","messages":[{"role":"user","content":"hi"}],"max_tokens":100}
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
        var jdbc = new JdbcTemplate(new DriverManagerDataSource(url, user, password));
        int provider = jdbc.queryForObject("""
            INSERT INTO providers (name, base_url, provider_type, cloud_provider_type, auth_name, auth_format)
            VALUES ('Synthetic Anthropic', 'https://api.anthropic.com', 'cloud', 'anthropic', 'x-api-key', '{}')
            RETURNING id
            """, Integer.class);
        int model = jdbc.queryForObject("""
            INSERT INTO models (name) VALUES (?) RETURNING id
            """, Integer.class, MODEL);
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
        List<UUID> attempts = java.util.stream.IntStream.range(0, 12).mapToObj(i -> UUID.randomUUID()).toList();
        List<Callable<String>> admit = attempts.stream()
            .<Callable<String>>map(attempt -> () -> reserve(jdbc, attempt, provider, model, PAYLOAD)).toList();
        List<String> results;
        try (var exec = Executors.newFixedThreadPool(12)) {
            var futures = exec.invokeAll(admit);
            var decisions = new java.util.ArrayList<String>();
            for (var future : futures) {
                decisions.add(future.get());
            }
            results = decisions;
        }
        assertThat(results).containsOnly("reserved", "exhausted");
        assertThat(results.stream().filter("reserved"::equals)).hasSize(1);
        UUID first = attempts.get(results.indexOf("reserved"));
        assertThat(reserve(jdbc, first, provider, model, PAYLOAD)).isEqualTo("duplicate");
        assertThat(reserve(jdbc, UUID.randomUUID(), provider, model, PAYLOAD.replace(":100", ":\"unbounded\"")))
            .isEqualTo("unbounded");

        assertThat(settle(jdbc, first, "{\"prompt_tokens\":10,\"completion_tokens\":4,\"usage_estimated\":1}"))
            .isFalse();
        assertThat(settle(jdbc, first, "{\"prompt_tokens\":10,\"completion_tokens\":4}")).isTrue();
        assertThat(settle(jdbc, first, "{\"prompt_tokens\":10,\"completion_tokens\":4}")).isFalse();
        assertThat(jdbc.queryForObject("""
            SELECT charged_micro_cents AS cost FROM provider_spend_months WHERE provider_id = ?
            """, Long.class, provider)).isEqualTo(18);

        UUID previous = UUID.randomUUID();
        jdbc.update("""
            INSERT INTO provider_spend_months (provider_id, month_start, held_micro_cents)
            VALUES (?, date_trunc('month', current_timestamp AT TIME ZONE 'UTC') - interval '1 month', 1000)
            """, provider);
        jdbc.update("""
            INSERT INTO provider_spend_attempts
                (attempt_id, provider_id, month_start, model_id, priced_at, reserved_micro_cents)
            VALUES (?, ?, date_trunc('month', current_timestamp AT TIME ZONE 'UTC') - interval '1 month',
                ?, current_timestamp, 1000)
            """, previous, provider, model);
        assertThat(settle(jdbc, previous, "{\"prompt_tokens\":20,\"completion_tokens\":30}")).isTrue();
        assertThat(jdbc.queryForList("""
            SELECT charged_micro_cents FROM provider_spend_months WHERE provider_id = ? ORDER BY month_start
            """, Long.class, provider)).containsExactly(80L, 18L);

        UUID wrongBound = UUID.randomUUID();
        assertThat(reserve(jdbc, wrongBound, provider, model, PAYLOAD)).isEqualTo("reserved");
        assertThat(settle(jdbc, wrongBound, "{\"prompt_tokens\":5000000,\"completion_tokens\":4}")).isTrue();
        assertThat(jdbc.queryForObject("""
            SELECT BOOL_OR(bound_violation) FROM provider_spend_months WHERE provider_id = ?
            """, Boolean.class, provider)).isTrue();
        assertThat(reserve(jdbc, UUID.randomUUID(), provider, model, PAYLOAD)).isEqualTo("exhausted");

        checkAdminPolicy(jdbc, provider);
    }

    private static void checkAdminPolicy(JdbcTemplate jdbc, int provider) {
        var audit = mock(AuditLogService.class);
        var service = new ProviderSpendPolicyService(new NamedParameterJdbcTemplate(jdbc), audit);
        var controller = new ProviderSpendPolicyController(service);
        var currentMonth = jdbc.queryForObject("""
            SELECT date_trunc('month', CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date
            """, LocalDate.class);
        Map<String, Object> current = Map.of(
            "enabled", true, "effective_month", currentMonth.toString(), "limit_usd", new BigDecimal("0.01500000"));
        assertThat(controller.get(provider)).containsEntry("current", current)
            .containsEntry("scheduled", null).containsEntry("bound_violation", true);

        assertThatThrownBy(() -> controller.get(-1)).isInstanceOf(ResponseStatusException.class)
            .hasMessageContaining("404");
        for (Change invalid : List.of(new Change(null, null), new Change(true, null),
                new Change(true, new BigDecimal("-1")), new Change(true, new BigDecimal("0.000000001")),
                new Change(false, BigDecimal.ONE))) {
            assertThatThrownBy(() -> controller.schedule(provider, invalid))
                .isInstanceOf(ResponseStatusException.class).hasMessageContaining("400");
        }

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
            "limit_usd", new BigDecimal("123.00000001"));
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

    private static String reserve(JdbcTemplate jdbc, UUID attempt, int provider, int model, String body) {
        return jdbc.queryForObject("""
            SELECT logos_reserve_provider_spend(?, ?, ?, 'messages', CAST(? AS jsonb))
            """, String.class, attempt, provider, model, body);
    }

    private static boolean settle(JdbcTemplate jdbc, UUID attempt, String usage) {
        return Boolean.TRUE.equals(jdbc.queryForObject("""
            SELECT logos_settle_provider_spend(?, CAST(? AS jsonb), NULL)
            """, Boolean.class, attempt, usage));
    }
}
