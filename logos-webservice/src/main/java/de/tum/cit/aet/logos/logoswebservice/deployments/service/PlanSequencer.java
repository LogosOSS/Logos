package de.tum.cit.aet.logos.logoswebservice.deployments.service;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeployerKind;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeploymentPlan;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeploymentStep;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.PlanStatus;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.StepStatus;

/**
 * The rules that move a plan on. A plan has two stages: the core, then all workers at once (the orchestrator lets only
 * one worker out of service at a time). The workers are released when the core ended well. If the core did not end well
 * they never start, because a worker waits for the profiles of the core and a broken core is no place to deploy to.
 * A worker that fails does not stop the others.
 *
 * <p>The class changes the entities it is given and touches no database, so the rules can be tested alone.
 */
public final class PlanSequencer {

    /** The {@code failure} of a step that Logos ended because the deployer was silent. */
    public static final String TIMED_OUT = "timed_out";

    private PlanSequencer() {
    }

    /** Move a plan on after one of its steps changed. */
    public static void advance(DeploymentPlan plan, List<DeploymentStep> steps, Instant now) {
        if (plan.getStatus() != PlanStatus.running || steps.isEmpty()) {
            return;
        }
        List<DeploymentStep> core = steps.stream().filter(step -> step.getKind() == DeployerKind.core).toList();
        if (!core.isEmpty() && core.stream().allMatch(step -> step.getStatus().isTerminal())) {
            boolean coreOk = core.stream().allMatch(step -> step.getStatus().isOk());
            for (DeploymentStep step : steps) {
                if (step.getStatus() != StepStatus.waiting) {
                    continue;
                }
                step.setLastActivityAt(now);
                if (coreOk) {
                    step.setStatus(StepStatus.ordered);
                } else {
                    cancel(step, "Not started: the core did not succeed.", now);
                }
            }
        }
        if (allTerminal(steps)) {
            finish(plan, steps, now);
        }
    }

    /** A step that ended after its plan was closed changes what the plan shows, unless an admin abandoned the plan. */
    public static void refreshAfterLateReport(DeploymentPlan plan, List<DeploymentStep> steps, Instant now) {
        if (plan.getStatus() != PlanStatus.running && plan.getAbandonedBy() == null && allTerminal(steps)) {
            finish(plan, steps, now);
        }
    }

    /** End the steps that did not start, and close the plan. A step that runs on a host goes on there. */
    public static void abandon(DeploymentPlan plan, List<DeploymentStep> steps, String who, Instant now) {
        for (DeploymentStep step : steps) {
            if (step.getStatus() == StepStatus.waiting || step.getStatus() == StepStatus.ordered) {
                cancel(step, "Not started: the plan was abandoned by " + who + ".", now);
            }
        }
        plan.setStatus(PlanStatus.failed);
        plan.setAbandonedBy(who);
        plan.setReason("Abandoned by " + who + ".");
        plan.setFinishedAt(now);
        plan.setLastActivityAt(now);
    }

    /** End the ordered and running steps that were silent since {@code cutoff}. Returns the steps it ended. */
    public static List<DeploymentStep> timeOut(List<DeploymentStep> steps, Instant cutoff, Duration limit, Instant now) {
        List<DeploymentStep> ended = new ArrayList<>();
        for (DeploymentStep step : steps) {
            boolean open = step.getStatus() == StepStatus.ordered || step.getStatus() == StepStatus.running;
            if (!open || !step.getLastActivityAt().isBefore(cutoff)) {
                continue;
            }
            boolean untaken = step.getStatus() == StepStatus.ordered;
            step.setStatus(StepStatus.failed);
            step.setFailure(TIMED_OUT);
            step.setMessage(untaken
                ? "Timed out: " + step.getDeployerName() + " did not take the order within " + minutes(limit) + " min."
                : "Timed out: no sign of life from " + step.getDeployerName() + " for " + minutes(limit) + " min.");
            step.setFinishedAt(now);
            step.setLastActivityAt(now);
            ended.add(step);
        }
        return ended;
    }

    public static boolean allTerminal(List<DeploymentStep> steps) {
        return !steps.isEmpty() && steps.stream().allMatch(step -> step.getStatus().isTerminal());
    }

    /** failed if any step failed or never started, else rolled back if any was, else succeeded. */
    public static PlanStatus statusOf(List<DeploymentStep> steps) {
        if (steps.stream().anyMatch(step -> step.getStatus() == StepStatus.failed || step.getStatus() == StepStatus.cancelled)) {
            return PlanStatus.failed;
        }
        if (steps.stream().anyMatch(step -> step.getStatus() == StepStatus.rolled_back)) {
            return PlanStatus.rolled_back;
        }
        return PlanStatus.succeeded;
    }

    /** One line for the history: why the plan did not succeed, or null. */
    public static String reasonOf(List<DeploymentStep> steps) {
        return steps.stream()
            .sorted(Comparator.comparingInt(DeploymentStep::getPosition))
            .filter(step -> step.getStatus() == StepStatus.failed || step.getStatus() == StepStatus.rolled_back)
            .findFirst()
            .map(step -> step.getDeployerName() + ": " + (step.getMessage() == null ? step.getStatus().name() : firstLine(step.getMessage())))
            .orElse(null);
    }

    private static void finish(DeploymentPlan plan, List<DeploymentStep> steps, Instant now) {
        plan.setStatus(statusOf(steps));
        plan.setReason(plan.getAbandonedBy() != null ? plan.getReason() : reasonOf(steps));
        plan.setFinishedAt(now);
        plan.setLastActivityAt(now);
    }

    private static void cancel(DeploymentStep step, String message, Instant now) {
        step.setStatus(StepStatus.cancelled);
        step.setMessage(message);
        step.setFinishedAt(now);
        step.setLastActivityAt(now);
    }

    private static long minutes(Duration limit) {
        return Math.max(1, limit.toMinutes());
    }

    private static String firstLine(String message) {
        String line = message.strip().lines().findFirst().orElse("");
        return line.length() > 200 ? line.substring(0, 200) + "…" : line;
    }
}
