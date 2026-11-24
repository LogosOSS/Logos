package de.tum.cit.aet.logos.logoswebservice.gateway;

import java.io.IOException;
import java.io.InputStream;
import java.util.concurrent.Callable;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

import org.springframework.web.context.request.NativeWebRequest;
import org.springframework.web.context.request.RequestAttributes;
import org.springframework.web.context.request.RequestContextHolder;
import org.springframework.web.context.request.async.CallableProcessingInterceptor;
import org.springframework.web.servlet.HandlerInterceptor;
import org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;

/**
 * Releases the client relay and preserves provider accounting when its request ends.
 *
 * <p>The forwarder starts reading the upstream before the relay task runs.
 * When MVC rejects that task, times it out, or the request fails before the
 * task ran, the bounded usage drain takes ownership of the reader. Spring calls
 * {@link #afterCompletion} for every async request once it is over, however
 * it ended; for a relay that ran to the end the pump is already closed and
 * cleanup does nothing. A detached usage drain owns its pump until
 * provider usage, EOF or its deadline; request completion leaves that pump open.
 */
public final class GatewayRelayCleanup implements CallableProcessingInterceptor, HandlerInterceptor {

    static final String ATTRIBUTE = GatewayRelayCleanup.class.getName() + ".pump";

    private static final String LIFECYCLE = GatewayRelayCleanup.class.getName() + ".lifecycle";

    static Lifecycle begin(HttpServletRequest request, GatewayInFlightLimit.Owner owner) {
        Lifecycle state = new Lifecycle(owner);
        request.setAttribute(LIFECYCLE, state);
        return state;
    }

    private static Lifecycle current() {
        RequestAttributes attributes = RequestContextHolder.getRequestAttributes();
        return attributes != null && attributes.getAttribute(LIFECYCLE, RequestAttributes.SCOPE_REQUEST)
            instanceof Lifecycle state ? state : null;
    }

    static GatewayInFlightLimit.Owner retainProvider() {
        Lifecycle state = current();
        return state == null ? GatewayInFlightLimit.Owner.noop() : state.owner.retain();
    }

    /** The body callback and request cleanup share one idempotent upstream close. */
    static CloseAction closeWhenRequestEnds(InputStream upstream) {
        Lifecycle state = current();
        GatewayInFlightLimit.Owner upstreamOwner;
        try {
            upstreamOwner = state == null ? GatewayInFlightLimit.Owner.noop() : state.owner.retain();
        } catch (IllegalStateException ended) {
            try {
                upstream.close();
            } catch (IOException ignored) {
                // This request has already ended; do not leave its late upstream open.
            }
            throw ended;
        }
        CloseAction close = new CloseAction(() -> {
            try {
                upstream.close();
            } catch (IOException ignored) {
                // Releasing a connection must not skip releasing its capacity.
            } finally {
                // A competing completion can release the request owner while close()
                // is still running. Keep this resource counted until close returns.
                upstreamOwner.close();
            }
        });
        if (state != null) state.register(close);
        return close;
    }

    static final class CloseAction implements Runnable {
        private final AtomicBoolean closed = new AtomicBoolean();
        private final Runnable action;
        CloseAction(Runnable action) { this.action = action; }
        boolean isClosed() { return closed.get(); }
        @Override public void run() {
            if (closed.compareAndSet(false, true)) action.run();
        }
    }

    static final class Lifecycle implements AutoCloseable {
        private final GatewayInFlightLimit.Owner owner;
        private final AtomicBoolean ended = new AtomicBoolean();
        private final AtomicReference<CloseAction> resource = new AtomicReference<>();
        private Lifecycle(GatewayInFlightLimit.Owner owner) { this.owner = owner; }

        private void register(CloseAction close) {
            if (!resource.compareAndSet(null, close)) throw new IllegalStateException("Upstream already registered");
            if (ended.get()) close.run();
        }

        StreamingResponseBody relay(StreamingResponseBody body) {
            return out -> {
                try {
                    if (ended.get()) throw new IOException("Gateway request already ended");
                    body.writeTo(out);
                } finally {
                    close();
                }
            };
        }

        @Override public void close() {
            if (!ended.compareAndSet(false, true)) return;
            try {
                CloseAction close = resource.get();
                if (close != null) close.run();
            } finally {
                owner.close();
            }
        }
    }

    @Override
    public void afterCompletion(HttpServletRequest request, HttpServletResponse response,
                                Object handler, Exception error) {
        if (!request.isAsyncStarted()) releaseClient(new org.springframework.web.context.request.ServletWebRequest(request));
    }

    /** Registers {@code pump} with the current request, if there is one. */
    static void closeWhenRequestEnds(GatewayCloudForwarder.UpstreamPump pump) {
        RequestAttributes attributes = RequestContextHolder.getRequestAttributes();
        if (attributes != null) {
            attributes.setAttribute(ATTRIBUTE, pump, RequestAttributes.SCOPE_REQUEST);
            Lifecycle state = current();
            if (state != null) state.register(new CloseAction(pump::closeClientRequest));
        }
    }

    @Override
    public <T> Object handleTimeout(NativeWebRequest request, Callable<T> task) {
        releaseClient(request);
        return RESULT_NONE;
    }

    @Override
    public <T> Object handleError(NativeWebRequest request, Callable<T> task, Throwable error) {
        releaseClient(request);
        return RESULT_NONE;
    }

    @Override
    public <T> void afterCompletion(NativeWebRequest request, Callable<T> task) {
        releaseClient(request);
    }

    private void releaseClient(NativeWebRequest request) {
        if (request.getAttribute(LIFECYCLE, RequestAttributes.SCOPE_REQUEST) instanceof Lifecycle state) {
            state.close();
            return;
        }
        if (request.getAttribute(ATTRIBUTE, RequestAttributes.SCOPE_REQUEST)
                instanceof GatewayCloudForwarder.UpstreamPump pump) {
            pump.closeClientRequest();
        }
    }
}
