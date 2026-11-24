package de.tum.cit.aet.logos.logoswebservice.identity.service;

import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Objects;
import java.util.Optional;
import java.util.UUID;

import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import de.tum.cit.aet.logos.logoswebservice.auth.KeycloakClaims;
import de.tum.cit.aet.logos.logoswebservice.auth.KeycloakProperties;
import de.tum.cit.aet.logos.logoswebservice.common.ConflictException;
import de.tum.cit.aet.logos.logoswebservice.common.RequestTiming;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.ApiKey;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.ApiKeyType;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.Role;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.User;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.ApiKeyRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.TeamMemberRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.TeamRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.UserRepository;

@Service
public class KeycloakUserSyncService {

    private final UserRepository userRepository;
    private final TeamRepository teamRepository;
    private final TeamMemberRepository memberRepository;
    private final ApiKeyRepository apiKeyRepository;
    private final KeycloakProperties props;

    public KeycloakUserSyncService(UserRepository userRepository, TeamRepository teamRepository,
                                   TeamMemberRepository memberRepository, ApiKeyRepository apiKeyRepository,
                                   KeycloakProperties props) {
        this.userRepository = userRepository;
        this.teamRepository = teamRepository;
        this.memberRepository = memberRepository;
        this.apiKeyRepository = apiKeyRepository;
        this.props = props;
    }

    @Transactional(readOnly = true)
    public Optional<User> findIfFresh(KeycloakClaims claims) {
        Instant cutoff = Instant.now().minus(Duration.ofMinutes(props.syncDebounceMinutes()));
        return userRepository.findByKeycloakId(UUID.fromString(claims.keycloakId()))
            .filter(user -> user.isActive()
                ? user.getLastSyncedAt() != null && user.getLastSyncedAt().isAfter(cutoff)
                : !canReactivate(user, claims));
    }

    private static boolean canReactivate(User user, KeycloakClaims claims) {
        // A signed token or directory observation must postdate the account deactivation.
        return claims.observedAt() != null
            && (user.getLastSyncedAt() == null || claims.observedAt().isAfter(user.getLastSyncedAt()));
    }

    @Transactional
    public User syncIfStale(KeycloakClaims claims) {
        return findIfFresh(claims).orElseGet(() -> syncFromClaims(claims));
    }

    @Transactional
    public User syncFromClaims(KeycloakClaims claims) {
        long started = System.nanoTime();
        UUID keycloakId = UUID.fromString(claims.keycloakId());
        User user = findOrCreate(keycloakId, claims);

        boolean isNew = user.getId() == null;
        boolean wasInactive = !isNew && !user.isActive();
        if (wasInactive && !canReactivate(user, claims)) return user;

        user.setKeycloakId(keycloakId);
        if (isNew) {
            user.setRole(Role.Names.APP_DEVELOPER);
            String prename = claims.prename() != null ? claims.prename() : "";
            String name = claims.name() != null ? claims.name() : "";
            String supplied = claims.username();
            if (supplied != null && !supplied.isBlank()
                && !userRepository.existsByUsername(supplied)) {
                user.setUsername(supplied);
            } else {
                user.setUsername(generateUniqueUsername(prename, name));
            }
        }
        user.setPrename(claims.prename() != null ? claims.prename() : "");
        user.setName(claims.name() != null ? claims.name() : "");
        if (claims.email() != null) user.setEmail(claims.email());
        user.setActive(true);
        user.setLastSyncedAt(Instant.now());
        user = userRepository.save(user);

        long userSaved = System.nanoTime();
        List<Integer> teamIds = wasInactive ? apiKeyRepository.findByUserId(user.getId()).stream()
            .map(ApiKey::getTeamId).filter(Objects::nonNull).distinct().sorted().toList() : List.of();
        long keysResolved = System.nanoTime();
        teamIds.forEach(teamRepository::findByIdForUpdate);
        long teamsLocked = System.nanoTime();
        if (wasInactive) reactivateUserKeys(user);
        deactivatePersonalKeys(user);
        RequestTiming.recordSync(userSaved - started, keysResolved - userSaved, teamsLocked - keysResolved,
            System.nanoTime() - teamsLocked, teamIds.size());
        return user;
    }

