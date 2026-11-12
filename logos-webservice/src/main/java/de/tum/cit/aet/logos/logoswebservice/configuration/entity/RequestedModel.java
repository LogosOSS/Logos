package de.tum.cit.aet.logos.logoswebservice.configuration.entity;

import java.time.OffsetDateTime;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

/**
 * A model that users have requested but that Logos does not serve yet.
 *
 * This is the registry: one row per requested model. Who voted for it lives in
 * {@link RequestedModelVote} (one row per voter), so the model's vote count is
 * the number of votes pointing at it. A model an admin adds to {@code models}
 * is no longer "requested" and is filtered out of the listing.
 */
@Entity
@Table(name = "requested_models")
public class RequestedModel {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Integer id;

    @Column(nullable = false)
    private String name;

    /**
     * The decisive Hugging Face gating verdict for this name: true when the
     * Hub rejects the repository with the model-gated reason, false when it
     * is reachable or rejected for another reason; null until the precheck
     * has produced a verdict — a timeout is no verdict and leaves this empty.
     */
    private Boolean isGated;

    /**
     * The team that first requested this model: set when the row is created
     * (or filled in on a row that predates the column) and never overwritten
     * — later votes from other teams count toward the total, they just do
     * not change who requested first. The team's app admin is the one who
     * accepts a gated model's license agreement.
     */
    private Integer firstRequestingTeam;

    /**
     * The SPDX license expression the model's Hugging Face card declares
     * (e.g. {@code apache-2.0}); several declared licenses are stored joined
     * as an OR expression. Null when the card names none.
     */
    private String licenseId;

    /**
     * The text of the repository's license file, fetched from Hugging Face
     * via the orchestrator's central HF_TOKEN. Null either while no fetch has
     * run yet (see {@code licenseFetchedDate}) or when the repository carries
     * no license file.
     */
    private String licenseText;

    /**
     * When the last license fetch ran. Null means "never fetched" — a failed
     * fetch leaves it null, so the row is simply retried on the next pass.
     */
    private OffsetDateTime licenseFetchedDate;

    public Integer getId() { return id; }
    public String getName() { return name; }
    public Boolean getIsGated() { return isGated; }
    public Integer getFirstRequestingTeam() { return firstRequestingTeam; }
    public String getLicenseId() { return licenseId; }
    public String getLicenseText() { return licenseText; }
    public OffsetDateTime getLicenseFetchedDate() { return licenseFetchedDate; }

    public void setName(String name) { this.name = name; }
    public void setIsGated(Boolean isGated) { this.isGated = isGated; }
    public void setFirstRequestingTeam(Integer firstRequestingTeam) { this.firstRequestingTeam = firstRequestingTeam; }
    public void setLicenseId(String licenseId) { this.licenseId = licenseId; }
    public void setLicenseText(String licenseText) { this.licenseText = licenseText; }
    public void setLicenseFetchedDate(OffsetDateTime licenseFetchedDate) { this.licenseFetchedDate = licenseFetchedDate; }
}
