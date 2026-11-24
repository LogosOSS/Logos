package de.tum.cit.aet.logos.logoswebservice.identity.dto;

import java.util.List;

/** Selected CSV rows and the optional team to which each person should be added. */
public record ImportUsersRequestDTO(List<Row> rows, Integer team_id) {
    public record Row(String prename, String name, String email, String team) {}
}
