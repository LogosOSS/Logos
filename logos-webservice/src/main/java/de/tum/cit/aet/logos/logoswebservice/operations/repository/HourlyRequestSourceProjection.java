package de.tum.cit.aet.logos.logoswebservice.operations.repository;

public interface HourlyRequestSourceProjection {
    long getHourEpoch();
    long getOtherRequests();
    long getAgentRequests();
    long getBenchmarkRequests();
}
