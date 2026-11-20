package de.tum.cit.aet.logos.logoswebservice.configuration.repository;

import java.util.List;
import java.util.Optional;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.transaction.annotation.Transactional;

import de.tum.cit.aet.logos.logoswebservice.configuration.entity.ModelLicenseApproval;

public interface ModelLicenseApprovalRepository extends JpaRepository<ModelLicenseApproval, Integer> {

    /** The Logos admin's global acceptance for a model (the row with no team). */
    Optional<ModelLicenseApproval> findByRequestedModelIdAndTeamIdIsNull(Integer requestedModelId);

    /**
     * Record the global acceptance of a model when the recheck finds a
     * previously gated repository readable again: accepting the repository's
     * terms on Hugging Face (which is what grants the central HF_TOKEN access)
     * is the acceptance, so the Logos admin must not accept the same agreement
     * a second time. The row carries no user (a system-recorded acceptance)
     * and the database's own timestamp.
     *
     * Idempotent and race-safe: the partial unique index on
     * {@code (requested_model_id) WHERE team_id IS NULL} makes a model that
     * is already accepted (manually or by an earlier recheck) a no-op.
     *
     * @return the number of rows inserted (0 when an acceptance exists already)
     */
    @Transactional
    @Modifying
    @Query(value = """
        INSERT INTO model_license_approvals (requested_model_id, team_id, user_id, approved_at)
        VALUES (:modelId, NULL, NULL, now())
        ON CONFLICT (requested_model_id) WHERE team_id IS NULL DO NOTHING
        """, nativeQuery = true)
    int recordGlobalApprovalIfAbsent(@Param("modelId") Integer modelId);

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
