package de.tum.cit.aet.logos.logoswebservice.operations.repository;

public interface PublicHourlyRequestsProjection {
    int getHour();
    long getRequests();
    long getTeams();
    long getLargestTeamRequests();
}
