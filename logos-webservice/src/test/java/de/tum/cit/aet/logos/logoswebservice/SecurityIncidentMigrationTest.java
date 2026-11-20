package de.tum.cit.aet.logos.logoswebservice;

import static org.assertj.core.api.Assertions.assertThat;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.DriverManager;
import java.util.List;
import java.util.Objects;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import liquibase.resource.CompositeResourceAccessor;
import liquibase.resource.DirectoryResourceAccessor;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.SingleConnectionDataSource;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

@Testcontainers
class SecurityIncidentMigrationTest {

    @Container
    @SuppressWarnings("resource")
    static PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:17")
        .withDatabaseName("logosdb").withUsername("postgres").withPassword("postgres");

    @Test
    void securityUpgradePreservesRepositoryAndPricingHistory(@TempDir Path directory) throws Exception {
        String master;
        try (var stream = getClass().getResourceAsStream("/liquibase/changelog/master.xml")) {
            master = new String(Objects.requireNonNull(stream).readAllBytes(), StandardCharsets.UTF_8);
        }
        List<String> securityMigrations = List.of(
            "liquibase/changelog/080_security_incidents.xml",
            "liquibase/changelog/081_security_manual_updates.xml");
        for (String migration : securityMigrations) {
            master = master.replace("    <include file=\"" + migration + "\"/>\n", "");
            assertThat(master).doesNotContain(migration);
        }
        Path baseline = directory.resolve("liquibase/changelog/master-before-security.xml");
        Files.createDirectories(baseline.getParent());
        Files.writeString(baseline, master);
        migrate(directory, "liquibase/changelog/master-before-security.xml");

        try (var connection = DriverManager.getConnection(
                postgres.getJdbcUrl(), postgres.getUsername(), postgres.getPassword())) {
            var jdbc = new JdbcTemplate(new SingleConnectionDataSource(connection, true));
            List<String> previousHistory = previousHistory(jdbc);
            assertThat(jdbc.queryForList("SELECT DISTINCT filename FROM databasechangelog", String.class))
                .contains("liquibase/changelog/078_repository_coding_agent.xml",
                    "liquibase/changelog/079_gateway_pricing_performance.xml");
            String pricing = jdbc.queryForObject("SELECT pg_get_functiondef("
                + "'public.logos_price_usage(integer,integer,timestamptz,text,jsonb)'::regprocedure)", String.class);

            migrate(directory, "liquibase/changelog/master.xml");
            migrate(directory, "liquibase/changelog/master.xml");

            assertThat(previousHistory(jdbc)).containsExactlyElementsOf(previousHistory);
            assertThat(jdbc.queryForList("SELECT id FROM databasechangelog WHERE filename IN (?, ?) ORDER BY orderexecuted",
                String.class, securityMigrations.get(0), securityMigrations.get(1)))
                .containsExactly("080-security-incidents", "081-security-manual-updates");
            assertThat(jdbc.queryForObject("SELECT pg_get_functiondef("
                + "'public.logos_price_usage(integer,integer,timestamptz,text,jsonb)'::regprocedure)", String.class))
                .isEqualTo(pricing);
            assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM information_schema.columns "
                + "WHERE table_schema='public' AND table_name='security_incidents' AND column_name='manual_override'", Integer.class))
                .isEqualTo(1);
        }
    }

    private static List<String> previousHistory(JdbcTemplate jdbc) {
        return jdbc.queryForList("SELECT row_to_json(d)::text FROM databasechangelog d "
            + "WHERE filename NOT IN ('liquibase/changelog/080_security_incidents.xml', "
            + "'liquibase/changelog/081_security_manual_updates.xml') ORDER BY orderexecuted", String.class);
    }

    private static void migrate(Path directory, String master) throws Exception {
        try (var resources = new CompositeResourceAccessor(
                new DirectoryResourceAccessor(directory), new ClassLoaderResourceAccessor());
                var connection = DriverManager.getConnection(
                    postgres.getJdbcUrl(), postgres.getUsername(), postgres.getPassword());
                var liquibase = new Liquibase(master, resources, new JdbcConnection(connection))) {
            liquibase.update(new Contexts(), new LabelExpression());
        }
    }
}
