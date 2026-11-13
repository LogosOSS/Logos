package de.tum.cit.aet.logos.logoswebservice.deployments.service;

import java.util.regex.Pattern;

import org.springframework.stereotype.Component;

import de.tum.cit.aet.logos.logoswebservice.deployments.DeploymentProperties;

/**
 * Which image tags this Logos may be given. Every tag must be a Docker tag, because the deployer passes it to Compose.
 * A protected Logos (Prod) accepts only {@code latest} and release tags, which come from reviewed code. A pull request
 * build can run on Dev and Test, which are not protected.
 */
@Component
public class TagPolicy {

    /** The grammar of a Docker tag, and the same limit the deployers check before they use a tag. */
    private static final Pattern DOCKER_TAG = Pattern.compile("[A-Za-z0-9_][A-Za-z0-9._-]{0,127}");
    private static final String RESERVED = "previous";

    private final DeploymentProperties properties;
    private final Pattern restricted;

    public TagPolicy(DeploymentProperties properties) {
        this.properties = properties;
        this.restricted = Pattern.compile(properties.restrictedTagPattern());
    }

    /** @throws IllegalArgumentException with a message for the admin if the tag is not allowed */
    public void check(String tag) {
        if (tag == null || !DOCKER_TAG.matcher(tag).matches()) {
            throw new IllegalArgumentException("The tag is not a Docker image tag.");
        }
        if (RESERVED.equals(tag)) {
            throw new IllegalArgumentException("The tag 'previous' is reserved for the previous version on the hosts.");
        }
        if (properties.protect() && !restricted.matcher(tag).matches()) {
            throw new IllegalArgumentException(
                "This Logos accepts only 'latest' and release tags, not '" + tag + "'.");
        }
    }
}
