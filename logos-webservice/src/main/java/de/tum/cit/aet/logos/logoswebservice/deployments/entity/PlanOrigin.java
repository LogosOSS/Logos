package de.tum.cit.aet.logos.logoswebservice.deployments.entity;

/**
 * Where a plan comes from: an admin of this Logos started it ({@code local}), or deployers of another environment
 * reported it ({@code observed}). This Logos only moves a local plan on; it shows an observed plan.
 */
public enum PlanOrigin {
    local, observed
}
