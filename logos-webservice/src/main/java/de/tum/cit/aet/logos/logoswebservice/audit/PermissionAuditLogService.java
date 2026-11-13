package de.tum.cit.aet.logos.logoswebservice.audit;

import java.sql.Timestamp;
import java.time.Instant;
import java.util.List;
import java.util.Map;

import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;

@Service
public class PermissionAuditLogService {
    private final NamedParameterJdbcTemplate jdbc;

    public PermissionAuditLogService(NamedParameterJdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /** Stable descending-ID pagination; names and team ownership are historical snapshots. */
    public List<Map<String, Object>> list(Integer teamId, Integer keyId, Integer modelId,
                                          Integer providerId, Instant from, Instant to,
                                          Long beforeId, int limit) {
        return jdbc.queryForList("""
            SELECT id, occurred_at, actor_user_id, actor_role, team_id, team_name, api_key_id,
                   scope_type, scope_id, scope_name, target_type, target_id, target_name, action, cause
            FROM permission_audit_log
            WHERE (CAST(:teamId AS integer) IS NULL OR team_id = :teamId)
              AND (CAST(:keyId AS integer) IS NULL OR api_key_id = :keyId)
              AND (CAST(:modelId AS integer) IS NULL OR (target_type = 'model' AND target_id = :modelId))
              AND (CAST(:providerId AS integer) IS NULL OR (target_type = 'provider' AND target_id = :providerId))
              AND (CAST(:fromTime AS timestamptz) IS NULL OR occurred_at >= :fromTime)
              AND (CAST(:toTime AS timestamptz) IS NULL OR occurred_at < :toTime)
              AND (CAST(:beforeId AS bigint) IS NULL OR id < :beforeId)
            ORDER BY id DESC
            LIMIT :limit
            """, new MapSqlParameterSource()
                .addValue("teamId", teamId).addValue("keyId", keyId)
                .addValue("modelId", modelId).addValue("providerId", providerId)
                .addValue("fromTime", from == null ? null : Timestamp.from(from))
                .addValue("toTime", to == null ? null : Timestamp.from(to))
                .addValue("beforeId", beforeId).addValue("limit", limit))
            .stream().map(row -> {
                row.put("occurred_at", ((Timestamp) row.get("occurred_at")).toInstant().toString());
                return row;
            }).toList();
    }
}
