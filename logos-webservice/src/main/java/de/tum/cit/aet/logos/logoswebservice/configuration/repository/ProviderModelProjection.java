package de.tum.cit.aet.logos.logoswebservice.configuration.repository;

import java.time.Instant;

public interface ProviderModelProjection {
    Integer getModelId();
    String getModelName();
    String getEndpoint();
    String getApiKey();

    /** 'supported', 'unsupported', 'unknown', or 'logos' for worker nodes (Logos runs their batches). */
    String getBatchSupport();
    String getBatchDeployment();
    String getBatchDetail();
    String getBatchSource();
    Instant getBatchCheckedAt();
}
