package de.tum.cit.aet.logos.logoswebservice.deployments.dto;

import java.util.List;
import java.util.Map;
import java.util.Set;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * The messages between Logos and the deployers on the hosts. A deployer asks for orders and tells how they go; Logos
 * never calls a host.
 */
public final class DeployerApi {

    private DeployerApi() {
    }

    /** A request for orders. Logos holds it until an order appears or {@code waitS} seconds pass. */
    @JsonIgnoreProperties(ignoreUnknown = true)
    public record PollRequest(@JsonProperty("wait_s") Double waitS, String kind, String version) {}

    /**
     * What a deployer is told to do: the deployment id, the tag, who asked for it, and the kind of deployer the order is
     * for. The deployer refuses an order for the other kind: it shows that a host has the key of another host.
     */
    public record Order(
        String id,
        String tag,
        @JsonProperty("requested_by") String requestedBy,
        String kind
    ) {}

    public record PollResponse(List<Order> orders) {}

    @JsonIgnoreProperties(ignoreUnknown = true)
    public record ReportRequest(String kind, String version, List<ReportItem> deployments) {}

    /** The state of one deployment on a host, or the refusal of an order ({@code rejected}). */
    @JsonIgnoreProperties(ignoreUnknown = true)
    public record ReportItem(
        String id,
        String phase,
        String outcome,
        String failure,
        String message,
        @JsonProperty("requested_by") String requestedBy,
        @JsonProperty("requested_at") String requestedAt,
        @JsonProperty("started_at") String startedAt,
        @JsonProperty("finished_at") String finishedAt,
        List<Map<String, Object>> events,
        @JsonProperty("failure_logs") String failureLogs,
        @JsonProperty("drained_requests") Integer drainedRequests,
        @JsonProperty("drain_seconds") Integer drainSeconds,
        @JsonProperty("sole_replica_models") List<String> soleReplicaModels,
        String rejected
    ) {}

    /** The ids that Logos took. A deployer sends an item again until its id is here. */
    public record ReportResponse(Set<String> acked) {}
}
