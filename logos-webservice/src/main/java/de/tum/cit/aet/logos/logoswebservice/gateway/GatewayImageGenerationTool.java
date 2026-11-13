package de.tum.cit.aet.logos.logoswebservice.gateway;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import com.fasterxml.jackson.databind.JsonNode;

/**
 * The Responses API {@code image_generation} tool on the direct cloud forward.
 *
 * <p>The tool names its own image model ({@code {"type": "image_generation",
 * "model": "gpt-image-1.5"}}), a second model the request runs: the key must be
 * allowed to use it like any other model. On Azure the image model is a
 * separate deployment of the resource, and Azure runs the tool only when that
 * deployment is named in {@value #AZURE_IMAGE_DEPLOYMENT_HEADER}. The header is
 * sent only for an Azure upstream and only when the body asks for the tool.
 */
public final class GatewayImageGenerationTool {

    public static final String TOOL_TYPE = "image_generation";
    public static final String AZURE_IMAGE_DEPLOYMENT_HEADER = "x-ms-oai-image-generation-deployment";
    /** The image model the tool uses when the entry names none (OpenAI's default). */
    public static final String DEFAULT_IMAGE_MODEL = "gpt-image-1";

    private static final Pattern AZURE_DEPLOYMENT = Pattern.compile("/openai/deployments/([^/?]+)");

    private GatewayImageGenerationTool() {
    }

    /** The image models the body's {@code image_generation} tools name, in order, without duplicates. */
    public static List<String> requestedImageModels(JsonNode root) {
        List<String> names = new ArrayList<>();
        if (root == null || !root.isObject()) {
            return names;
        }
        JsonNode tools = root.get("tools");
        if (tools == null || !tools.isArray()) {
            return names;
        }
        for (JsonNode tool : tools) {
            if (tool == null || !tool.isObject() || !TOOL_TYPE.equals(tool.path("type").asText(""))) {
                continue;
            }
            String model = tool.path("model").asText("").strip();
            String name = model.isEmpty() ? DEFAULT_IMAGE_MODEL : model;
            boolean seen = names.stream().anyMatch(n -> n.equalsIgnoreCase(name));
            if (!seen) {
                names.add(name);
            }
        }
        return names;
    }

    /**
     * The header naming the image deployment, for an Azure chat deployment.
     *
     * <p>Empty for any other upstream, and when the provider of {@code chat}
     * has no permitted deployment of the image model; Azure then refuses the
     * request with its own reason.
     *
     * @param chat             the deployment the request is forwarded to
     * @param imageDeployments the key's permitted deployments of the image model
     */
    public static Map<String, String> azureHeaders(GatewayDeployment chat, List<GatewayDeployment> imageDeployments) {
        if (chat == null || !isAzure(chat) || imageDeployments == null) {
            return Map.of();
        }
        for (GatewayDeployment image : imageDeployments) {
            if (image.providerId() == null || !image.providerId().equals(chat.providerId())) {
                continue;
            }
            Optional<String> name = azureDeploymentName(image.endpoint());
            if (name.isPresent()) {
                return Map.of(AZURE_IMAGE_DEPLOYMENT_HEADER, name.get());
            }
        }
        return Map.of();
    }

    static Optional<String> azureDeploymentName(String endpoint) {
        if (endpoint == null) {
            return Optional.empty();
        }
        Matcher m = AZURE_DEPLOYMENT.matcher(endpoint);
        return m.find() ? Optional.of(m.group(1)) : Optional.empty();
    }

    private static boolean isAzure(GatewayDeployment deployment) {
        String type = deployment.cloudProviderType() == null ? ""
            : deployment.cloudProviderType().strip().toLowerCase(Locale.ROOT);
        return "azure".equals(type);
    }
}
