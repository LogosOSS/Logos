package de.tum.cit.aet.logos.logoswebservice.deployments.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import de.tum.cit.aet.logos.logoswebservice.deployments.DeploymentProperties;

/**
 * Ends the steps whose deployer has been silent for too long. A plan can get stuck: the new webservice crashes while
 * it starts, a host is off, a report never arrives. Without this the plan would say "running" for ever and block the
 * next deployment. Several copies of the webservice may run this at once; each plan is locked while it is changed, so
 * the second copy finds nothing left to do.
 */
@Component
public class DeploymentSweep {

    private static final Logger log = LoggerFactory.getLogger(DeploymentSweep.class);

    private final DeploymentProperties properties;
    private final DeploymentReportService reports;

    public DeploymentSweep(DeploymentProperties properties, DeploymentReportService reports) {
        this.properties = properties;
        this.reports = reports;
    }

    @Scheduled(initialDelayString = "${logos.deployments.sweep-initial-delay:PT2M}",
               fixedDelayString = "${logos.deployments.sweep-interval:PT1M}")
    public void run() {
        if (!properties.enabled()) {
            return;
        }
        try {
            int ended = reports.closeSilentSteps();
            if (ended > 0) {
                log.warn("Ended {} deployment step(s) that were silent for more than {}", ended, properties.silenceLimit());
            }
        } catch (RuntimeException e) {
            log.warn("The sweep for silent deployment steps failed", e);
        }
    }
}
