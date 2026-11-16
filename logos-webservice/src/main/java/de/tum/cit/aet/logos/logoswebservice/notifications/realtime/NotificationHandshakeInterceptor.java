package de.tum.cit.aet.logos.logoswebservice.notifications.realtime;

import java.time.Instant;
import java.util.Map;

import org.springframework.dao.DataAccessException;
import org.springframework.http.HttpStatus;
import org.springframework.http.server.ServerHttpRequest;
import org.springframework.http.server.ServerHttpResponse;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationToken;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.WebSocketHandler;
import org.springframework.web.socket.server.HandshakeInterceptor;

import de.tum.cit.aet.logos.logoswebservice.auth.KeycloakClaimExtractor;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.User;
import de.tum.cit.aet.logos.logoswebservice.identity.service.KeycloakUserSyncService;
import de.tum.cit.aet.logos.logoswebservice.websocket.WebSocketSessionAuth;

/**
 * Handshake guard for {@code /ws/notifications}. Unlike the stats sockets,
 * every active user may connect. The user id and token expiry are kept for
 * ongoing authorization; recipients are checked against the database per push.
 */
@Component
public class NotificationHandshakeInterceptor implements HandshakeInterceptor {

    static final String USER_ID = WebSocketSessionAuth.USER_ID;

    private final KeycloakClaimExtractor claimExtractor;
    private final KeycloakUserSyncService syncService;

    public NotificationHandshakeInterceptor(KeycloakClaimExtractor claimExtractor,
                                            KeycloakUserSyncService syncService) {
        this.claimExtractor = claimExtractor;
        this.syncService = syncService;
    }

    @Override
    public boolean beforeHandshake(ServerHttpRequest request, ServerHttpResponse response,
                                   WebSocketHandler wsHandler, Map<String, Object> attributes) {
        var authentication = SecurityContextHolder.getContext().getAuthentication();
        if (!(authentication instanceof JwtAuthenticationToken jwtAuth)) {
            response.setStatusCode(HttpStatus.UNAUTHORIZED);
            return false;
        }
        Instant expiresAt = jwtAuth.getToken().getExpiresAt();
        if (expiresAt == null || !Instant.now().isBefore(expiresAt)) {
            response.setStatusCode(HttpStatus.UNAUTHORIZED);
            return false;
        }

        User user;
        try {
            user = syncService.syncIfStale(claimExtractor.extract(jwtAuth.getToken()));
        } catch (IllegalArgumentException | DataAccessException e) {
            response.setStatusCode(HttpStatus.UNAUTHORIZED);
            return false;
        }
        if (!user.isActive()) {
            response.setStatusCode(HttpStatus.FORBIDDEN);
            return false;
        }
        attributes.put(USER_ID, user.getId());
        attributes.put(WebSocketSessionAuth.EXPIRES_AT, expiresAt);
        return true;
    }

    @Override
    public void afterHandshake(ServerHttpRequest request, ServerHttpResponse response,
                               WebSocketHandler wsHandler, Exception exception) {}
}
