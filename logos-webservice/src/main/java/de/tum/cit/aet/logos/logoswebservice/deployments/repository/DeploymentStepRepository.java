package de.tum.cit.aet.logos.logoswebservice.deployments.repository;

import java.time.Instant;
import java.util.Collection;
import java.util.List;
import java.util.UUID;

import org.springframework.data.jpa.repository.JpaRepository;

import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeploymentStep;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.StepStatus;

public interface DeploymentStepRepository extends JpaRepository<DeploymentStep, UUID> {

    List<DeploymentStep> findByPlanIdOrderByPositionAscDeployerNameAsc(UUID planId);

    List<DeploymentStep> findByPlanIdIn(Collection<UUID> planIds);

    List<DeploymentStep> findByDeployerIdAndStatus(UUID deployerId, StepStatus status);

    List<DeploymentStep> findByStatusInAndLastActivityAtBefore(Collection<StepStatus> statuses, Instant cutoff);
}
