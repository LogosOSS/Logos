package de.tum.cit.aet.logos.logoswebservice.operations.service;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.annotation.Import;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.context.jdbc.Sql;
import org.springframework.transaction.annotation.Transactional;

import de.tum.cit.aet.logos.logoswebservice.TestContainersConfig;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

@SpringBootTest(properties = {
    "spring.liquibase.enabled=true",
    "spring.liquibase.change-log=classpath:liquibase/changelog/master.xml"
})
@Import(TestContainersConfig.class)
@Transactional
@Sql("/sql/seed-me-keys.sql")
class BatchPriceServiceIntegrationTest {
    @Autowired BatchPriceService service;
    @Autowired JdbcTemplate jdbc;
    @MockitoBean JwtDecoder jwtDecoder;

    @BeforeEach
    void seedPrices() {
        jdbc.update("UPDATE providers SET provider_type = 'cloud' WHERE id = 6101");
        jdbc.update("""
            INSERT INTO providers (id, name, base_url, provider_type, privacy_level, auth_name, auth_format)
            VALUES (6102, 'other-provider', 'https://example.com', 'cloud', 'LOCAL', 'Authorization', 'Bearer {}')
            """);
        jdbc.update("""
            INSERT INTO models (id, name, weight_latency, weight_accuracy, weight_cost, weight_quality)
            VALUES (5102, 'restricted-model', 0, 0, 0, 0)
            """);
        jdbc.update("INSERT INTO model_provider (provider_id, model_id) VALUES (6102, 5101), (6101, 5102)");
        for (int modelId : new int[] {5101, 5102}) {
            for (int providerId : new int[] {6101, 6102}) {
                for (String quantity : new String[] {"billed_input_uncached", "billed_output_text"}) {
                    int rate = providerId == 6101 ? 100 : 2000;
                    if (quantity.equals("billed_output_text")) rate *= 3;
                    jdbc.update("""
                        INSERT INTO token_prices (model_id, provider_id, type_id, price_per_k_unit, valid_from)
                        SELECT ?, ?, id, ?, '2021-01-01'::timestamptz FROM token_types WHERE name = ?
                        """, modelId, providerId, rate, quantity);
                }
            }
        }
    }

    @Test
    void inheritedPermissionsRequireBothModelAndProviderGrants() {
        assertThat(service.pricesForKey(1101, 3101)).containsExactly(
            new BatchPriceService.ModelPrice("test-model", 100L, 300L));
        assertThatThrownBy(() -> service.pricesForKey(1102, 3101))
            .isInstanceOf(BatchService.KeyNotOwnedException.class);
        jdbc.update("DELETE FROM team_provider_permissions WHERE team_id = 2101");
        assertThat(service.pricesForKey(1101, 3101)).isEmpty();
    }

    @Test
    void customPermissionsUseTheKeysModelAndProviderIntersection() {
        jdbc.update("UPDATE api_keys SET use_custom_permissions = true WHERE id = 3101");
        jdbc.update("INSERT INTO api_key_model_permissions (api_key_id, model_id) VALUES (3101, 5101)");
        jdbc.update("INSERT INTO api_key_provider_permissions (api_key_id, provider_id) VALUES (3101, 6102)");
        assertThat(service.pricesForKey(1101, 3101)).containsExactly(
            new BatchPriceService.ModelPrice("test-model", 2000L, 6000L));
        jdbc.update("DELETE FROM api_key_model_permissions WHERE api_key_id = 3101");
        assertThat(service.pricesForKey(1101, 3101)).isEmpty();
    }

    @Test
    void modelPricesWithNullProviderAreResolved() {
        jdbc.update("UPDATE token_prices SET provider_id = NULL WHERE model_id = 5101 AND provider_id = 6101");
        assertThat(service.pricesForKey(1101, 3101)).containsExactly(
            new BatchPriceService.ModelPrice("test-model", 100L, 300L));
    }

