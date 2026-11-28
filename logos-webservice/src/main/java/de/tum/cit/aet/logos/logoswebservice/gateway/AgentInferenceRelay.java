package de.tum.cit.aet.logos.logoswebservice.gateway;

import java.io.IOException;
import java.io.InputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CancellationException;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.Semaphore;
import java.util.concurrent.atomic.AtomicBoolean;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.dao.DataAccessException;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import org.springframework.web.server.ResponseStatusException;

/** Privileged upstream transport. The workspace grant is never an upstream credential. */
@Service
final class AgentInferenceRelay {
    private static final Duration IDLE = Duration.ofMinutes(30);
    private static final Set<String> MESSAGES_BASE_PATHS = Set.of("", "/", "/v1", "/v1/");
    private static final String ACTIVE_SESSION = """
        s.status IN ('starting', 'running') AND COALESCE(s.error, '') NOT LIKE 'cancel_requested:%'
        """;

    private static final String REMOTE_SCOPE = """
        p.provider_type = 'cloud' AND p.cloud_provider_type = 'logos'
        AND p.base_url = s.inference_base_url
        AND (
            (LOWER(BTRIM(COALESCE(p.auth_name, ''))) = '' AND COALESCE(p.auth_format, '') = '')
            OR (LOWER(BTRIM(COALESCE(p.auth_name, ''))) IN ('', 'authorization') AND p.auth_format = 'Bearer {}')
        )
        """;

    private static final String TEAM_SCOPE = """
        (
            (s.team_repository_id IS NULL AND s.repo_slug IS NULL AND s.trigger_kind IS DISTINCT FROM 'analysis')
            OR EXISTS (
                SELECT 1 FROM team_repositories tr
                JOIN team_privacy_providers pp ON pp.team_id = tr.team_id AND pp.provider_id = s.inference_provider_id
                WHERE tr.id = s.team_repository_id AND LOWER(tr.repo_slug) = LOWER(s.repo_slug)
                  AND (
                      (s.trigger_kind IS DISTINCT FROM 'analysis' AND tr.agent_enabled = TRUE)
                      OR (
                          s.trigger_kind = 'analysis' AND s.no_push AND EXISTS (
                              SELECT 1 FROM ai_workflow_analyses a
                              WHERE a.agent_session_id = s.id AND a.team_repository_id = tr.id AND a.team_id = tr.team_id
                                AND (tr.automatic_workflow_analysis
                                     OR NOT (a.automatically_queued OR s.created_by = 'logos-agent (analysis)'))
                          )
                      )
                  )
            )
        )
        """;

    record Grant(long sessionId, String hash, String tag, int providerId, int modelId, String model,
                 URI target, String operatingKey) {
        // Do not expose credential-bearing record component values in logging.
        @Override public String toString() { return "AgentInferenceGrant[sessionId=" + sessionId + "]"; }
    }

    private record Scope(String hash, String tag, int providerId, int modelId, String model, URI target) { }

    private final NamedParameterJdbcTemplate jdbc;
    private final ProviderSpendCapService providerSpend;
    private final HttpClient upstream;
    private final Semaphore admission = new Semaphore(8);
    private final Set<ActiveRelay> activeRelays = ConcurrentHashMap.newKeySet();

    @Autowired
    AgentInferenceRelay(NamedParameterJdbcTemplate jdbc, ProviderSpendCapService providerSpend) {
        this(jdbc, providerSpend, HttpClient.newBuilder()
            .version(HttpClient.Version.HTTP_1_1).followRedirects(HttpClient.Redirect.NEVER)
            .connectTimeout(Duration.ofSeconds(10)).build());
    }

    AgentInferenceRelay(NamedParameterJdbcTemplate jdbc, ProviderSpendCapService providerSpend,
            HttpClient upstream) {
        this.jdbc = jdbc;
        this.providerSpend = providerSpend;
        this.upstream = upstream;
    }

