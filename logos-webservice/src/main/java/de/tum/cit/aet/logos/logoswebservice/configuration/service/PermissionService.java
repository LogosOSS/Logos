package de.tum.cit.aet.logos.logoswebservice.configuration.service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import de.tum.cit.aet.logos.logoswebservice.configuration.entity.ApiKeyModelPermission;
import de.tum.cit.aet.logos.logoswebservice.configuration.entity.ApiKeyProviderPermission;
import de.tum.cit.aet.logos.logoswebservice.configuration.entity.TeamModelPermission;
import de.tum.cit.aet.logos.logoswebservice.configuration.entity.TeamProviderPermission;
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.ApiKeyModelPermissionRepository;
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.ApiKeyProviderPermissionRepository;
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.PermissionCatalogProjection;
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.ProviderRepository;
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.TeamModelPermissionRepository;
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.TeamProviderPermissionRepository;

@Service
public class PermissionService {

    /**
     * Stable key space for per-team provider-permission mutation locks:
     * {@code TEAM_PROVIDER_PERMS_LOCK_BASE + teamId}, keeping the keys
     * disjoint from other advisory-lock namespaces.
     */
    private static final long TEAM_PROVIDER_PERMS_LOCK_BASE = 0x5445414D5045524DL; // "TEAMPERM"

    /**
     * Advisory-lock key serializing one team's provider-permission mutations.
     * All mutation paths (full-set PUT, atomic add, atomic remove) acquire
     * {@code pg_advisory_xact_lock} with this key first, so under READ
     * COMMITTED a concurrent mutation cannot commit between another
     * mutation's targeted row write and its model-grant cascade.
     */
    public static long teamProviderPermsLockKey(int teamId) {
        return TEAM_PROVIDER_PERMS_LOCK_BASE + teamId;
    }

    /**
     * Stable key space for per-key permission mutation locks:
     * {@code API_KEY_PERMS_LOCK_BASE + keyId}, disjoint from
     * {@link #TEAM_PROVIDER_PERMS_LOCK_BASE}.
     */
    private static final long API_KEY_PERMS_LOCK_BASE = 0x4150494B50455200L; // "APIKPER"

    /**
     * Advisory-lock key serializing one API key's model and provider
     * permission mutations. Each replacement deletes and re-inserts the key's
     * grants, so without the lock two concurrent replacements under READ
     * COMMITTED can both delete before either inserts and commit the union
     * of both requested sets.
     */
    public static long apiKeyPermsLockKey(int keyId) {
        return API_KEY_PERMS_LOCK_BASE + keyId;
    }

    private final ApiKeyModelPermissionRepository apiKeyModelRepo;
    private final ApiKeyProviderPermissionRepository apiKeyProviderRepo;
    private final TeamModelPermissionRepository teamModelRepo;
    private final TeamProviderPermissionRepository teamProviderRepo;
    private final ProviderRepository providerRepo;

    public PermissionService(ApiKeyModelPermissionRepository apiKeyModelRepo,
                             ApiKeyProviderPermissionRepository apiKeyProviderRepo,
                             TeamModelPermissionRepository teamModelRepo,
                             TeamProviderPermissionRepository teamProviderRepo,
                             ProviderRepository providerRepo) {
        this.apiKeyModelRepo = apiKeyModelRepo;
        this.apiKeyProviderRepo = apiKeyProviderRepo;
        this.teamModelRepo = teamModelRepo;
        this.teamProviderRepo = teamProviderRepo;
        this.providerRepo = providerRepo;
    }

    /**
     * The full Logos catalog an API key's custom permissions can be chosen
     * from: every provider with the models it serves. Custom permissions are
     * not limited to the team's permissions or to the caller's own access, so
     * the catalog is not filtered. It carries only ids and names, never
     * provider URLs or credentials.
     */
    public List<Map<String, Object>> getPermissionCatalog() {
        Map<Integer, Map<String, Object>> providers = new LinkedHashMap<>();
        for (PermissionCatalogProjection row : providerRepo.findPermissionCatalog()) {
            Map<String, Object> provider = providers.computeIfAbsent(row.getProviderId(), id -> {
                Map<String, Object> p = new LinkedHashMap<>();
                p.put("id", id);
                p.put("name", row.getProviderName());
                p.put("models", new ArrayList<Map<String, Object>>());
                return p;
            });
            if (row.getModelId() != null) {
                @SuppressWarnings("unchecked")
                List<Map<String, Object>> models = (List<Map<String, Object>>) provider.get("models");
                models.add(Map.of("model_id", row.getModelId(), "model_name", row.getModelName()));
            }
        }
        return List.copyOf(providers.values());
    }

