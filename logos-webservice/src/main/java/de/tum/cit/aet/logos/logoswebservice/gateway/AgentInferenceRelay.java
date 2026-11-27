package de.tum.cit.aet.logos.logoswebservice.gateway;

import java.io.IOException;
import java.io.InputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.Arrays;
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

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.DataAccessException;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import org.springframework.web.server.ResponseStatusException;

/** Privileged upstream transport. The workspace grant is never an upstream credential. */
@Service
final class AgentInferenceRelay {
    private static final Logger LOG = LoggerFactory.getLogger(AgentInferenceRelay.class);
    private static final Duration IDLE = Duration.ofMinutes(30);
    private static final String ACTIVE = """
        s.status IN ('starting', 'running') AND COALESCE(s.error, '') NOT LIKE 'cancel_requested:%'
        """;

    record Grant(long sessionId, String hash, String tag, int providerId, int modelId, String model,
                 URI origin, String operatingKey) {
        // Do not expose credential-bearing record component values in logging.
        @Override public String toString() { return "AgentInferenceGrant[sessionId=" + sessionId + "]"; }
    }

    private record Scope(String hash, String tag, int providerId, int modelId) { }

    private final NamedParameterJdbcTemplate jdbc;
    private final ProviderSpendCapService providerSpend;
    private final HttpClient upstream;
    private final Set<URI> allowedOrigins;
    private final Semaphore admission = new Semaphore(8);
    private final Set<ActiveRelay> active = ConcurrentHashMap.newKeySet();

    @Autowired
    AgentInferenceRelay(NamedParameterJdbcTemplate jdbc, ProviderSpendCapService providerSpend,
            @Value("${LOGOS_AGENT_REMOTE_ALLOWED_ORIGINS:}") String origins) {
        this(jdbc, providerSpend, origins, HttpClient.newBuilder()
            .version(HttpClient.Version.HTTP_1_1).followRedirects(HttpClient.Redirect.NEVER)
            .connectTimeout(Duration.ofSeconds(10)).build());
    }

    AgentInferenceRelay(NamedParameterJdbcTemplate jdbc, ProviderSpendCapService providerSpend,
            String origins, HttpClient upstream) {
        this.jdbc = jdbc;
        this.providerSpend = providerSpend;
        this.upstream = upstream;
        Set<URI> parsed = new HashSet<>();
        try {
            Arrays.stream(origins.split(",", -1)).filter(s -> !s.isBlank()).forEach(s -> parsed.add(origin(s.strip())));
        } catch (IllegalArgumentException e) {
            // Disable the optional relay without making other routes unavailable.
            LOG.warn("Invalid remote agent origin configuration; agent inference relay disabled");
            parsed.clear();
        }
        this.allowedOrigins = Set.copyOf(parsed);
    }

    static URI origin(String value) {
        URI url = URI.create(value);
        if (!"https".equalsIgnoreCase(url.getScheme()) || url.getHost() == null || url.getPort() == 0
                || url.getPort() > 65535 || url.getRawUserInfo() != null
                || url.getRawQuery() != null || url.getRawFragment() != null
                || !("".equals(url.getRawPath()) || "/".equals(url.getRawPath()))) {
            throw new IllegalArgumentException("Remote agent deployment requires an HTTPS origin");
        }
        int port = url.getPort();
        return URI.create("https://" + url.getHost().toLowerCase(Locale.ROOT)
            + (port < 0 || port == 443 ? "" : ":" + port));
    }

    Grant authorize(String hash, String tag) {
        if (allowedOrigins.isEmpty()) throw unavailable();
        try {
            Map<String, String> identity = Map.of("hash", hash, "tag", tag);
            // Bind on first use. UPDATE only touches the one unbound row; matched
            // calls are read-only. Pause/resume detaches by clearing the grant.
            jdbc.update("""
                UPDATE agent_sessions s SET inference_client_tag = :tag
                WHERE inference_grant_hash = :hash AND inference_client_tag IS NULL AND
                """ + ACTIVE, identity);
            return jdbc.query("""
                SELECT s.id, s.inference_provider_id, m.id AS inference_model_id, m.name, p.base_url,
                       COALESCE(NULLIF(mp.api_key, ''), p.api_key, '') AS operating_key
                FROM agent_sessions s
                JOIN models m ON m.id = s.inference_model_id
                JOIN providers p ON p.id = s.inference_provider_id
                JOIN model_provider mp ON mp.provider_id = p.id AND mp.model_id = m.id
                WHERE s.inference_grant_hash = :hash AND s.inference_client_tag = :tag
                    AND p.provider_type = 'cloud' AND p.cloud_provider_type = 'other'
                    AND
                """ + ACTIVE, identity, rs -> {
                    if (!rs.next()) throw denied();
                    URI destination;
                    try {
                        destination = origin(rs.getString("base_url"));
                    } catch (IllegalArgumentException | NullPointerException e) {
                        throw unavailable();
                    }
                    String key = rs.getString("operating_key");
                    if (!allowedOrigins.contains(destination) || key == null || key.isBlank()
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
        active.add(relay);
        return relay;
    }

    HttpResponse<InputStream> forward(ActiveRelay relay, byte[] body, String beta) {
        Grant grant = relay.grant;
        providerSpend.check(grant.providerId());
        URI target = grant.origin().resolve("/v1/messages");
        HttpRequest.Builder request = HttpRequest.newBuilder(target).timeout(IDLE)
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
        ActiveRelay[] relays = active.toArray(ActiveRelay[]::new);
        if (relays.length == 0) return;
        Map<Long, Scope> valid = new HashMap<>();
        Set<Long> sessions = new HashSet<>();
        for (ActiveRelay relay : relays) sessions.add(relay.grant.sessionId());
        try {
            jdbc.query("""
                SELECT s.id, s.inference_grant_hash, s.inference_client_tag,
                       s.inference_provider_id, s.inference_model_id
                FROM agent_sessions s
                JOIN model_provider mp ON mp.provider_id = s.inference_provider_id
                                      AND mp.model_id = s.inference_model_id
                JOIN providers p ON p.id = s.inference_provider_id
                WHERE s.id IN (:sessions) AND p.provider_type = 'cloud' AND p.cloud_provider_type = 'other'
                    AND
                """ + ACTIVE, Map.of("sessions", sessions), rs -> {
                    long id = rs.getLong("id");
                    valid.put(id, new Scope(rs.getString("inference_grant_hash"),
                        rs.getString("inference_client_tag"), rs.getInt("inference_provider_id"),
                        rs.getInt("inference_model_id")));
                });
            long now = System.nanoTime();
            for (ActiveRelay relay : relays) {
                Grant grant = relay.grant;
                Scope scope = valid.get(grant.sessionId());
                if (scope == null || !grant.hash().equals(scope.hash()) || !grant.tag().equals(scope.tag())
                        || grant.providerId() != scope.providerId() || grant.modelId() != scope.modelId() || now - relay.lastActivity > IDLE.toNanos()) {
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
                active.remove(this);
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
