package de.tum.cit.aet.logos.logoswebservice.gateway;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.OffsetDateTime;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.Set;

import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;

/** Exact-decimal charging for the small set of text-only requests admitted under a provider cap. */
@Repository
class ProviderSpendPricing {
    private static final String INPUT = "billed_input_uncached";
    private static final String READ = "billed_input_cache_read";
    private static final String WRITE = "billed_input_cache_write";
    private static final String WRITE_1H = "billed_input_cache_write_1h";
    private static final String OUTPUT = "billed_output_text";
    private static final String REASONING = "billed_output_reasoning";
    private static final String REQUEST = "billed_requests";
    private static final Set<String> INPUT_RATES = Set.of(INPUT, READ, WRITE, WRITE_1H);
    private static final Set<String> OUTPUT_RATES = Set.of(OUTPUT, REASONING);
    private static final Set<String> USAGE = Set.of("prompt_tokens", "completion_tokens", "total_tokens",
        "prompt_cached_tokens", "prompt_cache_write_tokens", "prompt_cache_write_1h_tokens",
        "prompt_cache_miss_tokens", "completion_reasoning_tokens", "usage_shape_disjoint", "billed_requests");

    private final NamedParameterJdbcTemplate jdbc;

    ProviderSpendPricing(NamedParameterJdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    record Rate(long id, Integer model, Integer provider, String quantity, String unit, String tier,
                long contextTokens, OffsetDateTime validFrom, long value) {}

    List<Rate> rates(int provider, int model, OffsetDateTime pricedAt) {
        return jdbc.query("""
            SELECT tp.id, tp.model_id, tp.provider_id, tt.name, tp.unit, tp.service_tier,
                   tp.min_context_tokens, tp.valid_from, tp.price_per_k_unit
              FROM token_prices tp JOIN token_types tt ON tt.id = tp.type_id
             WHERE (tp.provider_id = :provider OR tp.provider_id IS NULL)
               AND (tp.model_id = :model OR tp.model_id IS NULL)
               AND tp.valid_from <= :pricedAt
            """, Map.of("provider", provider, "model", model, "pricedAt", pricedAt),
            (rs, row) -> new Rate(rs.getLong("id"), rs.getObject("model_id", Integer.class),
                rs.getObject("provider_id", Integer.class), rs.getString("name"), rs.getString("unit"),
                rs.getString("service_tier"), rs.getLong("min_context_tokens"),
                rs.getObject("valid_from", OffsetDateTime.class), rs.getLong("price_per_k_unit")));
    }

    static BigDecimal ceiling(List<Rate> rates, int inputTokens, int outputTokens) {
        if (resolve(rates, 0, "default", INPUT) == null || resolve(rates, 0, "default", OUTPUT) == null) {
            return null;
        }
        long input = max(rates, INPUT_RATES, "token");
        long output = max(rates, OUTPUT_RATES, "token");
        long request = max(rates, Set.of(REQUEST), "request");
        return product(inputTokens, input).add(product(outputTokens, output))
            .add(BigDecimal.valueOf(request)).movePointLeft(3).setScale(0, RoundingMode.CEILING);
    }

    static Long charge(List<Rate> rates, Map<String, Long> usage, String serviceTier, boolean anthropic) {
        if (usage == null || !usage.containsKey("prompt_tokens") || !usage.containsKey("completion_tokens")
                || usage.containsKey("usage_estimated")
                || usage.entrySet().stream().anyMatch(pair -> pair.getValue() == null || pair.getValue() < 0
                    || (pair.getValue() > 0 && !USAGE.contains(pair.getKey())))) {
            return null;
        }
        try {
            long prompt = usage.get("prompt_tokens");
            long completion = usage.get("completion_tokens");
            long cached = usage.getOrDefault("prompt_cached_tokens", 0L);
            long write = usage.getOrDefault("prompt_cache_write_tokens", 0L);
            long write1 = usage.getOrDefault("prompt_cache_write_1h_tokens", 0L);
            long reasoning = usage.getOrDefault("completion_reasoning_tokens", 0L);
            boolean disjoint = usage.containsKey("usage_shape_disjoint")
                || (anthropic && (usage.containsKey("prompt_cache_write_tokens")
                    || usage.containsKey("prompt_cache_write_1h_tokens") || cached > prompt));
            long uncached = disjoint ? prompt : usage.getOrDefault("prompt_cache_miss_tokens",
                Math.subtractExact(prompt, Math.addExact(cached, Math.addExact(write, write1))));
            long text = Math.subtractExact(completion, reasoning);
            if (text < 0 || uncached < 0 || (usage.containsKey("prompt_cache_miss_tokens") && (write > 0 || write1 > 0))) {
                return null;
            }
            long context = Math.addExact(uncached, Math.addExact(cached, Math.addExact(write, write1)));
            String tier = serviceTier == null ? "default" : serviceTier;
            Map<String, Long> quantities = Map.of(INPUT, uncached, READ, cached, WRITE, write,
                WRITE_1H, write1, OUTPUT, text, REASONING, reasoning, REQUEST, 1L);
            BigDecimal total = BigDecimal.ZERO;
            for (var entry : quantities.entrySet()) {
                if (entry.getValue() == 0) {
                    continue;
                }
                Long rate = resolve(rates, context, tier, entry.getKey());
                if (rate == null && !REQUEST.equals(entry.getKey())) {
                    return null;
                }
                if (rate != null) {
                    total = total.add(product(entry.getValue(), rate));
                }
            }
            if (total.signum() == 0 && prompt == 0 && completion == 0) {
                return null; // Match the normal ledger's no priced quantity result.
            }
            return total.movePointLeft(3).setScale(0, RoundingMode.HALF_UP).longValueExact();
        } catch (ArithmeticException e) {
            return null;
        }
    }

    private static Long resolve(List<Rate> rates, long context, String tier, String quantity) {
        List<String> order = switch (quantity) {
            case READ, WRITE -> List.of(quantity, INPUT);
            case WRITE_1H -> List.of(quantity, WRITE, INPUT);
            case REASONING -> List.of(quantity, OUTPUT);
            default -> List.of(quantity);
        };
        for (String candidate : order) {
            String unit = REQUEST.equals(candidate) ? "request" : "token";
            var rate = rates.stream().filter(r -> candidate.equals(r.quantity()) && unit.equals(r.unit())
                    && r.contextTokens() <= context && (tier.equals(r.tier()) || "default".equals(r.tier())))
                .max(Comparator.comparing((Rate r) -> r.model() != null)
                    .thenComparing(r -> r.provider() != null).thenComparing(r -> tier.equals(r.tier()))
                    .thenComparingLong(Rate::contextTokens).thenComparing(Rate::validFrom).thenComparingLong(Rate::id));
            if (rate.isPresent()) {
                return rate.get().value() < 0 ? null : rate.get().value();
            }
        }
        return null;
    }

    private static long max(List<Rate> rates, Set<String> quantities, String unit) {
        return rates.stream().filter(r -> quantities.contains(r.quantity()) && unit.equals(r.unit()))
            .mapToLong(Rate::value).filter(value -> value >= 0).max().orElse(0);
    }

    private static BigDecimal product(long quantity, long rate) {
        return BigDecimal.valueOf(quantity).multiply(BigDecimal.valueOf(rate));
    }
}
