package de.tum.cit.aet.logos.logoswebservice.gateway;

import java.net.URI;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataAccessException;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.web.server.ResponseStatusException;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

/** Budget one upstream attempt using the same database primitive as the orchestrator. */
@Service
public class ProviderSpendCapService {

    private static final Logger log = LoggerFactory.getLogger(ProviderSpendCapService.class);

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    public ProviderSpendCapService(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    UUID reserve(GatewayDeployment deployment, String inferencePath, String query, JsonNode payload) {
        UUID attempt = UUID.randomUUID();
        String kind = null;
        if ((query == null || query.isEmpty()) && payload != null
                && deployment.modelName().equals(payload.path("model").asText(null))) {
            try {
                kind = kindFor(URI.create(CloudForwardUrlBuilder.build(
                    deployment.baseUrl(), inferencePath, deployment.endpoint())));
            } catch (IllegalArgumentException e) {
                // A provider URL the gateway cannot validate is not a bounded route.
            }
        }
        String decision;
        try {
            decision = jdbc.queryForObject("""
                SELECT logos_reserve_provider_spend(:attempt, :provider, :model, :kind, CAST(:payload AS jsonb))
                """, new MapSqlParameterSource()
                    .addValue("attempt", attempt)
                    .addValue("provider", deployment.providerId())
                    .addValue("model", deployment.modelId())
                    .addValue("kind", kind)
                    .addValue("payload", kind != null && payload != null ? payload.toString() : null), String.class);
        } catch (DataAccessException e) {
            log.warn("Could not check provider spend limit for provider {}", deployment.providerId(), e);
            throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE,
                "Provider monthly spend limit is unavailable");
        }
        return switch (decision == null ? "" : decision) {
            case "uncapped" -> null;
            case "reserved" -> attempt;
            case "exhausted" -> throw new ResponseStatusException(HttpStatus.PAYMENT_REQUIRED,
                "Provider monthly spend limit reached");
            case "unbounded" -> throw new ResponseStatusException(HttpStatus.UNPROCESSABLE_ENTITY,
                "Request cost cannot be bounded under this provider's monthly spend limit");
            default -> throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE,
                "Provider monthly spend reservation could not be confirmed");
        };
    }

    void releaseUndispatched(UUID attempt) {
        if (attempt != null) {
            settle(attempt, null, null, true);
        }
    }

    void settle(UUID attempt, GatewayForwardResult result) {
        if (attempt == null || result == null) {
            return;
        }
        Map<String, Long> usage = result.usage();
        if (usage == null || usage.containsKey("usage_estimated")) {
            return;
        }
        Long prompt = usage.get("prompt_tokens");
        Long completion = usage.get("completion_tokens");
        if (!reportedCount(prompt) || !reportedCount(completion)
                || (usage.containsKey("provider_delivered_nothing") && (prompt != 0L || completion != 0L))) {
            return;
        }
        if (usage.containsKey("provider_delivered_nothing")) {
            usage = new LinkedHashMap<>(usage);
            usage.remove("provider_delivered_nothing");
        }
        try {
            settle(attempt, mapper.writeValueAsString(usage), result.serviceTier(), false);
        } catch (JsonProcessingException e) {
            log.warn("Could not encode usage for provider spend attempt {}", attempt, e);
        }
    }

    private static boolean reportedCount(Long count) {
        return count != null && count >= 0;
    }

    private void settle(UUID attempt, String usage, String tier, boolean notDispatched) {
        try {
            jdbc.queryForObject("""
                SELECT logos_settle_provider_spend(:attempt, CAST(:usage AS jsonb), :tier, :notDispatched)
                """, new MapSqlParameterSource()
                    .addValue("attempt", attempt)
                    .addValue("usage", usage)
                    .addValue("tier", tier)
                    .addValue("notDispatched", notDispatched), Boolean.class);
        } catch (DataAccessException e) {
            // An unknown outcome keeps its full hold.
            log.warn("Could not settle provider spend attempt {}; keeping reservation", attempt, e);
        }
    }

    static String kindFor(URI target) {
        String host = target.getHost();
        String path = target.getPath();
        if (!"https".equalsIgnoreCase(target.getScheme()) || host == null || path == null
                || target.getUserInfo() != null || (target.getPort() != -1 && target.getPort() != 443)) {
            return null;
        }
        if (target.getRawQuery() != null || target.getRawFragment() != null) {
            return null;
        }
        host = host.toLowerCase(java.util.Locale.ROOT);
        if (host.equals("api.openai.com")) {
            return switch (path) {
                case "/v1/chat/completions" -> "chat";
                case "/v1/responses" -> "responses";
                default -> null;
            };
        }
        if (host.equals("api.anthropic.com") && path.equals("/v1/messages")) {
            return "messages";
        }
        return null;
    }
}
