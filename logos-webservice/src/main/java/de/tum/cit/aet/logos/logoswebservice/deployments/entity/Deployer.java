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
 * A program on a host that obeys this Logos: it asks for orders and reports how they go. Only the SHA-256 hash of its
 * key is stored. The key is shown once, when the deployer is made.
 */
@Entity
@Table(name = "deployers")
public class Deployer {

    @Id
    private UUID id;

    @Column(nullable = false)
    private String name;

    @Column(nullable = false)
    private String environment;

    @Enumerated(EnumType.STRING)
    @Column(nullable = false)
    private DeployerKind kind;

    @Column(nullable = false)
    private String keyHash;

    @Column(nullable = false)
    private Instant createdAt;

    private Integer createdBy;

    private Instant lastSeenAt;

    private String version;

    public UUID getId() { return id; }
    public String getName() { return name; }
    public String getEnvironment() { return environment; }
    public DeployerKind getKind() { return kind; }
    public String getKeyHash() { return keyHash; }
    public Instant getCreatedAt() { return createdAt; }
    public Integer getCreatedBy() { return createdBy; }
    public Instant getLastSeenAt() { return lastSeenAt; }
    public String getVersion() { return version; }

    public void setId(UUID id) { this.id = id; }
    public void setName(String name) { this.name = name; }
    public void setEnvironment(String environment) { this.environment = environment; }
    public void setKind(DeployerKind kind) { this.kind = kind; }
    public void setKeyHash(String keyHash) { this.keyHash = keyHash; }
    public void setCreatedAt(Instant createdAt) { this.createdAt = createdAt; }
    public void setCreatedBy(Integer createdBy) { this.createdBy = createdBy; }
    public void setLastSeenAt(Instant lastSeenAt) { this.lastSeenAt = lastSeenAt; }
    public void setVersion(String version) { this.version = version; }
}
