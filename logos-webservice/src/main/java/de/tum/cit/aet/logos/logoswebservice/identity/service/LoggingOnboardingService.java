package de.tum.cit.aet.logos.logoswebservice.identity.service;

import java.util.Map;
import java.util.Optional;

import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import de.tum.cit.aet.logos.logoswebservice.audit.AuditLogService;

@Service
public class LoggingOnboardingService {

    private static final String ELIGIBLE_KEY = """
        ak.user_id = :userId
        AND ak.key_type = 'developer'
        AND ak.is_active = true
        AND ak.log = 'BILLING'
        AND ak.internal_service IS NULL
        AND EXISTS (
            SELECT 1 FROM team_members tm
            WHERE tm.user_id = ak.user_id AND tm.team_id = ak.team_id
        )
        """;

    private final NamedParameterJdbcTemplate jdbc;
    private final AuditLogService auditLog;

    public LoggingOnboardingService(NamedParameterJdbcTemplate jdbc, AuditLogService auditLog) {
        this.jdbc = jdbc;
        this.auditLog = auditLog;
    }

    public boolean isPending(int userId) {
        return Boolean.TRUE.equals(jdbc.queryForObject("""
            SELECT NOT EXISTS (SELECT 1 FROM user_logging_onboarding WHERE user_id = :userId)
               AND EXISTS (SELECT 1 FROM api_keys ak WHERE %s)
            """.formatted(ELIGIBLE_KEY), Map.of("userId", userId), Boolean.class));
    }

    @Transactional
    public Optional<Boolean> complete(int userId, boolean fullLogging) {
        int recorded = jdbc.update("""
            INSERT INTO user_logging_onboarding (user_id, full_logging)
            SELECT :userId, :fullLogging
            WHERE EXISTS (SELECT 1 FROM api_keys ak WHERE %s)
            ON CONFLICT (user_id) DO NOTHING
            """.formatted(ELIGIBLE_KEY), Map.of("userId", userId, "fullLogging", fullLogging));
        if (recorded == 0) {
            // A concurrent submission may have won the insert. Read its
            // committed choice so a retry can distinguish a conflict.
            return jdbc.queryForList("""
                SELECT full_logging FROM user_logging_onboarding WHERE user_id = :userId
                """, Map.of("userId", userId), Boolean.class).stream().findFirst();
        }
        if (fullLogging) enableFullLogging(userId);
        return Optional.of(fullLogging);
    }

    private void enableFullLogging(int userId) {
        var keys = jdbc.query("""
            UPDATE api_keys ak SET log = 'FULL'
            WHERE %s
            RETURNING ak.id, ak.team_id
            """.formatted(ELIGIBLE_KEY), Map.of("userId", userId),
            (rs, row) -> new ChangedKey(rs.getInt("id"), rs.getInt("team_id")));
        for (ChangedKey key : keys) {
            auditLog.record("api_key.log_level_changed", "api_key", key.id(), key.teamId(),
                Map.of("log", "BILLING"), Map.of("log", "FULL"));
        }
    }

    private record ChangedKey(int id, int teamId) {}
}
