package de.tum.cit.aet.logos.logoswebservice.deployments.service;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.springframework.stereotype.Component;

import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeploymentPlan;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeploymentStep;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.Role;
import de.tum.cit.aet.logos.logoswebservice.notifications.dto.NotificationTargetDTO;
import de.tum.cit.aet.logos.logoswebservice.notifications.dto.PublishNotificationRequestDTO;
import de.tum.cit.aet.logos.logoswebservice.notifications.service.NotificationService;

/**
 * Tells the Logos admins when a deployer ended its step. The bell shows one item for each step. The key of the step
 * makes a repeated call, for example after a retry of a report, add no second item.
 */
@Component
public class DeploymentNotifier {

    private static final int MAX_BODY_CHARS = 100;

    private final NotificationService notifications;

    public DeploymentNotifier(NotificationService notifications) {
        this.notifications = notifications;
    }

    public void stepEnded(DeploymentPlan plan, DeploymentStep step) {
        boolean good = step.getStatus().isOk();
        String what = switch (step.getStatus()) {
            case succeeded -> "deployed";
            case unchanged -> "unchanged";
            case rolled_back -> "rolled back";
            default -> "failed";
        };
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("plan_id", plan.getId().toString());
        payload.put("step_id", step.getId().toString());
        payload.put("tag", plan.getTag());
        payload.put("deployer", step.getDeployerName());
        payload.put("outcome", step.getStatus().name());
        payload.put("failure", step.getFailure());
        payload.put("message", step.getMessage());
        notifications.publishOnce(
            new PublishNotificationRequestDTO(
                good ? "deployment" : "deployment_failed",
                step.getDeployerName() + ": " + plan.getTag() + " " + what,
                firstLine(step.getMessage()),
                payload,
                "webservice",
                null,
                List.of(NotificationTargetDTO.role(Role.LOGOS_ADMIN))),
            "deployment-step:" + step.getId());
    }

    private static String firstLine(String message) {
        if (message == null || message.isBlank()) {
            return null;
        }
        String line = message.strip().lines().findFirst().orElse("");
        return line.length() > MAX_BODY_CHARS ? line.substring(0, MAX_BODY_CHARS - 1) + "…" : line;
    }
}