    public List<Integer> getApiKeyModelPermissions(int keyId) {
        return apiKeyModelRepo.findById_ApiKeyId(keyId).stream()
            .map(p -> p.getId().getModelId()).toList();
    }

    /**
     * Replaces a key's custom model grants. The team's model permissions are
     * the default for keys without custom permissions, not a ceiling: a key
     * with custom permissions (for example an application key) may hold models
     * the team default does not include.
     */
    @Transactional
    public void setApiKeyModelPermissions(int keyId, List<Integer> modelIds) {
        apiKeyProviderRepo.lockApiKeyPermissions(apiKeyPermsLockKey(keyId));
        apiKeyModelRepo.deleteById_ApiKeyId(keyId);
        apiKeyModelRepo.saveAll(modelIds.stream()
            .map(mid -> new ApiKeyModelPermission(keyId, mid)).toList());
    }

    public List<Integer> getApiKeyProviderPermissions(int keyId) {
        return apiKeyProviderRepo.findById_ApiKeyId(keyId).stream()
            .map(p -> p.getId().getProviderId()).toList();
    }

    /**
     * Replaces a key's custom provider grants independently of the team's
     * provider permissions, then drops the key's model grants that none of its
     * remaining providers serve.
     */
    @Transactional
    public void setApiKeyProviderPermissions(int keyId, List<Integer> providerIds) {
        apiKeyProviderRepo.lockApiKeyPermissions(apiKeyPermsLockKey(keyId));
        apiKeyProviderRepo.deleteById_ApiKeyId(keyId);
        apiKeyProviderRepo.saveAll(providerIds.stream()
            .map(pid -> new ApiKeyProviderPermission(keyId, pid)).toList());
        apiKeyModelRepo.deleteCascadeForApiKey(keyId);
    }

    public List<Integer> getTeamModelPermissions(int teamId) {
        return teamModelRepo.findById_TeamId(teamId).stream()
            .map(p -> p.getId().getModelId()).toList();
    }

    @Transactional
    public void setTeamModelPermissions(int teamId, List<Integer> modelIds) {
        teamProviderRepo.lockTeamProviderPermissions(teamProviderPermsLockKey(teamId));
        teamModelRepo.deleteById_TeamId(teamId);
        teamModelRepo.saveAll(modelIds.stream()
            .map(mid -> new TeamModelPermission(teamId, mid)).toList());
    }

    public List<Integer> getTeamProviderPermissions(int teamId) {
        return teamProviderRepo.findById_TeamId(teamId).stream()
            .map(p -> p.getId().getProviderId()).toList();
    }

    @Transactional
    public void setTeamProviderPermissions(int teamId, List<Integer> providerIds) {
        teamProviderRepo.lockTeamProviderPermissions(teamProviderPermsLockKey(teamId));
        teamProviderRepo.deleteById_TeamId(teamId);
        teamProviderRepo.saveAll(providerIds.stream()
            .map(pid -> new TeamProviderPermission(teamId, pid)).toList());
        teamModelRepo.deleteCascadeForTeam(teamId);
    }

    /**
     * Atomic one-click repair (model access page): appends a single
     * team-provider grant. Unlike the full-set PUT above — which works from a
     * client-side snapshot and can clobber a concurrent admin edit — this is a
     * single upsert that touches nothing but the requested row, so the team's
     * other grants and the model-grant cascade are left alone.
     */
    @Transactional
    public void addTeamProviderPermission(int teamId, Integer providerId) {
        teamProviderRepo.lockTeamProviderPermissions(teamProviderPermsLockKey(teamId));
        teamProviderRepo.grantIfAbsent(teamId, providerId);
    }

    /**
     * Atomic counterpart of {@link #addTeamProviderPermission}: removes a
     * single team-provider grant (one-click de-provisioning from the model
     * access page). The targeted delete never reads or replaces the team's
     * other provider grants, so a concurrent permission edit cannot be
     * undone by a stale client snapshot. Unlike the add path, it then runs
     * the model-grant cascade in the same transaction: the revoke itself is
     * what can orphan model grants (revoking the last host provider of a
     * model), and the cascade only prunes models no longer reachable through
     * any of the team's remaining grants — exactly what the full-set PUT
     * does.
     */
    @Transactional
    public void removeTeamProviderPermission(int teamId, Integer providerId) {
        teamProviderRepo.lockTeamProviderPermissions(teamProviderPermsLockKey(teamId));
        teamProviderRepo.revoke(teamId, providerId);
        teamModelRepo.deleteCascadeForTeam(teamId);
    }
}
