package de.tum.cit.aet.logos.logoswebservice.configuration.repository;

import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
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

    @Transactional
    @Modifying
    @Query(value = """
        DELETE FROM api_key_provider_permissions akpp
        USING api_keys ak
        WHERE akpp.api_key_id = ak.id
          AND ak.team_id = :teamId
          AND NOT EXISTS (
              SELECT 1 FROM team_provider_permissions tpp
              WHERE tpp.team_id = ak.team_id AND tpp.provider_id = akpp.provider_id
          )
        """, nativeQuery = true)
    void deleteOutsideTeamPermissions(@Param("teamId") int teamId);
}
