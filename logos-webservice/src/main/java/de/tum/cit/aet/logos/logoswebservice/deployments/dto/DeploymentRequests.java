package de.tum.cit.aet.logos.logoswebservice.deployments.dto;

/** The bodies of the admin requests. */
public final class DeploymentRequests {

    private DeploymentRequests() {
    }

    public record StartPlan(String tag) {}

    /** {@code kind} is optional. Only {@code core} is made here: a worker needs no deployer made. */
    public record CreateDeployer(String name, String kind) {}
}
