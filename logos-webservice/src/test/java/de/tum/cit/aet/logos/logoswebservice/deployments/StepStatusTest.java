package de.tum.cit.aet.logos.logoswebservice.deployments;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import de.tum.cit.aet.logos.logoswebservice.deployments.entity.StepStatus;

class StepStatusTest {

    @Test
    void aStepThatHasNotEndedIsNotTerminal() {
        assertThat(StepStatus.waiting.isTerminal()).isFalse();
        assertThat(StepStatus.ordered.isTerminal()).isFalse();
        assertThat(StepStatus.running.isTerminal()).isFalse();
        assertThat(StepStatus.succeeded.isTerminal()).isTrue();
        assertThat(StepStatus.cancelled.isTerminal()).isTrue();
    }

    @Test
    void onlySucceededAndUnchangedMeanThatTheNewVersionRuns() {
        assertThat(StepStatus.succeeded.isOk()).isTrue();
        assertThat(StepStatus.unchanged.isOk()).isTrue();
        assertThat(StepStatus.rolled_back.isOk()).isFalse();
        assertThat(StepStatus.failed.isOk()).isFalse();
    }

    @Test
    void anOutcomeBecomesAStatusAndAnythingElseDoesNot() {
        assertThat(StepStatus.fromOutcome("succeeded")).isEqualTo(StepStatus.succeeded);
        assertThat(StepStatus.fromOutcome("unchanged")).isEqualTo(StepStatus.unchanged);
        assertThat(StepStatus.fromOutcome("rolled_back")).isEqualTo(StepStatus.rolled_back);
        assertThat(StepStatus.fromOutcome("failed")).isEqualTo(StepStatus.failed);
        assertThat(StepStatus.fromOutcome("running")).isNull();
        assertThat(StepStatus.fromOutcome(null)).isNull();
    }
}