    static URI messagesTarget(String value) {
        URI url = URI.create(value);
        // Logos providers support Messages on the standard surface. The
        // registry can store the root or the same root with the API version.
        // Neither a per-model endpoint nor caller input chooses this URL.
        if (!"https".equalsIgnoreCase(url.getScheme()) || url.getHost() == null
                || url.getRawAuthority().endsWith(":") || url.getHost().contains("%")
                || url.getPort() == 0 || url.getPort() > 65535 || url.getRawUserInfo() != null
                || url.getRawQuery() != null || url.getRawFragment() != null
                || !MESSAGES_BASE_PATHS.contains(url.getRawPath())) {
            throw new IllegalArgumentException("Remote Agent deployment requires a Logos HTTPS base URL");
        }
        int port = url.getPort();
        return URI.create("https://" + url.getHost().toLowerCase(Locale.ROOT)
            + (port < 0 || port == 443 ? "" : ":" + port) + "/v1/messages");
    }

    Grant authorize(String hash, String tag) {
        try {
            Map<String, String> identity = Map.of("hash", hash, "tag", tag);
            // Bind on first use. UPDATE only touches the one unbound row; matched
            // calls are read-only. Pause/resume detaches by clearing the grant.
            jdbc.update("""
                UPDATE agent_sessions s SET inference_client_tag = :tag
                WHERE inference_grant_hash = :hash AND inference_client_tag IS NULL AND
                """ + ACTIVE_SESSION, identity);
            return jdbc.query("""
                SELECT s.id, s.inference_provider_id, m.id AS inference_model_id, m.name, p.base_url,
                       COALESCE(NULLIF(mp.api_key, ''), p.api_key, '') AS operating_key,
                """ + TEAM_SCOPE + """
                       AS privacy_permitted
                FROM agent_sessions s
                JOIN models m ON m.id = s.inference_model_id AND m.name = s.model
                JOIN providers p ON p.id = s.inference_provider_id
                JOIN model_provider mp ON mp.provider_id = p.id AND mp.model_id = m.id
                WHERE s.inference_grant_hash = :hash AND s.inference_client_tag = :tag
                    AND
                """ + REMOTE_SCOPE + " AND " + ACTIVE_SESSION, identity, rs -> {
                    if (!rs.next()) throw denied();
                    if (!rs.getBoolean("privacy_permitted")) {
                        throw new ResponseStatusException(HttpStatus.FORBIDDEN, "Agent repository privacy does not allow remote inference");
                    }
                    URI destination;
                    try {
                        destination = messagesTarget(rs.getString("base_url"));
                    } catch (IllegalArgumentException | NullPointerException e) {
                        throw unavailable();
                    }
                    String key = rs.getString("operating_key");
                    if (key == null || key.isBlank()
                            || key.length() > 512 || !key.matches("[A-Za-z0-9._~+/=-]+")) {
                        throw unavailable();
                    }
                    return new Grant(rs.getLong("id"), hash, tag, rs.getInt("inference_provider_id"),
                        rs.getInt("inference_model_id"), rs.getString("name"), destination, key);
                });
        } catch (DataAccessException e) {
            // Driver errors can contain query arguments: do not include them in responses.
            throw unavailable();
        }
    }

    ActiveRelay open(Grant grant) {
        if (!admission.tryAcquire()) throw unavailable();
        ActiveRelay relay = new ActiveRelay(grant);
        activeRelays.add(relay);
        return relay;
    }

    HttpResponse<InputStream> forward(ActiveRelay relay, byte[] body, String beta) {
        Grant grant = relay.grant;
        providerSpend.check(grant.providerId());
        HttpRequest.Builder request = HttpRequest.newBuilder(grant.target()).timeout(IDLE)
            .header("Authorization", "Bearer " + grant.operatingKey())
            .header("Content-Type", "application/json")
            .header("Accept", "application/json, text/event-stream")
            .header("anthropic-version", CloudAuthHeaders.ANTHROPIC_VERSION);
        if (beta != null) request.header("anthropic-beta", beta);
        try {
            if (relay.closed.get()) throw denied();
            var pending = upstream.sendAsync(request.POST(HttpRequest.BodyPublishers.ofByteArray(body)).build(),
                HttpResponse.BodyHandlers.ofInputStream());
            relay.attach(pending);
            var response = pending.get();
            relay.attach(response.body());
            if (relay.closed.get()) throw denied();
            return response;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw unavailable();
        } catch (CancellationException e) {
            throw denied();
        } catch (ExecutionException | IllegalArgumentException e) {
            throw new ResponseStatusException(HttpStatus.BAD_GATEWAY, "Remote inference is unavailable");
        }
    }

