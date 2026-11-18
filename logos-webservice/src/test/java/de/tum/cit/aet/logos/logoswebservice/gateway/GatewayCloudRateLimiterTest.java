package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.clearInvocations;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;

import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicInteger;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.test.annotation.DirtiesContext;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
import org.springframework.web.server.ResponseStatusException;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;
import org.testcontainers.utility.DockerImageName;

import de.tum.cit.aet.logos.logoswebservice.identity.entity.ApiKeyType;

/**
 * Shared cloud RPM/TPM via Redis — every webservice replica sees the same window.
 */
@SpringBootTest
@Testcontainers
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
class GatewayCloudRateLimiterTest {

    private static final String REDIS_PASSWORD = "test-only-redis-password";

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    GatewayCloudRateLimiter rateLimiter;

    @MockitoSpyBean
    StringRedisTemplate redis;

    @MockitoSpyBean
    GatewayDeploymentRepository deployments;

    @MockitoBean
    JwtDecoder jwtDecoder;

    @Container
    @SuppressWarnings("resource")
    static PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:17")
            .withDatabaseName("logosdb")
            .withUsername("postgres")
            .withPassword("root");

    @Container
    @SuppressWarnings("resource")
    static GenericContainer<?> redisContainer = new GenericContainer<>(DockerImageName.parse("redis:7-alpine"))
            .withExposedPorts(6379)
            .withCommand("redis-server", "--requirepass", REDIS_PASSWORD);

    @DynamicPropertySource
    @SuppressWarnings("unused")
    static void configureProperties(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", postgres::getJdbcUrl);
        registry.add("spring.datasource.username", postgres::getUsername);
        registry.add("spring.datasource.password", postgres::getPassword);
        registry.add("spring.datasource.driver-class-name", () -> "org.postgresql.Driver");
        registry.add("spring.liquibase.enabled", () -> "true");
        registry.add("spring.liquibase.change-log", () -> "classpath:liquibase/changelog/master.xml");
        registry.add("spring.jpa.hibernate.ddl-auto", () -> "validate");
        registry.add("spring.data.redis.password", () -> REDIS_PASSWORD);
        registry.add("spring.data.redis.host", redisContainer::getHost);
        registry.add("spring.data.redis.port", () -> redisContainer.getMappedPort(6379));
        registry.add("spring.data.redis.timeout", () -> "1s");
        registry.add("logos.gateway.budget-reservation-reconcile-seconds", () -> "3600");
    }

    private static final AtomicInteger SEQ = new AtomicInteger(1);
    private static final byte[] BODY =
        "{\"model\":\"m\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}".getBytes(StandardCharsets.UTF_8);

    @BeforeEach
    void flushRedis() {
        var factory = redis.getConnectionFactory();
        if (factory == null) {
            return;
        }
        try (var connection = factory.getConnection()) {
            connection.serverCommands().flushAll();
        }
        clearInvocations(redis, deployments);
    }

    @Test
    void noLimitsConfigured_isANoOp() {
        GatewayKey key = seedKey(null, null);
        assertThatCode(() -> rateLimiter.enforce(key, BODY)).doesNotThrowAnyException();
    }

    @Test
    void sharedRpm_rejectsOnceTheWindowIsFull() {
        GatewayKey key = seedKey(2, null);
        assertThatCode(() -> rateLimiter.enforce(key, BODY)).doesNotThrowAnyException();
        assertThatCode(() -> rateLimiter.enforce(key, BODY)).doesNotThrowAnyException();
        assertThatThrownBy(() -> rateLimiter.enforce(key, BODY))
            .isInstanceOf(ResponseStatusException.class)
            .satisfies(e -> assertThatStatus((ResponseStatusException) e, HttpStatus.TOO_MANY_REQUESTS))
            .hasMessageContaining("RPM limit reached");
    }

