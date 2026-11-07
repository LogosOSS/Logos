package de.tum.cit.aet.logos.logoswebservice.deployments;

import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.util.List;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import de.tum.cit.aet.logos.logoswebservice.deployments.service.TagPolicy;

class TagPolicyTest {

    private final DeploymentProperties properties = new DeploymentProperties(
        "Prod", List.of("Prod", "Dev", "Test"), null, null, null, null, null);
    private final TagPolicy policy = new TagPolicy(properties);

    @ParameterizedTest
    @ValueSource(strings = {"latest", "v1", "v2026.10.4", "v1.2.3-rc1", "v1_x"})
    void prodAcceptsLatestAndReleaseTags(String tag) {
        assertThatCode(() -> policy.check("Prod", tag)).doesNotThrowAnyException();
    }

    @ParameterizedTest
    @ValueSource(strings = {"pr-93", "main", "abc1234", "V1", "vx", "latest2"})
    void prodRefusesTheOthers(String tag) {
        assertThatThrownBy(() -> policy.check("Prod", tag))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("Prod accepts only 'latest' and release tags");
    }

    @ParameterizedTest
    @ValueSource(strings = {"pr-93", "main", "latest", "abc1234"})
    void devAndTestAcceptPullRequestBuilds(String tag) {
        assertThatCode(() -> policy.check("Dev", tag)).doesNotThrowAnyException();
        assertThatCode(() -> policy.check("Test", tag)).doesNotThrowAnyException();
    }

    @ParameterizedTest
    @ValueSource(strings = {"", " ", "a b", "-x", ".x", "x/y", "x:y", "tag\nmore", "a@b"})
    void aTagThatIsNoDockerTagIsRefused(String tag) {
        assertThatThrownBy(() -> policy.check("Dev", tag))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessage("The tag is not a Docker image tag.");
    }

    @Test
    void theTagIsNotLongerThan128Characters() {
        assertThatCode(() -> policy.check("Dev", "a".repeat(128))).doesNotThrowAnyException();
        assertThatThrownBy(() -> policy.check("Dev", "a".repeat(129))).isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void theReservedTagIsRefused() {
        assertThatThrownBy(() -> policy.check("Dev", "previous"))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("reserved");
    }

    @Test
    void nullIsRefused() {
        assertThatThrownBy(() -> policy.check("Dev", null)).isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void theRestrictedPatternAndTheProtectedEnvironmentsCanBeSet() {
        DeploymentProperties custom = new DeploymentProperties(
            "Prod", List.of("Prod", "Dev"), List.of("Prod", "Dev"), "stable-[0-9]+", null, null, null);
        TagPolicy strict = new TagPolicy(custom);

        assertThatCode(() -> strict.check("Dev", "stable-4")).doesNotThrowAnyException();
        assertThatThrownBy(() -> strict.check("Dev", "latest")).isInstanceOf(IllegalArgumentException.class);
    }
}
