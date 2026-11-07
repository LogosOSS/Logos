package de.tum.cit.aet.logos.logoswebservice.deployments.repository;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

import org.springframework.data.jpa.repository.JpaRepository;

import de.tum.cit.aet.logos.logoswebservice.deployments.entity.Deployer;

public interface DeployerRepository extends JpaRepository<Deployer, UUID> {

    Optional<Deployer> findByKeyHash(String keyHash);

    List<Deployer> findAllByOrderByEnvironmentAscKindAscNameAsc();

    List<Deployer> findByEnvironmentOrderByKindAscNameAsc(String environment);

    boolean existsByEnvironmentIgnoreCaseAndNameIgnoreCase(String environment, String name);
}
