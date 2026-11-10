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

    List<Deployer> findAllByOrderByEnvironmentAscKindAscNameAsc();

    List<Deployer> findByEnvironmentOrderByKindAscNameAsc(String environment);

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

    boolean existsByEnvironmentIgnoreCaseAndNameIgnoreCase(String environment, String name);
}
