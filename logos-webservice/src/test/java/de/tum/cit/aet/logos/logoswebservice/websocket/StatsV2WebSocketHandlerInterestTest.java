package de.tum.cit.aet.logos.logoswebservice.websocket;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyBoolean;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.atLeastOnce;
import static org.mockito.Mockito.clearInvocations;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.time.Instant;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.stubbing.Answer;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpMethod;
import org.springframework.http.ResponseEntity;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.web.client.RestTemplate;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import de.tum.cit.aet.logos.logoswebservice.operations.service.RequestLogService;
import de.tum.cit.aet.logos.logoswebservice.operations.service.RequestLogStatsService;
import de.tum.cit.aet.logos.logoswebservice.operations.service.VramService;
import de.tum.cit.aet.logos.logoswebservice.orchestrator.OrchestratorLiveStreamClient;

/**
 * Tab interest gates which channels a stats/v2 session receives.
 *
 * The page shows one tab at a time; pushing the idle tab's channel is wasted
 * work. These tests drive init / set_interest / tick themselves — the
 * handler's scheduled tick is stopped so it cannot race the assertions.
 */
class StatsV2WebSocketHandlerInterestTest {

    private VramService vramService;
    private RequestLogService requestLogService;
    private RequestLogStatsService statsService;
    private StatsV2WebSocketHandler handler;
    private WebSocketSession session;
    private ObjectMapper objectMapper;
    private final List<TextMessage> sent = new ArrayList<>();

    @BeforeEach
    void setUp() throws Exception {
        objectMapper = new ObjectMapper();
        vramService = mock(VramService.class);
        requestLogService = mock(RequestLogService.class);
        statsService = mock(RequestLogStatsService.class);
        when(statsService.getRequestLogStats(any(), any(), anyInt(), any(), any(), any(), any(), anyBoolean(), any()))
            .thenReturn(Map.of("bucketSeconds", 60));
        when(vramService.getVramStats(anyString(), anyInt()))
            .thenReturn(Map.of("providers", List.of(), "last_snapshot_id", 0));
        when(requestLogService.getLatestRequests(
                any(), any(), any(), any(), any(), any(), anyBoolean(), any(), any(), any(), anyInt(), anyBoolean(), any(), any()))
            .thenAnswer(inv -> {
                Map<String, Object> payload = new LinkedHashMap<>();
                payload.put("requests", List.of());
                payload.put("has_more", false);
                payload.put("next_cursor", null);
                return payload;
            });

        RestTemplate restTemplate = mock(RestTemplate.class);
        OrchestratorLiveStreamClient liveStreamClient = new OrchestratorLiveStreamClient(restTemplate);
        ReflectionTestUtils.setField(liveStreamClient, "orchestratorUrl", "http://orchestrator");
        ReflectionTestUtils.setField(liveStreamClient, "internalSecret", "secret");
        when(restTemplate.exchange(anyString(), any(HttpMethod.class), any(HttpEntity.class), any(Class.class)))
            .thenReturn(ResponseEntity.ok(Map.of("streams", List.of())));

        WebSocketSessionAuth sessionAuth = mock(WebSocketSessionAuth.class);
        when(sessionAuth.register(any(), org.mockito.ArgumentMatchers.anyBoolean())).thenReturn(true);
        when(sessionAuth.isAuthorized(any(), org.mockito.ArgumentMatchers.anyBoolean())).thenReturn(true);
        handler = new StatsV2WebSocketHandler(
            vramService, requestLogService, statsService,
            liveStreamClient, objectMapper, sessionAuth);
        handler.shutdown();

        session = mock(WebSocketSession.class);
        when(session.getId()).thenReturn("interest-session");
        when(session.isOpen()).thenReturn(true);
        doAnswer((Answer<Void>) inv -> {
            sent.add(inv.<TextMessage>getArgument(0));
            return null;
        }).when(session).sendMessage(any());
    }

    @AfterEach
    void tearDown() {
        handler.shutdown();
    }

    private List<String> pushedTypes() throws Exception {
        List<String> types = new ArrayList<>();
        for (TextMessage m : sent) {
            JsonNode node = objectMapper.readTree(m.getPayload());
            types.add(node.path("type").asText());
        }
        return types;
    }

    @SuppressWarnings("unchecked")
    private void invokeTick() {
        ReflectionTestUtils.invokeMethod(handler, "tick");
    }

