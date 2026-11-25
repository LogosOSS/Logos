package de.tum.cit.aet.logos.logoswebservice.identity.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.util.Map;
import java.util.Optional;

import jakarta.persistence.EntityManager;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.NullSource;
import org.junit.jupiter.params.provider.ValueSource;
import org.mockito.ArgumentCaptor;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;

import de.tum.cit.aet.logos.logoswebservice.audit.AuditLogService;
import de.tum.cit.aet.logos.logoswebservice.identity.dto.CreateAppKeyRequestDTO;
import de.tum.cit.aet.logos.logoswebservice.identity.dto.UpdateApiKeyRequestDTO;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.ApiKey;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.LogLevel;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.ApiKeyRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.TeamMemberRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.TeamRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.UserRepository;

/** Key creation and logging changes record settings without exposing key material. */
class ApiKeyAdminServiceAuditTest {

    private NamedParameterJdbcTemplate jdbc;
    private ApiKeyRepository keys;
    private ApiKeyAdminService service;
    private ApiKey key;

    @BeforeEach
    void setUp() {
        jdbc = mock(NamedParameterJdbcTemplate.class);
        keys = mock(ApiKeyRepository.class);
        AuditLogService audit = new AuditLogService(jdbc, mock(UserRepository.class));
        service = new ApiKeyAdminService(keys, mock(TeamRepository.class), mock(TeamMemberRepository.class), audit,
            mock(EntityManager.class));
        key = new ApiKey();
        key.setTeamId(3);
        key.setLog(LogLevel.BILLING);
        key.setSettings("{\"cloud_rpm_limit\":10}");
        when(keys.findByIdForUpdate(5)).thenReturn(Optional.of(key));
    }

    @ParameterizedTest
    @NullSource
    @ValueSource(strings = {"FULL", "BILLING"})
    void keyCreationRecordsInitialLoggingLevelWithoutCredentials(String requestedLevel) {
        when(keys.save(any(ApiKey.class))).thenAnswer(invocation -> {
            ApiKey created = invocation.getArgument(0);
            org.springframework.test.util.ReflectionTestUtils.setField(created, "id", 51);
            return created;
        });
        service.createAppKey(3, new CreateAppKeyRequestDTO(
            "new key", "application", "test", requestedLevel, null, null, null));

        ArgumentCaptor<MapSqlParameterSource> params = ArgumentCaptor.forClass(MapSqlParameterSource.class);
        verify(jdbc).update(anyString(), params.capture());
        assertThat(params.getValue().getValue("action")).isEqualTo("api_key.created");
        assertThat(params.getValue().getValue("targetType")).isEqualTo("api_key");
        assertThat(params.getValue().getValue("targetId")).isEqualTo("51");
        assertThat(params.getValue().getValue("teamId")).isEqualTo(3);
        String expected = requestedLevel == null ? "BILLING" : requestedLevel;
        assertThat(params.getValue().getValue("after")).isEqualTo("{\"log\":\"" + expected + "\"}");
    }

    @Test
    void legacySetLogRecordsTheChange() {
        service.setLog(5, "FULL");

        ArgumentCaptor<MapSqlParameterSource> params = ArgumentCaptor.forClass(MapSqlParameterSource.class);
        verify(jdbc).update(anyString(), params.capture());
        assertThat(params.getValue().getValue("action")).isEqualTo("api_key.log_level_changed");
        assertThat(params.getValue().getValue("teamId")).isEqualTo(3);
        assertThat((String) params.getValue().getValue("before")).contains("BILLING");
        assertThat((String) params.getValue().getValue("after")).contains("FULL");
    }

    @Test
    void legacySetLogToTheSameLevelRecordsNothing() {
        service.setLog(5, "BILLING");

        verify(jdbc, never()).update(anyString(), any(MapSqlParameterSource.class));
    }

    @Test
    void resavingUnchangedLimitsRecordsNothing() {
        service.updateKey(5, new UpdateApiKeyRequestDTO(null, null, null, null, 10, null, null, null, null));

        verify(jdbc, never()).update(anyString(), any(MapSqlParameterSource.class));
        verify(keys).save(eq(key));
    }
}