    @Test
    void rpmOnly_doesNotRecordTpmClaims() {
        GatewayKey key = seedKey(10, null);
        assertThatCode(() -> rateLimiter.enforce(key, BODY)).doesNotThrowAnyException();
        assertThat(redis.opsForZSet().zCard("gw:tpm:" + key.id())).isZero();
        assertThat(redis.opsForValue().get("gw:tpm:sum:" + key.id())).isNull();
    }

    @Test
    void rpmOnly_prunesExpiredTpmClaimsAndReducesSum() {
        // Prior TPM-enabled admissions left claims + a running sum. While TPM is
        // disabled, RPM admissions must still subtract expired claim tokens from
        // the sum — otherwise re-enabling TPM false-429s on the stale excess.
        GatewayKey key = seedKey(10, null);
        String tpmKey = "gw:tpm:" + key.id();
        String tpmSumKey = "gw:tpm:sum:" + key.id();
        redis.opsForZSet().add(tpmKey, "stale-a:400", 1.0);
        redis.opsForZSet().add(tpmKey, "stale-b:100", 2.0);
        redis.opsForValue().set(tpmSumKey, "500");

        assertThatCode(() -> rateLimiter.enforce(key, BODY)).doesNotThrowAnyException();

        assertThat(redis.opsForZSet().zCard(tpmKey)).isZero();
        assertThat(redis.opsForValue().get(tpmSumKey)).isEqualTo("0");
        // RPM-only must not write new TPM claims.
        assertThat(redis.opsForZSet().range(tpmKey, 0, -1)).isNullOrEmpty();
    }

    @Test
    void sharedTpm_rejectsWhenEstimatesWouldExceedTheLimit() {
        // BODY estimate is body.length/4; pick a limit that admits two then refuses.
        int estimate = GatewayCloudRateLimiter.estimateTokens(BODY);
        GatewayKey key = seedKey(null, estimate * 2);
        assertThatCode(() -> rateLimiter.enforce(key, BODY)).doesNotThrowAnyException();
        assertThatCode(() -> rateLimiter.enforce(key, BODY)).doesNotThrowAnyException();
        assertThatThrownBy(() -> rateLimiter.enforce(key, BODY))
            .isInstanceOf(ResponseStatusException.class)
            .satisfies(e -> assertThatStatus((ResponseStatusException) e, HttpStatus.TOO_MANY_REQUESTS))
            .hasMessageContaining("TPM limit reached");
    }

    @Test
    void authenticatedTeamRpmUsesSnapshotWithoutAnotherLookup() {
        GatewayKey legacy = seedKey(null, null);
        setTeamLimits(legacy, 2, null);
        GatewayKey key = deployments.findActiveKey(legacy.keyValue()).orElseThrow();

        rateLimiter.enforce(key, BODY);
        rateLimiter.enforce(key, BODY);
        assertThatThrownBy(() -> rateLimiter.enforce(key, BODY))
            .isInstanceOf(ResponseStatusException.class)
            .satisfies(e -> assertThatStatus((ResponseStatusException) e, HttpStatus.TOO_MANY_REQUESTS));
        verify(deployments, never()).findTeamCloudRateLimits(legacy.teamId());
    }

    @Test
    void legacyKeyLooksUpTeamDefaultsOncePerAdmission() {
        GatewayKey legacy = seedKey(null, null);
        setTeamLimits(legacy, 2, null);
        rateLimiter.enforce(legacy, BODY);
        verify(deployments, times(1)).findTeamCloudRateLimits(legacy.teamId());
    }

    @Test
    void newAuthenticationSeesChangedTeamLimit() {
        GatewayKey legacy = seedKey(null, null);
        setTeamLimits(legacy, 1, null);
        GatewayKey first = deployments.findActiveKey(legacy.keyValue()).orElseThrow();
        rateLimiter.enforce(first, BODY);

        setTeamLimits(legacy, 2, null);
        GatewayKey next = deployments.findActiveKey(legacy.keyValue()).orElseThrow();
        assertThat(first.teamCloudLimits().rpm()).isEqualTo(1);
        assertThat(next.teamCloudLimits().rpm()).isEqualTo(2);
        rateLimiter.enforce(next, BODY);
        assertThatThrownBy(() -> rateLimiter.enforce(next, BODY))
            .isInstanceOf(ResponseStatusException.class)
            .satisfies(e -> assertThatStatus((ResponseStatusException) e, HttpStatus.TOO_MANY_REQUESTS));
    }

