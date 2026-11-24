package de.tum.cit.aet.logos.logoswebservice.identity;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Duration;
import java.time.Instant;
import java.util.UUID;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import org.awaitility.Awaitility;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.annotation.Import;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.context.jdbc.Sql;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

import de.tum.cit.aet.logos.logoswebservice.TestContainersConfig;
import de.tum.cit.aet.logos.logoswebservice.auth.KeycloakClaims;
import de.tum.cit.aet.logos.logoswebservice.identity.dto.AddTeamMemberRequestDTO;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.ApiKey;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.ApiKeyType;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.Team;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.TeamMember;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.TeamMemberId;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.TeamMemberSource;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.User;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.ApiKeyRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.TeamMemberRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.TeamRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.UserRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.service.ApiKeyFactory;
import de.tum.cit.aet.logos.logoswebservice.identity.service.KeycloakUserSyncService;
import de.tum.cit.aet.logos.logoswebservice.identity.service.TeamService;
import de.tum.cit.aet.logos.logoswebservice.identity.service.UserService;

@SpringBootTest
@Import(TestContainersConfig.class)
@TestPropertySource(properties = {
        "spring.liquibase.enabled=true",
        "spring.liquibase.change-log=classpath:liquibase/changelog/master.xml",
        "logos.auth.roles.logos-admin=itg-admin",
        "logos.auth.roles.app-admin=chair-member",
        "logos.auth.sync-debounce-minutes=5",
        "logos.auth.auto-provision-teams=true"
})
@Sql(scripts = "/sql/seed-identity.sql", executionPhase = Sql.ExecutionPhase.BEFORE_TEST_METHOD)
@Sql(scripts = "/sql/cleanup-identity.sql", executionPhase = Sql.ExecutionPhase.AFTER_TEST_METHOD)
class KeycloakUserSyncServiceTest {

    @Autowired KeycloakUserSyncService syncService;
    @Autowired UserRepository userRepository;
    @MockitoBean JwtDecoder jwtDecoder;
    @Autowired TeamRepository teamRepository;
    @Autowired TeamMemberRepository memberRepository;
    @Autowired ApiKeyRepository apiKeyRepository;
    @Autowired ApiKeyFactory apiKeyFactory;
    @Autowired TeamService teamService;
    @Autowired UserService userService;
    @Autowired JdbcTemplate jdbc;
    @Autowired PlatformTransactionManager txManager;

    private static final String NEW_SUB = "33333333-3333-3333-3333-333333333333";

    private KeycloakClaims claims(String sub, String username) {
        return new KeycloakClaims(sub, username, "Pre", "Name", username + "@tum.de", true, Instant.now());
    }

