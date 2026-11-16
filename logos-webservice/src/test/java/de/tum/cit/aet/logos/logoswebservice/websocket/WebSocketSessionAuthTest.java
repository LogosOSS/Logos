package de.tum.cit.aet.logos.logoswebservice.websocket;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.clearInvocations;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.time.Clock;
import java.time.Instant;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.EnumSource;
import org.junit.jupiter.params.provider.MethodSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.annotation.Import;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.context.jdbc.Sql;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.handler.TextWebSocketHandler;

import com.fasterxml.jackson.databind.ObjectMapper;

import de.tum.cit.aet.logos.logoswebservice.TestContainersConfig;
import de.tum.cit.aet.logos.logoswebservice.notifications.realtime.NotificationEvent;
import de.tum.cit.aet.logos.logoswebservice.notifications.realtime.NotificationSocketHandler;
import de.tum.cit.aet.logos.logoswebservice.notifications.repository.NotificationRepository;
import de.tum.cit.aet.logos.logoswebservice.operations.service.RequestLogService;
import de.tum.cit.aet.logos.logoswebservice.operations.service.RequestLogStatsService;
import de.tum.cit.aet.logos.logoswebservice.operations.service.VramService;
import de.tum.cit.aet.logos.logoswebservice.orchestrator.OrchestratorLiveStreamClient;

@SpringBootTest
@Import(TestContainersConfig.class)
@TestPropertySource(properties = {
    "spring.liquibase.enabled=true",
    "spring.liquibase.change-log=classpath:liquibase/changelog/master.xml"
})
@Sql(scripts = "/sql/seed-identity.sql", executionPhase = Sql.ExecutionPhase.BEFORE_TEST_METHOD)
@Sql(scripts = "/sql/cleanup-identity.sql", executionPhase = Sql.ExecutionPhase.AFTER_TEST_METHOD)
class WebSocketSessionAuthTest {

    enum Endpoint { STATS, STATS_V2, NOTIFICATIONS }
    enum Trigger { TIMER, MESSAGE }
    enum Revocation { ROLE, INACTIVE, MISSING }

    @Autowired JdbcTemplate jdbc;
    @MockitoBean JwtDecoder jwtDecoder;

    private final Instant start = Instant.parse("2026-11-10T12:00:00Z");
    private Clock clock;
    private WebSocketSessionAuth auth;
    private TextWebSocketHandler handler;
    private WebSocketSession session;
    private NotificationRepository notifications;

    @BeforeEach
    void setUp() {
        clock = mock(Clock.class);
        when(clock.instant()).thenReturn(start);
        auth = new WebSocketSessionAuth(jdbc, clock);
        jdbc.update("UPDATE users SET role = 'logos_admin' WHERE id = 1001");
    }

    @AfterEach
    void stopHandlers() {
        if (handler instanceof StatsWebSocketHandler stats) stats.shutdown();
        if (handler instanceof StatsV2WebSocketHandler stats) stats.shutdown();
    }

    private void connect(Endpoint endpoint) throws Exception {
        ObjectMapper mapper = new ObjectMapper();
        VramService vram = mock(VramService.class);
        RequestLogService requests = mock(RequestLogService.class);
        when(vram.getVramStats(any())).thenReturn(Map.of("providers", List.of()));
        when(requests.getLatestRequests(any(), any())).thenReturn(Map.of("requests", List.of()));
        notifications = mock(NotificationRepository.class);
        handler = switch (endpoint) {
            case STATS -> new StatsWebSocketHandler(vram, requests, mapper, auth);
            case STATS_V2 -> new StatsV2WebSocketHandler(vram, requests, mock(RequestLogStatsService.class),
                mock(OrchestratorLiveStreamClient.class), mapper, auth);
            case NOTIFICATIONS -> new NotificationSocketHandler(mapper, notifications, auth);
        };
        stopHandlers();
        session = mock(WebSocketSession.class);
        when(session.getId()).thenReturn("authorized-session");
        when(session.isOpen()).thenReturn(true);
        when(session.getAttributes()).thenReturn(new HashMap<>(Map.of(
            WebSocketSessionAuth.USER_ID, 1001,
            WebSocketSessionAuth.EXPIRES_AT, start.plusSeconds(120))));
        handler.afterConnectionEstablished(session);
        clearInvocations(session);
    }

