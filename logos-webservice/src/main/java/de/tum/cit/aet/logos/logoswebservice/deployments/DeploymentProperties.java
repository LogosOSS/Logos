package de.tum.cit.aet.logos.logoswebservice.deployments;

import java.time.Duration;
import java.util.List;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * Settings for deployments from Logos. The feature is off while {@code environment} is empty: a Logos that was
 * installed from the documentation has no deployers and no deployment page.
 *
 * @param environment          the name of the environment this Logos belongs to, for example {@code Prod}
 * @param orderableEnvironments the environments an admin of this Logos may deploy. Default: only its own. Prod lists
 *                             Dev and Test as well, because the deployers of those obey Prod too.
 * @param protectedEnvironments environments that need the typed name of the environment as a confirmation and accept
 *                             only the tags of {@code restrictedTagPattern}. Default: {@code Prod}
 * @param restrictedTagPattern which tags a protected environment accepts. Default: {@code latest} and release tags
 * @param silenceLimit         how long a started step may stay silent before Logos ends it as timed out
 * @param onlineAfter          a deployer counts as online if it was seen within this time
 * @param pollHoldMax          the longest time a request for orders waits for one to appear
 */
@ConfigurationProperties(prefix = "logos.deployments")
public record DeploymentProperties(
    String environment,
    List<String> orderableEnvironments,
    List<String> protectedEnvironments,
    String restrictedTagPattern,
    Duration silenceLimit,
    Duration onlineAfter,
    Duration pollHoldMax
) {

    public static final String DEFAULT_RESTRICTED_TAG_PATTERN = "(latest|v[0-9][A-Za-z0-9._-]*)";

    public DeploymentProperties {
        environment = environment == null ? "" : environment.strip();
        orderableEnvironments = cleaned(orderableEnvironments);
        if (orderableEnvironments.isEmpty() && !environment.isEmpty()) {
            orderableEnvironments = List.of(environment);
        }
        protectedEnvironments = protectedEnvironments == null ? List.of("Prod") : cleaned(protectedEnvironments);
        restrictedTagPattern = restrictedTagPattern == null || restrictedTagPattern.isBlank()
            ? DEFAULT_RESTRICTED_TAG_PATTERN : restrictedTagPattern.strip();
        silenceLimit = silenceLimit == null ? Duration.ofMinutes(30) : silenceLimit;
        onlineAfter = onlineAfter == null ? Duration.ofSeconds(90) : onlineAfter;
        pollHoldMax = pollHoldMax == null ? Duration.ofSeconds(30) : pollHoldMax;
    }

    private static List<String> cleaned(List<String> names) {
        if (names == null) {
            return List.of();
        }
        return names.stream().map(String::strip).filter(name -> !name.isEmpty()).distinct().toList();
    }

    public boolean enabled() {
        return !environment.isEmpty();
    }

    /** The configured spelling of an environment this Logos may deploy, or null. */
    public String orderable(String name) {
        return orderableEnvironments.stream().filter(candidate -> candidate.equalsIgnoreCase(name)).findFirst().orElse(null);
    }

    public boolean isProtected(String name) {
        return protectedEnvironments.stream().anyMatch(candidate -> candidate.equalsIgnoreCase(name));
    }
}
