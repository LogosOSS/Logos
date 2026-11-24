package de.tum.cit.aet.logos.logoswebservice.auth;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "logos.auth")
public record KeycloakProperties(
    String clientId,
    int syncDebounceMinutes,
    Sync sync,
    String audience
) {
    public KeycloakProperties {
        if (audience == null || audience.isBlank()) {
            audience = clientId;
        }
    }

    public record Sync(
        boolean enabled,
        String cron,
        String adminBaseUrl,
        String realm,
        String clientId,
        String clientSecret
    ) {}
}
