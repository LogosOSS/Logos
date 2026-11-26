package de.tum.cit.aet.logos.logoswebservice.configuration.dto;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;

/**
 * Read-only snapshot of who can actually reach a single model.
 *
 * <p>Manual model and provider grants are shown separately from the effective
 * deployment pairs. A team can also reach Azure deployments automatically
 * without a manual model grant.
 */
public record ModelAccessResponseDTO(
        ModelDTO model,
        List<HostingProviderDTO> providers,
        List<TeamAccessDTO> teams,
        List<KeyAccessDTO> apiKeys) {

    public record ModelDTO(
            Integer id,
            String name,
            String description,
            String tags,
            List<String> aliases,
            BigDecimal inputUsdPerMillion,
            BigDecimal outputUsdPerMillion,
            Integer maxInputTokens,
            Instant lastUsedAt) {}

    public record HostingProviderDTO(
            Integer providerId,
            String name,
            String providerType,
            String privacyLevel,
            Long requestCount,
            Instant lastRequestAt) {}

    public record TeamAccessDTO(
            Integer teamId,
            String teamName,
            boolean modelGrant,
            List<ProviderGrantDTO> providerGrants,
            List<Integer> automaticProviderIds,
            boolean effectiveAccess) {}

    public record KeyAccessDTO(
            Integer keyId,
            String keyName,
            Boolean isActive,
            Integer teamId,
            String teamName,
            boolean modelGrant,
            List<ProviderGrantDTO> providerGrants,
            boolean effectiveAccess) {}

    /** Grant state of one key/team for a single hosting provider of the model. */
    public record ProviderGrantDTO(Integer providerId, boolean granted) {}
}
