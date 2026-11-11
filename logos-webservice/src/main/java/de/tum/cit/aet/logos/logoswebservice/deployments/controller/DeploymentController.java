package de.tum.cit.aet.logos.logoswebservice.deployments.controller;

import java.util.List;
import java.util.UUID;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestAttribute;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import de.tum.cit.aet.logos.logoswebservice.auth.AuthContext;
import de.tum.cit.aet.logos.logoswebservice.deployments.dto.DeployerDTO;
import de.tum.cit.aet.logos.logoswebservice.deployments.dto.DeploymentRequests;
import de.tum.cit.aet.logos.logoswebservice.deployments.dto.OverviewDTO;
import de.tum.cit.aet.logos.logoswebservice.deployments.dto.PlanDTO;
import de.tum.cit.aet.logos.logoswebservice.deployments.service.DeployerService;
import de.tum.cit.aet.logos.logoswebservice.deployments.service.DeploymentService;
import de.tum.cit.aet.logos.logoswebservice.deployments.service.DeploymentViews;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.Role;

/**
 * The deployments page: the overview, the history, starting and abandoning a deployment, and the deployers. Only Logos
 * admins may use it.
 */
@RestController
@RequestMapping("/deployments")
@PreAuthorize("hasAuthority('" + Role.Names.LOGOS_ADMIN + "')")
public class DeploymentController {

    private final DeploymentService deployments;
    private final DeployerService deployers;
    private final DeploymentViews views;

    public DeploymentController(DeploymentService deployments, DeployerService deployers, DeploymentViews views) {
        this.deployments = deployments;
        this.deployers = deployers;
        this.views = views;
    }

    @GetMapping("/overview")
    public OverviewDTO overview() {
        return deployments.overview();
    }

    @GetMapping("/plans")
    public List<PlanDTO> plans(@RequestParam(defaultValue = "50") int limit) {
        return deployments.plans(limit);
    }

    @GetMapping("/plans/{id}")
    public PlanDTO plan(@PathVariable UUID id) {
        return deployments.plan(id);
    }

    @PostMapping("/plans")
    public ResponseEntity<PlanDTO> start(
            @RequestBody DeploymentRequests.StartPlan body,
            @RequestAttribute("authContext") AuthContext auth) {
        PlanDTO plan = deployments.start(body.tag(), body.confirmation(), auth);
        return ResponseEntity.status(HttpStatus.CREATED).body(plan);
    }

    @PostMapping("/plans/{id}/abandon")
    public PlanDTO abandon(@PathVariable UUID id, @RequestAttribute("authContext") AuthContext auth) {
        return deployments.abandon(id, auth);
    }

    @GetMapping("/deployers")
    public List<DeployerDTO> deployers() {
        var now = java.time.Instant.now();
        return deployers.list().stream().map(deployer -> views.deployer(deployer, now)).toList();
    }

    /** Make a deployer. The key in the answer is shown once and cannot be read again. */
    @PostMapping("/deployers")
    public ResponseEntity<DeployerDTO.Created> createDeployer(
            @RequestBody DeploymentRequests.CreateDeployer body,
            @RequestAttribute("authContext") AuthContext auth) {
        DeployerService.Created created = deployers.create(body.name(), body.kind(), auth.userId());
        return ResponseEntity.status(HttpStatus.CREATED).body(created(created));
    }

    /** Give a deployer a new key. The old key stops working at once. */
    @PostMapping("/deployers/{id}/key")
    public DeployerDTO.Created replaceKey(@PathVariable UUID id) {
        return created(deployers.replaceKey(id));
    }

    @DeleteMapping("/deployers/{id}")
    public ResponseEntity<Void> deleteDeployer(@PathVariable UUID id) {
        deployers.delete(id);
        return ResponseEntity.noContent().build();
    }

    private DeployerDTO.Created created(DeployerService.Created created) {
        return new DeployerDTO.Created(views.deployer(created.deployer(), java.time.Instant.now()), created.key());
    }
}
