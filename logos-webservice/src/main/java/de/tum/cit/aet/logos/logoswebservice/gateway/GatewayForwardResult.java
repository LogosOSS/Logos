package de.tum.cit.aet.logos.logoswebservice.gateway;

import java.util.Arrays;
import java.util.Map;
import java.util.Objects;

/**
 * What a completed cloud forward reported about itself.
 *
 * @param usage        canonical token counts, empty when the response reported none
 * @param responseBody the response body to store, or {@code null} when the key
 *                     does not log payloads, the body was not retained, or it
 *                     streamed (a stream has no single body to store)
 * @param serviceTier  the tier the response reported (e.g. {@code flex}),
 *                     lowercased, or {@code null} when it reported none
 * @param timings      when the upstream was called, answered first and finished
 */
public record GatewayForwardResult(Map<String, Long> usage, byte[] responseBody, String serviceTier,
                                   GatewayForwardTimings timings) {

    public GatewayForwardResult(Map<String, Long> usage, byte[] responseBody, String serviceTier) {
        this(usage, responseBody, serviceTier, GatewayForwardTimings.NONE);
    }

    public GatewayForwardResult {
        if (timings == null) {
            timings = GatewayForwardTimings.NONE;
        }
    }

    static GatewayForwardResult none() {
        return new GatewayForwardResult(Map.of(), null, null);
    }

    @Override
    public boolean equals(Object other) {
        return other instanceof GatewayForwardResult result && Objects.equals(usage, result.usage)
            && Arrays.equals(responseBody, result.responseBody) && Objects.equals(serviceTier, result.serviceTier)
            && Objects.equals(timings, result.timings);
    }

    @Override
    public int hashCode() {
        return 31 * Objects.hash(usage, serviceTier, timings) + Arrays.hashCode(responseBody);
    }

    @Override
    public String toString() {
        return "GatewayForwardResult[usage=" + usage + ", responseBody="
            + (responseBody == null ? "null" : responseBody.length + " bytes") + ", serviceTier=" + serviceTier
            + ", timings=" + timings + "]";
    }
}
