package de.tum.cit.aet.logos.logoswebservice.configuration.dto;

import java.util.List;

/**
 * The model-request listing as the Models page renders it: the requested
 * models ({@code models}, most-voted first) and the deployment setting
 * {@code require_admin_license_approval} — whether the Logos admin still
 * records the license acceptances. It mirrors the webservice's
 * REQUIRE_LOGOS_ADMIN_LICENSE_APPROVAL environment variable (true while
 * unset); while it is false, a non-gated model's license is reference
 * material, and the user interface offers it read-only. With the global
 * SNAKE_CASE naming strategy this serializes to {@code models}/{@code
 * require_admin_license_approval}.
 */
public record ModelRequestsResponse(List<RequestedModelResponse> models,
                                    Boolean requireAdminLicenseApproval) {}
