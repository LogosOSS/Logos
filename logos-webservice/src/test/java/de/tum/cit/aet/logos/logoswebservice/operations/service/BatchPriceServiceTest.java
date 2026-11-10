package de.tum.cit.aet.logos.logoswebservice.operations.service;

import java.time.Instant;
import java.util.List;
import java.util.Optional;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import de.tum.cit.aet.logos.logoswebservice.configuration.entity.TokenPrice;
import de.tum.cit.aet.logos.logoswebservice.configuration.entity.TokenType;
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.TokenPriceRepository;
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.TokenTypeRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.ApiKey;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.ApiKeyRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.ModelAccessProjection;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.*;

class BatchPriceServiceTest {
    private final ApiKeyRepository keys = mock(ApiKeyRepository.class);
    private final TokenPriceRepository prices = mock(TokenPriceRepository.class);
    private final TokenTypeRepository types = mock(TokenTypeRepository.class);
    private final BatchPriceService service = new BatchPriceService(keys, prices, types);
    private final ApiKey key = new ApiKey();

    @BeforeEach
    void setUp() {
        key.setUserId(7);
        key.setIsActive(true);
        key.setTeamId(3);
        key.setUseCustomPermissions(false);
        when(keys.findById(10)).thenReturn(Optional.of(key));
        TokenType input = mock(TokenType.class);
        when(input.getId()).thenReturn(1);
        when(input.getName()).thenReturn("billed_input_uncached");
        TokenType output = mock(TokenType.class);
        when(output.getId()).thenReturn(2);
        when(output.getName()).thenReturn("billed_output_text");
        when(types.findAll()).thenReturn(List.of(input, output));
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
        verifyNoInteractions(prices, types);
        verify(keys, never()).findAccessibleModelsByTeam(anyInt());
        verify(keys, never()).findAccessibleModelsByKey(anyInt());
    }

    @Test
    void teamPermissionsRestrictProvidersAndReturnOnlyCurrentStandardTokenRates() {
        var cloud = deployment(1, "cloud");
        when(keys.findAccessibleModelsByTeam(3)).thenReturn(List.of(cloud));
        TokenPrice batch = price(1, 1, 50, "2026-01-01T00:00:00Z");
        batch.setServiceTier("batch");
        TokenPrice image = price(1, 1, 9000, "2026-01-01T00:00:00Z");
        image.setUnit("image");
        when(prices.findByModelId(20)).thenReturn(List.of(
            price(1, 1, 800, "2020-01-01T00:00:00Z"),
            price(1, 1, 100, "2021-01-01T00:00:00Z"),
            price(1, 2, 300, "2021-01-01T00:00:00Z"),
            price(1, 1, 999, "2999-01-01T00:00:00Z"),
            price(2, 1, 5000, "2021-01-01T00:00:00Z"), batch, image));

        assertThat(service.pricesForKey(7, 10)).containsExactly(
            new BatchPriceService.ModelPrice("model", 100L, 300L));
        verify(keys, never()).findAccessibleModelsByKey(anyInt());
    }

    @Test
    void customPermissionsUseOnlyTheKeysDeploymentsAndHighestCurrentContextRates() {
        key.setUseCustomPermissions(true);
        var cloud = deployment(1, "cloud");
        var otherCloud = deployment(2, "cloud");
        when(keys.findAccessibleModelsByKey(10)).thenReturn(List.of(cloud, otherCloud));
        TokenPrice longContext = price(1, 1, 400, "2021-01-01T00:00:00Z");
        longContext.setMinContextTokens(200000L);
        when(prices.findByModelId(20)).thenReturn(List.of(
            price(1, 1, 100, "2021-01-01T00:00:00Z"), longContext,
            price(1, 2, 200, "2021-01-01T00:00:00Z"),
            price(2, 1, 300, "2021-01-01T00:00:00Z"),
            price(2, 2, 600, "2021-01-01T00:00:00Z")));

        assertThat(service.pricesForKey(7, 10)).containsExactly(
            new BatchPriceService.ModelPrice("model", 400L, 600L));
        verify(keys, never()).findAccessibleModelsByTeam(anyInt());
    }

    @Test
    void missingCloudPricesAreUnknownWhileLocalModelsAreFree() {
        var cloud = deployment(1, "cloud");
        when(keys.findAccessibleModelsByTeam(3)).thenReturn(List.of(cloud));
        when(prices.findByModelId(20)).thenReturn(List.of(price(1, 1, 100, "2021-01-01T00:00:00Z")));
        assertThat(service.pricesForKey(7, 10)).containsExactly(
            new BatchPriceService.ModelPrice("model", 100L, null));
        var local = deployment(1, "logosnode");
        when(keys.findAccessibleModelsByTeam(3)).thenReturn(List.of(local));
        assertThat(service.pricesForKey(7, 10)).containsExactly(
            new BatchPriceService.ModelPrice("model", 0L, 0L));
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

    private TokenPrice price(int providerId, int typeId, long rate, String date) {
        TokenPrice price = new TokenPrice();
        price.setModelId(20);
        price.setProviderId(providerId);
        price.setTypeId(typeId);
        price.setPricePerKUnit(rate);
        price.setValidFrom(Instant.parse(date));
        return price;
    }
}
