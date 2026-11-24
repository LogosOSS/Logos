package de.tum.cit.aet.logos.logoswebservice.deployments.entity;

import java.time.Instant;
import java.util.UUID;

import org.hibernate.annotations.JdbcTypeCode;
import org.hibernate.type.SqlTypes;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

/**
 * The work of one deployer in a plan. The id is the deployment id that the deployer knows. {@link #status} is what
 * Logos knows; {@link #phase} is the phase that the deployer reported last.
 */
@Entity
@Table(name = "deployment_steps")
public class DeploymentStep {

    @Id
    private UUID id;

    @Column(nullable = false)
    private UUID planId;

    @Column(nullable = false)
    private int position;

    @Enumerated(EnumType.STRING)
    @Column(nullable = false)
    private DeployerKind kind;

    private UUID deployerId;

    @Column(nullable = false)
    private String deployerName;

    @Enumerated(EnumType.STRING)
    @Column(nullable = false)
    private StepStatus status;

    private String phase;

    private String failure;

    private String message;

    private String requestedBy;

    private Instant startedAt;

    private Instant finishedAt;

    @Column(nullable = false)
    private Instant lastActivityAt;

    /** The events the deployer recorded, a JSON array of {@code {at, phase, note}}. */
    @JdbcTypeCode(SqlTypes.JSON)
    @Column(columnDefinition = "jsonb", nullable = false)
    private String events = "[]";

    private String failureLogs;

    /** Facts about the drain and the replacement, a JSON object. */
    @JdbcTypeCode(SqlTypes.JSON)
    @Column(columnDefinition = "jsonb")
    private String details;

    public UUID getId() { return id; }
    public UUID getPlanId() { return planId; }
    public int getPosition() { return position; }
    public DeployerKind getKind() { return kind; }
    public UUID getDeployerId() { return deployerId; }
    public String getDeployerName() { return deployerName; }
    public StepStatus getStatus() { return status; }
    public String getPhase() { return phase; }
    public String getFailure() { return failure; }
    public String getMessage() { return message; }
    public String getRequestedBy() { return requestedBy; }
    public Instant getStartedAt() { return startedAt; }
    public Instant getFinishedAt() { return finishedAt; }
    public Instant getLastActivityAt() { return lastActivityAt; }
    public String getEvents() { return events; }
    public String getFailureLogs() { return failureLogs; }
    public String getDetails() { return details; }

    public void setId(UUID id) { this.id = id; }
    public void setPlanId(UUID planId) { this.planId = planId; }
    public void setPosition(int position) { this.position = position; }
    public void setKind(DeployerKind kind) { this.kind = kind; }
    public void setDeployerId(UUID deployerId) { this.deployerId = deployerId; }
    public void setDeployerName(String deployerName) { this.deployerName = deployerName; }
    public void setStatus(StepStatus status) { this.status = status; }
    public void setPhase(String phase) { this.phase = phase; }
    public void setFailure(String failure) { this.failure = failure; }
    public void setMessage(String message) { this.message = message; }
    public void setRequestedBy(String requestedBy) { this.requestedBy = requestedBy; }
    public void setStartedAt(Instant startedAt) { this.startedAt = startedAt; }
    public void setFinishedAt(Instant finishedAt) { this.finishedAt = finishedAt; }
    public void setLastActivityAt(Instant lastActivityAt) { this.lastActivityAt = lastActivityAt; }
    public void setEvents(String events) { this.events = events; }
    public void setFailureLogs(String failureLogs) { this.failureLogs = failureLogs; }
    public void setDetails(String details) { this.details = details; }
}
