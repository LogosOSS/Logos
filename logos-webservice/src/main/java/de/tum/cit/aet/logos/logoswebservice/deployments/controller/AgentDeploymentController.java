package de.tum.cit.aet.logos.logoswebservice.deployments.controller;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.UUID;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

import de.tum.cit.aet.logos.logoswebservice.common.ForbiddenException;
import de.tum.cit.aet.logos.logoswebservice.deployments.DeploymentProperties;
import de.tum.cit.aet.logos.logoswebservice.deployments.dto.DeploymentRequests;
import de.tum.cit.aet.logos.logoswebservice.deployments.dto.PlanDTO;
import de.tum.cit.aet.logos.logoswebservice.deployments.service.DeploymentService;

/**
 * The door for a Logos agent that wants to see the build of a pull request run on this Logos. It authenticates with the
 * agent deployment key ({@code Authorization: Bearer}), not with a login. The key lets the agent start such a
 * deployment and look at the deployments it started, nothing else.
 *
 * <p>The door is shut unless the key is set (LOGOS_DEPLOYMENTS_AGENT_KEY), and it stays shut on a protected Logos.
 */
@RestController
@RequestMapping("/deployments/agent")
public class AgentDeploymentController {

    private static final String BEARER = "Bearer ";

    private final DeploymentProperties properties;
    private final DeploymentService deployments;

    public AgentDeploymentController(DeploymentProperties properties, DeploymentService deployments) {
        this.properties = properties;
        this.deployments = deployments;
    }

    @PostMapping("/plans")
    public ResponseEntity<PlanDTO> start(
            @RequestHeader(value = "Authorization", required = false) String authorization,
            @RequestBody DeploymentRequests.StartPlan body) {
        authenticate(authorization);
        return ResponseEntity.status(HttpStatus.CREATED).body(deployments.startForAgent(body.tag()));
    }

    @GetMapping("/plans/{id}")
    public PlanDTO plan(
            @RequestHeader(value = "Authorization", required = false) String authorization,
            @PathVariable UUID id) {
        authenticate(authorization);
        return deployments.planForAgent(id);
    }

    private void authenticate(String authorization) {
        if (properties.protect() || properties.agentKey() == null) {
            throw new ForbiddenException("This Logos takes no deployments from an agent.");
        }
        boolean known = authorization != null
            && authorization.regionMatches(true, 0, BEARER, 0, BEARER.length())
            // Compared in a time that does not tell how much of the key was right.
            && MessageDigest.isEqual(
                authorization.substring(BEARER.length()).strip().getBytes(StandardCharsets.UTF_8),
                properties.agentKey().getBytes(StandardCharsets.UTF_8));
        if (!known) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "Unknown agent deployment key");
        }
    }
}
