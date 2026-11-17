package de.tum.cit.aet.logos.logoswebservice.configuration.dto;

import java.util.Map;

/**
 * The node-wide settings of a node. The map's keys are the orchestrator's
 * contract, and the orchestrator checks them.
 */
public record ReplaceNodeSettingsRequestDTO(
    Integer providerId,
    Integer expectedRevision,
    Map<String, Object> settings
) {}
