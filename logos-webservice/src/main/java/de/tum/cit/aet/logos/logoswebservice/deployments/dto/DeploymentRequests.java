package de.tum.cit.aet.logos.logoswebservice.deployments.dto;

/** The bodies of the admin requests. */
public final class DeploymentRequests {

    private DeploymentRequests() {
    }

    /**
     * Start a deployment. {@code confirmation} is the typed word that a protected Logos asks for.
     */
    public record StartPlan(String tag, String confirmation) {}

    public record CreateDeployer(String name, String kind) {}
}
