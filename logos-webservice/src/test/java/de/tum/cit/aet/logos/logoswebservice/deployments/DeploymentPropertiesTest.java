package de.tum.cit.aet.logos.logoswebservice.deployments;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Duration;
import java.util.List;

import org.junit.jupiter.api.Test;

class DeploymentPropertiesTest {

    @Test
    void theFeatureIsOffWithoutAnEnvironment() {
        DeploymentProperties properties = new DeploymentProperties("", null, null, null, null, null, null);

        assertThat(properties.enabled()).isFalse();
        assertThat(properties.orderableEnvironments()).isEmpty();
    }

    @Test
    void aLogosDeploysItsOwnEnvironmentByDefault() {
        DeploymentProperties properties = new DeploymentProperties(" Dev ", List.of(), null, null, null, null, null);

        assertThat(properties.enabled()).isTrue();
        assertThat(properties.environment()).isEqualTo("Dev");
        assertThat(properties.orderableEnvironments()).containsExactly("Dev");
    }

    @Test
    void theDefaultsAreSafe() {
        DeploymentProperties properties = new DeploymentProperties("Dev", null, null, null, null, null, null);

        assertThat(properties.protectedEnvironments()).containsExactly("Prod");
        assertThat(properties.silenceLimit()).isEqualTo(Duration.ofMinutes(30));
        assertThat(properties.onlineAfter()).isEqualTo(Duration.ofSeconds(90));
        assertThat(properties.pollHoldMax()).isEqualTo(Duration.ofSeconds(30));
        assertThat(properties.restrictedTagPattern()).isEqualTo(DeploymentProperties.DEFAULT_RESTRICTED_TAG_PATTERN);
    }

    @Test
    void environmentsAreMatchedWithoutRegardToCase() {
        DeploymentProperties properties = new DeploymentProperties(
            "Prod", List.of("Prod", " Dev ", "", "Test", "Dev"), null, null, null, null, null);

        assertThat(properties.orderableEnvironments()).containsExactly("Prod", "Dev", "Test");
        assertThat(properties.orderable("dev")).isEqualTo("Dev");
        assertThat(properties.orderable("staging")).isNull();
        assertThat(properties.isProtected("PROD")).isTrue();
        assertThat(properties.isProtected("Dev")).isFalse();
    }

    @Test
    void namesAreStrippedAndEmptyOrRepeatedOnesAreDropped() {
        DeploymentProperties properties =
            new DeploymentProperties("Dev", List.of(" Dev ", "", "Test", "Test"), List.of(" Prod ", " "), null, null, null, null);

        assertThat(properties.orderableEnvironments()).containsExactly("Dev", "Test");
        assertThat(properties.protectedEnvironments()).containsExactly("Prod");
    }
}
