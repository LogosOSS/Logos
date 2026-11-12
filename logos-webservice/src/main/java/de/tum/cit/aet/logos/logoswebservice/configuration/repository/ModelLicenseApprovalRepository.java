package de.tum.cit.aet.logos.logoswebservice.configuration.repository;

import java.util.List;
import java.util.Optional;

import org.springframework.data.jpa.repository.JpaRepository;

import de.tum.cit.aet.logos.logoswebservice.configuration.entity.ModelLicenseApproval;

public interface ModelLicenseApprovalRepository extends JpaRepository<ModelLicenseApproval, Integer> {

    /** The Logos admin's global acceptance for a model (the row with no team). */
    Optional<ModelLicenseApproval> findByRequestedModelIdAndTeamIdIsNull(Integer requestedModelId);

    /** Whether the Logos admin's global acceptance for a model exists. */
    boolean existsByRequestedModelIdAndTeamIdIsNull(Integer requestedModelId);

    /** Every acceptance for a model, global and team-level. */
    List<ModelLicenseApproval> findByRequestedModelId(Integer requestedModelId);

    /**
     * Drop every acceptance for a model — the global one and the team-level
     * rows that depend on it. Removing the global acceptance invalidates the
     * team acceptances, so they go with it.
     */
    void deleteByRequestedModelId(Integer requestedModelId);
}
