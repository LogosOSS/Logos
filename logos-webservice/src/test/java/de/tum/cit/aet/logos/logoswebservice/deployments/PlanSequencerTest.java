package de.tum.cit.aet.logos.logoswebservice.deployments;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.UUID;

import org.junit.jupiter.api.Test;

import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeployerKind;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeploymentPlan;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeploymentStep;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.PlanStatus;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.StepStatus;
import de.tum.cit.aet.logos.logoswebservice.deployments.service.PlanSequencer;

class PlanSequencerTest {

    private static final Instant T0 = Instant.parse("2026-11-07T10:00:00Z");
    private static final Instant NOW = T0.plus(Duration.ofMinutes(5));

    private static DeploymentPlan plan() {
        DeploymentPlan plan = new DeploymentPlan();
        plan.setId(UUID.randomUUID());
        plan.setTag("v1");
        plan.setStatus(PlanStatus.running);
        plan.setInitiator("Jane Doe");
        plan.setCreatedAt(T0);
        plan.setLastActivityAt(T0);
        return plan;
    }

    private static DeploymentStep step(String name, DeployerKind kind, int position, StepStatus status) {
        DeploymentStep step = new DeploymentStep();
        step.setId(UUID.randomUUID());
        step.setDeployerName(name);
        step.setKind(kind);
        step.setPosition(position);
        step.setStatus(status);
        step.setLastActivityAt(T0);
        return step;
    }

    private static DeploymentStep core(StepStatus status) {
        return step("core", DeployerKind.core, 0, status);
    }

    private static DeploymentStep worker(String name, int position, StepStatus status) {
        return step(name, DeployerKind.worker, position, status);
    }

    @Test
    void theWorkersAreReleasedWhenTheCoreEndedWell() {
        DeploymentPlan plan = plan();
        List<DeploymentStep> steps = List.of(core(StepStatus.succeeded), worker("w1", 1, StepStatus.waiting), worker("w2", 2, StepStatus.waiting));

        PlanSequencer.advance(plan, steps, NOW);

        assertThat(steps.get(1).getStatus()).isEqualTo(StepStatus.ordered);
        assertThat(steps.get(2).getStatus()).isEqualTo(StepStatus.ordered);
        assertThat(steps.get(1).getLastActivityAt()).isEqualTo(NOW);
        assertThat(plan.getStatus()).isEqualTo(PlanStatus.running);
    }

    @Test
    void anUnchangedCoreAlsoReleasesTheWorkers() {
        List<DeploymentStep> steps = List.of(core(StepStatus.unchanged), worker("w1", 1, StepStatus.waiting));

        PlanSequencer.advance(plan(), steps, NOW);

        assertThat(steps.get(1).getStatus()).isEqualTo(StepStatus.ordered);
    }

    @Test
    void theWorkersWaitWhileTheCoreRuns() {
        List<DeploymentStep> steps = List.of(core(StepStatus.running), worker("w1", 1, StepStatus.waiting));

        PlanSequencer.advance(plan(), steps, NOW);

        assertThat(steps.get(1).getStatus()).isEqualTo(StepStatus.waiting);
    }

    @Test
    void aFailedCoreCancelsTheWorkersAndFailsThePlan() {
        DeploymentPlan plan = plan();
        DeploymentStep core = core(StepStatus.failed);
        core.setMessage("docker compose up failed: port is already allocated\nmore text");
        List<DeploymentStep> steps = List.of(core, worker("w1", 1, StepStatus.waiting));

        PlanSequencer.advance(plan, steps, NOW);

        assertThat(steps.get(1).getStatus()).isEqualTo(StepStatus.cancelled);
        assertThat(steps.get(1).getMessage()).contains("the core did not succeed");
        assertThat(plan.getStatus()).isEqualTo(PlanStatus.failed);
        assertThat(plan.getReason()).isEqualTo("core: docker compose up failed: port is already allocated");
        assertThat(plan.getFinishedAt()).isEqualTo(NOW);
    }

