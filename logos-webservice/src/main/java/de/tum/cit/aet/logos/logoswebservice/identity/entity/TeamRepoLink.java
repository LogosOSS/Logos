package de.tum.cit.aet.logos.logoswebservice.identity.entity;

import java.time.Instant;
import java.util.List;

import org.hibernate.annotations.JdbcTypeCode;
import org.hibernate.type.SqlTypes;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

/**
 * A GitHub repository linked to a team for later AI-workflow analysis.
 *
 * Named {@code TeamRepoLink} rather than {@code TeamRepository} so it does not
 * collide with the Spring Data repository for {@link Team}.
 */
@Entity
@Table(name = "team_repositories")
public class TeamRepoLink {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Integer id;

    @Column(nullable = false)
    private Integer teamId;

    @Column(nullable = false)
    private String repoUrl;

    @Column(nullable = false)
    private String repoSlug;

    @Column(nullable = false)
    private String branch = "main";

    @JdbcTypeCode(SqlTypes.JSON)
    @Column(columnDefinition = "jsonb")
    private List<String> paths;

    @Column(nullable = false)
    private boolean agentEnabled;

    @Column(columnDefinition = "text")
    private String agentInstructions;

    @Column(columnDefinition = "text")
    private String verificationCommand;

    @Column(nullable = false)
    private Instant createdAt;

    @Column(nullable = false)
    private Instant updatedAt;

    public boolean isAgentEnabled() { return agentEnabled; }
    public String getAgentInstructions() { return agentInstructions; }
    public String getVerificationCommand() { return verificationCommand; }
    public void setAgentEnabled(boolean value) { agentEnabled = value; }
    public void setAgentInstructions(String value) { agentInstructions = value; }
    public void setVerificationCommand(String value) { verificationCommand = value; }

    public Integer getId() { return id; }
    public Integer getTeamId() { return teamId; }
    public String getRepoUrl() { return repoUrl; }
    public String getRepoSlug() { return repoSlug; }
    public String getBranch() { return branch; }
    public List<String> getPaths() { return paths; }
    public Instant getCreatedAt() { return createdAt; }
    public Instant getUpdatedAt() { return updatedAt; }

    public void setTeamId(Integer teamId) { this.teamId = teamId; }
    public void setRepoUrl(String repoUrl) { this.repoUrl = repoUrl; }
    public void setRepoSlug(String repoSlug) { this.repoSlug = repoSlug; }
    public void setBranch(String branch) { this.branch = branch; }
    public void setPaths(List<String> paths) { this.paths = paths; }
    public void setCreatedAt(Instant createdAt) { this.createdAt = createdAt; }
    public void setUpdatedAt(Instant updatedAt) { this.updatedAt = updatedAt; }
}
