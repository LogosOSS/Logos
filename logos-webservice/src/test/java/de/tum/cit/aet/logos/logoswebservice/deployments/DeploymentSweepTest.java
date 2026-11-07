package de.tum.cit.aet.logos.logoswebservice.deployments;

import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.util.List;

import org.junit.jupiter.api.Test;

import de.tum.cit.aet.logos.logoswebservice.deployments.service.DeploymentReportService;
import de.tum.cit.aet.logos.logoswebservice.deployments.service.DeploymentSweep;

class DeploymentSweepTest {

    private final DeploymentReportService reports = mock(DeploymentReportService.class);

    private DeploymentSweep sweep(String environment) {
        return new DeploymentSweep(new DeploymentProperties(environment, List.of(), null, null, null, null, null), reports);
    }

    @Test
    void doesNothingOnALogosWithoutDeployments() {
        sweep("").run();

        verify(reports, never()).closeSilentSteps();
    }

    @Test
    void endsTheSilentSteps() {
        when(reports.closeSilentSteps()).thenReturn(2);

        sweep("Dev").run();

        verify(reports).closeSilentSteps();
    }

    @Test
    void aFailureDoesNotStopTheSchedule() {
        when(reports.closeSilentSteps()).thenThrow(new IllegalStateException("database is down"));

        sweep("Dev").run();

        verify(reports).closeSilentSteps();
    }
}