    @Test
    void concurrentIdentityRefreshReturnsAndPreservesTheCommittedRole() throws Exception {
        var identity = claims(NEW_SUB, "synced");
        int userId = syncService.syncFromClaims(identity).getId();
        jdbc.update("UPDATE users SET role = 'logos_admin', last_synced_at = 'epoch' WHERE id = ?", userId);
        try (var executor = Executors.newSingleThreadExecutor()) {
            Future<User> refresh = new TransactionTemplate(txManager).execute(tx -> {
                int writerPid = jdbc.queryForObject("SELECT pg_backend_pid()", Integer.class);
                userService.updateRole(userId, "app_developer").orElseThrow();
                Future<User> pending = executor.submit(() -> syncService.syncIfStale(identity));
                Awaitility.await().atMost(Duration.ofSeconds(10)).until(() -> jdbc.queryForObject(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE ? = ANY(pg_blocking_pids(pid)))",
                    Boolean.class, writerPid));
                return pending;
            });
            assertThat(refresh.get(10, TimeUnit.SECONDS).getRole()).isEqualTo("app_developer");
            assertThat(userRepository.findById(userId).orElseThrow().getRole()).isEqualTo("app_developer");
        }
    }

    @Test
    void anOlderIdentityCannotReactivateAnAccountAfterTheDebounceExpires() {
        User member = syncService.syncFromClaims(claims(NEW_SUB, "synced"));
        syncService.deactivateUser(member);
        member = userRepository.findById(member.getId()).orElseThrow();
        Instant deactivated = Instant.now().minusSeconds(3600);
        member.setLastSyncedAt(deactivated);
        userRepository.save(member);
        KeycloakClaims stale = new KeycloakClaims(NEW_SUB, "synced", "Pre", "Name", "synced@tum.de", true,
            deactivated.minusSeconds(60));
        assertThat(syncService.syncIfStale(stale).isActive()).isFalse();
        assertThat(syncService.syncFromClaims(stale).isActive()).isFalse();
        assertThat(syncService.syncFromClaims(claims(NEW_SUB, "synced")).isActive()).isTrue();
    }

    @Test
    void firstLogin_withNoRoles_registersUserAndCreatesNoKeys() {
        User user = syncService.syncFromClaims(claims(NEW_SUB, "newbie"));

        assertThat(user.getId()).isNotNull();
        assertThat(user.getKeycloakId()).isEqualTo(UUID.fromString(NEW_SUB));
        assertThat(user.getRole()).isEqualTo("app_developer");
        assertThat(user.isActive()).isTrue();
        assertThat(user.getLastSyncedAt()).isNotNull();
        assertThat(apiKeyRepository.findByUserId(user.getId())).isEmpty();
        apiKeyRepository.findByUserId(user.getId()).forEach(apiKeyRepository::delete);
        userRepository.delete(user);
    }

    @Test
    void newUser_doesNotMintPersonalKey_andDeactivatesExistingOnes() {
        User user = syncService.syncFromClaims(claims(NEW_SUB, "newbie"));
        assertThat(user.getRole()).isEqualTo("app_developer");

        // Admins no longer receive an auto-provisioned team-less "master" key;
        // they obtain keys through team membership like every other user.
        assertThat(apiKeyRepository.findByUserIdAndTeamIdIsNullAndKeyType(user.getId(), ApiKeyType.developer))
            .isEmpty();

        // Any pre-existing team-less developer key is deactivated on next sync.
        ApiKey legacy = apiKeyFactory.createDeveloperKey(user, null);
        legacy.setIsActive(true);
        apiKeyRepository.save(legacy);

        syncService.syncFromClaims(claims(NEW_SUB, "newbie"));
        var personal = apiKeyRepository.findByUserIdAndTeamIdIsNullAndKeyType(user.getId(), ApiKeyType.developer);
        assertThat(personal).hasSize(1);
        assertThat(personal.get(0).getIsActive()).isFalse();

        apiKeyRepository.findByUserId(user.getId()).forEach(apiKeyRepository::delete);
        userRepository.delete(userRepository.findById(user.getId()).orElseThrow());
    }

    @Test
    void existingUserMatchedByEmail_getsLinkedToKeycloakId() {
        User existing = new User();
        existing.setUsername("legacyadmin");
        existing.setPrename("Legacy");
        existing.setName("Admin");
        existing.setRole("app_developer");
        existing.setEmail("legacy-admin@test.com");
        existing = userRepository.saveAndFlush(existing);

        try {
            User user = syncService.syncFromClaims(
                new KeycloakClaims(NEW_SUB, "unknown-username", "P", "N", "legacy-admin@test.com", true, null));
            assertThat(user.getId()).isEqualTo(existing.getId());
            assertThat(user.getKeycloakId()).isEqualTo(UUID.fromString(NEW_SUB));
        } finally {
            userRepository.findByKeycloakId(UUID.fromString(NEW_SUB)).ifPresent(userRepository::delete);
            userRepository.findById(existing.getId()).ifPresent(userRepository::delete);
        }
    }

    @Test
    void unlinkedUserWithTheSameNameButAnotherEmail_isNotAdopted() {
        // Seeded 1005 "Manual User" has no keycloak_id; a namesake must get an account of their own.
        try {
            User user = syncService.syncFromClaims(
                new KeycloakClaims(NEW_SUB, "namesake", "Manual", "User", "namesake@tum.de", true, null));
            assertThat(user.getId()).isNotEqualTo(1005);
            assertThat(userRepository.findById(1005).orElseThrow().getKeycloakId()).isNull();
        } finally {
            userRepository.findByKeycloakId(UUID.fromString(NEW_SUB)).ifPresent(userRepository::delete);
        }
    }

    @Test
    void existingUserWithDifferentKeycloakId_throwsConflictInsteadOfDuplicateEmail() {
        User existing = new User();
        existing.setUsername("recreated");
        existing.setPrename("Re");
        existing.setName("Created");
        existing.setRole("app_developer");
        existing.setEmail("recreated@test.com");
        existing.setKeycloakId(UUID.fromString("11111111-1111-1111-1111-111111111111"));
        existing = userRepository.saveAndFlush(existing);

        try {
            org.junit.jupiter.api.Assertions.assertThrows(
                de.tum.cit.aet.logos.logoswebservice.common.ConflictException.class,
                () -> syncService.syncFromClaims(
                    new KeycloakClaims(NEW_SUB, "recreated", "Re", "Created", "recreated@test.com", true, null)));
            // The login subject must not have been inserted as a second row.
            assertThat(userRepository.findByKeycloakId(UUID.fromString(NEW_SUB))).isEmpty();
        } finally {
            userRepository.findById(existing.getId()).ifPresent(userRepository::delete);
        }
    }

    @Test
    void syncIfStale_skipsWhenRecentlySynced() {
        User seeded = userRepository.findById(1001).orElseThrow();
        Instant before = seeded.getLastSyncedAt();
        User result = syncService.syncIfStale(
            claims(seeded.getKeycloakId().toString(), "testuser"));
        assertThat(result.getRole()).isEqualTo("app_developer");
        assertThat(result.getLastSyncedAt()).isEqualTo(before);
    }

    @Test
    void deactivateUser_disablesUserAndAllKeys() {
        User seeded = userRepository.findById(1001).orElseThrow();
        syncService.deactivateUser(seeded);
        assertThat(userRepository.findById(1001).orElseThrow().isActive()).isFalse();
        assertThat(apiKeyRepository.findById(3001).orElseThrow().getIsActive()).isFalse();
    }

    @Test
    void syncKeepingAdminRole_keepsOwnership() {
        User u1002 = userRepository.findById(1002).orElseThrow();
        User synced = syncService.syncFromClaims(
            claims(u1002.getKeycloakId().toString(), "adminuser"));

        assertThat(synced.getRole()).isEqualTo("app_admin");
        TeamMember m = memberRepository.findById(new TeamMemberId(1002, 2001)).orElseThrow();
        assertThat(m.getIsOwner()).isTrue();
    }

    @Test
    void loginPreservesManualMembership() {
        User u1001 = userRepository.findById(1001).orElseThrow();
        syncService.syncFromClaims(
            claims(u1001.getKeycloakId().toString(), "testuser"));

        TeamMember m = memberRepository.findById(new TeamMemberId(1001, 2001)).orElseThrow();
        assertThat(m.getSource()).isEqualTo(TeamMemberSource.MANUAL);
    }

    /**
     * A key carries its team's permissions and budget and the inference path
     * never re-checks membership, so the key of a team the user was removed
     * from while deactivated must not come back when Keycloak re-enables them.
     * Unlinking the team is one way to produce exactly that state.
     */
    @Test
    void reEnabledUser_doesNotRegainTheKeyOfATeamTheyWereRemovedFrom() {
        Team team = teamRepository.findById(2001).orElseThrow();
        User member = syncService.syncFromClaims(claims(NEW_SUB, "synced"));
        teamService.addMember(team.getId(), new AddTeamMemberRequestDTO(member.getId(), false));
        ApiKey teamKey = apiKeyRepository
            .findByUserIdAndTeamIdAndKeyType(member.getId(), team.getId(), ApiKeyType.developer)
            .getFirst();
        assertThat(teamKey.getIsActive()).isTrue();

        // Keycloak disables the account, and the team is unlinked while it is
        // off — which drops the membership and switches the key off with it.
        syncService.deactivateUser(userRepository.findById(member.getId()).orElseThrow());
        teamService.removeMember(team.getId(), member.getId());
        assertThat(memberRepository.findById(new TeamMemberId(member.getId(), team.getId()))).isEmpty();

        // The account comes back. The key must not.
        syncService.syncFromClaims(claims(NEW_SUB, "synced"));

        assertThat(apiKeyRepository.findById(teamKey.getId()).orElseThrow().getIsActive())
            .as("a key for a team the user is no longer in must stay inactive")
            .isFalse();
    }

    @Test
    void reEnabledUser_regainsTheKeyOfATeamTheyAreStillIn() {
        Team team = teamRepository.findById(2001).orElseThrow();
        User member = syncService.syncFromClaims(claims(NEW_SUB, "synced"));
        teamService.addMember(team.getId(), new AddTeamMemberRequestDTO(member.getId(), false));
        ApiKey teamKey = apiKeyRepository
            .findByUserIdAndTeamIdAndKeyType(member.getId(), team.getId(), ApiKeyType.developer)
            .getFirst();

        syncService.deactivateUser(userRepository.findById(member.getId()).orElseThrow());
        assertThat(apiKeyRepository.findById(teamKey.getId()).orElseThrow().getIsActive()).isFalse();

        syncService.syncFromClaims(claims(NEW_SUB, "synced"));

        assertThat(apiKeyRepository.findById(teamKey.getId()).orElseThrow().getIsActive()).isTrue();
    }

}
