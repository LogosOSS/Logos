package de.tum.cit.aet.logos.logoswebservice.gateway;

import de.tum.cit.aet.logos.logoswebservice.identity.entity.ApiKeyType;

/**
 * Active Logos API key fields the inference gateway needs (no JPA entity).
 *
 * <p>{@code logLevel} is the key's configured request-logging level
 * ({@code BILLING} or {@code FULL}), which decides whether a request's payloads
 * are stored alongside its billing row.
 */
public record GatewayKey(
        int id,
        String keyValue,
        String name,
        ApiKeyType keyType,
        Integer teamId,
        Integer userId,
        String environment,
        boolean useCustomPermissions,
        String settingsJson,
        int defaultPriority,
        String logLevel,
        TeamCloudLimits teamCloudLimits) {

    /** A key without a team-default snapshot retains the separate lookup path. */
    public GatewayKey(int id, String keyValue, String name, ApiKeyType keyType,
                      Integer teamId, Integer userId, String environment,
                      boolean useCustomPermissions, String settingsJson,
                      int defaultPriority, String logLevel) {
        this(id, keyValue, name, keyType, teamId, userId, environment,
            useCustomPermissions, settingsJson, defaultPriority, logLevel, null);
    }

    /** Defaults read with the key; null values mean that no positive team limit is set. */
    public record TeamCloudLimits(Integer rpm, Integer tpm) {
        public TeamCloudLimits {
            rpm = positiveOrNull(rpm);
            tpm = positiveOrNull(tpm);
        }

        private static Integer positiveOrNull(Integer value) {
            return value != null && value > 0 ? value : null;
        }
    }

    /** Whether this key's requests are logged with their payloads. */
    public boolean logsFullPayloads() {
        return "FULL".equalsIgnoreCase(logLevel);
    }
}
