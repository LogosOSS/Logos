package de.tum.cit.aet.logos.logoswebservice.deployments;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.HashSet;
import java.util.Set;

import org.junit.jupiter.api.Test;

import de.tum.cit.aet.logos.logoswebservice.deployments.service.DeployerKeys;

class DeployerKeysTest {

    @Test
    void aKeyIsLongRandomAndSafeInAHeader() {
        String key = DeployerKeys.newKey();

        assertThat(key).hasSize(64).matches("[A-Za-z0-9_-]+");
    }

    @Test
    void keysDoNotRepeat() {
        Set<String> keys = new HashSet<>();
        for (int i = 0; i < 200; i++) {
            keys.add(DeployerKeys.newKey());
        }

        assertThat(keys).hasSize(200);
    }

    @Test
    void theHashIsAStableSha256InHexAndDiffersFromTheKey() {
        String key = "the-key";

        assertThat(DeployerKeys.hash(key)).hasSize(64).matches("[0-9a-f]+").isEqualTo(DeployerKeys.hash(key)).isNotEqualTo(key);
        assertThat(DeployerKeys.hash("a")).isEqualTo("ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb");
    }

    @Test
    void anotherKeyHasAnotherHash() {
        assertThat(DeployerKeys.hash("a")).isNotEqualTo(DeployerKeys.hash("b"));
    }
}
