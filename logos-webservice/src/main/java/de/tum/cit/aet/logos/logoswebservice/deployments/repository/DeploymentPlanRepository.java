package de.tum.cit.aet.logos.logoswebservice.deployments.repository;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeploymentPlan;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.PlanStatus;
import jakarta.persistence.LockModeType;

public interface DeploymentPlanRepository extends JpaRepository<DeploymentPlan, UUID> {

    List<DeploymentPlan> findTop50ByOrderByCreatedAtDesc();

    List<DeploymentPlan> findTop30ByOrderByCreatedAtDesc();

    /** The next number for a deployment. */
    @Query(value = "SELECT nextval('deployment_plan_number_seq')", nativeQuery = true)
    long nextNumber();

    Optional<DeploymentPlan> findFirstByStatus(PlanStatus status);

    /** The plan, locked until the end of the transaction. Reports and the sweep take this before they change a plan. */
    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select p from DeploymentPlan p where p.id = :id")
    Optional<DeploymentPlan> findByIdForUpdate(@Param("id") UUID id);
}
