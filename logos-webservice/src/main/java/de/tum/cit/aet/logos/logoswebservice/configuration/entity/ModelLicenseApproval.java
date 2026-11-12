package de.tum.cit.aet.logos.logoswebservice.configuration.entity;

import java.time.OffsetDateTime;

import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

/**
 * One acceptance of a requested model's license agreement.
 *
 * The scope is the {@code team_id}: null is the Logos admin's global
 * acceptance (at most one per model), a team id is that team's app admin
 * acceptance (at most one per model per team). The team-level acceptance
 * exists only while the global one does — revoking the global acceptance
 * removes the team rows with it. {@code userId} and {@code approvedAt} are
 * the audit record: who accepted, and when.
 */
@Entity
@Table(name = "model_license_approvals")
public class ModelLicenseApproval {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Integer id;

    private Integer requestedModelId;

    /** Null for the Logos admin's global acceptance; the team otherwise. */
    private Integer teamId;

    private Integer userId;

    private OffsetDateTime approvedAt;

    public Integer getId() { return id; }
    public Integer getRequestedModelId() { return requestedModelId; }
    public Integer getTeamId() { return teamId; }
    public Integer getUserId() { return userId; }
    public OffsetDateTime getApprovedAt() { return approvedAt; }

    public void setRequestedModelId(Integer requestedModelId) { this.requestedModelId = requestedModelId; }
    public void setTeamId(Integer teamId) { this.teamId = teamId; }
    public void setUserId(Integer userId) { this.userId = userId; }
    public void setApprovedAt(OffsetDateTime approvedAt) { this.approvedAt = approvedAt; }
}
