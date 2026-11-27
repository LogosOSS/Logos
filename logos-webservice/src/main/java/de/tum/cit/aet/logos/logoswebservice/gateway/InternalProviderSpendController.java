package de.tum.cit.aet.logos.logoswebservice.gateway;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

/** Replicate the gateway's in-memory admission decision to the orchestrator. */
@RestController
public class InternalProviderSpendController {
    private final ProviderSpendCapService cap;
    private final byte[] secret;

    public InternalProviderSpendController(ProviderSpendCapService cap,
            @Value("${logos.orchestrator.internal-secret:}") String secret) {
        this.cap = cap;
        this.secret = secret.isBlank() ? new byte[0] : ("Bearer " + secret).getBytes(StandardCharsets.UTF_8);
    }

    @GetMapping("/internal/provider-spend/status")
    public ProviderSpendCapService.Status status(@RequestHeader(value = "Authorization", required = false) String authorization) {
        if (secret.length == 0 || authorization == null
                || !MessageDigest.isEqual(secret, authorization.getBytes(StandardCharsets.UTF_8))) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "Invalid internal authorization");
        }
        return cap.status();
    }
}
