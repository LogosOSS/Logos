package de.tum.cit.aet.logos.logoswebservice.websocket;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.io.IOException;
import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;

import com.fasterxml.jackson.databind.ObjectMapper;

import de.tum.cit.aet.logos.logoswebservice.operations.service.RequestLogService;
import de.tum.cit.aet.logos.logoswebservice.operations.service.VramService;

/** Sends go out one at a time per session, and a dead session leaves no state behind. */
class StatsWebSocketHandlerSendTest {

    private StatsWebSocketHandler handler;
    private WebSocketSession session;

    @BeforeEach
    void setUp() {
        VramService vram = mock(VramService.class);
        RequestLogService requests = mock(RequestLogService.class);
        when(vram.getVramStats(any())).thenReturn(Map.of("providers", List.of()));
        when(requests.getLatestRequests(isNull(), isNull())).thenReturn(Map.of("requests", List.of()));
        WebSocketSessionAuth sessionAuth = mock(WebSocketSessionAuth.class);
        when(sessionAuth.register(any(), org.mockito.ArgumentMatchers.anyBoolean())).thenReturn(true);
        when(sessionAuth.isAuthorized(any(), org.mockito.ArgumentMatchers.anyBoolean())).thenReturn(true);
        handler = new StatsWebSocketHandler(vram, requests, new ObjectMapper(), sessionAuth);
        session = mock(WebSocketSession.class);
        when(session.getId()).thenReturn("stats-v1-session");
        when(session.isOpen()).thenReturn(true);
    }

    @AfterEach
    void tearDown() {
        handler.shutdown();
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> field(String name) {
        return (Map<String, Object>) ReflectionTestUtils.getField(handler, name);
    }

    @Test
    void ping_is_answered_and_a_closed_connection_drops_its_send_lock() throws Exception {
        handler.afterConnectionEstablished(session);
        handler.handleMessage(session, new TextMessage("{\"action\":\"ping\"}"));

        ArgumentCaptor<TextMessage> captor = ArgumentCaptor.forClass(TextMessage.class);
        verify(session, org.mockito.Mockito.atLeast(3)).sendMessage(captor.capture());
        assertThat(captor.getAllValues()).anySatisfy(m -> assertThat(m.getPayload()).contains("\"pong\""));
        assertThat(field("sendLocks")).containsKey("stats-v1-session");

        handler.afterConnectionClosed(session, CloseStatus.NORMAL);
        assertThat(field("sendLocks")).isEmpty();
        assertThat(field("sessions")).isEmpty();
    }

    @Test
    void a_failed_send_forgets_the_session() throws Exception {
        doThrow(new IOException("broken pipe")).when(session).sendMessage(any());

        handler.afterConnectionEstablished(session);

        assertThat(field("sessions")).isEmpty();
        assertThat(field("states")).isEmpty();
        assertThat(field("sendLocks")).isEmpty();
    }

    @Test
    void a_closed_session_is_not_sent_to() throws Exception {
        when(session.isOpen()).thenReturn(false);

        handler.afterConnectionEstablished(session);

        verify(session, never()).sendMessage(any());
        assertThat(field("sendLocks")).isEmpty();
    }
}
