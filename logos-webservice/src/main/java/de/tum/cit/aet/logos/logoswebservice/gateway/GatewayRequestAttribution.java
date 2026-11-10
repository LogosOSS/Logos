package de.tum.cit.aet.logos.logoswebservice.gateway;

/**
 * Optional workflow / SLO attribution parsed from inbound inference headers.
 *
 * <p>Applications send {@code X-Logos-Workflow-Tag} so Logos can attribute
 * traffic to a workflow step and (on the orchestrator path) elevate queue
 * priority. The SLO is the one stored for that step, never one the request
 * names. Direct-cloud gateway rows store the same columns for historic
 * workflow benchmarks.
 */
public record GatewayRequestAttribution(
    String workflowTag,
    Integer workflowId,
    Integer workflowStepId,
    String requestSlo
) {
    public static final GatewayRequestAttribution EMPTY =
        new GatewayRequestAttribution(null, null, null, null);
}
