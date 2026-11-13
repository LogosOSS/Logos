package de.tum.cit.aet.logos.logoswebservice.deployments;

import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import de.tum.cit.aet.logos.logoswebservice.deployments.service.TagPolicy;

class TagPolicyTest {

    /** Prod. */
    private final TagPolicy protectedLogos = new TagPolicy(new DeploymentProperties(true, null, null, null, null));
    /** Dev and Test. */
    private final TagPolicy open = new TagPolicy(new DeploymentProperties(false, null, null, null, null));

    @ParameterizedTest
    @ValueSource(strings = {"latest", "v1", "v2026.10.4", "v1.2.3-rc1", "v1_x"})
    void aProtectedLogosAcceptsLatestAndReleaseTags(String tag) {
        assertThatCode(() -> protectedLogos.check(tag)).doesNotThrowAnyException();
    }

    @ParameterizedTest
    @ValueSource(strings = {"pr-93", "main", "abc1234", "V1", "vx", "latest2"})
    void aProtectedLogosRefusesTheOthers(String tag) {
        assertThatThrownBy(() -> protectedLogos.check(tag))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("This Logos accepts only 'latest' and release tags");
    }

    @ParameterizedTest
    @ValueSource(strings = {"pr-93", "main", "latest", "abc1234"})
    void anUnprotectedLogosAcceptsPullRequestBuilds(String tag) {
        assertThatCode(() -> open.check(tag)).doesNotThrowAnyException();
    }

    @ParameterizedTest
    @ValueSource(strings = {"", " ", "a b", "-x", ".x", "x/y", "x:y", "tag\nmore", "a@b"})
    void aTagThatIsNoDockerTagIsRefused(String tag) {
        assertThatThrownBy(() -> open.check(tag))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessage("The tag is not a Docker image tag.");
    }

    @Test
    void theTagIsNotLongerThan128Characters() {
        assertThatCode(() -> open.check("a".repeat(128))).doesNotThrowAnyException();
        assertThatThrownBy(() -> open.check("a".repeat(129))).isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void theReservedTagIsRefused() {
        assertThatThrownBy(() -> open.check("previous"))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("reserved");
    }

    @Test
    void nullIsRefused() {
        assertThatThrownBy(() -> open.check(null)).isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void theRestrictedPatternCanBeSet() {
        TagPolicy custom = new TagPolicy(new DeploymentProperties(true, "stable-[0-9]+", null, null, null));

        assertThatCode(() -> custom.check("stable-4")).doesNotThrowAnyException();
        assertThatThrownBy(() -> custom.check("latest")).isInstanceOf(IllegalArgumentException.class);
    }
}
