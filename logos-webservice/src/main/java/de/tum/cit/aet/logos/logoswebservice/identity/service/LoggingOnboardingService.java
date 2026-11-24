package de.tum.cit.aet.logos.logoswebservice.identity.service;

import java.util.List;
import java.util.Map;
import java.util.Optional;

import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.server.ResponseStatusException;

import de.tum.cit.aet.logos.logoswebservice.audit.AuditLogService;
import de.tum.cit.aet.logos.logoswebservice.identity.dto.TeamResponseDTO;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.TeamRepository;

@Service
public class LoggingOnboardingService {

    private static final String ELIGIBLE_KEY = """
        ak.user_id = :userId
        AND ak.key_type = 'developer'
        AND ak.is_active = true
        AND ak.internal_service IS NULL
        AND EXISTS (
            SELECT 1 FROM team_members tm
            WHERE tm.user_id = ak.user_id AND tm.team_id = ak.team_id
        )
        """;
    private static final String CHOICE_ACTION = "team_member.logging_onboarding_completed";

    private final NamedParameterJdbcTemplate jdbc;
    private final AuditLogService auditLog;
    private final TeamRepository teamRepository;

    public LoggingOnboardingService(
            NamedParameterJdbcTemplate jdbc, AuditLogService auditLog, TeamRepository teamRepository) {
        this.jdbc = jdbc;
        this.auditLog = auditLog;
        this.teamRepository = teamRepository;
    }

    public List<TeamResponseDTO> pendingTeams(int userId) {
        return jdbc.query("""
            SELECT DISTINCT t.id, t.name
            FROM api_keys ak
            JOIN teams t ON t.id = ak.team_id
            WHERE %s AND ak.log = 'BILLING'
              AND NOT EXISTS (
                SELECT 1 FROM audit_log a
                WHERE a.team_id = t.id AND a.action = :action
                  AND a.target_type = 'team_member' AND a.target_id = concat(t.id, '/', :userId)
              )
            ORDER BY t.name, t.id
            """.formatted(ELIGIBLE_KEY), Map.of("userId", userId, "action", CHOICE_ACTION),
            (rs, row) -> new TeamResponseDTO(rs.getInt("id"), rs.getString("name")));
    }

    @Transactional
    public boolean complete(int teamId, int userId, boolean fullLogging) {
        // Serialize first choices and recheck membership after locking the team.
        teamRepository.findByIdForUpdate(teamId)
            .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND, "Team not found"));
        var levels = eligibleKeyLevels(teamId, userId);
        if (levels.isEmpty()) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "An active developer key in this team is required");
        }

        var saved = recordedChoice(teamId, userId);
        if (saved.isPresent()) {
            return saved.get();
        }
        if (!levels.contains("BILLING")) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "An active billing-only developer key in this team is required");
        }
        if (fullLogging) enableFullLogging(teamId, userId);
        auditLog.record(CHOICE_ACTION, "team_member", choiceTarget(teamId, userId), teamId,
            Map.of(), Map.of("full_logging", fullLogging));
        return fullLogging;
    }

    private List<String> eligibleKeyLevels(int teamId, int userId) {
        return jdbc.queryForList("""
            SELECT ak.log::text FROM api_keys ak WHERE ak.team_id = :teamId AND %s
            """.formatted(ELIGIBLE_KEY), Map.of("teamId", teamId, "userId", userId), String.class);
    }

    private Optional<Boolean> recordedChoice(int teamId, int userId) {
        return jdbc.queryForList("""
            SELECT CAST(after_state ->> 'full_logging' AS boolean)
            FROM audit_log
            WHERE team_id = :teamId AND action = :action AND target_type = 'team_member' AND target_id = :target
            ORDER BY id
            LIMIT 1
            """, Map.of("teamId", teamId, "action", CHOICE_ACTION, "target", choiceTarget(teamId, userId)),
            Boolean.class).stream().findFirst();
    }

    private void enableFullLogging(int teamId, int userId) {
        var keyIds = jdbc.queryForList("""
            UPDATE api_keys ak SET log = 'FULL'
            WHERE ak.team_id = :teamId AND %s AND ak.log = 'BILLING'
            RETURNING ak.id
            """.formatted(ELIGIBLE_KEY), Map.of("teamId", teamId, "userId", userId), Integer.class);
        for (int keyId : keyIds) {
            auditLog.record("api_key.log_level_changed", "api_key", keyId, teamId,
                Map.of("log", "BILLING"), Map.of("log", "FULL"));
        }
    }

    private static String choiceTarget(int teamId, int userId) {
        return teamId + "/" + userId;
    }
}
