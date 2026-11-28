package de.tum.cit.aet.logos.logoswebservice.gateway;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;

import java.io.IOException;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpHeaders;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestMethod;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody;

/** The trusted local-only Agent ingress on the ordinary Logos inference surface. */
@RestController
final class AgentLocalInferenceController {
    private static final String MANAGED_PREFIX = "Bearer ac1.";
    private static final int MAX_MANAGED_AUTH_LENGTH = "Bearer ".length() + 1024;

    private final GatewayOrchestratorProxy proxy;
    private final GatewayInFlightLimit capacity;
    private final byte[] secret;

    AgentLocalInferenceController(GatewayOrchestratorProxy proxy, GatewayInFlightLimit capacity,
            @Value("${logos.orchestrator.internal-secret:}") String secret) {
        this.proxy = proxy;
        this.capacity = capacity;
        this.secret = AgentGatewayHeaders.secretBytes(secret);
    }

    @RequestMapping(path = "/v1/**", headers = AgentGatewayHeaders.MODE + "=local",
        method = {RequestMethod.GET, RequestMethod.HEAD, RequestMethod.POST, RequestMethod.PUT,
            RequestMethod.PATCH, RequestMethod.DELETE, RequestMethod.OPTIONS})
    ResponseEntity<StreamingResponseBody> proxyLocal(HttpServletRequest request, HttpServletResponse response)
            throws IOException {
        AgentGatewayHeaders.require(request, secret);
        String token = AgentGatewayHeaders.single(request, HttpHeaders.AUTHORIZATION);
        if (token == null || token.length() > MAX_MANAGED_AUTH_LENGTH || !token.startsWith(MANAGED_PREFIX)
                || request.getHeader("logos_key") != null || request.getHeader("logos-key") != null
                || request.getHeader("x-api-key") != null) {
            throw AgentGatewayHeaders.invalidAuthorization();
        }
        AgentInferenceRelayController.clientTag(AgentGatewayHeaders.single(request, AgentGatewayHeaders.CLIENT_TAG));
        // The orchestrator verifies the managed token's signature, expiry and
        // active local-only identity. Never resolve it as a public Logos key.
        var lifecycle = GatewayRelayCleanup.begin(request, capacity.tryAcquire());
        try {
            String path = request.getRequestURI().substring(request.getContextPath().length());
            byte[] body = GatewayBodyReader.read(request.getInputStream());
            var routed = GatewayClientFlush.flushingTo(response, proxy.proxy(request, path, body));
            if (routed.getBody() == null) return routed;
            return ResponseEntity.status(routed.getStatusCode()).headers(routed.getHeaders())
                .body(lifecycle.relay(routed.getBody()));
        } catch (IOException | RuntimeException | Error failure) {
            lifecycle.close();
            throw failure;
        }
    }
}