    @Test
    void providerPricesWithNullModelAreResolved() {
        jdbc.update("UPDATE token_prices SET model_id = NULL WHERE model_id = 5101 AND provider_id = 6101");
        assertThat(service.pricesForKey(1101, 3101)).containsExactly(
            new BatchPriceService.ModelPrice("test-model", 100L, 300L));
    }

    @Test
    void providerSpecificPricesTakePrecedenceOverMoreExpensiveFallbacks() {
        addPrices(5101, null, 9000, "2022-01-01", 200000, "default", "token");
        addPrices(null, 6101, 8000, "2022-01-01", 200000, "default", "token");
        addPrices(null, null, 7000, "2022-01-01", 200000, "default", "token");

        assertThat(service.pricesForKey(1101, 3101)).containsExactly(
            new BatchPriceService.ModelPrice("test-model", 100L, 300L));
    }

    @Test
    void modelSpecificFallbackTakesPrecedenceOverProviderOnlyPrice() {
        jdbc.update("DELETE FROM token_prices WHERE model_id = 5101 AND provider_id = 6101");
        addPrices(5101, null, 400, "2021-01-01", 0, "default", "token");
        addPrices(null, 6101, 8000, "2022-01-01", 200000, "default", "token");

        assertThat(service.pricesForKey(1101, 3101)).containsExactly(
            new BatchPriceService.ModelPrice("test-model", 400L, 1200L));
    }

    @Test
    void highestContextTierUsesLatestCurrentDefaultTokenPrice() {
        addPrices(5101, 6101, 9000, "2020-01-01", 200000, "default", "token");
        addPrices(5101, 6101, 400, "2021-01-01", 200000, "default", "token");
        addPrices(5101, 6101, 500, "2021-01-01", 200000, "default", "token");
        addPrices(5101, 6101, 8000, "2022-01-01", 0, "default", "token");
        addPrices(5101, 6101, 99000, "2999-01-01", 300000, "default", "token");
        addPrices(5101, 6101, 50, "2022-01-01", 300000, "batch", "token");
        addPrices(5101, 6101, 99000, "2022-01-01", 300000, "default", "image");

        assertThat(service.pricesForKey(1101, 3101)).containsExactly(
            new BatchPriceService.ModelPrice("test-model", 500L, 1500L));
    }

    @Test
    void maximumAcrossPermittedDeploymentsStillRequiresEveryDeploymentToBePriced() {
        jdbc.update("INSERT INTO team_provider_permissions (team_id, provider_id) VALUES (2101, 6102)");
        assertThat(service.pricesForKey(1101, 3101)).containsExactly(
            new BatchPriceService.ModelPrice("test-model", 2000L, 6000L));

        jdbc.update("DELETE FROM token_prices WHERE model_id = 5101 AND provider_id = 6102");
        assertThat(service.pricesForKey(1101, 3101)).containsExactly(
            new BatchPriceService.ModelPrice("test-model", null, null));

        jdbc.update("UPDATE providers SET provider_type = 'logosnode' WHERE id = 6102");
        assertThat(service.pricesForKey(1101, 3101)).containsExactly(
            new BatchPriceService.ModelPrice("test-model", 100L, 300L));
    }

    private void addPrices(Integer modelId, Integer providerId, long inputRate, String validFrom,
                           long contextTokens, String serviceTier, String unit) {
        jdbc.update("""
            INSERT INTO token_prices
                (model_id, provider_id, type_id, price_per_k_unit, valid_from, min_context_tokens, service_tier, unit)
            SELECT ?, ?, id, CASE WHEN name = 'billed_input_uncached' THEN ? ELSE ? END,
                   ?::timestamptz, ?, ?, ?
            FROM token_types WHERE name IN ('billed_input_uncached', 'billed_output_text')
            """, modelId, providerId, inputRate, inputRate * 3, validFrom, contextTokens, serviceTier, unit);
    }
}
