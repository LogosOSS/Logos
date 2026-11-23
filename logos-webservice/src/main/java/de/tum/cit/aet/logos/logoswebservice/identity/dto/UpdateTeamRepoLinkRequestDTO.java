package de.tum.cit.aet.logos.logoswebservice.identity.dto;

import java.util.List;

public record UpdateTeamRepoLinkRequestDTO(
    String repoUrl,
    String branch,
    List<String> paths,
    Boolean agentEnabled,
    String agentInstructions,
    String verificationCommand,
    List<String> agentEgressHosts,
    Boolean agentEgressUnrestricted,
    Boolean agentMayComment
) {

    /** True when the request changes a sandbox permission that only a logos admin may set. */
    public boolean changesSandboxPermissions() {
        return agentEgressHosts != null || agentEgressUnrestricted != null || agentMayComment != null;
    }
}
