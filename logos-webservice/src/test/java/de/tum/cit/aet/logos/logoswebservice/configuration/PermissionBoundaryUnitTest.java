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
import de.tum.cit.aet.logos.logoswebservice.common.ForbiddenException;
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

import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

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

    private PermissionController controller;
    private AuthContext appAdmin;

    @BeforeEach
    void setUp() {
        PermissionService permissionService = new PermissionService(
            apiKeyModelRepo, apiKeyProviderRepo, teamModelRepo, teamProviderRepo);
        controller = new PermissionController(permissionService, apiKeyAdminService);
        appAdmin = new AuthContext(USER_ID, Role.Names.APP_ADMIN);

        when(apiKeyAdminService.getKeyById(KEY_ID))
            .thenReturn(Optional.of(Map.of("team_id", TEAM_ID)));
        when(apiKeyAdminService.isTeamOwner(TEAM_ID, USER_ID)).thenReturn(true);
    }

    @Test
    void teamOwnerCannotGrantAProviderThatTheTeamDoesNotHave() {
        when(teamProviderRepo.findById_TeamId(TEAM_ID)).thenReturn(List.of());

        assertThrows(ForbiddenException.class, () -> controller.setApiKeyProviderPermissions(
            KEY_ID, new SetProviderPermissionsRequestDTO(List.of(6001)), appAdmin));

        verify(apiKeyProviderRepo, never()).deleteById_ApiKeyId(KEY_ID);
    }

    @Test
    void teamOwnerCannotGrantAModelThatTheTeamDoesNotHave() {
        when(teamModelRepo.findById_TeamId(TEAM_ID)).thenReturn(List.of());

        assertThrows(ForbiddenException.class, () -> controller.setApiKeyModelPermissions(
            KEY_ID, new SetModelPermissionsRequestDTO(List.of(5001)), appAdmin));

        verify(apiKeyModelRepo, never()).deleteById_ApiKeyId(KEY_ID);
    }
}
