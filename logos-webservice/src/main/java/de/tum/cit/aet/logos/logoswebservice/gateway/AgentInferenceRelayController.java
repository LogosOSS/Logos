package de.tum.cit.aet.logos.logoswebservice.gateway;

import com.fasterxml.jackson.core.JsonFactory;
import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.core.JsonToken;
import com.fasterxml.jackson.core.StreamReadFeature;

import jakarta.servlet.http.HttpServletRequest;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import java.util.regex.Pattern;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;
import org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody;

@RestController
public final class AgentInferenceRelayController {
    static final String MESSAGES = "/v1/messages";
    private static final int MAX_BODY_BYTES = 32 * 1024 * 1024;
    private static final Pattern GRANT = Pattern.compile("^Bearer ([A-Za-z0-9_-]{43})$");
    private static final Pattern BETA = Pattern.compile("^[A-Za-z0-9][A-Za-z0-9_,. -]{0,1023}$");
    private static final JsonFactory JSON = JsonFactory.builder()
        .enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION).build();

    private final AgentInferenceRelay relay;
    private final GatewayInFlightLimit capacity;
    private final byte[] secret;

    public AgentInferenceRelayController(AgentInferenceRelay relay, GatewayInFlightLimit capacity,
            @Value("${logos.orchestrator.internal-secret:}") String secret) {
        this.relay = relay;
        this.capacity = capacity;
        this.secret = AgentGatewayHeaders.secretBytes(secret);
    }

    @PostMapping(path = MESSAGES, headers = AgentGatewayHeaders.MODE + "=cloud",
        consumes = MediaType.APPLICATION_JSON_VALUE)
    public ResponseEntity<StreamingResponseBody> relayMessages(HttpServletRequest request) throws IOException {
        AgentGatewayHeaders.require(request, secret);
        String authorization = AgentGatewayHeaders.single(request, HttpHeaders.AUTHORIZATION);
        var bearer = GRANT.matcher(authorization == null ? "" : authorization);
        if (!bearer.matches()) throw AgentGatewayHeaders.invalidAuthorization();
        String tag = clientTag(AgentGatewayHeaders.single(request, AgentGatewayHeaders.CLIENT_TAG));
        // Anthropic's beta client may include this marker. Never copy the
        // original URI/query string to the remote operating-key request.
        if (request.getQueryString() != null && !"beta=true".equals(request.getQueryString())) throw invalidBody();
        String beta = AgentGatewayHeaders.single(request, "anthropic-beta");
        if (beta != null && !BETA.matcher(beta).matches()) throw invalidBody();

        var grant = relay.authorize(tokenHash(bearer.group(1)), tag);
        var lifecycle = GatewayRelayCleanup.begin(request, capacity.tryAcquire());
        try {
            var upstreamRelay = relay.open(grant);
            var close = GatewayRelayCleanup.closeWhenRequestEnds(upstreamRelay);
            try {
                byte[] body = readBody(request);
                verifyModel(body, grant.model());
                var upstream = relay.forward(upstreamRelay, body, beta);
                int status = upstream.statusCode();
                if (status < 200 || status >= 300) {
                    // Do not relay Location, Set-Cookie or provider diagnostics
                    // (which can contain privileged request or credential data).
                    close.run();
                    return errorResponse(status, upstream.headers().firstValue("Retry-After").orElse(null), lifecycle);
                }
                MediaType type = outputType(upstream.headers().firstValue("Content-Type").orElse(""));
                return ResponseEntity.status(status).contentType(type)
                    .header(HttpHeaders.CACHE_CONTROL, "no-store")
                    .header("X-Accel-Buffering", "no")
                    .body(lifecycle.relay(out -> {
                        try {
                            byte[] chunk = new byte[8192];
                            int count;
                            while ((count = upstreamRelay.read(chunk)) != -1) {
                                out.write(chunk, 0, count);
                                out.flush();
                            }
                        } finally {
                            close.run();
                        }
                    }));
            } catch (IOException | RuntimeException e) {
                close.run();
                throw e;
            }
        } catch (IOException | RuntimeException e) {
            lifecycle.close();
            throw e;
        }
    }

    private static ResponseEntity<StreamingResponseBody> errorResponse(
            int status, String retryAfter, GatewayRelayCleanup.Lifecycle lifecycle) {
        final int code;
        final String type;
        if (status == 400 || status == 413 || status == 422) {
            code = status;
            type = "invalid_request_error";
        } else if (status == 429) {
            code = status;
            type = "rate_limit_error";
        } else if (status == 503 || status == 529) {
            code = status;
            type = "overloaded_error";
        } else {
            code = 502;
            type = "api_error";
        }
        String json = "{\"type\":\"error\",\"error\":{\"type\":\"" + type
            + "\",\"message\":\"Remote inference request did not succeed\"}}";
        var response = ResponseEntity.status(code).contentType(MediaType.APPLICATION_JSON);
        if (code == 429 && retryAfter != null && retryAfter.matches("[0-9]{1,5}")) {
            response.header(HttpHeaders.RETRY_AFTER, retryAfter);
        }
        return response.body(lifecycle.relay(out -> out.write(json.getBytes(StandardCharsets.UTF_8))));
    }

    private static MediaType outputType(String value) {
        if (value.length() <= 128) {
            try {
                var type = MediaType.parseMediaType(value);
                if (MediaType.TEXT_EVENT_STREAM.equalsTypeAndSubtype(type)) return MediaType.TEXT_EVENT_STREAM;
                if (MediaType.APPLICATION_JSON.equalsTypeAndSubtype(type)) return MediaType.APPLICATION_JSON;
            } catch (IllegalArgumentException ignored) {
                // Do not include the provider's raw header in the error.
            }
        }
        throw new ResponseStatusException(HttpStatus.BAD_GATEWAY, "Invalid remote inference response");
    }

    private static byte[] readBody(HttpServletRequest request) throws IOException {
        if (request.getContentLengthLong() > MAX_BODY_BYTES) throw tooLarge();
        byte[] body = request.getInputStream().readNBytes(MAX_BODY_BYTES + 1);
        if (body.length > MAX_BODY_BYTES) throw tooLarge();
        return body;
    }

    static void verifyModel(byte[] body, String permittedModel) {
        try (JsonParser parser = JSON.createParser(new InputStreamReader(
                new ByteArrayInputStream(body), StandardCharsets.UTF_8.newDecoder()))) {
            if (parser.nextToken() != JsonToken.START_OBJECT) throw invalidBody();
            boolean matched = false;
            while (parser.nextToken() == JsonToken.FIELD_NAME) {
                String field = parser.currentName();
                JsonToken value = parser.nextToken();
                if ("model".equals(field)) {
                    if (value != JsonToken.VALUE_STRING || !permittedModel.equals(parser.getText())) {
                        throw invalidBody();
                    }
                    matched = true;
                } else {
                    parser.skipChildren();
                }
            }
            if (!matched || parser.currentToken() != JsonToken.END_OBJECT || parser.nextToken() != null) {
                throw invalidBody();
            }
        } catch (IOException e) {
            // Suppress parser diagnostics, which may contain request fields.
            throw invalidBody();
        }
    }

    static String tokenHash(String token) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                .digest(token.getBytes(StandardCharsets.US_ASCII)));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 is unavailable", e);
        }
    }

    static String clientTag(String value) {
        if (value == null || !value.matches("agent:[0-9A-Fa-f:.]{2,45}")) throw AgentGatewayHeaders.invalidAuthorization();
        try {
            InetAddress.ofLiteral(value.substring("agent:".length()));
            // Keep this exact gateway-written value for the DB comparison.
            return value;
        } catch (IllegalArgumentException e) {
            throw AgentGatewayHeaders.invalidAuthorization();
        }
    }

    private static ResponseStatusException invalidBody() {
        return new ResponseStatusException(HttpStatus.BAD_REQUEST, "Invalid agent inference request");
    }

    private static ResponseStatusException tooLarge() {
        return new ResponseStatusException(HttpStatus.PAYLOAD_TOO_LARGE, "Agent inference request is too large");
    }
}