    @Scheduled(scheduler = "agentInferenceRelayScheduler", fixedDelay = 1000)
    void closeInactive() {
        ActiveRelay[] relays = activeRelays.toArray(ActiveRelay[]::new);
        if (relays.length == 0) return;
        Map<Long, Scope> valid = new HashMap<>();
        Set<Long> sessions = new HashSet<>();
        for (ActiveRelay relay : relays) sessions.add(relay.grant.sessionId());
        try {
            jdbc.query("""
                SELECT s.id, s.inference_grant_hash, s.inference_client_tag,
                       s.inference_provider_id, s.inference_model_id, m.name, p.base_url
                FROM agent_sessions s
                JOIN models m ON m.id = s.inference_model_id AND m.name = s.model
                JOIN model_provider mp ON mp.provider_id = s.inference_provider_id
                                      AND mp.model_id = s.inference_model_id
                JOIN providers p ON p.id = s.inference_provider_id
                WHERE s.id IN (:sessions) AND
                """ + REMOTE_SCOPE + " AND " + ACTIVE_SESSION + " AND " + TEAM_SCOPE, Map.of("sessions", sessions), rs -> {
                    long id = rs.getLong("id");
                    try {
                        valid.put(id, new Scope(rs.getString("inference_grant_hash"),
                            rs.getString("inference_client_tag"), rs.getInt("inference_provider_id"),
                            rs.getInt("inference_model_id"), rs.getString("name"), messagesTarget(rs.getString("base_url"))));
                    } catch (IllegalArgumentException | NullPointerException ignored) {
                        // A changed/invalid target must no longer own an active stream.
                    }
                });
            long now = System.nanoTime();
            for (ActiveRelay relay : relays) {
                Grant grant = relay.grant;
                Scope scope = valid.get(grant.sessionId());
                if (scope == null || !grant.hash().equals(scope.hash()) || !grant.tag().equals(scope.tag())
                        || grant.providerId() != scope.providerId() || grant.modelId() != scope.modelId()
                        || !grant.model().equals(scope.model()) || !grant.target().equals(scope.target())
                        || now - relay.lastActivity > IDLE.toNanos()) {
                    relay.close();
                }
            }
        } catch (DataAccessException e) {
            // If assignment/cancellation cannot be checked, fail closed.
            for (ActiveRelay relay : relays) relay.close();
        }
    }

    final class ActiveRelay extends InputStream {
        private final Grant grant;
        private final AtomicBoolean closed = new AtomicBoolean();
        private volatile CompletableFuture<?> future;
        private volatile InputStream body;
        private volatile long lastActivity = System.nanoTime();

        ActiveRelay(Grant grant) { this.grant = grant; }

        void attach(CompletableFuture<?> pending) {
            future = pending;
            if (closed.get()) pending.cancel(true);
        }

        void attach(InputStream source) {
            body = source;
            if (closed.get()) closeBody(source);
        }

        @Override public int read() throws IOException {
            if (closed.get()) throw new IOException("Agent relay closed");
            int count = body.read();
            if (count >= 0) lastActivity = System.nanoTime();
            return count;
        }

        @Override public int read(byte[] bytes, int offset, int length) throws IOException {
            if (closed.get()) throw new IOException("Agent relay closed");
            int count = body.read(bytes, offset, length);
            if (count > 0) lastActivity = System.nanoTime();
            return count;
        }

        @Override public void close() {
            if (!closed.compareAndSet(false, true)) return;
            try {
                if (future != null) future.cancel(true);
                if (body != null) closeBody(body);
            } finally {
                activeRelays.remove(this);
                admission.release();
            }
        }

        private static void closeBody(InputStream source) {
            try {
                source.close();
            } catch (IOException ignored) {
                // Disconnect is final, including when the upstream already closed.
            }
        }
    }

    private static ResponseStatusException denied() {
        return new ResponseStatusException(HttpStatus.UNAUTHORIZED, "Agent inference grant is invalid");
    }

    private static ResponseStatusException unavailable() {
        return new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE, "Agent relay is unavailable");
    }
}
