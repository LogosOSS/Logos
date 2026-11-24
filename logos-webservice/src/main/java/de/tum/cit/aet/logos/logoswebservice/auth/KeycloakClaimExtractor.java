package de.tum.cit.aet.logos.logoswebservice.auth;


import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.stereotype.Component;

@Component
public class KeycloakClaimExtractor {

    public KeycloakClaims extract(Jwt jwt) {
        return new KeycloakClaims(
            jwt.getSubject(),
            jwt.getClaimAsString("preferred_username"),
            orEmpty(jwt.getClaimAsString("given_name")),
            orEmpty(jwt.getClaimAsString("family_name")),
            jwt.getClaimAsString("email"),
            Boolean.TRUE.equals(jwt.getClaims().get("email_verified")),
            jwt.getIssuedAt());
    }

    private static String orEmpty(String s) {
        return s != null ? s : "";
    }
}
