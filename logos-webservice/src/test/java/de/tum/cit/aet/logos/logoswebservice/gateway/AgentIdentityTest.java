package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

import de.tum.cit.aet.logos.logoswebservice.identity.repository.ApiKeyRepository;

@SpringBootTest
@Testcontainers
class AgentIdentityTest {
    @Container
    @SuppressWarnings("resource")
    static PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:17")
            .withDatabaseName("logosdb").withUsername("postgres").withPassword("root");

    @DynamicPropertySource
    static void configureProperties(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", postgres::getJdbcUrl);
        registry.add("spring.datasource.username", postgres::getUsername);
        registry.add("spring.datasource.password", postgres::getPassword);
        registry.add("spring.liquibase.enabled", () -> "true");
        registry.add("spring.liquibase.change-log", () -> "classpath:liquibase/changelog/master.xml");
        registry.add("spring.jpa.hibernate.ddl-auto", () -> "validate");
    }

    @Autowired JdbcTemplate jdbc;
    @Autowired GatewayDeploymentRepository gateway;
    @Autowired ApiKeyRepository keys;
    @MockitoBean JwtDecoder jwtDecoder;

    @Test
    void migrationCreatesOneUnprivilegedIdentityWhoseStoredValueIsNotABearer() {
        var rows = jdbc.queryForList("SELECT * FROM api_keys WHERE internal_service = 'agent'");
        assertThat(rows).hasSize(1);
        var row = rows.getFirst();
        assertThat(row.get("user_id")).isNull();
        assertThat(row.get("team_id")).isNull();
        assertThat(row.get("default_priority")).isEqualTo(1);
        assertThat(row.get("is_active")).isEqualTo(true);
        String stored = (String) row.get("key_value");
        assertThat(gateway.findActiveKey(stored)).isEmpty();
        assertThat(gateway.authenticateAndFindDeploymentsForModel(stored, "any-model")).isEmpty();
        assertThat(keys.findByKeyValueAndIsActiveTrue(stored)).isEmpty();
    }

    @Test
    void managedIdentityCannotAcquireHigherPriorityOrAnOwner() {
        assertThatThrownBy(() -> jdbc.update(
                "UPDATE api_keys SET default_priority = 10 WHERE internal_service = 'agent'"))
                .isInstanceOf(DataIntegrityViolationException.class);
        assertThatThrownBy(() -> jdbc.update(
                "UPDATE api_keys SET use_custom_permissions = false WHERE internal_service = 'agent'"))
                .isInstanceOf(DataIntegrityViolationException.class);
    }
}
