package de.tum.cit.aet.logos.logoswebservice.deployments.entity;

/**
 * What Logos knows about the work of one deployer in a plan.
 *
 * <p>{@code waiting}: not released yet (the core has to end first). {@code ordered}: released, and the deployer has not
 * taken it. {@code running}: the deployer reported progress. The rest has ended; {@code cancelled} means it never
 * started because the plan was abandoned or the core failed.
 */
public enum StepStatus {
    waiting, ordered, running, succeeded, unchanged, rolled_back, failed, cancelled;

    public boolean isTerminal() {
        return this != waiting && this != ordered && this != running;
    }

    /** The step ended and the new version runs. */
    public boolean isOk() {
        return this == succeeded || this == unchanged;
    }

    /** The status for an outcome that a deployer reports, or null for a word that is none. */
    public static StepStatus fromOutcome(String outcome) {
        if (outcome == null) {
            return null;
        }
        return switch (outcome) {
            case "succeeded" -> succeeded;
            case "unchanged" -> unchanged;
            case "rolled_back" -> rolled_back;
            case "failed" -> failed;
            default -> null;
        };
    }
}
