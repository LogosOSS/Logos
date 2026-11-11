package de.tum.cit.aet.logos.logoswebservice.deployments.dto;

import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * A deployment plan as the page shows it. The history lists plans with {@link StepBrief} dots; the detail view has the
 * full {@link StepDTO} of every deployer.
 */
public record PlanDTO(
    UUID id,
    long number,
    String tag,
    String status,
    String initiator,
    String reason,
    @JsonProperty("abandoned_by") String abandonedBy,
    @JsonProperty("created_at") Instant createdAt,
    @JsonProperty("finished_at") Instant finishedAt,
    @JsonProperty("last_activity_at") Instant lastActivityAt,
    List<StepBrief> steps,
    @JsonProperty("step_details") List<StepDTO> stepDetails
) {

    /** One dot of the history row: which deployer, and how its step stands. */
    public record StepBrief(String name, String kind, String status) {}

    /** The work of one deployer in a plan, with what it reported. */
    public record StepDTO(
        UUID id,
        int position,
        String kind,
        @JsonProperty("deployer_name") String deployerName,
        String status,
        String phase,
        String failure,
        String message,
        @JsonProperty("started_at") Instant startedAt,
        @JsonProperty("finished_at") Instant finishedAt,
        @JsonProperty("last_activity_at") Instant lastActivityAt,
        List<Map<String, Object>> events,
        @JsonProperty("failure_logs") String failureLogs,
        Map<String, Object> details
    ) {}
}
