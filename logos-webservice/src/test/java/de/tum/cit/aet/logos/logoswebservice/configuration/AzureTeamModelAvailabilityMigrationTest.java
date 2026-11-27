package de.tum.cit.aet.logos.logoswebservice.configuration;

import static org.assertj.core.api.Assertions.assertThat;

import java.sql.DriverManager;
import java.util.List;

import de.tum.cit.aet.logos.logoswebservice.gateway.GatewayDeployment;
import de.tum.cit.aet.logos.logoswebservice.gateway.GatewayDeploymentRepository;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.jdbc.datasource.SingleConnectionDataSource;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

@Testcontainers
class AzureTeamModelAvailabilityMigrationTest {
    @Container
    @SuppressWarnings("resource")
    static PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:17");

    @Test
    void grantsOnlySyncedAzurePairsToOptedInDefaultKeys() throws Exception {
        try (var connection = DriverManager.getConnection(
                postgres.getJdbcUrl(), postgres.getUsername(), postgres.getPassword());
                var resources = new ClassLoaderResourceAccessor();
                var liquibase = new Liquibase("liquibase/changelog/107_azure_team_model_availability.xml",
                    resources, new JdbcConnection(connection))) {
            var jdbc = new JdbcTemplate(new SingleConnectionDataSource(connection, true));
            jdbc.execute("""
                CREATE TABLE team_provider_permissions (team_id integer, provider_id integer,
                    PRIMARY KEY (team_id, provider_id));
                CREATE TABLE team_model_permissions (team_id integer, model_id integer,
                    PRIMARY KEY (team_id, model_id));
                CREATE TABLE teams (id integer PRIMARY KEY, privacy_scope text);
                CREATE TABLE providers (id integer PRIMARY KEY, provider_type text, cloud_provider_type text, base_url text);
                CREATE TABLE model_provider (model_id integer, provider_id integer, endpoint text,
                    PRIMARY KEY (model_id, provider_id));
                CREATE TABLE api_keys (id integer PRIMARY KEY, team_id integer, is_active boolean,
                    use_custom_permissions boolean, internal_service text);
                CREATE TABLE api_key_model_permissions (api_key_id integer, model_id integer,
                    PRIMARY KEY (api_key_id, model_id));
                CREATE TABLE api_key_provider_permissions (api_key_id integer, provider_id integer,
                    PRIMARY KEY (api_key_id, provider_id));
                CREATE VIEW allowed_team_provider_grants AS
                    SELECT tpp.* FROM team_provider_permissions tpp
                    JOIN teams t ON t.id = tpp.team_id
                    JOIN providers p ON p.id = tpp.provider_id
                    WHERE t.privacy_scope = 'WORLDWIDE' OR p.provider_type = 'logosnode';
                CREATE VIEW allowed_key_provider_grants AS SELECT * FROM api_key_provider_permissions;
                CREATE TABLE models (id integer PRIMARY KEY, name text);
                CREATE TABLE model_aliases (model_id integer, alias text);
                CREATE VIEW provider_processing_privacy AS
                    SELECT id AS provider_id, 'CLOUD_NOT_IN_EU_BY_US_PROVIDER' AS privacy_level FROM providers;

                INSERT INTO teams VALUES (1, 'WORLDWIDE'), (2, 'WORLDWIDE'), (3, 'LOCAL'), (4, 'WORLDWIDE');
                INSERT INTO providers VALUES (100, 'cloud', 'azure', 'https://first.example'),
                    (200, 'cloud', 'azure', 'https://second.example'),
                    (300, 'cloud', 'openai', 'https://other.example'), (400, 'logosnode', NULL, NULL);
                INSERT INTO team_provider_permissions VALUES
                    (1,100), (1,200), (1,300), (2,100), (3,100), (4,200);
                INSERT INTO team_model_permissions VALUES (1, 21);
                INSERT INTO model_provider VALUES
                    (11,100,'https://first.example/11'), (11,200,'https://second.example/11'),
                    (11,300,'https://other.example/11'), (21,100,'https://first.example/21'),
                    (31,100,'https://first.example/manual'), (31,400,NULL);
                INSERT INTO api_keys VALUES (101,1,true,false,NULL), (102,1,true,true,NULL),
                    (103,2,true,false,NULL), (104,3,true,false,NULL), (105,4,true,false,NULL),
                    (106,1,false,false,NULL), (107,1,true,false,'agent');
                INSERT INTO api_key_model_permissions VALUES (102,11);
                INSERT INTO api_key_provider_permissions VALUES (102,200);
                INSERT INTO models VALUES (11, 'same-model'), (21, 'manual-model'), (31, 'not-synced');
                INSERT INTO model_aliases VALUES (11, 'same-alias');
                ALTER TABLE teams ADD COLUMN default_cloud_rpm_limit integer, ADD COLUMN default_cloud_tpm_limit integer;
                ALTER TABLE model_provider ADD COLUMN api_key text;
                ALTER TABLE providers ADD COLUMN name text,
                    ADD COLUMN auth_name text, ADD COLUMN auth_format text, ADD COLUMN api_key text;
                ALTER TABLE api_keys ADD COLUMN key_value text, ADD COLUMN name text,
                    ADD COLUMN key_type text DEFAULT 'developer', ADD COLUMN user_id integer,
                    ADD COLUMN environment text, ADD COLUMN settings text,
                    ADD COLUMN default_priority integer, ADD COLUMN log text;
                UPDATE api_keys SET key_value = 'test-key-' || id;
                """);
            connection.commit();
            liquibase.update(new Contexts(), new LabelExpression());

            assertThat(routes(jdbc, 101)).containsExactly("21:100");
            jdbc.update("""
                UPDATE model_provider mp SET azure_synced_endpoint = endpoint, azure_synced_provider_url = p.base_url
                FROM providers p WHERE mp.provider_id = p.id AND model_id = 11
                """);
            jdbc.update("""
                INSERT INTO team_azure_model_auto_access VALUES (1,100), (1,300), (3,100)
                """);

            assertThat(routes(jdbc, 101)).containsExactly("11:100", "21:100");
            assertThat(routes(jdbc, 102)).containsExactly("11:200");
            var gateway = new GatewayDeploymentRepository(new NamedParameterJdbcTemplate(jdbc));
            assertThat(gateway.authenticateAndFindDeploymentsForModel("test-key-101", "same-alias")
                    .orElseThrow().deploymentsForModel())
                .extracting(GatewayDeployment::providerId).containsExactly(100);
            assertThat(gateway.findPermittedDeploymentsForModel(102, "same-model"))
                .extracting(GatewayDeployment::providerId).containsExactly(200);
            assertThat(gateway.findPermittedDeploymentsForModel(103, "same-model")).isEmpty();
            assertThat(gateway.authenticateAndFindDeploymentsForModel("test-key-106", "same-model")).isEmpty();
            for (int id : new int[] {103, 104, 105, 106, 107}) {
                assertThat(routes(jdbc, id)).isEmpty();
            }
            jdbc.update("""
                UPDATE model_provider mp SET azure_synced_endpoint = endpoint, azure_synced_provider_url = p.base_url
                FROM providers p WHERE mp.provider_id = p.id AND model_id = 21
                """);
            assertThat(routes(jdbc, 101)).containsExactly("11:100", "21:100");

            // A manually edited endpoint doesn't inherit Azure provenance.
            jdbc.update("UPDATE model_provider SET endpoint = 'https://other.example/11' WHERE model_id = 11 AND provider_id = 100");
            assertThat(routes(jdbc, 101)).containsExactly("21:100");
            jdbc.update("""
                UPDATE model_provider SET endpoint = azure_synced_endpoint WHERE model_id = 11 AND provider_id = 100
                """);
            assertThat(routes(jdbc, 101)).containsExactly("11:100", "21:100");

            jdbc.update("UPDATE providers SET base_url = 'https://new.example' WHERE id = 100");
            assertThat(routes(jdbc, 101)).containsExactly("21:100");
            jdbc.update("UPDATE providers SET base_url = 'https://first.example' WHERE id = 100");
            jdbc.update("UPDATE teams SET privacy_scope = 'LOCAL' WHERE id = 1");
            assertThat(routes(jdbc, 101)).isEmpty();
            jdbc.update("UPDATE teams SET privacy_scope = 'WORLDWIDE' WHERE id = 1");
            jdbc.update("DELETE FROM team_azure_model_auto_access WHERE team_id = 1");
            assertThat(routes(jdbc, 101)).containsExactly("21:100");
            assertThat(routes(jdbc, 102)).containsExactly("11:200");
            assertThat(gateway.findPermittedDeploymentsForModel(101, "same-model")).isEmpty();
            assertThat(gateway.findPermittedDeploymentsForModel(101, "manual-model"))
                .extracting(GatewayDeployment::providerId).containsExactly(100);

            verifySingleKeyLookupIsScopedToItsTeam(jdbc);

            connection.commit();
            liquibase.rollback(1, new Contexts(), new LabelExpression());
            assertThat(jdbc.queryForObject("SELECT count(*) FROM team_model_permissions", Integer.class)).isEqualTo(1);
        }
    }