    @Test
    void initWithoutInterestPushesBothChannels() throws Exception {
        handler.afterConnectionEstablished(session);
        handler.handleMessage(session, new TextMessage("{\"action\":\"init\"}"));

        assertThat(pushedTypes()).contains("timeline_init", "vram_init", "requests");
    }

    @Test
    void reconnectRestoresAnOpenEndEvenWhenTheSavedEndIsOld() throws Exception {
        Instant before = Instant.now();
        String start = before.minusSeconds(86_400).toString();
        String oldEnd = before.minusSeconds(3600).toString();
        handler.afterConnectionEstablished(session);
        handler.handleMessage(session, new TextMessage(objectMapper.writeValueAsString(Map.of(
            "action", "init", "interest", "requests",
            "timeline", Map.of("start", start, "end", oldEnd, "live", true)))));

        var endCaptor = org.mockito.ArgumentCaptor.forClass(String.class);
        verify(requestLogService).getLatestRequests(
            org.mockito.ArgumentMatchers.eq(start), endCaptor.capture(), any(), any(), any(), any(), anyBoolean(),
            any(), any(), any(), anyInt(), anyBoolean(), any(), any());
        assertThat(Instant.parse(endCaptor.getValue())).isAfterOrEqualTo(before);
        assertThat(pushedTypes()).contains("timeline_init", "requests");
        for (TextMessage message : sent) {
            String serverNow = objectMapper.readTree(message.getPayload()).path("server_now").asText();
            assertThat(Instant.parse(serverNow)).isBetween(before, Instant.now());
        }

        clearInvocations(requestLogService);
        invokeTick();
        verify(requestLogService, atLeastOnce()).getLatestRequests(
            org.mockito.ArgumentMatchers.eq(start), endCaptor.capture(), any(), any(), any(), any(), anyBoolean(),
            any(), any(), any(), anyInt(), anyBoolean(), any(), any());
        assertThat(Instant.parse(endCaptor.getValue())).isAfterOrEqualTo(before);
    }

    @Test
    void explicitHistoricalRangeStaysFixedEvenIfItsEndIsRecent() throws Exception {
        String start = Instant.now().minusSeconds(3600).toString();
        String end = Instant.now().minusSeconds(10).toString();
        handler.afterConnectionEstablished(session);
        handler.handleMessage(session, new TextMessage(objectMapper.writeValueAsString(Map.of(
            "action", "init", "interest", "requests",
            "timeline", Map.of("start", start, "end", end, "live", false)))));
        clearInvocations(requestLogService);
        invokeTick();
        verify(requestLogService, atLeastOnce()).getLatestRequests(
            org.mockito.ArgumentMatchers.eq(start), org.mockito.ArgumentMatchers.eq(end), any(), any(), any(), any(),
            anyBoolean(), any(), any(), any(), anyInt(), anyBoolean(), any(), any());
    }

    @Test
    void rangeChangeCanSwitchBetweenHistoricalAndLive() throws Exception {
        handler.afterConnectionEstablished(session);
        handler.handleMessage(session, new TextMessage("{\"action\":\"init\",\"interest\":\"requests\"}"));
        String start = Instant.now().minusSeconds(86_400).toString();
        String end = Instant.now().minusSeconds(3600).toString();
        for (boolean live : List.of(false, true)) {
            clearInvocations(requestLogService);
            handler.handleMessage(session, new TextMessage(objectMapper.writeValueAsString(Map.of(
                "action", "set_timeline_range", "start", start, "end", end, "live", live))));
            var endCaptor = org.mockito.ArgumentCaptor.forClass(String.class);
            verify(requestLogService).getLatestRequests(
                org.mockito.ArgumentMatchers.eq(start), endCaptor.capture(), any(), any(), any(), any(), anyBoolean(),
                any(), any(), any(), anyInt(), anyBoolean(), any(), any());
            if (live) assertThat(Instant.parse(endCaptor.getValue())).isAfter(Instant.parse(end));
            else assertThat(endCaptor.getValue()).isEqualTo(end);
        }
    }

    @Test
    void pingCarriesServerTimeForClockSynchronization() throws Exception {
        Instant before = Instant.now();
        handler.afterConnectionEstablished(session);
        handler.handleMessage(session, new TextMessage("{\"action\":\"ping\"}"));
        assertThat(pushedTypes()).containsExactly("pong");
        assertThat(Instant.parse(objectMapper.readTree(sent.getFirst().getPayload()).path("server_now").asText()))
            .isBetween(before, Instant.now());
    }

