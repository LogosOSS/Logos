package de.tum.cit.aet.logos.logoswebservice.configuration.repository;

import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.transaction.annotation.Transactional;
import de.tum.cit.aet.logos.logoswebservice.configuration.entity.ApiKeyProviderPermission;
import de.tum.cit.aet.logos.logoswebservice.configuration.entity.ApiKeyProviderPermissionId;

public interface ApiKeyProviderPermissionRepository
        extends JpaRepository<ApiKeyProviderPermission, ApiKeyProviderPermissionId> {

    List<ApiKeyProviderPermission> findById_ApiKeyId(Integer apiKeyId);

    @Transactional
    void deleteById_ApiKeyId(Integer apiKeyId);

    /**
     * Transaction-scoped advisory lock serializing one API key's permission
     * mutations (key from {@code PermissionService#apiKeyPermsLockKey});
     * released automatically with the surrounding transaction.
     */
    @Query(value = "SELECT pg_advisory_xact_lock(:key)", nativeQuery = true)
    void lockApiKeyPermissions(@Param("key") long key);
}
