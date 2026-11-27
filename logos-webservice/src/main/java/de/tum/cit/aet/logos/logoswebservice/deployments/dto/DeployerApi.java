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

    /**
     * A request for orders. Logos holds it until an order appears or {@code waitS} seconds pass. A deployer that can
     * download images early sends {@code knownPrefetch}, the id of the last {@link Prefetch} it got (empty: none yet).
     * Logos then also answers when it has another one for the deployer. Without the field, Logos announces nothing.
     */
    @JsonIgnoreProperties(ignoreUnknown = true)
    public record PollRequest(
        @JsonProperty("wait_s") Double waitS,
        String kind,
        String version,
        @JsonProperty("known_prefetch") String knownPrefetch
    ) {}

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

    /**
     * A deployment that a worker gets an order for if the core of the plan ends well. It is no order: the deployer may
     * download the images of the tag in the meantime, and changes nothing else. The id is the id of the plan.
     */
    public record Prefetch(String id, String tag) {}

    /** The open orders, and what there is to download early, if anything. */
    public record PollResponse(List<Order> orders, Prefetch prefetch) {}

    @JsonIgnoreProperties(ignoreUnknown = true)
    public record ReportRequest(
        String kind,
        String version,
        List<ReportItem> deployments,
        @JsonProperty("early_download") EarlyDownload earlyDownload
    ) {}

    /**
     * How the early download of a {@link Prefetch} stands on a worker. The id is the one of the announcement. The state
     * is {@code downloading}, {@code downloaded} or {@code failed}, and the message says why it failed.
     */
    @JsonIgnoreProperties(ignoreUnknown = true)
    public record EarlyDownload(String id, String state, String message) {}

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
