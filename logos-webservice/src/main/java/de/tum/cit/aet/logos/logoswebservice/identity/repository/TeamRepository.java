package de.tum.cit.aet.logos.logoswebservice.identity.repository;

import java.util.List;
import java.util.Optional;

import jakarta.persistence.LockModeType;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import de.tum.cit.aet.logos.logoswebservice.identity.entity.Team;

public interface TeamRepository extends JpaRepository<Team, Integer> {

    @Query("""
        SELECT DISTINCT t FROM Team t
        JOIN TeamMember tm ON tm.id.teamId = t.id
        WHERE tm.id.userId = :userId
        """)
    List<Team> findTeamsForUser(@Param("userId") Integer userId);

    Optional<Team> findByName(String name);

    Optional<Team> findFirstByName(String name);

    /** Serializes updates to team settings, permissions and membership within a transaction. */
    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("SELECT t FROM Team t WHERE t.id = :id")
    Optional<Team> findByIdForUpdate(@Param("id") Integer id);

    /** Teams an admin opted into the public stats page. */
    long countByShowOnPublicStatsTrue();

    /** Distinct public stats categories in use, alphabetically. */
    @Query("""
        SELECT DISTINCT t.publicCategory FROM Team t
        WHERE t.publicCategory IS NOT NULL
        ORDER BY t.publicCategory
        """)
    List<String> findDistinctPublicCategories();
}
