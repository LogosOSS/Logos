package de.tum.cit.aet.logos.logoswebservice.configuration.dto;

public record CalibrateUncalibratedRequestDTO(
    Integer providerId,
    /** Calibrate only this model; empty calibrates everything that needs it. */
    String model
) {}
