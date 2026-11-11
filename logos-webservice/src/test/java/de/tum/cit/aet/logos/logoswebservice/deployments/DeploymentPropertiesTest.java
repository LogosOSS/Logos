package de.tum.cit.aet.logos.logoswebservice.deployments;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Duration;

import org.junit.jupiter.api.Test;

class DeploymentPropertiesTest {

    @Test
    void theDefaultsAreSafe() {
        DeploymentProperties properties = new DeploymentProperties(true, null, null, null, null);

        assertThat(properties.silenceLimit()).isEqualTo(Duration.ofMinutes(30));
        assertThat(properties.onlineAfter()).isEqualTo(Duration.ofSeconds(90));
        assertThat(properties.pollHoldMax()).isEqualTo(Duration.ofSeconds(30));
        assertThat(properties.restrictedTagPattern()).isEqualTo(DeploymentProperties.DEFAULT_RESTRICTED_TAG_PATTERN);
    }

    @Test
    void aBlankTagPatternFallsBackToTheDefault() {
        // The properties file passes an empty value when the variable is not set.
        assertThat(new DeploymentProperties(true, "  ", null, null, null).restrictedTagPattern())
            .isEqualTo(DeploymentProperties.DEFAULT_RESTRICTED_TAG_PATTERN);
        assertThat(new DeploymentProperties(true, " stable-[0-9]+ ", null, null, null).restrictedTagPattern())
            .isEqualTo("stable-[0-9]+");
    }
}
