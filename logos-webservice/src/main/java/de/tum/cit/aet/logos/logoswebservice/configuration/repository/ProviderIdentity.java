package de.tum.cit.aet.logos.logoswebservice.configuration.repository;

/** The id and the name of a provider: all that a caller needs to know who owns a key. */
public interface ProviderIdentity {
    Integer getId();
    String getName();
}
