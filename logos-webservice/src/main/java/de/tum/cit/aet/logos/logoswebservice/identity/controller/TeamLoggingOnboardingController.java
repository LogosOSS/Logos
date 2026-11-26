package de.tum.cit.aet.logos.logoswebservice.identity.controller;

import java.util.Map;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestAttribute;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

import de.tum.cit.aet.logos.logoswebservice.auth.AuthContext;
import de.tum.cit.aet.logos.logoswebservice.identity.dto.LoggingOnboardingRequestDTO;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.Role;
import de.tum.cit.aet.logos.logoswebservice.identity.service.LoggingOnboardingService;

@RestController
@PreAuthorize("hasAnyAuthority('" + Role.Names.LOGOS_ADMIN + "', '" + Role.Names.APP_ADMIN
    + "', '" + Role.Names.APP_DEVELOPER + "')")
public class TeamLoggingOnboardingController {

    private final LoggingOnboardingService service;

    public TeamLoggingOnboardingController(LoggingOnboardingService service) {
        this.service = service;
    }

    @GetMapping("/teams/{teamId}/logging-onboarding")
    public ResponseEntity<Map<String, Boolean>> getStatus(
            @PathVariable int teamId, @RequestAttribute("authContext") AuthContext auth) {
        if (auth.userId() == null) return ResponseEntity.status(HttpStatus.FORBIDDEN).build();
        return ResponseEntity.ok(Map.of("pending", service.isPending(teamId, auth.userId())));
    }

    @PostMapping("/teams/{teamId}/logging-onboarding")
    public ResponseEntity<Map<String, Boolean>> complete(
            @PathVariable int teamId,
            @RequestAttribute("authContext") AuthContext auth,
            @RequestBody LoggingOnboardingRequestDTO request) {
        if (auth.userId() == null) return ResponseEntity.status(HttpStatus.FORBIDDEN).build();
        if (request.full_logging() == null) return ResponseEntity.badRequest().build();
        boolean saved = service.complete(teamId, auth.userId(), request.full_logging());
        if (saved != request.full_logging()) {
            return ResponseEntity.status(HttpStatus.CONFLICT).body(Map.of("full_logging", saved));
        }
        return ResponseEntity.noContent().build();
    }
}
