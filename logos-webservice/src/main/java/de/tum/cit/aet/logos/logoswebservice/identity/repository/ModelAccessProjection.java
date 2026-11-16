package de.tum.cit.aet.logos.logoswebservice.identity.repository;

public interface ModelAccessProjection {
    Integer getModelId();
    Integer getProviderId();
    String getModelName();
    String getProviderName();
    String getProviderType();
}
