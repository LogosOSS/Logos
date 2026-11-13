package de.tum.cit.aet.logos.logoswebservice.gateway;

import java.util.Map;
import java.util.OptionalDouble;

/**
 * Prices settled token usage for a live cloud response body.
 *
 * <p>Implementations must be best-effort: never throw into the response path.
 * An empty result means the client response stays without a cost line.
 */
@FunctionalInterface
interface GatewayCostPricer {

    /**
     * @param usage       canonical token counts (e.g. {@code prompt_tokens})
     * @param serviceTier lowercased tier from the response, or {@code null}
     * @return USD cost, or empty when pricing is unknown
     */
    OptionalDouble priceUsd(Map<String, Long> usage, String serviceTier);
}
