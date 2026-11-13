package de.tum.cit.aet.logos.logoswebservice.audit;

import java.time.Instant;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

import de.tum.cit.aet.logos.logoswebservice.identity.entity.Role;

/** Read-only history; deleted keys remain visible through their recorded team. */
@RestController
@RequestMapping("/admin/permission-audit-log")
public class PermissionAuditLogController {
    private final PermissionAuditLogService service;

    public PermissionAuditLogController(PermissionAuditLogService service) {
        this.service = service;
    }

    @GetMapping
    @PreAuthorize("hasAuthority('" + Role.Names.LOGOS_ADMIN + "')")
    public ResponseEntity<?> list(
            @RequestParam(name = "team_id", required = false) Integer teamId,
            @RequestParam(name = "api_key_id", required = false) Integer keyId,
            @RequestParam(name = "model_id", required = false) Integer modelId,
            @RequestParam(name = "provider_id", required = false) Integer providerId,
            @RequestParam(name = "from", required = false) Instant from,
            @RequestParam(name = "to", required = false) Instant to,
            @RequestParam(name = "before_id", required = false) Long beforeId,
            @RequestParam(name = "limit", defaultValue = "50") int limit) {
        if (limit < 1 || limit > 200 || (beforeId != null && beforeId < 1)
                || (from != null && to != null && !from.isBefore(to))) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Invalid time range, cursor or limit (1–200)");
        }
        return ResponseEntity.ok(service.list(teamId, keyId, modelId, providerId, from, to, beforeId, limit));
    }
}
