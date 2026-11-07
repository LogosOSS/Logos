package de.tum.cit.aet.logos.logoswebservice.deployments.dto;

/** The bodies of the admin requests. */
public final class DeploymentRequests {

    private DeploymentRequests() {
    }

    /**
     * Start a deployment. {@code confirmation} is the typed name of the environment, needed for a protected one.
     */
    public record StartPlan(String environment, String tag, String confirmation) {}

    public record CreateDeployer(String name, String environment, String kind) {}
}
