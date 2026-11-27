package de.tum.cit.aet.logos.logoswebservice.configuration.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyMap;
import static org.mockito.ArgumentMatchers.contains;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.inOrder;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.util.Map;

import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.mockito.ArgumentCaptor;
import org.mockito.ArgumentMatchers;
import org.springframework.jdbc.core.ResultSetExtractor;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.jdbc.core.namedparam.SqlParameterSource;

import de.tum.cit.aet.logos.logoswebservice.audit.AuditLogService;
import de.tum.cit.aet.logos.logoswebservice.gateway.ProviderSpendCapService;

class ProviderSpendPolicyServiceTest {

    @ParameterizedTest
    @CsvSource({
        "2026-12-01T13:59:59+14:00,2026-11-01",
        "2026-12-01T14:00:00+14:00,2026-12-01"
    })
    void immediateEditSelectsTheUtcMonthWithThePostLockDatabaseTime(String clock, String effectiveMonth) {
        var jdbc = mock(NamedParameterJdbcTemplate.class);
        var cap = mock(ProviderSpendCapService.class);
        when(cap.recordedCosts(7, LocalDate.parse(effectiveMonth))).thenReturn(BigDecimal.ZERO);
        var service = new ProviderSpendPolicyService(jdbc, mock(AuditLogService.class), cap);
        when(jdbc.query(contains("FOR UPDATE"), anyMap(),
            ArgumentMatchers.<ResultSetExtractor<Boolean>>any())).thenReturn(true);
        when(jdbc.queryForObject(eq("SELECT clock_timestamp()"), eq(Map.of()), eq(OffsetDateTime.class)))
            .thenReturn(OffsetDateTime.parse(clock));

        service.update(7, new ProviderSpendPolicyService.Change(true, new BigDecimal("42")));

        var order = inOrder(jdbc);
        order.verify(jdbc).query(contains("FOR UPDATE"), anyMap(), ArgumentMatchers.<ResultSetExtractor<Boolean>>any());
        order.verify(jdbc).queryForObject("SELECT clock_timestamp()", Map.of(), OffsetDateTime.class);
        var writes = ArgumentCaptor.forClass(SqlParameterSource.class);
        verify(jdbc).update(contains("INSERT INTO provider_spend_policies"), writes.capture());
        assertThat(writes.getValue().getValue("month")).isEqualTo(LocalDate.parse(effectiveMonth));
        assertThat(writes.getValue().getValue("cap")).isEqualTo(4_200_000_000L);
    }
}
