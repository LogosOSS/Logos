package de.tum.cit.aet.logos.logoswebservice;

import java.sql.DriverManager;

import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.Test;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.SingleConnectionDataSource;
import org.testcontainers.containers.PostgreSQLContainer;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class AgentInferenceMigrationTest {
    @Test
    void preservesLegacySessionsAndGuardsPinnedGrantsThroughRollback() throws Exception {
        try (var postgres = new PostgreSQLContainer<>("postgres:17")) {
            postgres.start();
            try (var connection = DriverManager.getConnection(postgres.getJdbcUrl(), postgres.getUsername(), postgres.getPassword());
                    var resources = new ClassLoaderResourceAccessor();
                    var liquibase = new Liquibase("liquibase/changelog/111_agent_inference_settings.xml",
                        resources, new JdbcConnection(connection))) {
                var jdbc = new JdbcTemplate(new SingleConnectionDataSource(connection, true));
                jdbc.execute("CREATE TABLE agent_sessions (id INTEGER PRIMARY KEY, model TEXT)");
                jdbc.execute("INSERT INTO agent_sessions VALUES (1, 'existing-local')");
                connection.commit();
                liquibase.update(new Contexts(), new LabelExpression());

                assertThat(jdbc.queryForObject("SELECT model FROM agent_sessions WHERE id = 1", String.class))
                    .isEqualTo("existing-local");
                assertThat(jdbc.queryForObject("SELECT count(*) FROM agent_inference_settings", Integer.class)).isZero();
                jdbc.update("""
                    INSERT INTO agent_inference_settings (id, mode, model, provider_id, model_id, updated_by)
                    VALUES (1, 'cloud', 'fixture-model', 12, 34, 'fixture-admin')
                    """);
                assertThatThrownBy(() -> jdbc.update("""
                    INSERT INTO agent_sessions (id, model, inference_provider_id, inference_model_id, inference_client_tag)
                    VALUES (2, 'fixture-model', 12, 34, 'agent:10.0.1.2')
                    """)).isInstanceOf(DataIntegrityViolationException.class);
                connection.rollback();
                jdbc.update("""
                    INSERT INTO agent_sessions (id, model, inference_provider_id, inference_model_id,
                        inference_grant_hash, inference_client_tag)
                    VALUES (2, 'fixture-model', 12, 34, repeat('1', 64), 'agent:10.0.1.2')
                    """);
                connection.commit();
                assertThatThrownBy(() -> jdbc.update("""
                    INSERT INTO agent_sessions (id, model, inference_provider_id, inference_model_id, inference_grant_hash)
                    VALUES (3, 'fixture-model', 12, 34, repeat('1', 64))
                    """)).isInstanceOf(DataIntegrityViolationException.class);
                connection.rollback();
                liquibase.rollback(1, new Contexts(), new LabelExpression());
                assertThat(jdbc.queryForList("SELECT model FROM agent_sessions ORDER BY id", String.class))
                    .containsExactly("existing-local", "fixture-model");
                assertThat(jdbc.queryForObject("SELECT to_regclass('agent_inference_settings')::text", String.class)).isNull();
            }
        }
    }
}
