package de.tum.cit.aet.logos.logoswebservice.configuration.service;

import java.util.List;
import java.util.Set;
import java.util.function.Function;
import java.util.function.ToIntFunction;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import de.tum.cit.aet.logos.logoswebservice.audit.PermissionAuditContext;
import de.tum.cit.aet.logos.logoswebservice.common.ForbiddenException;
import de.tum.cit.aet.logos.logoswebservice.configuration.entity.ApiKeyModelPermission;
import de.tum.cit.aet.logos.logoswebservice.configuration.entity.ApiKeyProviderPermission;
import de.tum.cit.aet.logos.logoswebservice.configuration.entity.TeamModelPermission;
import de.tum.cit.aet.logos.logoswebservice.configuration.entity.TeamProviderPermission;
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.ApiKeyModelPermissionRepository;
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.ApiKeyProviderPermissionRepository;
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

    private final PermissionAuditContext audit;
    private final ApiKeyModelPermissionRepository apiKeyModelRepo;
    private final ApiKeyProviderPermissionRepository apiKeyProviderRepo;
    private final TeamModelPermissionRepository teamModelRepo;
    private final TeamProviderPermissionRepository teamProviderRepo;

    public PermissionService(ApiKeyModelPermissionRepository apiKeyModelRepo,
                             ApiKeyProviderPermissionRepository apiKeyProviderRepo,
                             TeamModelPermissionRepository teamModelRepo,
                             TeamProviderPermissionRepository teamProviderRepo,
                             PermissionAuditContext audit) {
        this.audit = audit;
        this.apiKeyModelRepo = apiKeyModelRepo;
        this.apiKeyProviderRepo = apiKeyProviderRepo;
        this.teamModelRepo = teamModelRepo;
        this.teamProviderRepo = teamProviderRepo;
    }

    public List<Integer> getApiKeyModelPermissions(int keyId) {
        return apiKeyModelRepo.findById_ApiKeyId(keyId).stream()
            .map(p -> p.getId().getModelId()).toList();
    }

    @Transactional
    public void setApiKeyModelPermissions(int keyId, List<Integer> modelIds) {
        setApiKeyModelPermissions(keyId, modelIds, null);
    }

    @Transactional
    public void setApiKeyModelPermissions(int keyId, List<Integer> modelIds, Integer teamIdConstraint) {
        lockApiKeyPermissions(keyId);
        if (teamIdConstraint != null) {
            teamProviderRepo.lockTeamProviderPermissions(teamProviderPermsLockKey(teamIdConstraint));
            Set<Integer> allowed = teamModelRepo.findById_TeamId(teamIdConstraint).stream()
                .map(p -> p.getId().getModelId()).collect(java.util.stream.Collectors.toSet());
            if (!allowed.containsAll(modelIds)) {
                throw new ForbiddenException("API key model permissions must be a subset of its team's permissions");
            }
        }
        audit.withCause("direct", () -> replaceGrants(
            apiKeyModelRepo, apiKeyModelRepo.findById_ApiKeyId(keyId), modelIds,
            p -> p.getId().getModelId(), id -> new ApiKeyModelPermission(keyId, id)));
    }

    public List<Integer> getApiKeyProviderPermissions(int keyId) {
        return apiKeyProviderRepo.findById_ApiKeyId(keyId).stream()
            .map(p -> p.getId().getProviderId()).toList();
    }

    @Transactional
    public void setApiKeyProviderPermissions(int keyId, List<Integer> providerIds) {
        setApiKeyProviderPermissions(keyId, providerIds, null);
    }

    @Transactional
    public void setApiKeyProviderPermissions(int keyId, List<Integer> providerIds, Integer teamIdConstraint) {
        lockApiKeyPermissions(keyId);
        if (teamIdConstraint != null) {
            teamProviderRepo.lockTeamProviderPermissions(teamProviderPermsLockKey(teamIdConstraint));
            Set<Integer> allowed = teamProviderRepo.findById_TeamId(teamIdConstraint).stream()
                .map(p -> p.getId().getProviderId()).collect(java.util.stream.Collectors.toSet());
            if (!allowed.containsAll(providerIds)) {
                throw new ForbiddenException("API key provider permissions must be a subset of its team's permissions");
            }
        }
        audit.withCause("direct", () -> replaceGrants(
            apiKeyProviderRepo, apiKeyProviderRepo.findById_ApiKeyId(keyId), providerIds,
            p -> p.getId().getProviderId(), id -> new ApiKeyProviderPermission(keyId, id)));
        audit.withCause("cascade", () -> apiKeyModelRepo.deleteCascadeForApiKey(keyId));
    }

    public List<Integer> getTeamModelPermissions(int teamId) {
        return teamModelRepo.findById_TeamId(teamId).stream()
            .map(p -> p.getId().getModelId()).toList();
    }

    @Transactional
    public void setTeamModelPermissions(int teamId, List<Integer> modelIds) {
        teamProviderRepo.lockTeamProviderPermissions(teamProviderPermsLockKey(teamId));
        audit.withCause("direct", () -> replaceGrants(
            teamModelRepo, teamModelRepo.findById_TeamId(teamId), modelIds,
            p -> p.getId().getModelId(), id -> new TeamModelPermission(teamId, id)));
        audit.withCause("cascade", () -> apiKeyModelRepo.deleteOutsideTeamPermissions(teamId));
    }

    public List<Integer> getTeamProviderPermissions(int teamId) {
        return teamProviderRepo.findById_TeamId(teamId).stream()
            .map(p -> p.getId().getProviderId()).toList();
    }

    @Transactional
    public void setTeamProviderPermissions(int teamId, List<Integer> providerIds) {
        teamProviderRepo.lockTeamProviderPermissions(teamProviderPermsLockKey(teamId));
        audit.withCause("direct", () -> replaceGrants(
            teamProviderRepo, teamProviderRepo.findById_TeamId(teamId), providerIds,
            p -> p.getId().getProviderId(), id -> new TeamProviderPermission(teamId, id)));
        audit.withCause("cascade", () -> {
            teamModelRepo.deleteCascadeForTeam(teamId);
            pruneApiKeyPermissionsToTeam(teamId);
        });
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
        audit.withCause("cascade", () -> {
            teamModelRepo.deleteCascadeForTeam(teamId);
            pruneApiKeyPermissionsToTeam(teamId);
        });
    }

    private void lockApiKeyPermissions(int keyId) {
        // Admin overrides also share the team's lock with pruning. Teamless
        // keys use the negative ID space, disjoint from positive team IDs.
        Integer scope = teamProviderRepo.findApiKeyPermissionLockScope(keyId);
        teamProviderRepo.lockTeamProviderPermissions(teamProviderPermsLockKey(scope == null ? -keyId : scope));
    }

    private static <T, ID> void replaceGrants(JpaRepository<T, ID> repository, List<T> current,
                                              List<Integer> requested, ToIntFunction<T> targetId,
                                              Function<Integer, T> create) {
        Set<Integer> desired = Set.copyOf(requested);
        Set<Integer> existing = current.stream().map(targetId::applyAsInt)
            .collect(java.util.stream.Collectors.toSet());
        repository.deleteAll(current.stream().filter(p -> !desired.contains(targetId.applyAsInt(p))).toList());
        repository.saveAll(desired.stream().filter(id -> !existing.contains(id)).map(create).toList());
    }

    private void pruneApiKeyPermissionsToTeam(int teamId) {
        apiKeyProviderRepo.deleteOutsideTeamPermissions(teamId);
        apiKeyModelRepo.deleteOutsideTeamPermissions(teamId);
    }
}
