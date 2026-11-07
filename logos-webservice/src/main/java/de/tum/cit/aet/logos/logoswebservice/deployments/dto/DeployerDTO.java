package de.tum.cit.aet.logos.logoswebservice.deployments.dto;

import java.time.Instant;
import java.util.UUID;

import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * A deployer as the page shows it. {@code online} means it was seen recently; the page greys out the button of an
 * environment whose core deployer is not online. The key is never part of this: it is shown once, in
 * {@link Created}.
 */
public record DeployerDTO(
    UUID id,
    String name,
    String environment,
    String kind,
    boolean online,
    @JsonProperty("last_seen_at") Instant lastSeenAt,
    String version,
    @JsonProperty("created_at") Instant createdAt
) {

    /** The answer to making a deployer or replacing its key. This is the only time the key is shown. */
    public record Created(DeployerDTO deployer, String key) {}
}
