package de.tum.cit.aet.logos.logoswebservice.configuration.repository;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Optional;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.transaction.annotation.Transactional;

import de.tum.cit.aet.logos.logoswebservice.configuration.entity.RequestedModel;

public interface RequestedModelRepository extends JpaRepository<RequestedModel, Integer> {

    Optional<RequestedModel> findByNameIgnoreCase(String name);

    /** Registry rows without a Hugging Face verdict yet (never checked, or the check timed out). */
    List<RequestedModel> findByIsGatedIsNull();

    /** Registry rows flagged as gated, for re-verification after a license acceptance. */
    List<RequestedModel> findByIsGatedTrue();

    /**
     * Registry rows the central HF_TOKEN can read but that carry no license
     * yet — the backfill target of the scheduled license fetch.
     */
    List<RequestedModel> findByIsGatedFalseAndLicenseFetchedDateIsNull();

    /**
     * Record a fetched license on a row that has none yet. The condition makes
     * concurrent fetches (a vote and the recheck at once) idempotent: the
     * second update finds the date already set and changes nothing.
     *
     * @return the number of rows updated (0 when the row already had a license)
     */
    @Transactional
    @Modifying
    @Query("UPDATE RequestedModel m SET m.licenseId = :licenseId, m.licenseText = :licenseText, "
        + "m.licenseFetchedDate = :fetchedAt WHERE m.id = :id AND m.licenseFetchedDate IS NULL")
    int storeLicenseIfAbsent(@Param("id") Integer id,
                             @Param("licenseId") String licenseId,
                             @Param("licenseText") String licenseText,
                             @Param("fetchedAt") OffsetDateTime fetchedAt);

    /**
     * Record a decisive Hugging Face gating verdict on a row that has none
     * yet. The condition keeps a concurrent vote (which stores its own fresh
     * verdict) from being clobbered by a stale sweep.
     *
     * @return the number of rows updated (0 when the row already has a verdict)
     */
    @Modifying
    @Query("UPDATE RequestedModel m SET m.isGated = :status WHERE m.id = :id AND m.isGated IS NULL")
    int setIsGatedIfNull(@Param("id") Integer id, @Param("status") Boolean status);

    /**
     * Clear the gating flag on a row that is still flagged (a recheck found
     * the repository reachable, so the license was accepted). The condition
     * keeps a concurrent vote — which stores its own fresh verdict — from
     * being clobbered by a stale recheck.
     *
     * @return the number of rows updated (0 when the row is no longer flagged)
     */
    @Transactional
    @Modifying
    @Query("UPDATE RequestedModel m SET m.isGated = false WHERE m.id = :id AND m.isGated = true")
    int clearGatedFlagIfStillSet(@Param("id") Integer id);

    /**
     * Acquires a transaction-scoped advisory lock on the given key, blocking
     * until any other holder's transaction commits or rolls back. Used to
     * serialize the check-and-increment in {@code RequestedModelService} so two
     * simultaneous requests for the same never-before-seen model cannot both
     * insert; released automatically with the surrounding transaction.
     */
    @Query(value = "SELECT pg_advisory_xact_lock(:key)", nativeQuery = true)
    void lockRequestedModelsNamespace(@Param("key") long key);
}