    @Test
    void authenticatedTeamTpmIsEnforced() {
        GatewayKey legacy = seedKey(null, null);
        setTeamLimits(legacy, null, GatewayCloudRateLimiter.estimateTokens(BODY));
        GatewayKey key = deployments.findActiveKey(legacy.keyValue()).orElseThrow();
        rateLimiter.enforce(key, BODY);
        assertThatThrownBy(() -> rateLimiter.enforce(key, BODY))
            .isInstanceOf(ResponseStatusException.class)
            .hasMessageContaining("TPM limit reached");
        verify(deployments, never()).findTeamCloudRateLimits(legacy.teamId());
    }

    @Test
    void nonpositiveKeySettingsDisablePositiveTeamDefaults() {
        for (String prefix : new String[] {"cloud_", ""}) {
            for (int value : new int[] {0, -1}) {
                GatewayKey legacy = seedKey(null, null);
                setTeamLimits(legacy, 1, 1);
                jdbc.update("UPDATE api_keys SET settings=CAST(? AS jsonb) WHERE id=?",
                    "{\"" + prefix + "rpm_limit\":" + value + ",\"" + prefix + "tpm_limit\":" + value + "}", legacy.id());
                GatewayKey key = deployments.findActiveKey(legacy.keyValue()).orElseThrow();
                rateLimiter.enforce(key, BODY);
                rateLimiter.enforce(key, BODY);
            }
        }
        verifyNoInteractions(redis);
    }

    @Test
    void cloudKeySettingsOverrideGenericKeyAndTeamLimits() {
        GatewayKey legacy = seedKey(null, null);
        setTeamLimits(legacy, 3, null);
        jdbc.update("UPDATE api_keys SET settings='{\"cloud_rpm_limit\":1,\"rpm_limit\":0}'::jsonb WHERE id=?", legacy.id());
        GatewayKey key = deployments.findActiveKey(legacy.keyValue()).orElseThrow();
        rateLimiter.enforce(key, BODY);
        assertThatThrownBy(() -> rateLimiter.enforce(key, BODY))
            .isInstanceOf(ResponseStatusException.class).hasMessageContaining("RPM limit reached");

        jdbc.update("UPDATE api_keys SET settings='{\"cloud_rpm_limit\":0,\"rpm_limit\":1}'::jsonb WHERE id=?", legacy.id());
        GatewayKey disabled = deployments.findActiveKey(legacy.keyValue()).orElseThrow();
        rateLimiter.enforce(disabled, BODY);
        rateLimiter.enforce(disabled, BODY);
    }

    @Test
    void genericKeySettingsOverrideTeamLimits() {
        GatewayKey legacy = seedKey(null, null);
        setTeamLimits(legacy, 3, null);
        jdbc.update("UPDATE api_keys SET settings='{\"rpm_limit\":1}'::jsonb WHERE id=?", legacy.id());
        GatewayKey key = deployments.findActiveKey(legacy.keyValue()).orElseThrow();
        rateLimiter.enforce(key, BODY);
        assertThatThrownBy(() -> rateLimiter.enforce(key, BODY))
            .isInstanceOf(ResponseStatusException.class).hasMessageContaining("RPM limit reached");
    }

