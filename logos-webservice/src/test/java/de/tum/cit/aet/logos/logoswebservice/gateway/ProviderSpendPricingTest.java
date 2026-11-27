package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.Test;

class ProviderSpendPricingTest {
    private static final OffsetDateTime AT = OffsetDateTime.parse("2026-11-01T00:00:00Z");
    private static final String INPUT = "billed_input_uncached";
    private static final String OUTPUT = "billed_output_text";
    private static final String READ = "billed_input_cache_read";
    private static final String WRITE = "billed_input_cache_write";
    private static final String WRITE_LONG = "billed_input_cache_write_1h";
    private static final String REASON = "billed_output_reasoning";
    private static final String REQUEST = "billed_requests";

    @Test
    void reservesTheHighestApplicableKnownRatesButSettlesTheReportedQuantities() {
        var rates = new ArrayList<>(List.of(rate(INPUT, 1000), rate(OUTPUT, 2000), rate(READ, 200),
            rate(WRITE, 1300), rate(WRITE_LONG, 2100), rate(REASON, 4000), rate(REQUEST, 500)));
        rates.add(new ProviderSpendPricing.Rate(9L, 1, 1, REQUEST, "token", "default", 0, AT, 99_000));
        assertThat(ProviderSpendPricing.ceiling(rates, 20, 10)).isEqualByComparingTo("83");
        var actual = ProviderSpendPricing.quantities(Map.of("prompt_tokens", 10L, "completion_tokens", 4L,
            "prompt_cached_tokens", 2L, "prompt_cache_write_tokens", 3L, "prompt_cache_write_1h_tokens", 1L,
            "completion_reasoning_tokens", 2L), false);
        assertThat(actual.input()).isEqualTo(10);
        assertThat(actual.output()).isEqualTo(4);
        assertThat(ProviderSpendPricing.charge(rates, actual, null)).isEqualTo(23L);
        assertThat(ProviderSpendPricing.charge(rates, actual, "priority")).isEqualTo(23L);
    }

    @Test
    void usesExactTierAndContextOrFallsBackWithoutDoubleCountingCachedTokens() {
        var rates = List.of(rate(INPUT, 1000), rate(OUTPUT, 2000),
            new ProviderSpendPricing.Rate(2, null, null, INPUT, "token", "priority", 12, AT, 9000),
            new ProviderSpendPricing.Rate(3, 1, 1, INPUT, "token", "priority", 12, AT, 3000));
        var disjoint = ProviderSpendPricing.quantities(Map.of("prompt_tokens", 6L, "completion_tokens", 4L,
            "prompt_cached_tokens", 7L, "prompt_cache_write_1h_tokens", 1L), true);
        assertThat(disjoint.input()).isEqualTo(14);
        assertThat(ProviderSpendPricing.charge(rates, disjoint, "priority")).isEqualTo(50L);

        var hitMiss = ProviderSpendPricing.quantities(Map.of("prompt_tokens", 10L, "completion_tokens", 4L,
            "prompt_cached_tokens", 2L, "prompt_cache_miss_tokens", 8L, "completion_reasoning_tokens", 2L), false);
        assertThat(hitMiss.input()).isEqualTo(10);
        assertThat(ProviderSpendPricing.charge(rates, hitMiss, "priority")).isEqualTo(18L);
    }

