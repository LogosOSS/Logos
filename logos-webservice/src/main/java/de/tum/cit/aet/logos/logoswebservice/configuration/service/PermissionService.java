package de.tum.cit.aet.logos.logoswebservice.configuration.service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.Function;
import java.util.function.ToIntFunction;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import de.tum.cit.aet.logos.logoswebservice.audit.AuditLogService;
import de.tum.cit.aet.logos.logoswebservice.audit.PermissionAuditContext;
import de.tum.cit.aet.logos.logoswebservice.common.ForbiddenException;
import de.tum.cit.aet.logos.logoswebservice.common.NotFoundException;
import de.tum.cit.aet.logos.logoswebservice.configuration.entity.ApiKeyModelPermission;
import de.tum.cit.aet.logos.logoswebservice.configuration.entity.ApiKeyProviderPermission;
import de.tum.cit.aet.logos.logoswebservice.configuration.entity.CloudProviderType;
import de.tum.cit.aet.logos.logoswebservice.configuration.entity.ProviderType;
import de.tum.cit.aet.logos.logoswebservice.configuration.entity.TeamModelPermission;
import de.tum.cit.aet.logos.logoswebservice.configuration.entity.TeamProviderPermission;
import de.tum.cit.aet.logos.logoswebservice.configuration.entity.TeamProviderPermissionId;
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
     * All mutation paths (full-set PUT, atomic add/remove, Azure opt-in) acquire
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
     * permission mutations. Each replacement reads the current grants before
     * applying a delta, so concurrent replacements must share this lock.
     */
    public static long apiKeyPermsLockKey(int keyId) {
        return API_KEY_PERMS_LOCK_BASE + keyId;
    }

    private final ApiKeyModelPermissionRepository apiKeyModelRepo;
    private final ApiKeyProviderPermissionRepository apiKeyProviderRepo;
    private final TeamModelPermissionRepository teamModelRepo;
    private final TeamProviderPermissionRepository teamProviderRepo;
    private final ProviderRepository providerRepo;
    private final PermissionAuditContext audit;
    private final AuditLogService settingsAudit;

    public PermissionService(ApiKeyModelPermissionRepository apiKeyModelRepo,
                             ApiKeyProviderPermissionRepository apiKeyProviderRepo,
                             TeamModelPermissionRepository teamModelRepo,
                             TeamProviderPermissionRepository teamProviderRepo,
                             ProviderRepository providerRepo,
                             PermissionAuditContext audit, AuditLogService settingsAudit) {
        this.audit = audit;
        this.settingsAudit = settingsAudit;
        this.apiKeyModelRepo = apiKeyModelRepo;
        this.apiKeyProviderRepo = apiKeyProviderRepo;
        this.teamModelRepo = teamModelRepo;
        this.teamProviderRepo = teamProviderRepo;
        this.providerRepo = providerRepo;
    }

    /** A legacy key may have explicit grants without a team. Key access is checked by the controller. */
    public Integer teamForKey(int keyId) {
        return providerRepo.teamForKey(keyId).orElse(null);
    }

    private Set<Integer> eligibleIds(Integer teamId, boolean models) {
        return models ? providerRepo.eligibleModels(teamId) : providerRepo.eligibleProviders(teamId);
    }

    private Set<Integer> checkScope(Integer teamId, boolean models, List<Integer> ids) {
        Set<Integer> eligible = eligibleIds(teamId, models);
        if (!eligible.containsAll(ids)) {
            throw new ForbiddenException(
                "The team's privacy level excludes a requested model or provider.");
        }
        return eligible;
    }

    public List<Map<String, Object>> getPermissionCatalog(Integer teamId) {
        Map<Integer, Map<String, Object>> providers = new LinkedHashMap<>();
        for (PermissionCatalogProjection row : providerRepo.findPermissionCatalog(teamId)) {
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
        Set<Integer> allowed = eligibleIds(teamForKey(keyId), true);
        return apiKeyModelRepo.findById_ApiKeyId(keyId).stream()
            .map(p -> p.getId().getModelId()).filter(allowed::contains).toList();
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
        Set<Integer> eligible = checkScope(teamForKey(keyId), true, modelIds);
        audit.withCause("direct", () -> replaceGrants(
            apiKeyModelRepo, apiKeyModelRepo.findById_ApiKeyId(keyId), modelIds, eligible,
            p -> p.getId().getModelId(), id -> new ApiKeyModelPermission(keyId, id)));
    }

    public List<Integer> getApiKeyProviderPermissions(int keyId) {
        Set<Integer> allowed = eligibleIds(teamForKey(keyId), false);
        return apiKeyProviderRepo.findById_ApiKeyId(keyId).stream()
            .map(p -> p.getId().getProviderId()).filter(allowed::contains).toList();
    }

    /**
     * Replaces a key's custom provider grants independently of the team's
     * provider permissions, then drops the key's model grants that none of its
     * remaining providers serve.
     */
    @Transactional
    public void setApiKeyProviderPermissions(int keyId, List<Integer> providerIds) {
        apiKeyProviderRepo.lockApiKeyPermissions(apiKeyPermsLockKey(keyId));
        Set<Integer> eligible = checkScope(teamForKey(keyId), false, providerIds);
        audit.withCause("direct", () -> replaceGrants(
            apiKeyProviderRepo, apiKeyProviderRepo.findById_ApiKeyId(keyId), providerIds, eligible,
            p -> p.getId().getProviderId(), id -> new ApiKeyProviderPermission(keyId, id)));
        audit.withCause("cascade", () -> apiKeyModelRepo.deleteCascadeForApiKey(keyId));
    }

    public List<Integer> getTeamModelPermissions(int teamId) {
        Set<Integer> allowed = eligibleIds(teamId, true);
        return teamModelRepo.findById_TeamId(teamId).stream()
            .map(p -> p.getId().getModelId()).filter(allowed::contains).toList();
    }

    @Transactional
    public void setTeamModelPermissions(int teamId, List<Integer> modelIds) {
        teamProviderRepo.lockTeamProviderPermissions(teamProviderPermsLockKey(teamId));
        Set<Integer> eligible = checkScope(teamId, true, modelIds);
        audit.withCause("direct", () -> replaceGrants(
            teamModelRepo, teamModelRepo.findById_TeamId(teamId), modelIds, eligible,
            p -> p.getId().getModelId(), id -> new TeamModelPermission(teamId, id)));
    }

    public List<Integer> getTeamProviderPermissions(int teamId) {
        Set<Integer> allowed = eligibleIds(teamId, false);
        return teamProviderRepo.findById_TeamId(teamId).stream()
            .map(p -> p.getId().getProviderId()).filter(allowed::contains).toList();
    }

    @Transactional
    public void setTeamProviderPermissions(int teamId, List<Integer> providerIds) {
        teamProviderRepo.lockTeamProviderPermissions(teamProviderPermsLockKey(teamId));
        Set<Integer> eligible = checkScope(teamId, false, providerIds);
        audit.withCause("direct", () -> replaceGrants(
            teamProviderRepo, teamProviderRepo.findById_TeamId(teamId), providerIds, eligible,
            p -> p.getId().getProviderId(), id -> new TeamProviderPermission(teamId, id)));
        audit.withCause("cascade", () -> teamModelRepo.deleteCascadeForTeam(teamId));
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
        checkScope(teamId, false, List.of(providerId));
        audit.withCause("repair", () -> teamProviderRepo.grantIfAbsent(teamId, providerId));
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
        audit.withCause("direct", () -> teamProviderRepo.revoke(teamId, providerId));
        audit.withCause("cascade", () -> teamModelRepo.deleteCascadeForTeam(teamId));
    }

    public List<Integer> getTeamAzureAutoAccess(int teamId) {
        Set<Integer> allowed = eligibleIds(teamId, false);
        return teamProviderRepo.findAzureAutoAccess(teamId).stream().filter(allowed::contains).toList();
    }

    @Transactional
    public void setTeamAzureAutoAccess(int teamId, int providerId, boolean enabled) {
        teamProviderRepo.lockTeamProviderPermissions(teamProviderPermsLockKey(teamId));
        if (!teamProviderRepo.existsById(new TeamProviderPermissionId(teamId, providerId))) {
            throw new NotFoundException("The team does not have a grant for this provider.");
        }
        if (enabled) {
            checkScope(teamId, false, List.of(providerId));
            var provider = providerRepo.findById(providerId)
                .orElseThrow(() -> new NotFoundException("Provider not found."));
            if (provider.getProviderType() != ProviderType.cloud || provider.getCloudProviderType() != CloudProviderType.azure) {
                throw new IllegalArgumentException("Automatic model access is available only for Azure providers.");
            }
        }
        int changed = enabled
            ? teamProviderRepo.enableAzureAutoAccess(teamId, providerId)
            : teamProviderRepo.disableAzureAutoAccess(teamId, providerId);
        if (changed > 0) {
            settingsAudit.record("team.azure_model_auto_access_changed", "provider", providerId, teamId,
                Map.of("auto_azure_models", !enabled), Map.of("auto_azure_models", enabled));
        }
    }

    private static <T, ID> void replaceGrants(JpaRepository<T, ID> repository, List<T> current,
                                              List<Integer> requested, Set<Integer> eligible, ToIntFunction<T> targetId,
                                              Function<Integer, T> create) {
        Set<Integer> desired = Set.copyOf(requested);
        Set<Integer> existing = current.stream().map(targetId::applyAsInt)
            .collect(java.util.stream.Collectors.toSet());
        // The editor can replace only grants visible under the current privacy scope.
        repository.deleteAll(current.stream().filter(p -> {
            int id = targetId.applyAsInt(p);
            return eligible.contains(id) && !desired.contains(id);
        }).toList());
        repository.saveAll(desired.stream().filter(id -> !existing.contains(id)).map(create).toList());
    }
}
