package de.tum.cit.aet.logos.logoswebservice.configuration;

import java.util.List;
import java.util.Map;
import java.util.Optional;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import de.tum.cit.aet.logos.logoswebservice.auth.AuthContext;
import de.tum.cit.aet.logos.logoswebservice.configuration.controller.PermissionController;
import de.tum.cit.aet.logos.logoswebservice.configuration.dto.SetModelPermissionsRequestDTO;
import de.tum.cit.aet.logos.logoswebservice.configuration.dto.SetProviderPermissionsRequestDTO;
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.ApiKeyModelPermissionRepository;
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.ApiKeyProviderPermissionRepository;
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.TeamModelPermissionRepository;
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.TeamProviderPermissionRepository;
import de.tum.cit.aet.logos.logoswebservice.configuration.service.PermissionService;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.Role;
import de.tum.cit.aet.logos.logoswebservice.identity.service.ApiKeyAdminService;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

/**
 * Team permissions are the default for API keys without custom permissions,
 * not a ceiling for keys with custom permissions.
 */
@ExtendWith(MockitoExtension.class)
class PermissionBoundaryUnitTest {

    private static final int KEY_ID = 3001;
    private static final int TEAM_ID = 2001;
    private static final int USER_ID = 1002;

    @Mock ApiKeyModelPermissionRepository apiKeyModelRepo;
    @Mock ApiKeyProviderPermissionRepository apiKeyProviderRepo;
    @Mock TeamModelPermissionRepository teamModelRepo;
    @Mock TeamProviderPermissionRepository teamProviderRepo;
    @Mock ApiKeyAdminService apiKeyAdminService;

    private PermissionService permissionService;
    private PermissionController controller;
    private AuthContext appAdmin;

    @BeforeEach
    void setUp() {
        permissionService = new PermissionService(
            apiKeyModelRepo, apiKeyProviderRepo, teamModelRepo, teamProviderRepo);
        controller = new PermissionController(permissionService, apiKeyAdminService);
        appAdmin = new AuthContext(USER_ID, Role.Names.APP_ADMIN);
    }

    private void keyBelongsToOwnedTeam() {
        when(apiKeyAdminService.getKeyById(KEY_ID))
            .thenReturn(Optional.of(Map.of("team_id", TEAM_ID)));
        when(apiKeyAdminService.isTeamOwner(TEAM_ID, USER_ID)).thenReturn(true);
    }

    @Test
    void teamOwnerCanGrantAProviderThatTheTeamDoesNotHave() {
        keyBelongsToOwnedTeam();

        assertEquals(200, controller.setApiKeyProviderPermissions(
            KEY_ID, new SetProviderPermissionsRequestDTO(List.of(6001)), appAdmin).getStatusCode().value());

        verify(apiKeyProviderRepo).deleteById_ApiKeyId(KEY_ID);
        verify(apiKeyProviderRepo).saveAll(anyList());
        verify(apiKeyModelRepo).deleteCascadeForApiKey(KEY_ID);
        verifyNoInteractions(teamProviderRepo, teamModelRepo);
    }

    @Test
    void teamOwnerCanGrantAModelThatTheTeamDoesNotHave() {
        keyBelongsToOwnedTeam();

        assertEquals(200, controller.setApiKeyModelPermissions(
            KEY_ID, new SetModelPermissionsRequestDTO(List.of(5001)), appAdmin).getStatusCode().value());

        verify(apiKeyModelRepo).deleteById_ApiKeyId(KEY_ID);
        verify(apiKeyModelRepo).saveAll(anyList());
        verifyNoInteractions(teamProviderRepo, teamModelRepo);
    }

    @Test
    void changingTeamModelPermissionsLeavesApiKeyGrantsAlone() {
        permissionService.setTeamModelPermissions(TEAM_ID, List.of());

        verifyNoInteractions(apiKeyModelRepo, apiKeyProviderRepo);
    }

    @Test
    void changingTeamProviderPermissionsLeavesApiKeyGrantsAlone() {
        permissionService.setTeamProviderPermissions(TEAM_ID, List.of());
        permissionService.removeTeamProviderPermission(TEAM_ID, 6001);

        verifyNoInteractions(apiKeyModelRepo, apiKeyProviderRepo);
    }
}
