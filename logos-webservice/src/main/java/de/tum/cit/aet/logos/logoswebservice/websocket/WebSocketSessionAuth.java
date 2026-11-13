package de.tum.cit.aet.logos.logoswebservice.websocket;

import java.io.IOException;
import java.time.Clock;
import java.time.Instant;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.dao.DataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.WebSocketSession;

import jakarta.annotation.PostConstruct;
import jakarta.annotation.PreDestroy;

/** Revalidates open sockets against the shared database, independently of data pushes. */
@Component
public class WebSocketSessionAuth {

    public static final String USER_ID = "userId";
    public static final String EXPIRES_AT = "tokenExpiresAt";
    // Private application code: clients must force a Keycloak refresh before reconnecting.
    public static final CloseStatus TOKEN_EXPIRED = new CloseStatus(4001, "Token expired");
    private static final Logger log = LoggerFactory.getLogger(WebSocketSessionAuth.class);
    private static final long RECHECK_SECONDS = 30;

    private static class Authorization {
        final WebSocketSession session;
        final boolean adminRequired;
        Instant checkedAt;
        boolean denied;

        Authorization(WebSocketSession session, boolean adminRequired) {
            this.session = session;
            this.adminRequired = adminRequired;
        }
    }

    private final JdbcTemplate jdbc;
    private final Clock clock;
    private final ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor();
    private final Map<String, Authorization> sessions = new ConcurrentHashMap<>();

    @Autowired
    public WebSocketSessionAuth(JdbcTemplate jdbc) {
        this(jdbc, Clock.systemUTC());
    }

    WebSocketSessionAuth(JdbcTemplate jdbc, Clock clock) {
        this.jdbc = jdbc;
        this.clock = clock;
    }

    @PostConstruct
    void start() {
        // Identity expiry must not wait behind price refreshes or other long-running scheduled jobs.
        scheduler.scheduleAtFixedRate(this::revalidateSessions, 1, 1, TimeUnit.SECONDS);
    }

    @PreDestroy
    void shutdown() {
        scheduler.shutdownNow();
    }

    public boolean register(WebSocketSession session, boolean adminRequired) {
        sessions.put(session.getId(), new Authorization(session, adminRequired));
        return isAuthorized(session, true);
    }

    public void unregister(WebSocketSession session) {
        sessions.remove(session.getId());
    }

    /** Client messages always force a database read; pushes may reuse a check for up to 30 seconds. */
    public boolean isAuthorized(WebSocketSession session, boolean force) {
        Authorization auth = sessions.get(session.getId());
        if (auth == null) return false;
        synchronized (auth) {
            if (auth.denied || !session.isOpen()) return false;
            Instant now = clock.instant();
            Object expiry = session.getAttributes().get(EXPIRES_AT);
            Object userId = session.getAttributes().get(USER_ID);
            if (!(expiry instanceof Instant expiresAt) || !(userId instanceof Integer)) {
                return deny(auth, CloseStatus.POLICY_VIOLATION);
            }
            if (!now.isBefore(expiresAt)) return deny(auth, TOKEN_EXPIRED);
            if (!force && auth.checkedAt != null && now.isBefore(auth.checkedAt.plusSeconds(RECHECK_SECONDS))) {
                return true;
            }
            try {
                // Scalar SQL bypasses both identity-sync caches and JPA's persistence context on every replica.
                Boolean allowed = jdbc.queryForObject("""
                    SELECT EXISTS (SELECT 1 FROM users
                    WHERE id = ? AND is_active = true AND (? = false OR role = 'logos_admin'))
                    """, Boolean.class, userId, auth.adminRequired);
                if (!Boolean.TRUE.equals(allowed)) return deny(auth, CloseStatus.POLICY_VIOLATION);
                if (!clock.instant().isBefore(expiresAt)) return deny(auth, TOKEN_EXPIRED);
                auth.checkedAt = now;
                return true;
            } catch (DataAccessException e) {
                log.warn("WebSocket authorization query failed", e);
                return deny(auth, CloseStatus.SERVER_ERROR);
            }
        }
    }

    // An idle socket (including stats/v2 before init) must expire without waiting for a push or a ping.
    void revalidateSessions() {
        for (Authorization auth : sessions.values()) {
            if (!auth.session.isOpen()) unregister(auth.session);
            else isAuthorized(auth.session, false);
        }
    }

    private boolean deny(Authorization auth, CloseStatus status) {
        auth.denied = true;
        try {
            auth.session.close(status);
        } catch (IOException | RuntimeException e) {
            log.debug("Could not close unauthorized WebSocket {}", auth.session.getId(), e);
        } finally {
            sessions.remove(auth.session.getId(), auth);
        }
        return false;
    }
}
