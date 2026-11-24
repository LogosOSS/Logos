package de.tum.cit.aet.logos.logoswebservice.configuration.repository;

import java.util.List;
import java.util.Optional;
import java.util.Set;

import jakarta.persistence.LockModeType;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import de.tum.cit.aet.logos.logoswebservice.configuration.entity.Provider;

public interface ProviderRepository extends JpaRepository<Provider, Integer> {

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select p from Provider p where p.id = :id")
    Optional<Provider> findForUpdate(@Param("id") Integer id);

    List<Provider> findByCloudProviderTypeIsNotNull();

    /**
     * The logosnode providers that have this key. The key of a cloud provider is a key of another service, so only the
     * type {@code logosnode} counts, like in the orchestrator. At most two rows are read: a key that several providers
     * share does not name one worker, and the caller has to refuse it.
     */
    @Query(value = """
        SELECT id, name
        FROM providers
        WHERE api_key = :apiKey AND provider_type = 'logosnode'
        ORDER BY id
        LIMIT 2
        """, nativeQuery = true)
    List<ProviderIdentity> findLogosnodeByApiKey(@Param("apiKey") String apiKey);

    @Query(value = """
        SELECT id, name, base_url, api_key, provider_type::text, cloud_provider_type::text,
               privacy_level::text, eu_processing_verified, auth_name, auth_format
        FROM providers ORDER BY name ASC, id ASC
        """, nativeQuery = true)
    List<ProviderProjection> findAllForAdmin();

    @Query(value = """
        WITH effective_provider_ids AS (
            SELECT DISTINCT tpp.provider_id AS id
            FROM allowed_team_provider_grants tpp
            JOIN team_members tm ON tpp.team_id = tm.team_id
            WHERE tm.user_id = :userId
            UNION
            SELECT DISTINCT akpp.provider_id AS id
            FROM allowed_key_provider_grants akpp
            JOIN api_keys ak ON akpp.api_key_id = ak.id
            WHERE ak.user_id = :userId AND ak.is_active = true AND ak.use_custom_permissions = true
        )
        SELECT DISTINCT p.id, p.name, p.base_url, NULL::text AS api_key, p.provider_type::text,
               p.cloud_provider_type::text, p.privacy_level::text, p.eu_processing_verified, p.auth_name, p.auth_format
        FROM providers p
        JOIN effective_provider_ids ep ON p.id = ep.id
        ORDER BY p.name ASC
        """, nativeQuery = true)
    List<ProviderProjection> findAllForUser(@Param("userId") Integer userId);

    /**
     * Every provider with the models it serves, for choosing the custom
     * permissions of an API key. Selects names and ids only.
     */
    @Query(value = """
        SELECT p.id AS provider_id, p.name AS provider_name, m.id AS model_id, m.name AS model_name
        FROM providers p
        LEFT JOIN model_provider mp ON mp.provider_id = p.id
        LEFT JOIN models m ON m.id = mp.model_id
        WHERE CAST(:teamId AS integer) IS NULL OR EXISTS (
            SELECT 1 FROM team_privacy_providers scope WHERE scope.provider_id = p.id AND scope.team_id = :teamId)
        ORDER BY p.name ASC, p.id ASC, m.name ASC, m.id ASC
        """, nativeQuery = true)
    List<PermissionCatalogProjection> findPermissionCatalog(@Param("teamId") Integer teamId);

    @Query(value = """
        SELECT m.id FROM models m WHERE CAST(:teamId AS integer) IS NULL
        UNION
        SELECT DISTINCT mp.model_id FROM team_privacy_providers p
        JOIN model_provider mp ON mp.provider_id = p.provider_id WHERE p.team_id = :teamId
        UNION
        SELECT m.id FROM models m, teams t WHERE t.id = :teamId AND t.privacy_scope = 'WORLDWIDE'
        AND NOT EXISTS (SELECT 1 FROM model_provider mp WHERE mp.model_id = m.id)
        """, nativeQuery = true)
    Set<Integer> eligibleModels(@Param("teamId") Integer teamId);

    @Query(value = "SELECT id FROM providers WHERE CAST(:teamId AS integer) IS NULL UNION SELECT provider_id FROM team_privacy_providers WHERE team_id = :teamId", nativeQuery = true)
    Set<Integer> eligibleProviders(@Param("teamId") Integer teamId);

    @Query(value = "SELECT team_id FROM api_keys WHERE id = :keyId", nativeQuery = true)
    Optional<Integer> teamForKey(@Param("keyId") int keyId);

    @Query(value = """
        SELECT EXISTS (
            SELECT 1
            FROM allowed_team_provider_grants tpp
            JOIN team_members tm ON tm.team_id = tpp.team_id
            WHERE tm.user_id = :userId AND tpp.provider_id = :providerId
            UNION ALL
            SELECT 1
            FROM allowed_key_provider_grants akpp
            JOIN api_keys ak ON ak.id = akpp.api_key_id
            WHERE ak.user_id = :userId
              AND ak.is_active = true
              AND ak.use_custom_permissions = true
              AND akpp.provider_id = :providerId
        )
        """, nativeQuery = true)
    boolean isAccessibleToUser(@Param("providerId") int providerId, @Param("userId") int userId);
}
