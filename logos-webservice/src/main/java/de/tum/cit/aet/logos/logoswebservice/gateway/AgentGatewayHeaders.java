package de.tum.cit.aet.logos.logoswebservice.gateway;

import jakarta.servlet.http.HttpServletRequest;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Collections;
import java.util.List;

import org.springframework.http.HttpStatus;
import org.springframework.web.server.ResponseStatusException;

/** Internal control headers replaced by the isolated Agent edge, never accepted from a workspace as-is. */
final class AgentGatewayHeaders {
    static final String MODE = "X-Logos-Agent-Inference";
    static final String SECRET = "X-Logos-Agent-Relay-Secret";
    static final String CLIENT_TAG = "X-Logos-Client-Tag";

    private AgentGatewayHeaders() {
    }

    static byte[] secretBytes(String secret) {
        return secret.isBlank() ? new byte[0] : secret.getBytes(StandardCharsets.UTF_8);
    }

    static void require(HttpServletRequest request, byte[] secret) {
        String supplied = single(request, SECRET);
        if (secret.length == 0 || supplied == null
                || !MessageDigest.isEqual(secret, supplied.getBytes(StandardCharsets.UTF_8))) {
            throw invalidAuthorization();
        }
    }

    static String single(HttpServletRequest request, String name) {
        List<String> values = Collections.list(request.getHeaders(name));
        if (values.size() > 1) throw invalidAuthorization();
        return values.isEmpty() ? null : values.getFirst();
    }

    static ResponseStatusException invalidAuthorization() {
        return new ResponseStatusException(HttpStatus.UNAUTHORIZED, "Invalid agent inference authorization");
    }
}