    private void trigger(Trigger trigger) throws Exception {
        if (trigger == Trigger.TIMER) {
            when(clock.instant()).thenReturn(start.plusSeconds(30));
            auth.revalidateSessions();
        } else {
            // No time has passed since the handshake: messages must bypass the recent check.
            handler.handleMessage(session, new TextMessage("{\"action\":\"set_scope\",\"team_id\":2001}"));
        }
    }

    @ParameterizedTest
    @EnumSource(Endpoint.class)
    void idleSessionsCloseAtTokenExpiry(Endpoint endpoint) throws Exception {
        connect(endpoint);
        when(clock.instant()).thenReturn(start.plusSeconds(119));
        auth.revalidateSessions();
        verify(session, never()).close(any());

        when(clock.instant()).thenReturn(start.plusSeconds(120));
        auth.revalidateSessions();

        verify(session).close(WebSocketSessionAuth.TOKEN_EXPIRED);
        verify(session, never()).sendMessage(any());
        assertThat(auth.isAuthorized(session, true)).isFalse();
    }

    @ParameterizedTest
    @EnumSource(Endpoint.class)
    void expiredTokensCannotReceivePushes(Endpoint endpoint) throws Exception {
        connect(endpoint);
        when(clock.instant()).thenReturn(start.plusSeconds(120));
        if (handler instanceof NotificationSocketHandler inbox) {
            when(notifications.findAddressedActiveUsers(any(), any())).thenReturn(List.of(1001));
            inbox.deliverLocally(new NotificationEvent(7L, Map.of("id", 7)));
        } else {
            // A push already being prepared must still check expiry at send time.
            ReflectionTestUtils.invokeMethod(handler, "send", session, Map.of("type", "requests"));
        }
        handler.handleMessage(session, new TextMessage("{\"action\":\"ping\"}"));
        verify(session).close(WebSocketSessionAuth.TOKEN_EXPIRED);
        verify(session, never()).sendMessage(any());
    }

    @ParameterizedTest
    @EnumSource(Endpoint.class)
    void expiredTokensCannotHandleClientMessages(Endpoint endpoint) throws Exception {
        connect(endpoint);
        when(clock.instant()).thenReturn(start.plusSeconds(120));
        handler.handleMessage(session, new TextMessage("{\"action\":\"ping\"}"));
        verify(session).close(WebSocketSessionAuth.TOKEN_EXPIRED);
        verify(session, never()).sendMessage(any());
    }

    static Stream<Arguments> revocations() {
        return Stream.of(Endpoint.values()).flatMap(endpoint -> Stream.of(Trigger.values()).flatMap(trigger ->
            Stream.of(Revocation.values())
                .filter(reason -> endpoint != Endpoint.NOTIFICATIONS || reason != Revocation.ROLE)
                .map(reason -> Arguments.of(endpoint, trigger, reason))));
    }

    @ParameterizedTest
    @MethodSource("revocations")
    void databaseRevocationsCloseSessionsBeforeFurtherUse(Endpoint endpoint, Trigger trigger, Revocation reason)
            throws Exception {
        connect(endpoint);
        // Direct SQL models a change committed by another replica, without touching any local identity cache.
        switch (reason) {
            case ROLE -> jdbc.update("UPDATE users SET role = 'app_developer' WHERE id = 1001");
            case INACTIVE -> jdbc.update("UPDATE users SET is_active = false WHERE id = 1001");
            case MISSING -> {
                // Point at a missing identity without deleting a fixture's referenced rows.
                session.getAttributes().put(WebSocketSessionAuth.USER_ID, -1);
            }
        }
        trigger(trigger);
        verify(session).close(CloseStatus.POLICY_VIOLATION);
        verify(session, never()).sendMessage(any());
        assertThat(auth.isAuthorized(session, false)).isFalse();
    }

    @ParameterizedTest
    @EnumSource(Trigger.class)
    void notificationsRemainAvailableWithoutAdminRole(Trigger trigger) throws Exception {
        connect(Endpoint.NOTIFICATIONS);
        jdbc.update("UPDATE users SET role = 'app_developer' WHERE id = 1001");
        trigger(trigger);
        assertThat(auth.isAuthorized(session, true)).isTrue();
        verify(session, never()).close(any());
    }
}
