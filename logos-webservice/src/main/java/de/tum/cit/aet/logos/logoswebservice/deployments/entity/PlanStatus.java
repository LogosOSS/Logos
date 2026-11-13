package de.tum.cit.aet.logos.logoswebservice.deployments.entity;

/** How a deployment plan stands. A plan is {@code running} until every step has ended. */
public enum PlanStatus {
    running, succeeded, failed, rolled_back
}
