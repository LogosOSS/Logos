package de.tum.cit.aet.logos.logoswebservice.deployments.dto;

import java.util.List;
import java.util.UUID;

import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * What the page needs to show the environments an admin may deploy. When the feature is off ({@code enabled} false)
 * the page tells the admin that no deployer is set up.
 */
public record OverviewDTO(
    boolean enabled,
    String environment,
    List<Target> targets,
    @JsonProperty("suggested_tags") List<String> suggestedTags
) {

    /**
     * One environment: its deployers, the plan that runs there, and whether a new deployment can start.
     * {@code blockedReason} says why not, in a sentence for the admin.
     */
    public record Target(
        String name,
        boolean own,
        @JsonProperty("requires_confirmation") boolean requiresConfirmation,
        List<DeployerDTO> deployers,
        @JsonProperty("running_plan_id") UUID runningPlanId,
        /** How the latest deployment of the environment ended, or null if there is none. The switch shows it as a dot. */
        @JsonProperty("last_status") String lastStatus,
        @JsonProperty("can_start") boolean canStart,
        @JsonProperty("blocked_reason") String blockedReason
    ) {}
}
