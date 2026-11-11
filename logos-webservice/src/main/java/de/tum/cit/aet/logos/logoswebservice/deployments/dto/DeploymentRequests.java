package de.tum.cit.aet.logos.logoswebservice.deployments.dto;

/** The bodies of the admin requests. */
public final class DeploymentRequests {

    private DeploymentRequests() {
    }

    public record StartPlan(String tag) {}

    public record CreateDeployer(String name, String kind) {}
}
