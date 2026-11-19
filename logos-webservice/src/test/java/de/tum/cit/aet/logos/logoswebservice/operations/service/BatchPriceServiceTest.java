package de.tum.cit.aet.logos.logoswebservice.operations.service;

import java.util.List;
import java.util.Optional;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import de.tum.cit.aet.logos.logoswebservice.configuration.repository.TokenPriceRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.ApiKey;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.ApiKeyRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.ModelAccessProjection;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.*;

class BatchPriceServiceTest {
    private final ApiKeyRepository keys = mock(ApiKeyRepository.class);
    private final TokenPriceRepository prices = mock(TokenPriceRepository.class);
    private final BatchPriceService service = new BatchPriceService(keys, prices);
    private final ApiKey key = new ApiKey();

    @BeforeEach
    void setUp() {
        key.setUserId(7);
        key.setIsActive(true);
        key.setTeamId(3);
        key.setUseCustomPermissions(false);
        when(keys.findById(10)).thenReturn(Optional.of(key));
    }

    @Test
    void refusesMissingInactiveAndOtherUsersKeysBeforeReadingPrices() {
        when(keys.findById(10)).thenReturn(Optional.empty());
        assertDenied();
        when(keys.findById(10)).thenReturn(Optional.of(key));
        key.setIsActive(false);
        assertDenied();
        key.setIsActive(true);
        key.setUserId(8);
        assertDenied();
        verifyNoInteractions(prices);
        verify(keys, never()).findAccessibleModelsByTeam(anyInt());
        verify(keys, never()).findAccessibleModelsByKey(anyInt());
    }

    @Test
    void teamPermissionsResolveOnlyPermittedDeploymentsAtTheHighestContextTier() {
        var cloud = deployment(1, "cloud");
        when(keys.findAccessibleModelsByTeam(3)).thenReturn(List.of(cloud));
        resolvedPrices(1, 100L, 300L);

        assertThat(service.pricesForKey(7, 10)).containsExactly(
            new BatchPriceService.ModelPrice("model", 100L, 300L));
        verify(prices).resolveCurrentDefaultUnitPrice("billed_input_uncached", 20, 1, Long.MAX_VALUE);
        verify(prices).resolveCurrentDefaultUnitPrice("billed_output_text", 20, 1, Long.MAX_VALUE);
        verifyNoMoreInteractions(prices);
        verify(keys, never()).findAccessibleModelsByKey(anyInt());
    }

    @Test
    void customPermissionsTakeTheMaximumResolvedRateAcrossTheKeysDeployments() {
        key.setUseCustomPermissions(true);
        var deployments = List.of(deployment(1, "cloud"), deployment(2, "cloud"));
        when(keys.findAccessibleModelsByKey(10)).thenReturn(deployments);
        resolvedPrices(1, 400L, 200L);
        resolvedPrices(2, 300L, 600L);

        assertThat(service.pricesForKey(7, 10)).containsExactly(
            new BatchPriceService.ModelPrice("model", 400L, 600L));
        verify(keys, never()).findAccessibleModelsByTeam(anyInt());
    }

    @Test
    void missingCloudPriceRemainsUnknownDespiteAnotherPricedDeployment() {
        var deployments = List.of(deployment(1, "cloud"), deployment(2, "cloud"));
        when(keys.findAccessibleModelsByTeam(3)).thenReturn(deployments);
        resolvedPrices(1, 100L, 300L);
        resolvedPrices(2, 200L, null);
        assertThat(service.pricesForKey(7, 10)).containsExactly(
            new BatchPriceService.ModelPrice("model", 200L, null));

        resolvedPrices(2, null, 600L);
        assertThat(service.pricesForKey(7, 10)).containsExactly(
            new BatchPriceService.ModelPrice("model", null, 600L));
    }

    @Test
    void localModelsAreFreeWithoutResolvingPrices() {
        var local = deployment(1, "logosnode");
        when(keys.findAccessibleModelsByTeam(3)).thenReturn(List.of(local));
        assertThat(service.pricesForKey(7, 10)).containsExactly(
            new BatchPriceService.ModelPrice("model", 0L, 0L));
        verifyNoInteractions(prices);
    }

    @Test
    void noModelPermissionsExposeNoPrices() {
        when(keys.findAccessibleModelsByTeam(3)).thenReturn(List.of());
        assertThat(service.pricesForKey(7, 10)).isEmpty();
        verifyNoInteractions(prices);
    }

    private void assertDenied() {
        assertThatThrownBy(() -> service.pricesForKey(7, 10))
            .isInstanceOf(BatchService.KeyNotOwnedException.class);
    }

    private ModelAccessProjection deployment(int providerId, String providerType) {
        var row = mock(ModelAccessProjection.class);
        when(row.getModelId()).thenReturn(20);
        when(row.getModelName()).thenReturn("model");
        when(row.getProviderId()).thenReturn(providerId);
        when(row.getProviderType()).thenReturn(providerType);
        return row;
    }

    private void resolvedPrices(int providerId, Long input, Long output) {
        when(prices.resolveCurrentDefaultUnitPrice("billed_input_uncached", 20, providerId, Long.MAX_VALUE))
            .thenReturn(input);
        when(prices.resolveCurrentDefaultUnitPrice("billed_output_text", 20, providerId, Long.MAX_VALUE))
            .thenReturn(output);
    }
}
