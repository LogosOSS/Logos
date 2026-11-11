package de.tum.cit.aet.logos.logoswebservice.deployments.controller;

import java.time.Duration;
import java.util.List;
import java.util.Set;

import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

import de.tum.cit.aet.logos.logoswebservice.deployments.DeploymentProperties;
import de.tum.cit.aet.logos.logoswebservice.deployments.dto.DeployerApi;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.Deployer;
import de.tum.cit.aet.logos.logoswebservice.deployments.service.DeployerService;
import de.tum.cit.aet.logos.logoswebservice.deployments.service.DeploymentReportService;

/**
 * The door for the deployers on the hosts. They authenticate with their own key ({@code Authorization: Bearer}), not
 * with a login. Logos never calls a host: a deployer asks for orders here and tells how they go.
 *
 * <p>A request for orders is held until an order appears or the wait is over (long polling). Each check re-reads the
 * database, because the order may have been made by another copy of the webservice. The request also shows that the
 * deployer is alive.
 */
@RestController
@RequestMapping("/deployments/deployer")
public class DeployerApiController {

    private static final double DEFAULT_WAIT_S = 25.0;
    private static final long CHECK_EVERY_MS = 1000;

    private final DeploymentProperties properties;
    private final DeployerService deployers;
    private final DeploymentReportService reports;

    public DeployerApiController(
            DeploymentProperties properties, DeployerService deployers, DeploymentReportService reports) {
        this.properties = properties;
        this.deployers = deployers;
        this.reports = reports;
    }

    @PostMapping("/poll")
    public DeployerApi.PollResponse poll(
            @RequestHeader(value = "Authorization", required = false) String authorization,
            @RequestBody(required = false) DeployerApi.PollRequest body) {
        Deployer deployer = authenticate(authorization);
        deployers.touch(deployer.getId(), body == null ? null : body.version());
        double wait = body == null || body.waitS() == null ? DEFAULT_WAIT_S : body.waitS();
        long holdMs = (long) (Math.max(0.0, wait) * 1000);
        long limitMs = Math.min(holdMs, properties.pollHoldMax().toMillis());
        long deadline = System.nanoTime() + Duration.ofMillis(limitMs).toNanos();
        while (true) {
            List<DeployerApi.Order> orders = reports.openOrders(deployer);
            if (!orders.isEmpty() || System.nanoTime() >= deadline) {
                return new DeployerApi.PollResponse(orders);
            }
            try {
                Thread.sleep(CHECK_EVERY_MS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return new DeployerApi.PollResponse(List.of());
            }
        }
    }

    @PostMapping("/report")
    public DeployerApi.ReportResponse report(
            @RequestHeader(value = "Authorization", required = false) String authorization,
            @RequestBody DeployerApi.ReportRequest body) {
        Deployer deployer = authenticate(authorization);
        deployers.touch(deployer.getId(), body == null ? null : body.version());
        Set<String> acked = reports.apply(deployer, body);
        return new DeployerApi.ReportResponse(acked);
    }

    private Deployer authenticate(String authorization) {
        return deployers.authenticate(authorization)
            .orElseThrow(() -> new ResponseStatusException(HttpStatus.UNAUTHORIZED, "Unknown deployer key"));
    }
}
