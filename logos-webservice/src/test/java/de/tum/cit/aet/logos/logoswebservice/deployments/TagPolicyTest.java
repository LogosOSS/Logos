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
    @ValueSource(strings = {"latest", "1.0.2", "2026.10.4", "1.2.3-rc1", "1.2.3-rc.1"})
    void aProtectedLogosAcceptsLatestAndReleaseVersions(String tag) {
        assertThatCode(() -> protectedLogos.check(tag)).doesNotThrowAnyException();
    }

    @ParameterizedTest
    @ValueSource(strings = {"pr-93", "main", "abc1234", "1.2", "latest2"})
    void aProtectedLogosRefusesTheOthers(String tag) {
        assertThatThrownBy(() -> protectedLogos.check(tag))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("This Logos accepts only 'latest' and release versions like '1.0.2'");
    }

    /** The release workflow tags the image of the release v1.0.2 with 1.0.2, so no image has the tag v1.0.2. */
    @Test
    void theGitTagOfAReleaseIsRefused() {
        assertThatThrownBy(() -> protectedLogos.check("v1.0.2"))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessage("This Logos accepts only 'latest' and release versions like '1.0.2', not 'v1.0.2'.");
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

    @ParameterizedTest
    @ValueSource(strings = {"previous", "prefetched"})
    void aReservedTagIsRefused(String tag) {
        assertThatThrownBy(() -> open.check(tag))
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
