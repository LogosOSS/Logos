package de.tum.cit.aet.logos.logoswebservice.gateway;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Map;
import java.util.UUID;

import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

/** The existing orchestrator executor uses the same ledger as the direct gateway. */
@RestController
@RequestMapping("/internal/provider-spend")
public class InternalProviderSpendController {
    private final ProviderSpendCapService cap;
    private final byte[] secret;

    public InternalProviderSpendController(ProviderSpendCapService cap,
            @Value("${logos.orchestrator.internal-secret:}") String secret) {
        this.cap = cap;
        this.secret = secret.isBlank() ? new byte[0] : ("Bearer " + secret).getBytes(StandardCharsets.UTF_8);
    }

    record Reserve(int providerId, Integer modelId, String upstreamUrl, JsonNode shape) {}
    record Reservation(UUID attemptId) {}
    record Settle(UUID attemptId, Map<String, Long> usage, String tier, boolean undispatched) {}

    @PostMapping("/reserve")
    public Reservation reserve(@RequestHeader(value = "Authorization", required = false) String authorization,
            @RequestBody Reserve request) {
        requireSecret(authorization);
        if (request.providerId() < 1 || request.modelId() != null && request.modelId() < 1) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Invalid provider or model");
        }
        return new Reservation(cap.reserve(request.providerId(), request.modelId(), request.upstreamUrl(), request.shape()));
    }

    @PostMapping("/settle")
    public void settle(@RequestHeader(value = "Authorization", required = false) String authorization,
            @RequestBody Settle request) {
        requireSecret(authorization);
        if (request.attemptId() == null) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Missing provider spend attempt");
        }
        if (request.undispatched()) {
            cap.releaseUndispatched(request.attemptId());
        } else {
            cap.settle(request.attemptId(), request.usage(), request.tier());
        }
    }

    private void requireSecret(String authorization) {
        if (secret.length == 0 || authorization == null
                || !MessageDigest.isEqual(secret, authorization.getBytes(StandardCharsets.UTF_8))) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "Invalid internal authorization");
        }
    }
}
