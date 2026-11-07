package de.tum.cit.aet.logos.logoswebservice.deployments.repository;

import java.time.Instant;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeploymentPlan;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.PlanOrigin;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.PlanStatus;
import jakarta.persistence.LockModeType;

public interface DeploymentPlanRepository extends JpaRepository<DeploymentPlan, UUID> {

    List<DeploymentPlan> findTop50ByOrderByCreatedAtDesc();

    List<DeploymentPlan> findTop50ByEnvironmentIgnoreCaseOrderByCreatedAtDesc(String environment);

    List<DeploymentPlan> findTop30ByOrderByCreatedAtDesc();

    Optional<DeploymentPlan> findFirstByEnvironmentIgnoreCaseOrderByCreatedAtDesc(String environment);

    /** The next number for a deployment. */
    @Query(value = "SELECT nextval('deployment_plan_number_seq')", nativeQuery = true)
    long nextNumber();

    Optional<DeploymentPlan> findFirstByEnvironmentIgnoreCaseAndOriginAndStatus(
            String environment, PlanOrigin origin, PlanStatus status);

    /** The plan, locked until the end of the transaction. Reports and the sweep take this before they change a plan. */
    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select p from DeploymentPlan p where p.id = :id")
    Optional<DeploymentPlan> findByIdForUpdate(@Param("id") UUID id);

    /** Make the row of an observed plan if it is not there, so that two reports at once cannot both insert it. */
    @Modifying
    @Query(value = """
            INSERT INTO deployment_plans
                (id, number, environment, tag, status, origin, initiator, created_at, last_activity_at)
            VALUES (:id, nextval('deployment_plan_number_seq'), :environment, :tag, 'running', 'observed',
                    :initiator, :createdAt, :createdAt)
            ON CONFLICT (id) DO NOTHING
            """, nativeQuery = true)
    int insertObservedIfAbsent(
            @Param("id") UUID id,
            @Param("environment") String environment,
            @Param("tag") String tag,
            @Param("initiator") String initiator,
            @Param("createdAt") Instant createdAt);
}