    @Test
    void unknownIncompleteOrInconsistentReportsNeverReleaseAHold() {
        assertThat(ProviderSpendPricing.quantities(null, false)).isNull();
        assertThat(ProviderSpendPricing.quantities(Map.of("completion_tokens", 2L), false)).isNull();
        assertThat(ProviderSpendPricing.quantities(Map.of("prompt_tokens", 2L), false)).isNull();
        assertThat(ProviderSpendPricing.quantities(Map.of("prompt_tokens", 2L, "completion_tokens", 1L,
            "completion_reasoning_tokens", 2L), false)).isNull();
        assertThat(ProviderSpendPricing.quantities(Map.of("prompt_tokens", 2L, "completion_tokens", 2L,
            "prompt_cached_tokens", 3L), false)).isNull();
        assertThat(ProviderSpendPricing.quantities(Map.of("prompt_tokens", 4L, "completion_tokens", 2L,
            "prompt_cache_miss_tokens", 3L, "prompt_cache_write_tokens", 1L), false)).isNull();
        assertThat(ProviderSpendPricing.quantities(Map.of("prompt_tokens", 4L, "completion_tokens", 2L,
            "prompt_cache_miss_tokens", 3L, "prompt_cache_write_1h_tokens", 1L), false)).isNull();
        assertThat(ProviderSpendPricing.quantities(Map.of("prompt_tokens", 2L, "completion_tokens", 2L,
            "prompt_cached_tokens", -1L), false)).isNull();
        assertThat(ProviderSpendPricing.quantities(Map.of("prompt_tokens", 2L, "completion_tokens", 2L,
            "prompt_cached_tokens", Long.MAX_VALUE, "prompt_cache_write_tokens", 1L), false)).isNull();
        var missing = new HashMap<>(Map.of("prompt_tokens", 2L, "completion_tokens", 2L));
        missing.put("prompt_cached_tokens", null);
        assertThat(ProviderSpendPricing.quantities(missing, false)).isNull();
        assertThat(ProviderSpendPricing.hasUnboundedCharges(null)).isFalse();
        assertThat(ProviderSpendPricing.hasUnboundedCharges(Map.of("other_billable", 0L))).isFalse();
        assertThat(ProviderSpendPricing.hasUnboundedCharges(Map.of("other_billable", 2L, "usage_estimated", 1L))).isFalse();
        assertThat(ProviderSpendPricing.hasUnboundedCharges(Map.of("other_billable", 2L))).isTrue();
        assertThat(ProviderSpendPricing.hasUnboundedCharges(Map.of("estimated_unknown_quantity", 2L))).isTrue();
        assertThat(ProviderSpendPricing.hasUnboundedCharges(Map.of("estimated_prompt_tokens", 2L))).isFalse();
        assertThat(ProviderSpendPricing.quantities(Map.of("estimated_prompt_tokens", 2L, "completion_tokens", 0L),
            false)).isNull();
        assertThat(ProviderSpendPricing.quantities(Map.of("prompt_tokens", 0L, "completion_tokens", 0L,
            "total_tokens", 2L), false)).isNull();
    }

    @Test
    void distinguishesUnpricedFromAnExplicitZeroRateAndRefusesOverflow() {
        assertThat(ProviderSpendPricing.ceiling(List.of(rate(INPUT, 1000)), 10, 5)).isNull();
        assertThat(ProviderSpendPricing.ceiling(List.of(rate(OUTPUT, 1000)), 10, 5)).isNull();
        assertThat(ProviderSpendPricing.ceiling(List.of(rate(INPUT, -10), rate(OUTPUT, 1000)), 10, 5)).isNull();
        var actual = ProviderSpendPricing.quantities(Map.of("prompt_tokens", 2000L, "completion_tokens", 3L), false);
        assertThat(ProviderSpendPricing.charge(List.of(rate(INPUT, 1000)), actual, null)).isNull();
        assertThat(ProviderSpendPricing.charge(List.of(rate(INPUT, Long.MAX_VALUE), rate(OUTPUT, 0)), actual, null)).isNull();
        var zero = ProviderSpendPricing.quantities(Map.of("prompt_tokens", 0L, "completion_tokens", 0L,
            "total_tokens", 0L), false);
        assertThat(ProviderSpendPricing.charge(List.of(), zero, null)).isNull();
        assertThat(ProviderSpendPricing.charge(List.of(rate(REQUEST, 0)), zero, null)).isZero();
        assertThat(ProviderSpendPricing.charge(List.of(rate(REQUEST, 1000), rate(INPUT, 1000), rate(OUTPUT, 2000)),
            zero, null)).isEqualTo(1L);
        assertThat(ProviderSpendPricing.charge(List.of(rate(INPUT, 1000), rate(OUTPUT, 2000)), zero, null)).isZero();
        assertThat(ProviderSpendPricing.charge(List.of(rate(INPUT, 1000)), zero, null)).isNull();
        assertThat(ProviderSpendPricing.charge(List.of(rate(OUTPUT, 2000)), zero, null)).isNull();
        var contextualOutput = new ProviderSpendPricing.Rate(2, 1, 1, OUTPUT, "token", "priority", 1, AT, 2000);
        assertThat(ProviderSpendPricing.charge(List.of(rate(INPUT, 1000), contextualOutput), zero, "priority")).isNull();
    }

    private static ProviderSpendPricing.Rate rate(String quantity, long value) {
        return new ProviderSpendPricing.Rate(1, 1, 1, quantity, REQUEST.equals(quantity) ? "request" : "token",
            "default", 0, AT, value);
    }
}
