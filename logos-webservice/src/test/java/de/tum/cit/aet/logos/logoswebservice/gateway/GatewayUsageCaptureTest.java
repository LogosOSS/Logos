package de.tum.cit.aet.logos.logoswebservice.gateway;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import org.junit.jupiter.api.Test;

import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * Without a pricer the capture must forward bytes verbatim. With a pricer,
 * settled usage frames gain {@code cost} / {@code cost_currency}.
 */
class GatewayUsageCaptureTest {

    private final ObjectMapper mapper = new ObjectMapper();

    private static final String SSE = "text/event-stream; charset=utf-8";

    /** Feed the payload through in fixed-size pieces, as a network read would. */
    private Result stream(String contentType, String payload, int chunkSize) throws IOException {
        return stream(contentType, payload, chunkSize, null);
    }

    private Result stream(String contentType, String payload, int chunkSize, GatewayCostPricer pricer)
            throws IOException {
        ByteArrayOutputStream sink = new ByteArrayOutputStream();
        byte[] bytes = payload.getBytes(StandardCharsets.UTF_8);
        GatewayUsageCapture capture = new GatewayUsageCapture(
            sink, mapper, contentType, false, Instant::now, pricer);
        for (int i = 0; i < bytes.length; i += chunkSize) {
            capture.write(bytes, i, Math.min(chunkSize, bytes.length - i));
        }
        capture.close();
        return new Result(capture.usage(), sink.toString(StandardCharsets.UTF_8), capture.serviceTier());
    }

    private record Result(Map<String, Long> usage, String forwarded, String serviceTier) {
    }

    @Test
    void zeroProviderUsageAfterDisconnectDoesNotRetainAReservation() throws Exception {
        GatewayUsageCapture capture = new GatewayUsageCapture(
            java.io.OutputStream.nullOutputStream(), new ObjectMapper(), "text/event-stream");
        capture.write("data: {\"usage\":{\"prompt_tokens\":0,\"completion_tokens\":0}}\n\n"
            .getBytes(StandardCharsets.UTF_8));
        assertThat(capture.drainComplete()).isTrue();
        assertThat(capture.disconnectedUsage(new byte[0], false))
            .containsEntry("provider_delivered_nothing", 1L);
    }

    @Test
    void splitErrorFrameWaitsForReportedUsageBeforeStoppingDrain() throws Exception {
        GatewayUsageCapture capture = new GatewayUsageCapture(java.io.OutputStream.nullOutputStream(), mapper, SSE);
        capture.detachClient();
        capture.write("event: error\n".getBytes(StandardCharsets.UTF_8));
        assertThat(capture.drainComplete()).isFalse();
        capture.write(("data: {\"message\":\"provider failed\","
            + "\"usage\":{\"prompt_tokens\":37,\"completion_tokens\":19}}\n\n").getBytes(StandardCharsets.UTF_8));
        assertThat(capture.drainComplete()).isTrue();
        assertThat(capture.upstreamError()).isEqualTo("provider failed");
        assertThat(capture.disconnectedUsage(new byte[0], false))
            .containsExactlyInAnyOrderEntriesOf(Map.of("prompt_tokens", 37L, "completion_tokens", 19L));
    }

    @Test
    void failedResponseWithoutErrorDetailsNeverEstimatesUsage() throws Exception {
        GatewayUsageCapture capture = new GatewayUsageCapture(java.io.OutputStream.nullOutputStream(), mapper, SSE);
        capture.write("data: {\"type\":\"response.failed\",\"response\":{\"usage\":null}}\n\n"
            .getBytes(StandardCharsets.UTF_8));
        assertThat(capture.drainComplete()).isTrue();
        assertThat(capture.upstreamError()).contains("response.failed");
        assertThat(capture.disconnectedUsage("prompt".getBytes(StandardCharsets.UTF_8), true)).isEmpty();
    }

    @Test
    void jsonResponse_isForwardedVerbatimAndPriced() throws IOException {
        String body = """
            {"id":"chatcmpl-1","choices":[{"message":{"content":"hello"}}],
             "usage":{"prompt_tokens":120,"completion_tokens":30}}""";

        Result result = stream("application/json", body, 8);

        assertThat(result.forwarded()).isEqualTo(body);
        assertThat(result.usage()).containsExactlyInAnyOrderEntriesOf(Map.of(
            "prompt_tokens", 120L, "completion_tokens", 30L));
    }

