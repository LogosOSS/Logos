package de.tum.cit.aet.logos.logoswebservice.deployments.dto;

import java.util.List;

import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * What the page needs: the deployers, and whether a new deployment can start. {@code blockedReason} says why not, in a
 * sentence for the admin. {@code confirmationWord} is what the admin has to type to start a deployment on a protected
 * Logos, or null if this Logos needs no confirmation.
 */
public record OverviewDTO(
    @JsonProperty("confirmation_word") String confirmationWord,
    List<DeployerDTO> deployers,
    @JsonProperty("can_start") boolean canStart,
    @JsonProperty("blocked_reason") String blockedReason,
    @JsonProperty("suggested_tags") List<String> suggestedTags
) {}
