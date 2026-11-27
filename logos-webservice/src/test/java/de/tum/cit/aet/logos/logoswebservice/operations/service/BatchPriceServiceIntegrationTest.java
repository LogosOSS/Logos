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
    @Autowired BatchHistoricalCostService history;
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
    void historyUsesFinalizedOwnedKeyRowsAndSeparatesNativeFromReplay() {
        insertHistory(3101, 2101, 6101, "batch-1-a", "batch", 800_000, true, "success");
        insertHistory(3101, 2101, 6101, "batch-1-b", "batch", 1_000_000, true, "success");
        insertHistory(3101, 2101, 6101, "batch-1-c", "batch", 1_200_000, true, "success");
        insertHistory(3101, 2101, 6101, "normal-1", "default", 1_000_000, true, "success");
        insertHistory(3101, 2101, 6101, "normal-2", "default", 2_000_000, true, "success");
        insertHistory(3101, 2101, 6101, "normal-3", "default", 3_000_000, true, "success");
        insertHistory(3101, 2101, 6101, "pending", "default", 99_000_000, false, "success");
        insertHistory(3101, 2101, 6101, "failed", "default", 99_000_000, true, "error");
        insertHistory(3101, null, 6101, "other-tenant", "default", 99_000_000, true, "success");
        insertHistory(3101, 2101, 6102, "old-provider", "default", 99_000_000, true, "success");
        insertHistory(3101, 2101, 6101, "prior-window", "default", 99_000_000, true, "success");
        jdbc.update("UPDATE log_entry SET timestamp_request = NOW() - INTERVAL '31 days' WHERE request_id = 'prior-window'");

        insertHistory(3101, 2101, 6101, "prior-owner", "default", 99_000_000, true, "success");
        jdbc.update("UPDATE log_entry SET user_id = 1102 WHERE request_id = 'prior-owner'");
        insertHistory(3101, 2101, 6101, "cloud-unpriced", "default", 99_000_000, true, "success");
        jdbc.update("UPDATE log_entry SET settled_cost_micro_cents = NULL WHERE request_id = 'cloud-unpriced'");

        var rows = history.historicalCostsForKey(1101, 3101);
        assertThat(rows).hasSize(1);
        assertThat(rows.getFirst().model_name()).isEqualTo("test-model");
        assertThat(rows.getFirst().provider_average_usd()).isEqualByComparingTo("0.01");
        assertThat(rows.getFirst().logos_average_usd()).isEqualByComparingTo("0.02");
        assertThatThrownBy(() -> history.historicalCostsForKey(1102, 3101))
            .isInstanceOf(BatchService.KeyNotOwnedException.class);

        jdbc.update("DELETE FROM log_entry WHERE request_id = 'batch-1-c'");
        assertThat(history.historicalCostsForKey(1101, 3101).getFirst().provider_average_usd()).isNull();
    }

    @Test
    void onlyKnownUnmeteredLocalHistoryCanMakeAnUnknownCostZero() {
        jdbc.update("UPDATE providers SET provider_type = 'logosnode' WHERE id = 6101");
        for (int i = 0; i < 3; i++) {
            insertHistory(3101, 2101, 6101, "local-" + i, "default", 0, true, "success");
        }
        jdbc.update("UPDATE log_entry SET settled_cost_micro_cents = NULL WHERE request_id LIKE 'local-%'");
        var row = history.historicalCostsForKey(1101, 3101).getFirst();
        assertThat(row.logos_average_usd()).isEqualByComparingTo("0");
        assertThat(row.provider_average_usd()).isNull();
        jdbc.update("UPDATE providers SET provider_type = 'cloud' WHERE id = 6101");
        assertThat(history.historicalCostsForKey(1101, 3101).getFirst().logos_average_usd()).isNull();
    }

    private void insertHistory(int key, Integer team, int provider, String request, String tier, long microCents,
                               boolean finalized, String status) {
        jdbc.update("""
            INSERT INTO log_entry (api_key_id, team_id, user_id, model_id, provider_id, timestamp_request, timestamp_response,
                request_id, result_status, service_tier, cost_finalized, settled_cost_micro_cents)
            VALUES (?, ?, 1101, 5101, ?, NOW(), NOW(), ?, CAST(? AS result_status_enum), ?, ?, ?)
            """, key, team, provider, request, status, tier, finalized, microCents);
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
