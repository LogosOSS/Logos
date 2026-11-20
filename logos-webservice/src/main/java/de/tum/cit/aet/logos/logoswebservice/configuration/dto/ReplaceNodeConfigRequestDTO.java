package de.tum.cit.aet.logos.logoswebservice.configuration.dto;

import java.util.List;
import java.util.Map;

/**
 * The whole model list of a node. Entries stay maps: their keys
 * (model_name, capability, overrides) are the orchestrator's contract, and
 * the orchestrator checks them.
 */
public record ReplaceNodeConfigRequestDTO(
    Integer providerId,
    Integer expectedRevision,
    List<Map<String, Object>> entries
) {}
