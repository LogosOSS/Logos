package de.tum.cit.aet.logos.logoswebservice.identity.dto;

/**
 * Update a workflow's lifecycle or soft-delete it. The tag is fixed once stored.
 * Null fields are left unchanged; {@code deleted=true} soft-deletes.
 */
public record UpdateWorkflowRequestDTO(
    String status,
    Boolean deleted
) {}