    @Test
    void bothKeyQueriesMapNullableAndNonpositiveTeamDefaults() {
        Integer[][] cases = {{null, null, null, null}, {null, 200, null, 200},
            {0, -1, null, null}, {-2, 0, null, null}, {7, 9000, 7, 9000}};
        for (Integer[] values : cases) {
            GatewayKey legacy = seedKey(null, null);
            setTeamLimits(legacy, values[0], values[1]);
            GatewayKey direct = deployments.findActiveKey(legacy.keyValue()).orElseThrow();
            GatewayKey combined = deployments.authenticateAndFindDeploymentsForModel(legacy.keyValue(), "missing")
                .orElseThrow().key();
            for (GatewayKey key : new GatewayKey[] {direct, combined}) {
                assertThat(key.teamCloudLimits()).isNotNull();
                assertThat(key.teamCloudLimits().rpm()).isEqualTo(values[2]);
                assertThat(key.teamCloudLimits().tpm()).isEqualTo(values[3]);
            }
        }
    }

    @Test
    void bothKeyQueriesReturnKnownEmptyDefaultsWithoutATeam() {
        GatewayKey legacy = seedKey(null, null);
        jdbc.update("UPDATE api_keys SET team_id=NULL WHERE id=?", legacy.id());
        GatewayKey direct = deployments.findActiveKey(legacy.keyValue()).orElseThrow();
        GatewayKey combined = deployments.authenticateAndFindDeploymentsForModel(legacy.keyValue(), "missing")
            .orElseThrow().key();
        for (GatewayKey key : new GatewayKey[] {direct, combined}) {
            assertThat(key.teamId()).isNull();
            assertThat(key.teamCloudLimits()).isNotNull();
            assertThat(key.teamCloudLimits().rpm()).isNull();
            assertThat(key.teamCloudLimits().tpm()).isNull();
            rateLimiter.enforce(key, BODY);
        }
        verify(deployments, never()).findTeamCloudRateLimits(org.mockito.ArgumentMatchers.anyInt());
        verifyNoInteractions(redis);
    }

    @Test
    void bothKeyQueriesRejectMissingAndInactiveKeys() {
        assertThat(deployments.findActiveKey("missing")).isEmpty();
        assertThat(deployments.authenticateAndFindDeploymentsForModel("missing", "m")).isEmpty();
        GatewayKey key = seedKey(null, null);
        jdbc.update("UPDATE api_keys SET is_active=FALSE WHERE id=?", key.id());
        assertThat(deployments.findActiveKey(key.keyValue())).isEmpty();
        assertThat(deployments.authenticateAndFindDeploymentsForModel(key.keyValue(), "m")).isEmpty();
    }

    @Test
    void emptySnapshotSkipsUnavailableRedisAndPositiveSnapshotFailsClosed() {
        GatewayKey empty = deployments.findActiveKey(seedKey(null, null).keyValue()).orElseThrow();
        GatewayKey legacy = seedKey(null, null);
        setTeamLimits(legacy, 2, null);
        GatewayKey positive = deployments.findActiveKey(legacy.keyValue()).orElseThrow();
        redisContainer.getDockerClient().pauseContainerCmd(redisContainer.getContainerId()).exec();
        try {
            rateLimiter.enforce(empty, BODY);
            verifyNoInteractions(redis);
            assertThatThrownBy(() -> rateLimiter.enforce(positive, BODY))
                .isInstanceOf(ResponseStatusException.class)
                .satisfies(e -> assertThatStatus((ResponseStatusException) e, HttpStatus.SERVICE_UNAVAILABLE));
        } finally {
            redisContainer.getDockerClient().unpauseContainerCmd(redisContainer.getContainerId()).exec();
        }
    }

    @Test
    void combinedKeyQueryKeepsCustomPermissionsIndependentOfTeamDefaults() {
        GatewayKey key = seedKey(null, null);
        setTeamLimits(key, 7, 9000);
        int[] teamDeployment = seedDeployment(), customDeployment = seedDeployment();
        jdbc.update("INSERT INTO team_model_permissions(team_id,model_id) VALUES (?,?)", key.teamId(), teamDeployment[0]);
        jdbc.update("INSERT INTO team_provider_permissions(team_id,provider_id) VALUES (?,?)", key.teamId(), teamDeployment[1]);
        jdbc.update("INSERT INTO api_key_model_permissions(api_key_id,model_id) VALUES (?,?)", key.id(), customDeployment[0]);
        jdbc.update("INSERT INTO api_key_provider_permissions(api_key_id,provider_id) VALUES (?,?)", key.id(), customDeployment[1]);

        assertPermittedModel(key, teamDeployment[0], true);
        assertPermittedModel(key, customDeployment[0], false);
        jdbc.update("UPDATE api_keys SET use_custom_permissions=TRUE WHERE id=?", key.id());
        assertPermittedModel(key, teamDeployment[0], false);
        assertPermittedModel(key, customDeployment[0], true);
    }

