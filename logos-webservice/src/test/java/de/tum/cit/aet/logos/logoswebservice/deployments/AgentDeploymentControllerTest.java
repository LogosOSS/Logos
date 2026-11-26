package de.tum.cit.aet.logos.logoswebservice.deployments;

import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.util.UUID;

import org.junit.jupiter.api.Test;

import de.tum.cit.aet.logos.logoswebservice.common.ForbiddenException;
import de.tum.cit.aet.logos.logoswebservice.deployments.controller.AgentDeploymentController;
import de.tum.cit.aet.logos.logoswebservice.deployments.dto.DeploymentRequests;

/**
 * When the door of a Logos agent is shut: without a key, and on a protected Logos. The request ends before anything
 * is looked up, so no service is needed here. {@link DeploymentFlowTest} covers the open door.
 */
class AgentDeploymentControllerTest {

    private static AgentDeploymentController controller(boolean protect, String agentKey) {
        return new AgentDeploymentController(
            new DeploymentProperties(protect, null, null, null, null, agentKey), null);
    }

    private static void assertShut(AgentDeploymentController controller, String authorization) {
        assertThatThrownBy(() -> controller.start(authorization, new DeploymentRequests.StartPlan("pr-93")))
            .isInstanceOf(ForbiddenException.class);
        assertThatThrownBy(() -> controller.plan(authorization, UUID.randomUUID()))
            .isInstanceOf(ForbiddenException.class);
    }

    @Test
    void withoutAKeyNoAgentCanDeploy() {
        assertShut(controller(false, null), "Bearer ");
        assertShut(controller(false, "  "), "Bearer   ");
    }

    @Test
    void aProtectedLogosRefusesTheRightKey() {
        assertShut(controller(true, "the-agent-key"), "Bearer the-agent-key");
    }
}
