package de.tum.cit.aet.logos.logoswebservice.auth;

import java.time.Instant;

public record KeycloakClaims(
    String keycloakId,
    String username,
    String prename,
    String name,
    String email,
    boolean verifiedEmail,
    Instant observedAt
) {}