    @Test
    void initWithLocalProvidersInterestPushesOnlyVram() throws Exception {
        handler.afterConnectionEstablished(session);
        handler.handleMessage(session, new TextMessage(
            "{\"action\":\"init\",\"interest\":\"local-providers\"}"));

        assertThat(pushedTypes()).contains("vram_init");
        assertThat(pushedTypes()).doesNotContain("timeline_init", "requests", "stats");
        verify(statsService, never()).getRequestLogStats(any(), any(), anyInt(), any(), any(), any(), any(), anyBoolean(), any());
    }

    @Test
    void initWithRequestsInterestPushesOnlyRequestChannels() throws Exception {
        handler.afterConnectionEstablished(session);
        handler.handleMessage(session, new TextMessage(
            "{\"action\":\"init\",\"interest\":\"requests\"}"));

        assertThat(pushedTypes()).contains("timeline_init", "requests");
        assertThat(pushedTypes()).doesNotContain("vram_init", "vram_delta");
        verify(vramService, never()).getVramStats(anyString(), anyInt());
    }

    @Test
    void tickSkipsIdleChannel() throws Exception {
        handler.afterConnectionEstablished(session);
        handler.handleMessage(session, new TextMessage(
            "{\"action\":\"init\",\"interest\":\"local-providers\"}"));
        clearInvocations(requestLogService, vramService, statsService);
        sent.clear();

        // Even ticks would push requests; every tick would push VRAM.
        for (int i = 0; i < 4; i++) {
            invokeTick();
        }

        verify(requestLogService, never()).getLatestRequests(
            any(), any(), any(), any(), any(), any(), anyBoolean(), any(), any(), any(), anyInt(), anyBoolean(), any(), any());
        // Delta path reuses getVramStats with the cursor; init already cleared.
        verify(vramService, atLeastOnce()).getVramStats(anyString(), anyInt());
        assertThat(pushedTypes()).doesNotContain("requests", "stats", "timeline_init");
    }

    @Test
    void requestsInterestStillReceivesVramAtALowCadence() throws Exception {
        handler.afterConnectionEstablished(session);
        handler.handleMessage(session, new TextMessage(
            "{\"action\":\"init\",\"interest\":\"requests\"}"));
        sent.clear();

        // The first tick is on the slow cadence: the baseline goes out once,
        // so the failed-GPU banner has data on the Requests tab.
        invokeTick();
        assertThat(pushedTypes()).containsOnlyOnce("vram_init");

        clearInvocations(vramService);
        for (int i = 1; i < 10; i++) {
            invokeTick();
        }
        verify(vramService, never()).getVramStats(anyString(), anyInt());

        invokeTick();
        assertThat(pushedTypes()).containsOnlyOnce("vram_init");
        verify(vramService, atLeastOnce()).getVramStats(anyString(), anyInt());
    }

    @Test
    void setInterestToRequestsReInitsRequestChannelsOnly() throws Exception {
        handler.afterConnectionEstablished(session);
        handler.handleMessage(session, new TextMessage(
            "{\"action\":\"init\",\"interest\":\"local-providers\"}"));
        clearInvocations(requestLogService, vramService, statsService);
        sent.clear();

        handler.handleMessage(session, new TextMessage(
            "{\"action\":\"set_interest\",\"interest\":\"requests\"}"));

        assertThat(pushedTypes()).contains("timeline_init", "requests");
        assertThat(pushedTypes()).doesNotContain("vram_init", "vram_delta");
        verify(vramService, never()).getVramStats(anyString(), anyInt());
    }

    @Test
    void setInterestToLocalProvidersReInitsVramOnly() throws Exception {
        handler.afterConnectionEstablished(session);
        handler.handleMessage(session, new TextMessage(
            "{\"action\":\"init\",\"interest\":\"requests\"}"));
        clearInvocations(requestLogService, vramService, statsService);
        sent.clear();

        handler.handleMessage(session, new TextMessage(
            "{\"action\":\"set_interest\",\"interest\":\"local-providers\"}"));

        assertThat(pushedTypes()).contains("vram_init");
        assertThat(pushedTypes()).doesNotContain("timeline_init", "requests", "stats");
        verify(statsService, never()).getRequestLogStats(any(), any(), anyInt(), any(), any(), any(), any(), anyBoolean(), any());
    }
}
