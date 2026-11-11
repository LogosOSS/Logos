package de.tum.cit.aet.logos.logoswebservice.deployments;

import java.time.Duration;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.boot.context.properties.bind.DefaultValue;

/**
 * Settings for deployments from Logos. A Logos deploys only itself: its deployers obey it, and no setting names an
 * environment. A Logos without deployers has nothing to deploy, and its page says so.
 *
 * @param protect              whether this Logos asks for a typed confirmation and accepts only the tags of
 *                             {@code restrictedTagPattern}. Default: yes, so that a Logos that nobody configured is
 *                             safe. Prod keeps it; Dev and Test switch it off.
 * @param restrictedTagPattern which tags a protected Logos accepts. Default: {@code latest} and release tags
 * @param silenceLimit         how long a started step may stay silent before Logos ends it as timed out
 * @param onlineAfter          a deployer counts as online if it was seen within this time
 * @param pollHoldMax          the longest time a request for orders waits for one to appear
 */
@ConfigurationProperties(prefix = "logos.deployments")
public record DeploymentProperties(
    @DefaultValue("true") boolean protect,
    String restrictedTagPattern,
    Duration silenceLimit,
    Duration onlineAfter,
    Duration pollHoldMax
) {

    public static final String DEFAULT_RESTRICTED_TAG_PATTERN = "(latest|v[0-9][A-Za-z0-9._-]*)";

    public DeploymentProperties {
        restrictedTagPattern = restrictedTagPattern == null || restrictedTagPattern.isBlank()
            ? DEFAULT_RESTRICTED_TAG_PATTERN : restrictedTagPattern.strip();
        silenceLimit = silenceLimit == null ? Duration.ofMinutes(30) : silenceLimit;
        onlineAfter = onlineAfter == null ? Duration.ofSeconds(90) : onlineAfter;
        pollHoldMax = pollHoldMax == null ? Duration.ofSeconds(30) : pollHoldMax;
    }
}