    @Test
    void sseStream_readsTheTerminalUsageChunk() throws IOException {
        String body = """
            data: {"choices":[{"delta":{"content":"he"}}]}

            data: {"choices":[{"delta":{"content":"llo"}}]}

            data: {"choices":[],"usage":{"prompt_tokens":42,"completion_tokens":8}}

            data: [DONE]

            """;

        Result result = stream(SSE, body, 7);

        assertThat(result.forwarded()).isEqualTo(body);
        assertThat(result.usage()).containsExactlyInAnyOrderEntriesOf(Map.of(
            "prompt_tokens", 42L, "completion_tokens", 8L));
    }

    @Test
    void sseUsageSplitAcrossChunkBoundaries_isStillRead() throws IOException {
        // Network chunk boundaries do not line up with SSE event boundaries;
        // a per-chunk parse would lose the usage event entirely.
        String body = "data: {\"choices\":[],\"usage\":{\"prompt_tokens\":7,\"completion_tokens\":3}}\n\n";

        assertThat(stream(SSE, body, 1).usage())
            .containsExactlyInAnyOrderEntriesOf(Map.of("prompt_tokens", 7L, "completion_tokens", 3L));
    }

    @Test
    void sseWithCrLfAndNoTrailingNewline_isStillRead() throws IOException {
        String body = "data: {\"usage\":{\"prompt_tokens\":5}}\r\n\r\n"
            + "data: {\"usage\":{\"prompt_tokens\":5,\"completion_tokens\":2}}";

        Result result = stream(SSE, body, 16);

        assertThat(result.forwarded()).isEqualTo(body);
        assertThat(result.usage())
            .containsExactlyInAnyOrderEntriesOf(Map.of("prompt_tokens", 5L, "completion_tokens", 2L));
    }

    @Test
    void responsesApiStream_takesUsageFromTheCompletedEvent() throws IOException {
        String body = """
            event: response.output_text.delta
            data: {"type":"response.output_text.delta","delta":"hi"}

            event: response.completed
            data: {"type":"response.completed","response":{"usage":{"input_tokens":64,"output_tokens":12}}}

            """;

        assertThat(stream(SSE, body, 13).usage())
            .containsExactlyInAnyOrderEntriesOf(Map.of("prompt_tokens", 64L, "completion_tokens", 12L));
    }

    @Test
    void streamWithoutUsage_forwardsEverythingAndReportsNone() throws IOException {
        String body = """
            data: {"choices":[{"delta":{"content":"hello"}}]}

            data: [DONE]

            """;

        Result result = stream(SSE, body, 9);

        assertThat(result.forwarded()).isEqualTo(body);
        assertThat(result.usage()).isEmpty();
    }

    @Test
    void jsonBodyPastTheCaptureCap_isForwardedWholeButNotParsed() throws IOException {
        // The cap bounds the mirror, never the response: the client still gets
        // every byte, the request simply falls back to the flat reservation.
        String filler = "x".repeat(GatewayUsageCapture.JSON_CAPTURE_LIMIT_BYTES + 1024);
        String body = "{\"pad\":\"" + filler + "\",\"usage\":{\"prompt_tokens\":10}}";

        Result result = stream("application/json", body, 64 * 1024);

        assertThat(result.forwarded()).isEqualTo(body);
        assertThat(result.usage()).isEmpty();
    }

    @Test
    void oversizedSseEvent_isSkippedWithoutLosingLaterUsage() throws IOException {
        String huge = "data: {\"pad\":\"" + "y".repeat(GatewayUsageCapture.SSE_LINE_LIMIT_BYTES) + "\"}\n";
        String body = huge + "data: {\"usage\":{\"prompt_tokens\":11,\"completion_tokens\":4}}\n\n";

        Result result = stream(SSE, body, 32 * 1024);

        assertThat(result.forwarded()).isEqualTo(body);
        assertThat(result.usage())
            .containsExactlyInAnyOrderEntriesOf(Map.of("prompt_tokens", 11L, "completion_tokens", 4L));
    }

