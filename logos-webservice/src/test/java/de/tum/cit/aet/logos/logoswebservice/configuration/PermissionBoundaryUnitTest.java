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
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.PermissionCatalogProjection;
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.ProviderRepository;
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
    @Mock ProviderRepository providerRepo;
    @Mock ApiKeyAdminService apiKeyAdminService;

    private PermissionService permissionService;
    private PermissionController controller;
    private AuthContext appAdmin;

    @BeforeEach
    void setUp() {
        permissionService = new PermissionService(
            apiKeyModelRepo, apiKeyProviderRepo, teamModelRepo, teamProviderRepo, providerRepo);
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

        verify(apiKeyProviderRepo).lockApiKeyPermissions(PermissionService.apiKeyPermsLockKey(KEY_ID));
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

        verify(apiKeyProviderRepo).lockApiKeyPermissions(PermissionService.apiKeyPermsLockKey(KEY_ID));
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

    @Test
    void permissionCatalogGroupsModelsByProviderAndKeepsProvidersWithoutModels() {
        when(providerRepo.findPermissionCatalog()).thenReturn(List.of(
            row(6001, "cloud", 5001, "gpt-4"),
            row(6001, "cloud", 5002, "gpt-3.5"),
            row(6002, "empty", null, null)));

        List<Map<String, Object>> catalog = permissionService.getPermissionCatalog();

        assertEquals(List.of(
            Map.of("id", 6001, "name", "cloud", "models", List.of(
                Map.of("model_id", 5001, "model_name", "gpt-4"),
                Map.of("model_id", 5002, "model_name", "gpt-3.5"))),
            Map.of("id", 6002, "name", "empty", "models", List.of())), catalog);
    }

    private static PermissionCatalogProjection row(
            Integer providerId, String providerName, Integer modelId, String modelName) {
        return new PermissionCatalogProjection() {
            @Override public Integer getProviderId() { return providerId; }
            @Override public String getProviderName() { return providerName; }
            @Override public Integer getModelId() { return modelId; }
            @Override public String getModelName() { return modelName; }
        };
    }
}
