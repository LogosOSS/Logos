package de.tum.cit.aet.logos.logoswebservice.deployments.repository;

import java.time.Instant;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import de.tum.cit.aet.logos.logoswebservice.deployments.entity.Deployer;

public interface DeployerRepository extends JpaRepository<Deployer, UUID> {

    Optional<Deployer> findByKeyHash(String keyHash);

    Optional<Deployer> findByProviderId(Integer providerId);

    /**
     * Make the row of a worker at its first contact. The unique provider makes the insert safe: two first contacts at
     * the same moment, for example at two copies of the webservice, give one row, and the loser changes nothing and
     * does not abort its transaction.
     */
    @Modifying(flushAutomatically = true)
    @Query(value = """
        INSERT INTO deployers (id, name, kind, provider_id)
        VALUES (:id, :name, 'worker', :providerId)
        ON CONFLICT (provider_id) DO NOTHING
        """, nativeQuery = true)
    int insertWorkerIfAbsent(@Param("id") UUID id, @Param("name") String name, @Param("providerId") Integer providerId);

    /** A worker follows the name of its provider. The name is the only column that is written. */
    @Modifying(flushAutomatically = true, clearAutomatically = true)
    @Query("update Deployer d set d.name = :name where d.id = :id")
    int rename(@Param("id") UUID id, @Param("name") String name);

    /** The core deployer first: {@code core} sorts before {@code worker}. */
    List<Deployer> findAllByOrderByKindAscNameAsc();

    /**
     * The heartbeat writes its own columns and no others. Loading the row and saving it would write the key hash as
     * well, and could undo a new key that was made in between.
     */
    @Modifying
    @Query("update Deployer d set d.lastSeenAt = :seenAt where d.id = :id")
    int markSeen(@Param("id") UUID id, @Param("seenAt") Instant seenAt);

    @Modifying
    @Query("update Deployer d set d.lastSeenAt = :seenAt, d.version = :version where d.id = :id")
    int markSeenWithVersion(@Param("id") UUID id, @Param("seenAt") Instant seenAt, @Param("version") String version);

    boolean existsByNameIgnoreCase(String name);
}
