package de.tum.cit.aet.logos.logoswebservice.configuration.dto;

/**
 * A requested model as the Models page reports it: the registry row id, the
 * model name, how many users have voted for it, whether the calling user has,
 * whether the Hugging Face repository is gated for the central HF_TOKEN
 * (null until the precheck has produced a verdict), the team that first
 * requested the model (null until a vote named one), and the license state:
 * {@code license_status} is {@code pending} (no license fetched yet),
 * {@code available} (a license exists to accept) or {@code no_license}
 * (the repository carries no license information), and {@code license_approved}
 * whether the Logos admin has accepted the agreement. With the global
 * SNAKE_CASE naming strategy this serializes to {@code id}/{@code name}/
 * {@code request_count}/{@code has_voted}/{@code is_gated}/
 * {@code first_requesting_team}/{@code license_status}/{@code license_approved}.
 */
public record RequestedModelResponse(
    Integer id,
    String name,
    Integer requestCount,
    Boolean hasVoted,
    Boolean isGated,
    Integer firstRequestingTeam,
    String licenseStatus,
    Boolean licenseApproved
) {}