    /** A big catalogue for other teams must not be sorted/deduplicated during one key's request. */
    private static void verifySingleKeyLookupIsScopedToItsTeam(JdbcTemplate jdbc) {
        jdbc.execute("""
            INSERT INTO teams SELECT id, 'WORLDWIDE' FROM generate_series(1001, 3500) id;
            INSERT INTO team_provider_permissions
                SELECT id, provider_id FROM generate_series(1001, 3500) id
                CROSS JOIN unnest(ARRAY[100, 200, 400]) provider_id;
            INSERT INTO model_provider (model_id, provider_id)
                SELECT model_id, 400 FROM generate_series(601, 840) model_id;
            INSERT INTO team_model_permissions
                SELECT id, model_id FROM generate_series(1001, 3500) id
                CROSS JOIN generate_series(601, 624) model_id;
            INSERT INTO api_keys SELECT id, id, true, false, NULL, 'test-key-' || id
                FROM generate_series(1001, 3500) id;
            INSERT INTO team_provider_permissions VALUES (1, 400);
            INSERT INTO team_model_permissions VALUES (1, 601);
            ANALYZE teams, providers, model_provider, team_provider_permissions, team_model_permissions, api_keys;
            """);

        assertThat(routes(jdbc, 101)).containsExactly("21:100", "601:400");
        String plan = String.join("\n", jdbc.queryForList("""
            EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
            SELECT model_id, provider_id
            FROM allowed_key_model_deployments
            WHERE api_key_id = 101 AND model_id = 601 AND provider_id = 400
            """, String.class));
        // A set-level distinct would process other teams even for this point lookup.
        assertThat(plan).doesNotContain("HashAggregate", "GroupAggregate", "Unique");
        assertThat(plan).containsPattern("Index Cond:.*team_id =");
        jdbc.update("DELETE FROM team_model_permissions WHERE team_id > 1000 OR model_id = 601");
    }

    private static List<String> routes(JdbcTemplate jdbc, int keyId) {
        return jdbc.queryForList("""
            SELECT model_id || ':' || provider_id FROM allowed_key_model_deployments
            WHERE api_key_id = ? ORDER BY model_id, provider_id
            """, String.class, keyId);
    }
}
