package de.tum.cit.aet.logos.logoswebservice.configuration.dto;

import java.time.OffsetDateTime;

/**
 * A requested model's license, as the acceptance dialog renders it.
 * {@code status} is {@code pending} (no license fetched yet), {@code available}
 * (a license exists to accept) or {@code no_license} (the repository carries
 * no license information). {@code licenseText} is the license file's text when
 * the repository carries one, {@code licenseId} the SPDX expression the model
 * card declares (null when it names none), and {@code approved}/{@code
 * approvedAt} the Logos admin's global acceptance (null while it is absent).
 */
public record ModelLicenseResponse(
    String status,
    String licenseId,
    String licenseText,
    Boolean approved,
    OffsetDateTime approvedAt
) {}
