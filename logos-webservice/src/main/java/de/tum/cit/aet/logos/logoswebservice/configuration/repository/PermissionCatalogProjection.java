package de.tum.cit.aet.logos.logoswebservice.configuration.repository;

/**
 * One provider-model pair of the permission catalog. Carries only names and
 * ids, never provider URLs or credentials. The model fields are null for a
 * provider that serves no model.
 */
public interface PermissionCatalogProjection {
    Integer getProviderId();
    String getProviderName();
    Integer getModelId();
    String getModelName();
}