    private void assertPermittedModel(GatewayKey key, int modelId, boolean allowed) {
        String name = jdbc.queryForObject("SELECT name FROM models WHERE id=?", String.class, modelId);
        GatewayAuthContext context = deployments.authenticateAndFindDeploymentsForModel(key.keyValue(), name).orElseThrow();
        assertThat(context.key().teamCloudLimits().rpm()).isEqualTo(7);
        assertThat(context.deploymentsForModel()).hasSize(allowed ? 1 : 0);
        assertThat(deployments.findPermittedDeploymentsForModel(key.id(), name)).hasSize(allowed ? 1 : 0);
        if (allowed) assertThat(context.deploymentsForModel().getFirst().modelId()).isEqualTo(modelId);
    }

    private int[] seedDeployment() {
        int model = jdbc.queryForObject("INSERT INTO models(name) VALUES (?) RETURNING id", Integer.class, "m-" + SEQ.getAndIncrement());
        int provider = jdbc.queryForObject("INSERT INTO providers(name,base_url,provider_type,auth_name,auth_format) "
            + "VALUES (?,'http://localhost','cloud','Authorization','Bearer %s') RETURNING id", Integer.class, "p-" + SEQ.getAndIncrement());
        jdbc.update("INSERT INTO model_provider(model_id,provider_id) VALUES (?,?)", model, provider);
        return new int[] {model, provider};
    }

    private void setTeamLimits(GatewayKey key, Integer rpm, Integer tpm) {
        jdbc.update("UPDATE teams SET default_cloud_rpm_limit=?, default_cloud_tpm_limit=? WHERE id=?", rpm, tpm, key.teamId());
    }

    private static void assertThatStatus(ResponseStatusException e, HttpStatus expected) {
        org.assertj.core.api.Assertions.assertThat(e.getStatusCode()).isEqualTo(expected);
    }

    private GatewayKey seedKey(Integer rpm, Integer tpm) {
        int teamId = jdbc.queryForObject(
            "INSERT INTO teams (name, default_cloud_rpm_limit, default_cloud_tpm_limit, "
            + "default_monthly_budget_micro_cents, team_monthly_budget_micro_cents) "
            + "VALUES (?, NULL, NULL, 1000000000, 1000000000) RETURNING id",
            Integer.class, "t-" + SEQ.getAndIncrement());
        String settings;
        if (rpm == null && tpm == null) {
            settings = "{}";
        } else if (rpm != null && tpm != null) {
            settings = "{\"cloud_rpm_limit\":" + rpm + ",\"cloud_tpm_limit\":" + tpm + "}";
        } else if (rpm != null) {
            settings = "{\"cloud_rpm_limit\":" + rpm + "}";
        } else {
            settings = "{\"cloud_tpm_limit\":" + tpm + "}";
        }
        String keyValue = "lg-" + SEQ.getAndIncrement();
        int apiKeyId = jdbc.queryForObject(
            "INSERT INTO api_keys (key_value, name, team_id, key_type, log, settings) "
            + "VALUES (?, ?, ?, 'application'::api_key_type_enum, 'BILLING'::logging_enum, "
            + "CAST(? AS jsonb)) RETURNING id",
            Integer.class, keyValue, "k-" + SEQ.get(), teamId, settings);
        return new GatewayKey(apiKeyId, keyValue, "k", ApiKeyType.application,
            teamId, null, "test", false, settings, 0, "BILLING");
    }
}