    @Test
    void theCoreThatRolledBackIsNoGoodEither() {
        List<DeploymentStep> steps = List.of(core(StepStatus.rolled_back), worker("w1", 1, StepStatus.waiting));

        PlanSequencer.advance(plan(), steps, NOW);

        assertThat(steps.get(1).getStatus()).isEqualTo(StepStatus.cancelled);
    }

    @Test
    void thePlanSucceedsWhenEveryStepSucceeded() {
        DeploymentPlan plan = plan();

        PlanSequencer.advance(plan, List.of(core(StepStatus.succeeded), worker("w1", 1, StepStatus.unchanged)), NOW);

        assertThat(plan.getStatus()).isEqualTo(PlanStatus.succeeded);
        assertThat(plan.getReason()).isNull();
    }

    @Test
    void aFailedWorkerDoesNotStopTheOthersAndFailsThePlanAtTheEnd() {
        DeploymentPlan plan = plan();
        DeploymentStep w1 = worker("w1", 1, StepStatus.failed);
        w1.setMessage("the node did not drain within 900 s");
        List<DeploymentStep> steps = List.of(core(StepStatus.succeeded), w1, worker("w2", 2, StepStatus.running));

        PlanSequencer.advance(plan, steps, NOW);
        assertThat(plan.getStatus()).isEqualTo(PlanStatus.running);

        steps.get(2).setStatus(StepStatus.succeeded);
        PlanSequencer.advance(plan, steps, NOW);

        assertThat(plan.getStatus()).isEqualTo(PlanStatus.failed);
        assertThat(plan.getReason()).isEqualTo("w1: the node did not drain within 900 s");
    }

    @Test
    void aWorkerThatRolledBackMakesTheRolledBackPlan() {
        DeploymentPlan plan = plan();

        PlanSequencer.advance(plan, List.of(core(StepStatus.succeeded), worker("w1", 1, StepStatus.rolled_back)), NOW);

        assertThat(plan.getStatus()).isEqualTo(PlanStatus.rolled_back);
    }

    @Test
    void aPlanWithoutACoreEndsWhenTheWorkersEnded() {
        DeploymentPlan plan = plan();
        List<DeploymentStep> steps = List.of(worker("w1", 1, StepStatus.succeeded), worker("w2", 2, StepStatus.running));

        PlanSequencer.advance(plan, steps, NOW);
        assertThat(plan.getStatus()).isEqualTo(PlanStatus.running);

        steps.get(1).setStatus(StepStatus.succeeded);
        PlanSequencer.advance(plan, steps, NOW);
        assertThat(plan.getStatus()).isEqualTo(PlanStatus.succeeded);
    }

    @Test
    void aClosedPlanIsNotMovedOn() {
        DeploymentPlan plan = plan();
        plan.setStatus(PlanStatus.failed);
        List<DeploymentStep> steps = List.of(core(StepStatus.succeeded), worker("w1", 1, StepStatus.waiting));

        PlanSequencer.advance(plan, steps, NOW);

        assertThat(steps.get(1).getStatus()).isEqualTo(StepStatus.waiting);
    }

    @Test
    void abandoningCancelsWhatDidNotStartAndLeavesWhatRuns() {
        DeploymentPlan plan = plan();
        List<DeploymentStep> steps = List.of(
            core(StepStatus.succeeded), worker("w1", 1, StepStatus.running), worker("w2", 2, StepStatus.ordered), worker("w3", 3, StepStatus.waiting));

        PlanSequencer.abandon(plan, steps, "Jane Doe", NOW);

        assertThat(steps.get(1).getStatus()).isEqualTo(StepStatus.running);
        assertThat(steps.get(2).getStatus()).isEqualTo(StepStatus.cancelled);
        assertThat(steps.get(3).getStatus()).isEqualTo(StepStatus.cancelled);
        assertThat(steps.get(3).getMessage()).isEqualTo("Not started: the plan was abandoned by Jane Doe.");
        assertThat(plan.getStatus()).isEqualTo(PlanStatus.failed);
        assertThat(plan.getAbandonedBy()).isEqualTo("Jane Doe");
        assertThat(plan.getReason()).isEqualTo("Abandoned by Jane Doe.");
        assertThat(plan.getFinishedAt()).isEqualTo(NOW);
    }

