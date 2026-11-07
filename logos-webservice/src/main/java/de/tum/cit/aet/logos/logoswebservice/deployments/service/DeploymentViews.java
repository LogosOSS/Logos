package de.tum.cit.aet.logos.logoswebservice.deployments.service;

import java.time.Instant;
import java.util.List;
import java.util.Map;

import org.springframework.stereotype.Component;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;

import de.tum.cit.aet.logos.logoswebservice.deployments.DeploymentProperties;
import de.tum.cit.aet.logos.logoswebservice.deployments.dto.DeployerDTO;
import de.tum.cit.aet.logos.logoswebservice.deployments.dto.PlanDTO;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.Deployer;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeploymentPlan;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeploymentStep;

/** Turns entities into what the page gets. The events and details are stored as JSON text and read here. */
@Component
public class DeploymentViews {

    private final ObjectMapper json;
    private final DeploymentProperties properties;

    public DeploymentViews(ObjectMapper json, DeploymentProperties properties) {
        this.json = json;
        this.properties = properties;
    }

    public PlanDTO brief(DeploymentPlan plan, List<DeploymentStep> steps) {
        return plan(plan, steps, false);
    }

    public PlanDTO detail(DeploymentPlan plan, List<DeploymentStep> steps) {
        return plan(plan, steps, true);
    }

    private PlanDTO plan(DeploymentPlan plan, List<DeploymentStep> steps, boolean detail) {
        List<DeploymentStep> ordered = steps.stream()
            .sorted(java.util.Comparator.comparingInt(DeploymentStep::getPosition).thenComparing(DeploymentStep::getDeployerName))
            .toList();
        return new PlanDTO(
            plan.getId(),
            plan.getNumber(),
            plan.getEnvironment(),
            plan.getTag(),
            plan.getStatus().name(),
            plan.getOrigin().name(),
            plan.getInitiator(),
            plan.getReason(),
            plan.getAbandonedBy(),
            plan.getCreatedAt(),
            plan.getFinishedAt(),
            plan.getLastActivityAt(),
            ordered.stream()
                .map(step -> new PlanDTO.StepBrief(step.getDeployerName(), step.getKind().name(), step.getStatus().name()))
                .toList(),
            detail ? ordered.stream().map(this::step).toList() : null);
    }

    public PlanDTO.StepDTO step(DeploymentStep step) {
        return new PlanDTO.StepDTO(
            step.getId(),
            step.getPosition(),
            step.getKind().name(),
            step.getDeployerName(),
            step.getStatus().name(),
            step.getPhase(),
            step.getFailure(),
            step.getMessage(),
            step.getStartedAt(),
            step.getFinishedAt(),
            step.getLastActivityAt(),
            list(step.getEvents()),
            step.getFailureLogs(),
            map(step.getDetails()));
    }

    public DeployerDTO deployer(Deployer deployer, Instant now) {
        boolean online = deployer.getLastSeenAt() != null
            && deployer.getLastSeenAt().isAfter(now.minus(properties.onlineAfter()));
        return new DeployerDTO(
            deployer.getId(),
            deployer.getName(),
            deployer.getEnvironment(),
            deployer.getKind().name(),
            online,
            deployer.getLastSeenAt(),
            deployer.getVersion(),
            deployer.getCreatedAt());
    }

    private List<Map<String, Object>> list(String raw) {
        if (raw == null || raw.isBlank()) {
            return List.of();
        }
        try {
            return json.readValue(raw, new TypeReference<List<Map<String, Object>>>() {});
        } catch (Exception e) {
            return List.of();
        }
    }

    private Map<String, Object> map(String raw) {
        if (raw == null || raw.isBlank()) {
            return Map.of();
        }
        try {
            return json.readValue(raw, new TypeReference<Map<String, Object>>() {});
        } catch (Exception e) {
            return Map.of();
        }
    }
}
