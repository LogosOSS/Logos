package de.tum.cit.aet.logos.logoswebservice.operations.repository;

public interface PublicHourlyRequestsProjection {
    int getHour();
    int getSpan();
    long getRequests();
    long getTeams();
    long getLargestTeamRequests();
}
