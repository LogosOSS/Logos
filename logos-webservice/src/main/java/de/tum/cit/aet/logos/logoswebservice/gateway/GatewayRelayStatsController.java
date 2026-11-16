package de.tum.cit.aet.logos.logoswebservice.gateway;

import java.util.LinkedHashMap;
import java.util.Map;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * Per-replica relay occupancy for the gateway concurrency benchmark.
 *
 * <p>Not routed through Traefik; the benchmark reaches each replica on the
 * compose network (or via {@code docker run --network container:...}).
 */
@RestController
@RequestMapping("/internal/gateway_relay_stats")
public class GatewayRelayStatsController {

    private final GatewayInFlightLimit inFlightLimit;

    public GatewayRelayStatsController(GatewayInFlightLimit inFlightLimit) {
        this.inFlightLimit = inFlightLimit;
    }

    @GetMapping
    public Map<String, Object> stats() {
        Map<String, Object> snapshot = new LinkedHashMap<>(GatewayRelayOccupancy.snapshot());
        snapshot.put("in_flight", inFlightLimit.inFlight());
        snapshot.put("limit", inFlightLimit.limit());
        snapshot.put("limit_enabled", inFlightLimit.limit() > 0);
        snapshot.put("rejected_total", inFlightLimit.rejected());
        snapshot.put("executor_kind", "virtual-per-task");
        return snapshot;
    }

    @PostMapping("/reset")
    public ResponseEntity<Map<String, Object>> reset() {
        GatewayRelayOccupancy.resetStep();
        return ResponseEntity.ok(stats());
    }
}
