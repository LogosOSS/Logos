package de.tum.cit.aet.logos.logoswebservice.deployments.service;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;
import java.util.Base64;
import java.util.HexFormat;

/**
 * Keys of deployers. A key is 48 random bytes, so a fast hash is enough to store it: nobody can guess a key from its
 * hash. Only the hash is stored. Logos shows the key once, when it makes it.
 */
public final class DeployerKeys {

    private static final SecureRandom RANDOM = new SecureRandom();

    private DeployerKeys() {
    }

    public static String newKey() {
        byte[] bytes = new byte[48];
        RANDOM.nextBytes(bytes);
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
    }

    public static String hash(String key) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(key.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(digest);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 is not available", e);
        }
    }
}
