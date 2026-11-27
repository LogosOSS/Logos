package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.anyMap;
import static org.mockito.ArgumentMatchers.contains;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.clearInvocations;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import java.math.BigDecimal;
import java.sql.ResultSet;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

import org.junit.jupiter.api.Test;
import org.mockito.ArgumentMatchers;
import org.springframework.dao.TransientDataAccessResourceException;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.web.server.ResponseStatusException;

class ProviderSpendCapServiceTest {
    private final NamedParameterJdbcTemplate jdbc = mock(NamedParameterJdbcTemplate.class);
    private final AtomicLong clock = new AtomicLong();
    private final ProviderSpendCapService service = new ProviderSpendCapService(jdbc, clock::get);

    @Test
    void warmsBothUncappedAndCappedWithoutAnyRequestTimeDatabaseWork() {
        configureClock("2026-11-27T22:00:00Z");
        configureLimits(Map.of(2, 100L, 3, 100L, 4, 0L));
        when(jdbc.queryForObject(contains("SUM"), ArgumentMatchers.<Map<String, ?>>argThat(params ->
                params != null && params.get("provider").equals(2)), eq(BigDecimal.class)))
            .thenReturn(BigDecimal.valueOf(99));
        when(jdbc.queryForObject(contains("SUM"), ArgumentMatchers.<Map<String, ?>>argThat(params ->
                params != null && params.get("provider").equals(3)), eq(BigDecimal.class)))
            .thenReturn(BigDecimal.valueOf(100));
        service.refresh();

        clearInvocations(jdbc);
        for (int i = 0; i < 10_000; i++) {
            service.check(1);
            service.check(2);
        }
        assertRejected(3, 402);
        assertRejected(4, 402);
        assertThat(service.status().blockedProviderIds()).containsExactlyInAnyOrder(3, 4);
        assertThat(service.status().validForMs()).isEqualTo(5000);
        verifyNoInteractions(jdbc);
    }

    @Test
    void capsFailClosedOnMissingTotalsButFreshPolicyOnlyFallbackAllowsUncapped() {
        configureClock("2026-11-27T22:00:00Z");
        configureLimits(Map.of(2, 100L));
        when(jdbc.queryForObject(contains("SUM"), anyMap(), eq(BigDecimal.class)))
            .thenThrow(new TransientDataAccessResourceException("synthetic read interruption"));

        assertRejected(1, 503);
        service.refresh();
        clearInvocations(jdbc);
        service.check(1);
        assertRejected(2, 503);
        assertThatThrownBy(service::status).isInstanceOf(ResponseStatusException.class).hasMessageContaining("503");
        clock.set(TimeUnit.SECONDS.toNanos(5));
        assertRejected(1, 503);
        verifyNoInteractions(jdbc);
    }

    @Test
    void utcRolloverUsesTheNextRefreshWithoutInterruptingUncappedCalls() {
        configureClock("2026-12-01T13:59:59+14:00");
        configureLimits(Map.of(1, 0L));
        service.refresh();
        assertRejected(1, 402);
        assertThat(service.status().validForMs()).isEqualTo(5000);
        clock.set(TimeUnit.SECONDS.toNanos(1));
        service.check(2);
        assertRejected(1, 402);

        configureClock("2026-12-01T14:00:00+14:00");
        configureLimits(Map.of());
        service.refresh();
        service.check(1);
        assertThat(service.status().blockedProviderIds()).isEmpty();
    }

    private void configureClock(String instant) {
        when(jdbc.queryForObject("SELECT statement_timestamp()", Map.of(), OffsetDateTime.class))
            .thenReturn(OffsetDateTime.parse(instant));
    }

    private void configureLimits(Map<Integer, Long> limits) {
        doAnswer(call -> {
            RowMapper<Object> mapper = call.getArgument(2);
            var result = new ArrayList<>();
            for (var entry : limits.entrySet()) {
                ResultSet rs = mock(ResultSet.class);
                when(rs.getInt(1)).thenReturn(entry.getKey());
                when(rs.getLong(2)).thenReturn(entry.getValue());
                result.add(mapper.mapRow(rs, 0));
            }
            return result;
        }).when(jdbc).query(contains("DISTINCT ON"), anyMap(), ArgumentMatchers.<RowMapper<Object>>any());
    }

    private void assertRejected(int provider, int status) {
        assertThatThrownBy(() -> service.check(provider)).isInstanceOfSatisfying(ResponseStatusException.class,
            error -> assertThat(error.getStatusCode().value()).isEqualTo(status));
    }
}
