package de.tum.cit.aet.logos.logoswebservice.configuration;

import static org.assertj.core.api.Assertions.assertThat;

import java.sql.DriverManager;
import java.util.List;
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
import org.springframework.jdbc.datasource.DriverManagerDataSource;
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
