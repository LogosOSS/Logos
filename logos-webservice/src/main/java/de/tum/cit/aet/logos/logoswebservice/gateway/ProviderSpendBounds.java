package de.tum.cit.aet.logos.logoswebservice.gateway;

import java.net.URI;
import java.util.Locale;
import java.util.Set;

import com.fasterxml.jackson.databind.JsonNode;

/** Supported request protocols; model revisions and ceilings are reviewed deployment data, never a code list. */
final class ProviderSpendBounds {

    private static final String OPENAI = "api.openai.com";
    private static final String ANTHROPIC = "api.anthropic.com";
    private static final Set<String> CHAT = Set.of("model", "messages", "max_tokens", "max_completion_tokens",
        "stream", "stream_options", "temperature", "top_p", "stop", "n", "timeout_s", "service_tier",
        "frequency_penalty", "presence_penalty", "seed", "user", "reasoning_effort");
    private static final Set<String> MESSAGES = Set.of("model", "messages", "system", "max_tokens", "stop_sequences",
        "stream", "temperature", "top_k", "top_p", "timeout_s");
    private static final Set<String> RESPONSES = Set.of("model", "input", "instructions", "max_output_tokens",
        "stream", "temperature", "top_p", "timeout_s", "service_tier");
    private static final Set<String> TURN = Set.of("role", "content", "name");
    private static final Set<String> BLOCK = Set.of("type", "text");

    private ProviderSpendBounds() {}

    static String officialUrl(String value) {
        URI target = parse(value);
        if (!secure(target) || target.getRawQuery() != null || kind(target) == null) {
            return null;
        }
        return "https://" + target.getHost().toLowerCase(Locale.ROOT) + target.getRawPath();
    }

    static String kind(URI target) {
        if (!secure(target) || target.getRawQuery() != null) {
            return null;
        }
        String host = target.getHost().toLowerCase(Locale.ROOT);
        String path = target.getRawPath();
        if (OPENAI.equals(host) && "/v1/chat/completions".equals(path)) {
            return "chat";
        }
        if (OPENAI.equals(host) && "/v1/responses".equals(path)) {
            return "responses";
        }
        return ANTHROPIC.equals(host) && "/v1/messages".equals(path) ? "messages" : null;
    }

    static boolean trustedEvidence(String requestUrl, String revision, String outputField, String source) {
        URI official = parse(requestUrl);
        String kind = kind(official);
        URI evidence = parse(source);
        if (kind == null || !secure(evidence) || evidence.getRawQuery() != null
                || evidence.getRawPath() == null || evidence.getRawPath().length() < 2 || revision == null) {
            return false;
        }
        String evidenceHost = evidence.getHost().toLowerCase(Locale.ROOT);
        // Date suffixes identify concrete, non-moving provider snapshots, without listing models.
        return switch (kind) {
            case "chat", "responses" -> revision.matches("[a-z0-9][a-z0-9._-]*-20[0-9]{2}-[0-9]{2}-[0-9]{2}")
                && Set.of("developers.openai.com", "platform.openai.com").contains(evidenceHost)
                && ("chat".equals(kind) ? "max_completion_tokens".equals(outputField)
                    || "max_tokens".equals(outputField) : "max_output_tokens".equals(outputField));
            case "messages" -> revision.matches("[a-z0-9][a-z0-9._-]*-20[0-9]{6}")
                && Set.of("platform.claude.com", "docs.anthropic.com").contains(evidenceHost)
                && "max_tokens".equals(outputField);
            default -> false;
        };
    }

    static Integer outputLimit(String kind, String outputField, JsonNode payload, int modelLimit) {
        if (kind == null || payload == null || !payload.isObject()) {
            return null;
        }
        if ("responses".equals(kind)) {
            if (!only(payload, RESPONSES) || !payload.path("input").isTextual()
                    || !optionalText(payload, "instructions")) {
                return null;
            }
        } else {
            boolean messages = "messages".equals(kind);
            if (!only(payload, messages ? MESSAGES : CHAT) || !payload.path("messages").isArray()
                    || (messages && !optionalText(payload, "system"))
                    || (!messages && (payload.has("max_tokens") && payload.has("max_completion_tokens")
                        || payload.has("n") && (!payload.path("n").isIntegralNumber()
                            || !payload.path("n").canConvertToInt() || payload.path("n").intValue() != 1)))) {
                return null;
            }
            for (JsonNode turn : payload.path("messages")) {
                if (!turn.isObject() || !only(turn, TURN) || !turn.path("role").isTextual() || !validContent(turn, messages)) {
                    return null;
                }
            }
        }
        JsonNode limit = payload.path(outputField);
        if (!limit.isIntegralNumber() || !limit.canConvertToInt() || limit.intValue() < 1 || limit.intValue() > modelLimit) {
            return null;
        }
        return limit.intValue();
    }

    private static boolean validContent(JsonNode turn, boolean messages) {
        JsonNode content = turn.path("content");
        if (content.isTextual()) {
            return true;
        }
        if (!messages || !content.isArray()) {
            return false;
        }
        for (JsonNode block : content) {
            if (!block.isObject() || !only(block, BLOCK) || !"text".equals(block.path("type").asText())
                    || !block.path("text").isTextual()) {
                return false;
            }
        }
        return true;
    }

    private static boolean only(JsonNode node, Set<String> keys) {
        var fields = node.fieldNames();
        while (fields.hasNext()) {
            if (!keys.contains(fields.next())) {
                return false;
            }
        }
        return true;
    }

    private static boolean optionalText(JsonNode node, String field) {
        return !node.has(field) || node.path(field).isTextual();
    }

    private static URI parse(String value) {
        try {
            return value == null ? null : URI.create(value);
        } catch (IllegalArgumentException e) {
            return null;
        }
    }

    private static boolean secure(URI uri) {
        return uri != null && "https".equalsIgnoreCase(uri.getScheme()) && uri.getHost() != null
            && uri.getUserInfo() == null && (uri.getPort() == -1 || uri.getPort() == 443) && uri.getRawFragment() == null;
    }
}
