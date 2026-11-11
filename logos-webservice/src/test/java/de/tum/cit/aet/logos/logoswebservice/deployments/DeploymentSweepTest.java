package de.tum.cit.aet.logos.logoswebservice.deployments;

import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import org.junit.jupiter.api.Test;

import de.tum.cit.aet.logos.logoswebservice.deployments.service.DeploymentReportService;
import de.tum.cit.aet.logos.logoswebservice.deployments.service.DeploymentSweep;

class DeploymentSweepTest {

    private final DeploymentReportService reports = mock(DeploymentReportService.class);
    private final DeploymentSweep sweep =
        new DeploymentSweep(new DeploymentProperties(false, null, null, null, null), reports);

    @Test
    void endsTheSilentSteps() {
        when(reports.closeSilentSteps()).thenReturn(2);

        sweep.run();

        verify(reports).closeSilentSteps();
    }

    @Test
    void aFailureDoesNotStopTheSchedule() {
        when(reports.closeSilentSteps()).thenThrow(new IllegalStateException("database is down"));

        sweep.run();

        verify(reports).closeSilentSteps();
    }
}
