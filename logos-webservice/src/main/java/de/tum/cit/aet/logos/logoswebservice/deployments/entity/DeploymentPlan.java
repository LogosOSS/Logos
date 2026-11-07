package de.tum.cit.aet.logos.logoswebservice.deployments.entity;

import java.time.Instant;
import java.util.UUID;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

/**
 * The to-do list of one deployment of an environment: the core first, then all workers. The steps are in
 * {@link DeploymentStep}.
 */
@Entity
@Table(name = "deployment_plans")
public class DeploymentPlan {

    @Id
    private UUID id;

    @Column(nullable = false)
    private long number;

    @Column(nullable = false)
    private String environment;

    @Column(nullable = false)
    private String tag;

    @Enumerated(EnumType.STRING)
    @Column(nullable = false)
    private PlanStatus status;

    @Enumerated(EnumType.STRING)
    @Column(nullable = false)
    private PlanOrigin origin;

    @Column(nullable = false)
    private String initiator;

    private Integer initiatorUserId;

    private String reason;

    private String abandonedBy;

    @Column(nullable = false)
    private Instant createdAt;

    private Instant finishedAt;

    @Column(nullable = false)
    private Instant lastActivityAt;

    public UUID getId() { return id; }
    public long getNumber() { return number; }
    public String getEnvironment() { return environment; }
    public String getTag() { return tag; }
    public PlanStatus getStatus() { return status; }
    public PlanOrigin getOrigin() { return origin; }
    public String getInitiator() { return initiator; }
    public Integer getInitiatorUserId() { return initiatorUserId; }
    public String getReason() { return reason; }
    public String getAbandonedBy() { return abandonedBy; }
    public Instant getCreatedAt() { return createdAt; }
    public Instant getFinishedAt() { return finishedAt; }
    public Instant getLastActivityAt() { return lastActivityAt; }

    public void setId(UUID id) { this.id = id; }
    public void setNumber(long number) { this.number = number; }
    public void setEnvironment(String environment) { this.environment = environment; }
    public void setTag(String tag) { this.tag = tag; }
    public void setStatus(PlanStatus status) { this.status = status; }
    public void setOrigin(PlanOrigin origin) { this.origin = origin; }
    public void setInitiator(String initiator) { this.initiator = initiator; }
    public void setInitiatorUserId(Integer initiatorUserId) { this.initiatorUserId = initiatorUserId; }
    public void setReason(String reason) { this.reason = reason; }
    public void setAbandonedBy(String abandonedBy) { this.abandonedBy = abandonedBy; }
    public void setCreatedAt(Instant createdAt) { this.createdAt = createdAt; }
    public void setFinishedAt(Instant finishedAt) { this.finishedAt = finishedAt; }
    public void setLastActivityAt(Instant lastActivityAt) { this.lastActivityAt = lastActivityAt; }
}