    @Test
    void oversizedSseEventWithPricer_passesThroughInChunksAndLaterUsageIsStillEnriched() throws IOException {
        String huge = "data: {\"pad\":\"" + "y".repeat(GatewayUsageCapture.SSE_LINE_LIMIT_BYTES) + "\"}\n";
        String usageLine = "data: {\"usage\":{\"prompt_tokens\":11,\"completion_tokens\":4}}\n\n";

        Result result = stream(SSE, huge + usageLine, 8 * 1024, (usage, tier) -> java.util.OptionalDouble.of(0.25));

        // The oversized line is forwarded unchanged, in pieces, without being parsed.
        assertThat(result.forwarded()).startsWith(huge);
        assertThat(result.usage())
            .containsExactlyInAnyOrderEntriesOf(Map.of("prompt_tokens", 11L, "completion_tokens", 4L));
        // The later usage frame is still complete and carries the price.
        assertThat(result.forwarded().substring(huge.length())).contains("\"prompt_tokens\":11").contains("0.25");
    }

    @Test
    void oversizedSseEventWithPricer_inOneWriteEndsAtItsNewline() throws IOException {
        String huge = "data: {\"pad\":\"" + "z".repeat(GatewayUsageCapture.SSE_LINE_LIMIT_BYTES + 10) + "\"}\n";
        String usageLine = "data: {\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":2}}\n\n";
        String body = huge + usageLine;

        Result result = stream(SSE, body, body.length(), (usage, tier) -> java.util.OptionalDouble.of(0.5));

        assertThat(result.forwarded()).startsWith(huge);
        assertThat(result.usage())
            .containsExactlyInAnyOrderEntriesOf(Map.of("prompt_tokens", 3L, "completion_tokens", 2L));
        assertThat(result.forwarded().substring(huge.length())).contains("0.5");
    }

    @Test
    void singleByteWritesAreCapturedToo() throws IOException {
        ByteArrayOutputStream sink = new ByteArrayOutputStream();
        String body = "{\"usage\":{\"prompt_tokens\":3}}";
        GatewayUsageCapture capture = new GatewayUsageCapture(sink, mapper, "application/json");
        for (byte b : body.getBytes(StandardCharsets.UTF_8)) {
            capture.write(b);
        }
        capture.close();

        assertThat(sink.toString(StandardCharsets.UTF_8)).isEqualTo(body);
        assertThat(capture.usage()).containsExactlyInAnyOrderEntriesOf(Map.of("prompt_tokens", 3L));
    }

    @Test
    void jsonResponse_readsTheServiceTierAlongsideUsage() throws IOException {
        String body = """
            {"usage":{"prompt_tokens":120,"completion_tokens":30},"service_tier":"Flex"}""";

        Result result = stream("application/json", body, 8);

        assertThat(result.serviceTier()).isEqualTo("flex");
        assertThat(result.usage()).containsEntry("prompt_tokens", 120L);
    }

    @Test
    void responsesStream_readsTheTierFromTheCompletedEvent() throws IOException {
        // SSE data fields are single-line; the whole event JSON rides on one
        // data: line.
        String body = """
            event: response.completed
            data: {"type":"response.completed","response":{"service_tier":"flex","usage":{"input_tokens":64,"output_tokens":12}}}

            """;

        Result result = stream(SSE, body, 13);

        assertThat(result.serviceTier()).isEqualTo("flex");
        assertThat(result.usage()).containsEntry("prompt_tokens", 64L);
    }

    @Test
    void responseWithoutTier_reportsNone() throws IOException {
        String body = "{\"usage\":{\"prompt_tokens\":5}}";

        Result result = stream("application/json", body, 8);

        assertThat(result.serviceTier()).isNull();
        assertThat(result.usage()).containsEntry("prompt_tokens", 5L);
    }

    @Test
    void contentTypeDecidesTheParseMode() {
        assertThat(GatewayUsageCapture.isEventStream("text/event-stream")).isTrue();
        assertThat(GatewayUsageCapture.isEventStream("TEXT/EVENT-STREAM; charset=utf-8")).isTrue();
        assertThat(GatewayUsageCapture.isEventStream("application/json")).isFalse();
        assertThat(GatewayUsageCapture.isEventStream(null)).isFalse();
    }

    /** Writes each chunk on its own, one tick of the clock apart. */
    private GatewayUsageCapture streamTicking(String contentType, String... chunks) throws IOException {
        Instant start = Instant.parse("2026-01-01T00:00:00Z");
        AtomicInteger tick = new AtomicInteger();
        GatewayUsageCapture capture = new GatewayUsageCapture(new ByteArrayOutputStream(), mapper, contentType,
            false, () -> start.plusSeconds(tick.getAndIncrement()));
        for (String chunk : chunks) {
            byte[] bytes = chunk.getBytes(StandardCharsets.UTF_8);
            capture.write(bytes, 0, bytes.length);
        }
        capture.close();
        return capture;
    }

    @Test
    void sseStream_firstTokenIsTheFirstEventWithContentNotTheFirstByte() throws IOException {
        GatewayUsageCapture capture = streamTicking(SSE,
            ": keep-alive\n\n",
            "data: {\"choices\":[{\"delta\":{\"role\":\"assistant\",\"content\":\"\"}}]}\n\n",
            "data: {\"choices\":[{\"delta\":{\"content\":\"hel\"}}]}\n\n",
            "data: {\"choices\":[{\"delta\":{\"content\":\"lo\"}}]}\n\n");

        assertThat(capture.firstByteAt()).isEqualTo(Instant.parse("2026-01-01T00:00:00Z"));
        assertThat(capture.firstTokenAt()).isEqualTo(Instant.parse("2026-01-01T00:00:02Z"));
    }

    @Test
    void sseStream_withoutRecognisedContent_fallsBackToTheFirstByte() throws IOException {
        GatewayUsageCapture capture = streamTicking(SSE,
            "data: {\"something\":\"else\"}\n\n",
            "data: {\"something\":\"more\"}\n\n");

        assertThat(capture.firstTokenAt()).isEqualTo(Instant.parse("2026-01-01T00:00:00Z"));
    }

    @Test
    void jsonResponse_firstTokenIsTheStartOfTheBody() throws IOException {
        GatewayUsageCapture capture = streamTicking("application/json",
            "{\"choices\":[{\"message\":", "{\"content\":\"hi\"}}]}");

        assertThat(capture.firstTokenAt()).isEqualTo(capture.firstByteAt())
            .isEqualTo(Instant.parse("2026-01-01T00:00:00Z"));
    }

    @Test
    void emptyResponse_hasNoFirstToken() throws IOException {
        assertThat(streamTicking(SSE).firstTokenAt()).isNull();
    }

    @Test
    void contentEventsAreRecognisedAcrossTheStreamFormats() throws IOException {
        assertThat(carries("{\"choices\":[{\"delta\":{\"content\":\"a\"}}]}")).isTrue();
        assertThat(carries("{\"choices\":[{\"delta\":{\"reasoning_content\":\"a\"}}]}")).isTrue();
        assertThat(carries("{\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0}]}}]}")).isTrue();
        assertThat(carries("{\"choices\":[{\"text\":\"a\"}]}")).isTrue();
        assertThat(carries("{\"type\":\"response.output_text.delta\",\"delta\":\"a\"}")).isTrue();
        assertThat(carries(
            "{\"type\":\"content_block_delta\",\"delta\":{\"type\":\"text_delta\",\"text\":\"a\"}}"))
            .isTrue();

        assertThat(carries("{\"choices\":[{\"delta\":{\"role\":\"assistant\"}}]}")).isFalse();
        assertThat(carries("{\"choices\":[],\"usage\":{\"prompt_tokens\":1}}")).isFalse();
        assertThat(carries("{\"type\":\"response.created\"}")).isFalse();
        assertThat(carries("{\"type\":\"message_start\"}")).isFalse();
    }

    private boolean carries(String event) throws IOException {
        return GatewayUsageCapture.carriesContent(mapper.readTree(event));
    }

    @Test
    void jsonResponse_withPricer_injectsCostAndCurrency() throws IOException {
        String body = """
            {"id":"chatcmpl-1","choices":[{"message":{"content":"hello"}}],\
            "usage":{"prompt_tokens":120,"completion_tokens":30}}""";

        Result result = stream("application/json", body, 8,
            (usage, tier) -> java.util.OptionalDouble.of(0.00012345));

        assertThat(result.usage()).containsEntry("prompt_tokens", 120L);
        var root = mapper.readTree(result.forwarded());
        assertThat(root.path("usage").path("cost").asDouble()).isEqualTo(0.00012345);
        assertThat(root.path("usage").path("cost_currency").asText()).isEqualTo("USD");
        assertThat(root.path("choices").path(0).path("message").path("content").asText()).isEqualTo("hello");
    }

    @Test
    void jsonResponse_whenPricerReturnsEmpty_forwardsWithoutCost() throws IOException {
        String body = "{\"usage\":{\"prompt_tokens\":5,\"completion_tokens\":1}}";

        Result result = stream("application/json", body, 4,
            (usage, tier) -> java.util.OptionalDouble.empty());

        assertThat(result.forwarded()).isEqualTo(body);
        assertThat(mapper.readTree(result.forwarded()).path("usage").has("cost")).isFalse();
    }

    @Test
    void sseStream_withPricer_enrichesOnlySettledUsageChunk() throws IOException {
        String body = """
            data: {"choices":[{"delta":{"content":"hi"}}]}

            data: {"choices":[],"usage":{"prompt_tokens":42,"completion_tokens":8}}

            data: [DONE]

            """;

        Result result = stream(SSE, body, 7,
            (usage, tier) -> java.util.OptionalDouble.of(0.0042));

        assertThat(result.forwarded()).contains("\"cost\":0.0042");
        assertThat(result.forwarded()).contains("\"cost_currency\":\"USD\"");
        assertThat(result.forwarded()).contains("data: {\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}");
        assertThat(result.forwarded()).contains("data: [DONE]");
        // Content deltas must not pick up a cost line.
        assertThat(result.forwarded()).doesNotContain("\"content\":\"hi\",\"cost\"");
    }

    @Test
    void sseStream_outputOnlyUsage_isNotPriced() throws IOException {
        String body = "data: {\"usage\":{\"completion_tokens\":9}}\n\n";

        Result result = stream(SSE, body, 3,
            (usage, tier) -> java.util.OptionalDouble.of(1.0));

        assertThat(GatewayUsageCapture.usageIsSettled(mapper.readTree("{\"completion_tokens\":9}"))).isFalse();
        assertThat(result.forwarded()).doesNotContain("\"cost\"");
    }

    @Test
    void responsesApiStream_withPricer_enrichesNestedResponseUsage() throws IOException {
        String body = """
            event: response.completed
            data: {"type":"response.completed","response":{"usage":{"input_tokens":64,"output_tokens":12}}}

            """;

        Result result = stream(SSE, body, 13,
            (usage, tier) -> java.util.OptionalDouble.of(0.00083));

        var event = mapper.readTree(result.forwarded().lines()
            .filter(l -> l.startsWith("data:"))
            .map(l -> l.substring(5).strip())
            .findFirst()
            .orElseThrow());
        assertThat(event.path("response").path("usage").path("cost").asDouble()).isEqualTo(0.00083);
        assertThat(event.path("response").path("usage").path("cost_currency").asText()).isEqualTo("USD");
    }

    @Test
    void jsonBodyPastTheCaptureCap_withPricer_stillForwardsWholeBody() throws IOException {
        String filler = "x".repeat(GatewayUsageCapture.JSON_CAPTURE_LIMIT_BYTES + 1024);
        String body = "{\"pad\":\"" + filler + "\",\"usage\":{\"prompt_tokens\":10}}";

        Result result = stream("application/json", body, 64 * 1024,
            (usage, tier) -> java.util.OptionalDouble.of(9.0));

        assertThat(result.forwarded()).isEqualTo(body);
        assertThat(result.usage()).isEmpty();
        assertThat(result.forwarded()).doesNotContain("\"cost\"");
    }

    @Test
    void usageIsSettled_requiresAnInputSideSignal() {
        assertThat(GatewayUsageCapture.usageIsSettled(mapper.createObjectNode().put("prompt_tokens", 1)))
            .isTrue();
        assertThat(GatewayUsageCapture.usageIsSettled(mapper.createObjectNode().put("total_tokens", 1)))
            .isTrue();
        assertThat(GatewayUsageCapture.usageIsSettled(mapper.createObjectNode().put("completion_tokens", 1)))
            .isFalse();
        assertThat(GatewayUsageCapture.usageIsSettled(null)).isFalse();
    }

    @Test
    void detachClient_flushesHeldJsonWithCostBeforeDroppingTheDelegate() throws IOException {
        ByteArrayOutputStream sink = new ByteArrayOutputStream();
        String body = "{\"usage\":{\"prompt_tokens\":5,\"completion_tokens\":1}}";
        GatewayUsageCapture capture = new GatewayUsageCapture(
            sink, mapper, "application/json", false, Instant::now,
            (usage, tier) -> java.util.OptionalDouble.of(0.001));
        capture.write(body.getBytes(StandardCharsets.UTF_8));
        assertThat(sink.size()).isZero();

        capture.detachClient();

        var usage = mapper.readTree(sink.toByteArray()).path("usage");
        assertThat(usage.path("cost").asDouble()).isEqualTo(0.001);
        assertThat(usage.path("cost_currency").asText()).isEqualTo("USD");
        // A second close must not write again onto the null sink.
        capture.close();
        assertThat(capture.usage()).containsEntry("prompt_tokens", 5L);
    }

    @Test
    void pricerThatThrows_leavesTheBodyWithoutCost() throws IOException {
        String body = "{\"usage\":{\"prompt_tokens\":5,\"completion_tokens\":1}}";
        Result result = stream("application/json", body, 8, (usage, tier) -> {
            throw new IllegalStateException("pricing unavailable");
        });
        assertThat(result.forwarded()).isEqualTo(body);
        assertThat(mapper.readTree(result.forwarded()).path("usage").has("cost")).isFalse();
    }

    @Test
    void audioMpeg_withPricer_forwardsBytesBeforeClose() throws IOException {
        ByteArrayOutputStream sink = new ByteArrayOutputStream();
        byte[] audio = new byte[] {1, 2, 3, 4, 5};
        GatewayUsageCapture capture = new GatewayUsageCapture(
            sink, mapper, "audio/mpeg", false, Instant::now,
            (usage, tier) -> java.util.OptionalDouble.of(9.0));
        capture.write(audio);
        assertThat(sink.toByteArray()).isEqualTo(audio);
        capture.close();
        assertThat(sink.toByteArray()).isEqualTo(audio);
        assertThat(capture.usage()).isEmpty();
    }

    @Test
    void detachClient_preservesPartialSseLineForDrain() throws IOException {
        ByteArrayOutputStream sink = new ByteArrayOutputStream();
        GatewayUsageCapture capture = new GatewayUsageCapture(
            sink, mapper, SSE, false, Instant::now,
            (usage, tier) -> java.util.OptionalDouble.of(0.001));
        capture.write("data: {\"usage\":{\"prompt_tok".getBytes(StandardCharsets.UTF_8));
        capture.detachClient();
        capture.write("ens\":42,\"completion_tokens\":8}}\n\n".getBytes(StandardCharsets.UTF_8));
        capture.close();

        assertThat(capture.usage())
            .containsEntry("prompt_tokens", 42L)
            .containsEntry("completion_tokens", 8L);
    }

    @Test
    void clientDisconnectMidChunk_stillCapturesTrailingUsage() throws IOException {
        OutputStream failing = new OutputStream() {
            @Override
            public void write(int b) throws IOException {
                throw new IOException("client went away");
            }

            @Override
            public void write(byte[] b, int off, int len) throws IOException {
                throw new IOException("client went away");
            }
        };
        GatewayUsageCapture capture = new GatewayUsageCapture(
            failing, mapper, SSE, false, Instant::now,
            (usage, tier) -> java.util.OptionalDouble.of(0.001));
        byte[] chunk = """
            data: {"choices":[{"delta":{"content":"hi"}}]}

            data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}

            """.getBytes(StandardCharsets.UTF_8);

        org.assertj.core.api.Assertions.assertThatThrownBy(() -> capture.write(chunk))
            .isInstanceOf(GatewayUsageCapture.ClientDisconnectedException.class);
        capture.close();

        assertThat(capture.usage())
            .containsEntry("prompt_tokens", 7L)
            .containsEntry("completion_tokens", 3L);
    }

    @Test
    void contentTypeDecidesJsonVsBinaryEnrichment() {
        assertThat(GatewayUsageCapture.isJsonBody("application/json")).isTrue();
        assertThat(GatewayUsageCapture.isJsonBody("application/json; charset=utf-8")).isTrue();
        assertThat(GatewayUsageCapture.isJsonBody("application/problem+json")).isTrue();
        assertThat(GatewayUsageCapture.isJsonBody("audio/mpeg")).isFalse();
        assertThat(GatewayUsageCapture.isJsonBody("text/event-stream")).isFalse();
        assertThat(GatewayUsageCapture.isJsonBody(null)).isFalse();
    }
}
