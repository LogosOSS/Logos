package de.tum.cit.aet.logos.logoswebservice.identity.dto;

import java.util.List;

/** Confirm or override the SLO of a workflow step. The tag is fixed once stored. */
public record UpdateWorkflowStepRequestDTO(
    String confirmedSlo,
    List<String> confirmedObjectivePriority,
    String name
) {}
