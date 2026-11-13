package de.tum.cit.aet.logos.logoswebservice.configuration;

import static org.assertj.core.api.Assertions.assertThat;

import java.sql.DriverManager;

import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.SingleConnectionDataSource;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

@Testcontainers
class ProviderCapacityMigrationTest {

    @Container
    @SuppressWarnings("resource")
    static PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:17");

    @Test
    void upgradePreservesConfiguredValuesAndMakesNewCapacityOptional() throws Exception {
        try (var connection = DriverManager.getConnection(
                postgres.getJdbcUrl(), postgres.getUsername(), postgres.getPassword());
                var resources = new ClassLoaderResourceAccessor();
                var liquibase = new Liquibase("liquibase/changelog/072_optional_provider_capacity.xml",
                    resources, new JdbcConnection(connection))) {
            var jdbc = new JdbcTemplate(new SingleConnectionDataSource(connection, true));
            jdbc.execute("""
                CREATE TABLE providers (
                    id integer PRIMARY KEY,
                    provider_type text NOT NULL,
                    parallel_capacity integer DEFAULT 20
                )
                """);
            jdbc.execute("""
                INSERT INTO providers VALUES
                    (1, 'cloud', 20), (2, 'cloud', 200), (3, 'cloud', 900), (4, 'cloud', NULL),
                    (5, 'logosnode', 20), (6, 'logosnode', 200), (7, 'logosnode', NULL)
                """);
            connection.commit();
            var configured = jdbc.queryForList("SELECT * FROM providers ORDER BY id");

            liquibase.update(new Contexts(), new LabelExpression());
            liquibase.update(new Contexts(), new LabelExpression());

            assertThat(jdbc.queryForList("SELECT * FROM providers ORDER BY id")).isEqualTo(configured);
            jdbc.execute("INSERT INTO providers (id, provider_type) VALUES (8, 'cloud'), (9, 'logosnode')");
            assertThat(jdbc.queryForList("SELECT parallel_capacity FROM providers WHERE id >= 8", Integer.class))
                .containsExactly(null, null);
            jdbc.update("UPDATE providers SET parallel_capacity = 200 WHERE id = 8");
            assertThat(jdbc.queryForObject("SELECT parallel_capacity FROM providers WHERE id = 8", Integer.class))
                .isEqualTo(200);
            jdbc.update("UPDATE providers SET parallel_capacity = NULL WHERE id = 8");
            connection.commit();

            liquibase.rollback(1, new Contexts(), new LabelExpression());

            assertThat(jdbc.queryForList("SELECT * FROM providers WHERE id < 8 ORDER BY id")).isEqualTo(configured);
            assertThat(jdbc.queryForObject("SELECT parallel_capacity FROM providers WHERE id = 8", Integer.class))
                .isNull();
            jdbc.execute("INSERT INTO providers (id, provider_type) VALUES (10, 'cloud')");
            assertThat(jdbc.queryForObject("SELECT parallel_capacity FROM providers WHERE id = 10", Integer.class))
                .isEqualTo(20);
        }
    }
}
