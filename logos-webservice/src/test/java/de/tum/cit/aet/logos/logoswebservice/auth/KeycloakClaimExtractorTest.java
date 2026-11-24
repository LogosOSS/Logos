package de.tum.cit.aet.logos.logoswebservice.auth;


import org.junit.jupiter.api.Test;
import org.springframework.security.oauth2.jwt.Jwt;
import static org.assertj.core.api.Assertions.assertThat;

class KeycloakClaimExtractorTest {

    private final KeycloakClaimExtractor extractor = new KeycloakClaimExtractor();

    private Jwt.Builder baseJwt() {
        return Jwt.withTokenValue("token")
            .header("alg", "RS256")
            .subject("11111111-1111-1111-1111-111111111111")
            .claim("preferred_username", "alice")
            .claim("given_name", "Alice")
            .claim("family_name", "Artemis")
            .claim("email", "alice@tum.de");
    }

    @Test
    void extractsIdentityFields() {
        KeycloakClaims claims = extractor.extract(baseJwt().build());
        assertThat(claims.keycloakId()).isEqualTo("11111111-1111-1111-1111-111111111111");
        assertThat(claims.username()).isEqualTo("alice");
        assertThat(claims.prename()).isEqualTo("Alice");
        assertThat(claims.name()).isEqualTo("Artemis");
        assertThat(claims.email()).isEqualTo("alice@tum.de");
    }

    @Test
    void requiresAnExplicitVerifiedEmailClaim() {
        assertThat(extractor.extract(baseJwt().build()).verifiedEmail()).isFalse();
        assertThat(extractor.extract(baseJwt().claim("email_verified", true).build()).verifiedEmail()).isTrue();
    }

    @Test
    void missingClaimsYieldNullUsername() {
        Jwt jwt = Jwt.withTokenValue("token").header("alg", "RS256")
            .subject("22222222-2222-2222-2222-222222222222").build();
        KeycloakClaims claims = extractor.extract(jwt);
        assertThat(claims.verifiedEmail()).isFalse();
        assertThat(claims.username()).isNull();
        assertThat(claims.prename()).isEmpty();
        assertThat(claims.name()).isEmpty();
    }
}