    @Test
    void aSilentStepEndsAsTimedOutAndAFreshOneStays() {
        DeploymentStep silent = worker("w1", 1, StepStatus.running);
        DeploymentStep fresh = worker("w2", 2, StepStatus.running);
        fresh.setLastActivityAt(NOW.minus(Duration.ofMinutes(1)));
        DeploymentStep untaken = worker("w3", 3, StepStatus.ordered);
        DeploymentStep waiting = worker("w4", 4, StepStatus.waiting);
        List<DeploymentStep> steps = List.of(silent, fresh, untaken, waiting);
        Instant later = T0.plus(Duration.ofMinutes(45));

        List<DeploymentStep> ended = PlanSequencer.timeOut(steps, later.minus(Duration.ofMinutes(30)), Duration.ofMinutes(30), later);

        assertThat(ended).containsExactlyInAnyOrder(silent, untaken, fresh);
        assertThat(silent.getStatus()).isEqualTo(StepStatus.failed);
        assertThat(silent.getFailure()).isEqualTo(PlanSequencer.TIMED_OUT);
        assertThat(silent.getMessage()).isEqualTo("Timed out: no sign of life from w1 for 30 min.");
        assertThat(untaken.getMessage()).isEqualTo("Timed out: w3 did not take the order within 30 min.");
        assertThat(waiting.getStatus()).isEqualTo(StepStatus.waiting);
    }

    @Test
    void aStepThatWasSeenRecentlyIsNotTimedOut() {
        DeploymentStep fresh = worker("w1", 1, StepStatus.running);
        fresh.setLastActivityAt(NOW);

        List<DeploymentStep> ended = PlanSequencer.timeOut(List.of(fresh), NOW.minus(Duration.ofMinutes(30)), Duration.ofMinutes(30), NOW);

        assertThat(ended).isEmpty();
        assertThat(fresh.getStatus()).isEqualTo(StepStatus.running);
    }

    @Test
    void aTimedOutCoreCancelsTheWorkers() {
        DeploymentPlan plan = plan();
        List<DeploymentStep> steps = List.of(core(StepStatus.running), worker("w1", 1, StepStatus.waiting));
        Instant later = T0.plus(Duration.ofMinutes(45));

        PlanSequencer.timeOut(steps, later.minus(Duration.ofMinutes(30)), Duration.ofMinutes(30), later);
        PlanSequencer.advance(plan, steps, later);

        assertThat(plan.getStatus()).isEqualTo(PlanStatus.failed);
        assertThat(steps.get(1).getStatus()).isEqualTo(StepStatus.cancelled);
    }

    @Test
    void aLateSuccessChangesAPlanThatTimedOut() {
        DeploymentPlan plan = plan();
        DeploymentStep w1 = worker("w1", 1, StepStatus.failed);
        w1.setFailure(PlanSequencer.TIMED_OUT);
        List<DeploymentStep> steps = List.of(core(StepStatus.succeeded), w1);
        PlanSequencer.advance(plan, steps, NOW);
        assertThat(plan.getStatus()).isEqualTo(PlanStatus.failed);

        w1.setStatus(StepStatus.succeeded);
        PlanSequencer.refreshAfterLateReport(plan, steps, NOW.plusSeconds(60));

        assertThat(plan.getStatus()).isEqualTo(PlanStatus.succeeded);
    }

    @Test
    void anAbandonedPlanStaysFailedWhateverComesLater() {
        DeploymentPlan plan = plan();
        DeploymentStep w1 = worker("w1", 1, StepStatus.running);
        List<DeploymentStep> steps = List.of(core(StepStatus.succeeded), w1);
        PlanSequencer.abandon(plan, steps, "Jane Doe", NOW);

        w1.setStatus(StepStatus.succeeded);
        PlanSequencer.refreshAfterLateReport(plan, steps, NOW.plusSeconds(60));

        assertThat(plan.getStatus()).isEqualTo(PlanStatus.failed);
        assertThat(plan.getReason()).isEqualTo("Abandoned by Jane Doe.");
    }
}
