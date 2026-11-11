package de.tum.cit.aet.logos.logoswebservice.notifications.realtime;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import java.time.Instant;
import java.util.HashMap;
import java.util.Map;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
import org.springframework.http.server.ServerHttpResponse;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationToken;

import de.tum.cit.aet.logos.logoswebservice.auth.KeycloakClaimExtractor;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.User;
import de.tum.cit.aet.logos.logoswebservice.identity.service.KeycloakUserSyncService;
import de.tum.cit.aet.logos.logoswebservice.websocket.WebSocketSessionAuth;

class NotificationHandshakeInterceptorTest {
    private final KeycloakUserSyncService sync = mock(KeycloakUserSyncService.class);
    private final NotificationHandshakeInterceptor interceptor = new NotificationHandshakeInterceptor(
        mock(KeycloakClaimExtractor.class), sync);
    private final ServerHttpResponse response = mock(ServerHttpResponse.class);
    private final Map<String, Object> attributes = new HashMap<>();

    @AfterEach
    void clearContext() {
        SecurityContextHolder.clearContext();
    }

    private void authenticate(Instant expiry, boolean active) {
        Jwt jwt = Jwt.withTokenValue("token").header("alg", "RS256").subject("alice").expiresAt(expiry).build();
        SecurityContextHolder.getContext().setAuthentication(new JwtAuthenticationToken(jwt));
        User user = mock(User.class);
        when(user.getId()).thenReturn(123);
        when(user.isActive()).thenReturn(active);
        when(user.getRole()).thenReturn("app_developer");
        when(sync.syncIfStale(any())).thenReturn(user);
    }

    @Test
    void activeNonAdminStoresIdentityAndExpiry() {
        Instant expiry = Instant.now().plusSeconds(300);
        authenticate(expiry, true);
        assertThat(interceptor.beforeHandshake(null, response, null, attributes)).isTrue();
        assertThat(attributes).containsEntry(WebSocketSessionAuth.USER_ID, 123)
            .containsEntry(WebSocketSessionAuth.EXPIRES_AT, expiry);
    }

    @Test
    void expiredTokenIsRejected() {
        authenticate(Instant.now().minusSeconds(1), true);
        assertThat(interceptor.beforeHandshake(null, response, null, attributes)).isFalse();
        org.mockito.Mockito.verify(response).setStatusCode(HttpStatus.UNAUTHORIZED);
    }

    @Test
    void inactiveUserIsRejected() {
        authenticate(Instant.now().plusSeconds(300), false);
        assertThat(interceptor.beforeHandshake(null, response, null, attributes)).isFalse();
        org.mockito.Mockito.verify(response).setStatusCode(HttpStatus.FORBIDDEN);
    }
}
