package de.tum.cit.aet.logos.logoswebservice.deployments.dto;

import java.util.List;

import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * What the page needs: the deployers, and whether a new deployment can start. {@code blockedReason} says why not, in a
 * sentence for the admin. {@code requiresConfirmation} is true on a protected Logos: the page shows a warning and asks
 * the admin to confirm in a dialog before it starts a deployment. The dialog is a step of the page only; the Logos does
 * not check it.
 */
public record OverviewDTO(
    @JsonProperty("requires_confirmation") boolean requiresConfirmation,
    List<DeployerDTO> deployers,
    @JsonProperty("can_start") boolean canStart,
    @JsonProperty("blocked_reason") String blockedReason,
    @JsonProperty("suggested_tags") List<String> suggestedTags
) {}
