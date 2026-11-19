package de.tum.cit.aet.logos.logoswebservice.common;

import java.io.IOException;
import java.util.Arrays;
import java.util.TreeSet;

import org.springframework.beans.factory.ObjectProvider;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.web.HttpRequestMethodNotSupportedException;
import org.springframework.web.HttpMediaTypeNotAcceptableException;
import org.springframework.web.HttpMediaTypeNotSupportedException;
import org.springframework.web.filter.OncePerRequestFilter;
import org.springframework.web.method.HandlerMethod;
import org.springframework.web.servlet.HandlerExecutionChain;
import org.springframework.web.servlet.mvc.method.annotation.RequestMappingHandlerMapping;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletRequestWrapper;
import jakarta.servlet.http.HttpServletResponse;

/**
 * Adapts explicitly opted-in QUERY handlers to Spring MVC 7.0's POST body decoding.
 * Runs after authentication, authorization and CORS, which see the original method.
 * Controller interceptors and method security still run during normal MVC dispatch.
 */
public class HttpQueryFilter extends OncePerRequestFilter {

    private final ObjectProvider<RequestMappingHandlerMapping> mappings;

    public HttpQueryFilter(ObjectProvider<RequestMappingHandlerMapping> mappings) {
        this.mappings = mappings;
    }

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        return !"QUERY".equals(request.getMethod()) && !"OPTIONS".equals(request.getMethod());
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        HttpServletRequest post = new HttpServletRequestWrapper(request) {
            @Override
            public String getMethod() {
                return "POST";
            }
        };
        boolean supported = supportsQuery(post, response);
        if ("OPTIONS".equals(request.getMethod())) {
            if (supported) {
                response.setHeader(HttpHeaders.ALLOW, "POST, QUERY, OPTIONS");
                response.setHeader("Accept-Query", MediaType.APPLICATION_JSON_VALUE);
                response.setStatus(HttpServletResponse.SC_NO_CONTENT);
            } else {
                chain.doFilter(request, response);
            }
            return;
        }
        if (!supported) {
            response.sendError(HttpServletResponse.SC_METHOD_NOT_ALLOWED);
            return;
        }
        response.setHeader("Accept-Query", MediaType.APPLICATION_JSON_VALUE);
        if (!isJson(request.getContentType())) {
            response.sendError(HttpServletResponse.SC_UNSUPPORTED_MEDIA_TYPE);
            return;
        }
        chain.doFilter(post, response);
    }

    private boolean supportsQuery(HttpServletRequest request, HttpServletResponse response) throws ServletException {
        try {
            HandlerExecutionChain handler = mappings.getObject().getHandler(request);
            if (handler != null && handler.getHandler() instanceof HandlerMethod method) {
                if (method.hasMethodAnnotation(ReadOnlyQuery.class)) return true;
                String[] allowed = mappings.getObject().getHandlerMethods().entrySet().stream()
                    .filter(entry -> entry.getValue().getMethod().equals(method.getMethod()))
                    .flatMap(entry -> entry.getKey().getMethodsCondition().getMethods().stream())
                    .map(Enum::name).toArray(String[]::new);
                advertiseAllowedMethods(response, allowed);
            } else {
                advertiseAllowedMethods(response, new String[0]);
            }
            return false;
        } catch (HttpRequestMethodNotSupportedException e) {
            advertiseAllowedMethods(response, e.getSupportedMethods());
            return false;
        } catch (HttpMediaTypeNotSupportedException | HttpMediaTypeNotAcceptableException e) {
            advertiseAllowedMethods(response, new String[0]);
            return false;
        } catch (Exception e) {
            throw new ServletException("Could not resolve HTTP QUERY handler", e);
        }
    }

    private static void advertiseAllowedMethods(HttpServletResponse response, String[] methods) {
        TreeSet<String> allowed = new TreeSet<>();
        if (methods != null) allowed.addAll(Arrays.asList(methods));
        if (allowed.contains("GET")) allowed.add("HEAD");
        allowed.add("OPTIONS");
        response.setHeader(HttpHeaders.ALLOW, String.join(", ", allowed));
    }

    private static boolean isJson(String contentType) {
        if (contentType == null) return false;
        try {
            return MediaType.APPLICATION_JSON.isCompatibleWith(MediaType.parseMediaType(contentType));
        } catch (IllegalArgumentException e) {
            return false;
        }
    }
}