    @Transactional
    public void deactivateUser(User user) {
        user = userRepository.findByIdForUpdate(user.getId()).orElseThrow();
        user.setActive(false);
        user.setLastSyncedAt(Instant.now());
        userRepository.save(user);
        apiKeyRepository.findByUserId(user.getId()).forEach(k -> {
            k.setIsActive(false);
            apiKeyRepository.save(k);
        });
    }

    private User findOrCreate(UUID keycloakId, KeycloakClaims claims) {
        Optional<User> byId = userRepository.findByKeycloakIdForUpdate(keycloakId);
        if (byId.isPresent()) return byId.get();

        if (claims.email() != null) {
            Optional<User> byEmail = userRepository.findByEmailForUpdate(claims.email());
            if (byEmail.isPresent()) {
                User existing = byEmail.get();
                // Adopt an as-yet-unlinked row (seed/admin-created user signing in for the first time).
                if (existing.getKeycloakId() == null && existing.isActive() && claims.verifiedEmail()) return existing;
                if (existing.getKeycloakId() == null) {
                    throw new ConflictException("Linking this account requires an active user and verified email.");
                }
                if (keycloakId.equals(existing.getKeycloakId())) return existing;
                // The email belongs to a *different* Keycloak subject (we already established no row
                // matches the current sub). We deliberately do not relink — letting a new subject
                // absorb an existing account is an account-takeover vector. Falling through would
                // attempt an insert that dies with an opaque idx_users_email violation, so surface a
                // clear, actionable 409 instead. Happens when an account is recreated or migrated.
                throw new ConflictException(
                    "Email " + claims.email() + " is already linked to Keycloak subject "
                    + existing.getKeycloakId() + ", but this login presented subject " + keycloakId
                    + ". The user's keycloak_id must be reconciled before they can sign in.");
            }
        }

        // A matching name is no proof of identity: two people can share one, so an
        // unlinked account is adopted by email only.
        return new User();
    }

    private String generateUniqueUsername(String prename, String name) {
        String p = prename.toLowerCase().replaceAll("[^a-z]", "");
        String n = name.toLowerCase().replaceAll("[^a-z]", "");
        if (p.isEmpty() && n.isEmpty()) p = "user";

        for (int i = 1; i <= p.length(); i++) {
            String candidate = p.substring(0, i) + n;
            if (!userRepository.existsByUsername(candidate)) return candidate;
        }
        if (p.isEmpty() && !userRepository.existsByUsername(n)) return n;

        String base = p.isEmpty() ? n : p.substring(0, 1) + n;
        for (int i = 1; i <= 999; i++) {
            String candidate = base + i;
            if (!userRepository.existsByUsername(candidate)) return candidate;
        }
        return base + UUID.randomUUID().toString().replace("-", "").substring(0, 6);
    }

    /**
     * Switches the user's keys back on when Keycloak re-enables their account —
     * except the ones bound to a team they are no longer in.
     *
     * The caller locks the teams before checking membership and reactivating keys.
     */
    private void reactivateUserKeys(User user) {
        apiKeyRepository.findByUserId(user.getId()).forEach(k -> {
            if (k.getTeamId() != null && !memberRepository.isMember(k.getTeamId(), user.getId())) return;
            k.setIsActive(true);
            apiKeyRepository.save(k);
        });
    }

    /**
     * Deactivate any team-less ("personal") developer keys.
     *
     * <p>logos_admins used to receive an auto-provisioned team-less key that the
     * orchestrator treated as a master key (unlimited model/provider access, no
     * rate limit or budget). Admins now obtain keys through team membership like
     * every other user, so no team-less key is ever minted and any lingering one
     * is deactivated.
     */
    private void deactivatePersonalKeys(User user) {
        List<ApiKey> personal =
            apiKeyRepository.findByUserIdAndTeamIdIsNullAndKeyType(user.getId(), ApiKeyType.developer);
        personal.forEach(k -> { k.setIsActive(false); apiKeyRepository.save(k); });
    }

}
