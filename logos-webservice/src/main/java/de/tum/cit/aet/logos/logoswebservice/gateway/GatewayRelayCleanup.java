package de.tum.cit.aet.logos.logoswebservice.gateway;

import java.util.concurrent.Callable;

import org.springframework.web.context.request.NativeWebRequest;
import org.springframework.web.context.request.RequestAttributes;
import org.springframework.web.context.request.RequestContextHolder;
import org.springframework.web.context.request.async.CallableProcessingInterceptor;

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
public final class GatewayRelayCleanup implements CallableProcessingInterceptor {

    static final String ATTRIBUTE = GatewayRelayCleanup.class.getName() + ".pump";

    /** Registers {@code pump} with the current request, if there is one. */
    static void closeWhenRequestEnds(GatewayCloudForwarder.UpstreamPump pump) {
        RequestAttributes attributes = RequestContextHolder.getRequestAttributes();
        if (attributes != null) {
            attributes.setAttribute(ATTRIBUTE, pump, RequestAttributes.SCOPE_REQUEST);
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
        if (request.getAttribute(ATTRIBUTE, RequestAttributes.SCOPE_REQUEST)
                instanceof GatewayCloudForwarder.UpstreamPump pump) {
            pump.closeClientRequest();
        }
    }
}
