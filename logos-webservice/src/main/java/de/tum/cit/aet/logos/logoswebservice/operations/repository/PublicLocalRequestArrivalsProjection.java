package de.tum.cit.aet.logos.logoswebservice.operations.repository;

import java.time.Instant;

public interface PublicLocalRequestArrivalsProjection {
    Instant getBucket();
    long getRegular();
    long getAgent();
    long getBenchmark();
}
