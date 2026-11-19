package de.tum.cit.aet.logos.logoswebservice.gateway;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.Map;
import java.util.OptionalDouble;
import java.util.Set;
import java.util.function.Supplier;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;

/**
 * Sniffs the token usage and the service tier out of a cloud response while it
 * streams to the client.
 *
 * <p>Without a {@link GatewayCostPricer} the client's bytes are never delayed
 * or altered: every write passes straight through to the delegate and the copy
 * is what gets inspected. With a pricer, settled usage frames get
 * {@code cost} / {@code cost_currency} injected (JSON held until close; SSE
 * rewritten per completed line). Two shapes matter, and each is bounded so a
 * large response cannot grow the heap with the body it is forwarding:
 *
 * <ul>
 *   <li><b>SSE</b> — only the current line is buffered. Each {@code data:} line
 *       is parsed as it completes and the last usage seen wins, which is the
 *       terminal {@code stream_options.include_usage} chunk (Chat Completions)
 *       or the {@code response.completed} event (Responses API).</li>
 *   <li><b>JSON</b> — buffered up to {@link #JSON_CAPTURE_LIMIT_BYTES} and
 *       parsed once the stream closes. A body past the cap yields no usage
 *       rather than a truncated parse.</li>
 * </ul>
 *
 * <p>With {@code retainBody} the JSON buffer is kept after parsing so a key
 * configured for full request logging can store the response. It stays off
 * otherwise, so a body no one will read is dropped as soon as its usage is out.
 * When a pricer rewrites the body, the retained copy is the enriched one the
 * client received.
 */
final class GatewayUsageCapture extends OutputStream {

    /**
     * A chat completion carrying usage is far below this; the cap only exists
     * so an unexpectedly large non-streaming body (a long embeddings batch)
     * cannot be mirrored in full.
     */
    static final int JSON_CAPTURE_LIMIT_BYTES = 1024 * 1024;

    /** One SSE event that exceeds this is skipped rather than buffered. */
    static final int SSE_LINE_LIMIT_BYTES = 256 * 1024;

    private static final Set<String> SETTLED_USAGE_KEYS = Set.of(
        "prompt_tokens", "input_tokens", "inputTokens", "promptTokens", "total_tokens");

    private OutputStream delegate;
    private boolean finalUsage;
    private boolean terminalEvent;
    private String upstreamError;
    private String sseEventType = "";
    private long completionCharacters;
    private final ObjectMapper objectMapper;
    private final boolean sse;
    /** True for JSON responses that may carry a usage object to enrich. */
    private final boolean jsonBody;
    private final boolean retainBody;
    private final Supplier<Instant> clock;
    private final GatewayCostPricer pricer;

    private final ByteArrayOutputStream buffer = new ByteArrayOutputStream();
    private boolean bufferOverflowed;
    /** After a JSON capture overflow with a pricer, remaining bytes pass through. */
    private boolean jsonPassthrough;
    private Map<String, Long> usage = Map.of();
    private String serviceTier;
    private byte[] capturedBody;
    private Instant firstByteAt;
    private Instant firstTokenAt;
    /** When the chunk being written arrived, before it was passed to the client. */
    private Instant chunkArrivedAt;

    GatewayUsageCapture(OutputStream delegate, ObjectMapper objectMapper, String upstreamContentType) {
        this(delegate, objectMapper, upstreamContentType, false, Instant::now, null);
    }

    /**
     * @param clock dates the arrival of each chunk; the forwarder passes the
     *              instant its reader took the chunk from the upstream, so a
     *              slow client does not shift it
     */
    GatewayUsageCapture(OutputStream delegate, ObjectMapper objectMapper,
                        String upstreamContentType, boolean retainBody, Supplier<Instant> clock) {
        this(delegate, objectMapper, upstreamContentType, retainBody, clock, null);
    }

    /**
     * @param pricer when non-null, injects {@code usage.cost} / {@code cost_currency}
     *               onto settled usage before the client sees the bytes
     */
    GatewayUsageCapture(OutputStream delegate, ObjectMapper objectMapper,
                        String upstreamContentType, boolean retainBody, Supplier<Instant> clock,
                        GatewayCostPricer pricer) {
        this.delegate = delegate;
        this.clock = clock;
        this.objectMapper = objectMapper;
        this.sse = isEventStream(upstreamContentType);
        this.jsonBody = isJsonBody(upstreamContentType);
        this.retainBody = retainBody;
        this.pricer = pricer;
    }

    static boolean isEventStream(String contentType) {
        return contentType != null
            && contentType.toLowerCase(java.util.Locale.ROOT).contains("text/event-stream");
    }

    /** Held-body enrichment applies only to JSON, never to binary audio/speech. */
    static boolean isJsonBody(String contentType) {
        if (contentType == null) {
            return false;
        }
        String lower = contentType.toLowerCase(java.util.Locale.ROOT);
        return lower.contains("application/json") || lower.contains("+json");
    }

    /**
     * When the first response body byte arrived from the upstream; null while
     * none has. For a stream this can be a role-only event or a keep-alive.
     */
    Instant firstByteAt() {
        return firstByteAt;
    }

    /**
     * When the first token arrived from the upstream; null while none has.
     *
     * <p>For a stream it is the first event that carries generated content
     * (see {@link #carriesContent}), not the first byte: a stream often opens
     * with a role-only delta or a keep-alive, which would understate the time
     * to the first token. A stream in which no event is recognised as content
     * (an unfamiliar event format) falls back to the first byte, so the figure
     * stays available. A JSON body has no tokens in flight; its first byte is
     * the moment the whole answer starts to arrive and stands in for it.
     */
    Instant firstTokenAt() {
        return firstTokenAt != null ? firstTokenAt : firstByteAt;
    }

    /** The usage seen so far; empty when the response carried none. */
    Map<String, Long> usage() {
        return usage;
    }

    /** The service tier the response reported, lowercased; null when it reported none. */
    String serviceTier() {
        return serviceTier;
    }

    /**
     * The response body, when it was retained and fits the cap. Null for a
     * stream, which has no single body, and whenever retention is off.
     */
    byte[] capturedBody() {
        return capturedBody;
    }

    @Override
    public void write(int b) throws IOException {
        write(new byte[] {(byte) b}, 0, 1);
    }

    @Override
    public void write(byte[] b, int off, int len) throws IOException {
        // Overridden so the pass-through stays a bulk copy: FilterOutputStream's
        // default would forward this byte by byte.
        // Taken before the write: a slow client must not delay the instant
        // this chunk is recorded as having arrived from the upstream.
        chunkArrivedAt = clock.get();
        markFirstByte(len);
        if (pricer != null && jsonBody) {
            writeJsonEnriching(b, off, len);
            return;
        }
        if (pricer != null && sse) {
            captureSseEnriching(b, off, len);
            return;
        }
        // Binary and other non-JSON bodies (e.g. audio/mpeg) pass through even
        // when a pricer is configured — there is no usage object to enrich.
        capture(b, off, len);
        writeToClient(b, off, len);
    }

    @Override
    public void flush() throws IOException {
        try {
            delegate.flush();
        } catch (IOException e) {
            throw new ClientDisconnectedException(e);
        }
    }

    @Override
    public void close() throws IOException {
        try {
            // Emit any held enrichable body while the client stream may still
            // be attached. detachClient() does the same first so a forwarder
            // that detaches before close does not drop the response.
            flushHeldToClient();
        } finally {
            delegate.close();
        }
    }

    /** Client I/O is distinct from an upstream read failure. */
    static final class ClientDisconnectedException extends IOException {
        ClientDisconnectedException(IOException cause) {
            super(cause.getMessage(), cause);
        }
    }

    /**
     * Stop writing to the servlet output. Flushes a held JSON body when a
     * pricer is active so the client still receives it. SSE buffer and parser
     * state are preserved for a background drain that may continue the stream.
     */
    void detachClient() {
        try {
            if (pricer != null && jsonBody) {
                finishJson();
            }
        } catch (IOException ignored) {
            // Client may already be gone; close() still parses for billing.
        }
        delegate = OutputStream.nullOutputStream();
    }

    private void flushHeldToClient() throws IOException {
        if (sse) {
            finishSseLine(false);
            return;
        }
        if (pricer != null && jsonBody) {
            finishJson();
            return;
        }
        finishJson();
    }

    boolean drainComplete() {
        return finalUsage || terminalEvent;
    }

    String upstreamError() {
        return upstreamError;
    }

    /** Persist the estimate marker alongside canonical, priceable token counts. */
    Map<String, Long> disconnectedUsage(byte[] request, boolean timedOut) {
        if (upstreamError != null) {
            return usage;
        }
        if (finalUsage) {
            return usage.isEmpty() ? Map.of("provider_delivered_nothing", 1L) : usage;
        }
        if (firstByteAt == null && !timedOut) {
            return Map.of("provider_delivered_nothing", 1L);
        }
        long prompt = request == null ? 0 : (new String(request, StandardCharsets.UTF_8).length() + 3L) / 4;
        long completion = (completionCharacters + 3) / 4;
        return Map.of("prompt_tokens", prompt, "completion_tokens", completion,
            "total_tokens", prompt + completion, "usage_estimated", 1L);
    }

    private void writeToClient(byte[] b) throws IOException {
        writeToClient(b, 0, b.length);
    }

    private void writeToClient(byte[] b, int off, int len) throws IOException {
        if (len <= 0) {
            return;
        }
        try {
            delegate.write(b, off, len);
        } catch (IOException e) {
            throw new ClientDisconnectedException(e);
        }
    }

    private void writeJsonEnriching(byte[] b, int off, int len) throws IOException {
        if (jsonPassthrough) {
            writeToClient(b, off, len);
            return;
        }
        if (bufferOverflowed) {
            return;
        }
        if (buffer.size() + len > JSON_CAPTURE_LIMIT_BYTES) {
            bufferOverflowed = true;
            jsonPassthrough = true;
            byte[] held = buffer.toByteArray();
            buffer.reset();
            writeToClient(held);
            writeToClient(b, off, len);
            return;
        }
        buffer.write(b, off, len);
    }

    private static long textLength(JsonNode node) {
        if (node.isTextual()) {
            return node.asText().codePointCount(0, node.asText().length());
        }
        if (node.isArray()) {
            long count = 0;
            for (JsonNode part : node) {
                count += textLength(part.isObject() ? part.path("text") : part);
            }
            return count;
        }
        return 0;
    }

    private void observeEvent(JsonNode event) {
        String type = event.path("type").asText(sseEventType);
        JsonNode error = event.path("error");
        if (java.util.Set.of("response.failed", "response.error", "error").contains(type)
                || "error".equals(sseEventType) || (error.isObject() && !error.isEmpty())
                || nonEmptyText(error)) {
            if (error.isMissingNode() || error.isNull()) {
                error = event.path("response").path("error");
            }
            upstreamError = error.isTextual() ? error.asText()
                : error.path("message").asText(event.path("message").asText("Provider stream reported " + type));
            terminalEvent = true;
        }
        JsonNode reported = event.path("response").path("usage");
        if (!reported.isObject()) {
            reported = event.path("usage");
        }
        if (reported.isObject() && !reported.isEmpty() && !"message_start".equals(event.path("type").asText())) {
            finalUsage = true;
        }
        for (JsonNode choice : event.path("choices")) {
            completionCharacters += textLength(choice.path("text"));
            JsonNode delta = choice.path("delta");
            for (String field : new String[] {"content", "reasoning", "reasoning_content"}) {
                completionCharacters += textLength(delta.path(field));
            }
            for (JsonNode tool : delta.path("tool_calls")) {
                completionCharacters += textLength(tool.path("function").path("arguments"));
            }
        }
        if (java.util.Set.of("response.completed", "response.incomplete", "response.failed", "message_stop")
                .contains(type)) {
            terminalEvent = true;
        }
        if (type.startsWith("response.") && type.endsWith(".delta")) {
            completionCharacters += textLength(event.path("delta"));
        } else if ("content_block_delta".equals(type)) {
            for (String field : new String[] {"text", "thinking", "partial_json"}) {
                completionCharacters += textLength(event.path("delta").path(field));
            }
        }
    }

    private void markFirstByte(int len) {
        if (firstByteAt == null && len > 0) {
            firstByteAt = chunkArrivedAt;
        }
    }

    private void capture(byte[] b, int off, int len) throws IOException {
        if (len <= 0) {
            return;
        }
        if (sse) {
            captureSse(b, off, len);
        } else {
            captureJson(b, off, len);
        }
    }

    private void captureJson(byte[] b, int off, int len) {
        if (bufferOverflowed) {
            return;
        }
        if (buffer.size() + len > JSON_CAPTURE_LIMIT_BYTES) {
            bufferOverflowed = true;
            buffer.reset();
            return;
        }
        buffer.write(b, off, len);
    }

    private void captureSse(byte[] b, int off, int len) throws IOException {
        for (int i = off; i < off + len; i++) {
            byte current = b[i];
            if (current == '\n') {
                finishSseLine(true);
                continue;
            }
            if (bufferOverflowed) {
                continue;
            }
            if (buffer.size() >= SSE_LINE_LIMIT_BYTES) {
                bufferOverflowed = true;
                buffer.reset();
                continue;
            }
            buffer.write(current);
        }
    }

    /**
     * SSE with a pricer: emit each completed line (enriched when settled usage
     * is present). Oversized lines pass through in bulk until the next newline.
     * A client disconnect still captures the rest of the current chunk so a
     * trailing usage frame in the same read is not lost before the drain runs.
     */
    private void captureSseEnriching(byte[] b, int off, int len) throws IOException {
        ClientDisconnectedException disconnect = null;
        int i = off;
        final int end = off + len;
        while (i < end) {
            if (bufferOverflowed) {
                int nl = indexOf(b, i, end, (byte) '\n');
                int sliceEnd = nl < 0 ? end : nl + 1;
                disconnect = writeOrRemember(disconnect, b, i, sliceEnd - i);
                if (nl >= 0) {
                    bufferOverflowed = false;
                }
                i = sliceEnd;
                continue;
            }
            int nl = indexOf(b, i, end, (byte) '\n');
            if (nl < 0) {
                disconnect = appendSseLineBytes(disconnect, b, i, end - i);
                break;
            }
            disconnect = appendSseLineBytes(disconnect, b, i, nl - i);
            try {
                finishSseLine(true);
            } catch (ClientDisconnectedException e) {
                disconnect = e;
                delegate = OutputStream.nullOutputStream();
            }
            i = nl + 1;
        }
        if (disconnect != null) {
            throw disconnect;
        }
    }

    private ClientDisconnectedException appendSseLineBytes(
            ClientDisconnectedException disconnect, byte[] b, int off, int len) throws IOException {
        int i = off;
        final int end = off + len;
        while (i < end) {
            if (buffer.size() >= SSE_LINE_LIMIT_BYTES) {
                disconnect = writeOrRemember(disconnect, buffer.toByteArray());
                buffer.reset();
                bufferOverflowed = true;
                int nl = indexOf(b, i, end, (byte) '\n');
                int sliceEnd = nl < 0 ? end : nl + 1;
                disconnect = writeOrRemember(disconnect, b, i, sliceEnd - i);
                if (nl >= 0) {
                    bufferOverflowed = false;
                    i = sliceEnd;
                    continue;
                }
                return disconnect;
            }
            buffer.write(b[i]);
            i++;
        }
        return disconnect;
    }

    private ClientDisconnectedException writeOrRemember(
            ClientDisconnectedException disconnect, byte[] b) throws IOException {
        return writeOrRemember(disconnect, b, 0, b.length);
    }

    private ClientDisconnectedException writeOrRemember(
            ClientDisconnectedException disconnect, byte[] b, int off, int len) throws IOException {
        if (disconnect != null || len <= 0) {
            return disconnect;
        }
        try {
            writeToClient(b, off, len);
            return null;
        } catch (ClientDisconnectedException e) {
            delegate = OutputStream.nullOutputStream();
            return e;
        }
    }

    private static int indexOf(byte[] b, int from, int to, byte value) {
        for (int i = from; i < to; i++) {
            if (b[i] == value) {
                return i;
            }
        }
        return -1;
    }

    /**
     * @param emitEmptyLine when true, an empty buffer means a blank SSE line
     *                      from the upstream and must be forwarded; when false
     *                      (end of stream) an empty buffer is a no-op
     */
    private void finishSseLine(boolean emitEmptyLine) throws IOException {
        if (bufferOverflowed) {
            bufferOverflowed = false;
            buffer.reset();
            if (pricer != null && emitEmptyLine) {
                writeToClient(new byte[] {'\n'});
            }
            return;
        }
        byte[] lineBytes = buffer.toByteArray();
        buffer.reset();
        if (lineBytes.length == 0) {
            sseEventType = "";
            if (pricer != null && emitEmptyLine) {
                writeToClient(new byte[] {'\n'});
            }
            return;
        }
        String line = new String(lineBytes, StandardCharsets.UTF_8);
        byte[] toEmit = lineBytes;
        if (line.isBlank()) {
            sseEventType = "";
        } else if (line.startsWith("event:")) {
            sseEventType = line.substring(6).strip();
            if (java.util.Set.of("response.failed", "response.error", "error").contains(sseEventType)) {
                upstreamError = "Provider stream reported " + sseEventType;
                // Wait for the data line before stopping the drain: it can carry usage.
            }
        } else {
            if ("data: [DONE]".equals(line.strip())) {
                terminalEvent = true;
            }
            JsonNode event = GatewayUsageExtractor.parseSseDataLine(objectMapper, line);
            if (event != null) {
                observeEvent(event);
                if (firstTokenAt == null && carriesContent(event)) {
                    firstTokenAt = chunkArrivedAt;
                }
                Map<String, Long> found = GatewayUsageExtractor.fromEnvelope(event);
                if (!found.isEmpty()) {
                    // Later events refine earlier ones (Responses API reports usage on
                    // response.completed after incremental events).
                    usage = found;
                }
                String tier = GatewayUsageExtractor.serviceTier(event);
                if (tier != null) {
                    serviceTier = tier;
                }
                if (pricer != null) {
                    byte[] enriched = enrichSseDataLine(line, event);
                    if (enriched != null) {
                        toEmit = enriched;
                    }
                }
            }
        }
        if (pricer != null) {
            writeToClient(toEmit);
            writeToClient(new byte[] {'\n'});
        }
    }

    /**
     * Whether a streamed event carries generated content: a non-empty text,
     * reasoning or tool-call delta of a Chat Completions chunk, the text of a
     * legacy Completions chunk, a non-empty {@code *.delta} event of the
     * Responses API, or a {@code content_block_delta} of the Messages API.
     * Role-only deltas, usage-only chunks and lifecycle events are not.
     */
    static boolean carriesContent(JsonNode event) {
        JsonNode choices = event.path("choices");
        if (choices.isArray()) {
            for (JsonNode choice : choices) {
                JsonNode delta = choice.path("delta");
                if (nonEmptyText(delta.path("content")) || nonEmptyText(delta.path("reasoning_content"))
                        || nonEmptyText(delta.path("reasoning")) || nonEmptyText(choice.path("text"))
                        || (delta.path("tool_calls").isArray() && !delta.path("tool_calls").isEmpty())) {
                    return true;
                }
            }
        }
        String type = event.path("type").asText("");
        if (type.endsWith(".delta") && nonEmptyText(event.path("delta"))) {
            return true;
        }
        if ("content_block_delta".equals(type)) {
            JsonNode delta = event.path("delta");
            return nonEmptyText(delta.path("text")) || nonEmptyText(delta.path("thinking"))
                || nonEmptyText(delta.path("partial_json"));
        }
        return false;
    }

    private static boolean nonEmptyText(JsonNode node) {
        if (node.isTextual()) {
            return !node.asText().isEmpty();
        }
        // Some providers send content as an array of parts.
        return node.isArray() && !node.isEmpty();
    }

    private void finishJson() throws IOException {
        if (jsonPassthrough || bufferOverflowed || buffer.size() == 0) {
            return;
        }
        byte[] body = buffer.toByteArray();
        buffer.reset();
        JsonNode root = GatewayUsageExtractor.parseJsonBody(objectMapper, body);
        if (root != null) {
            observeEvent(root);
            Map<String, Long> found = GatewayUsageExtractor.fromEnvelope(root);
            if (!found.isEmpty()) {
                usage = found;
            }
            serviceTier = GatewayUsageExtractor.serviceTier(root);
        }
        byte[] toWrite = body;
        if (pricer != null && root instanceof ObjectNode objectRoot) {
            byte[] enriched = enrichJsonObject(objectRoot);
            if (enriched != null) {
                toWrite = enriched;
            }
        }
        if (retainBody) {
            capturedBody = toWrite;
        }
        // Only the held-JSON enrichment path deferred the client write.
        if (pricer != null && jsonBody) {
            writeToClient(toWrite);
        }
    }

    /**
     * Inject cost onto a settled usage object. Returns null when nothing changes.
     */
    private byte[] enrichJsonObject(ObjectNode root) {
        ObjectNode target = root;
        JsonNode nested = root.get("response");
        if (nested instanceof ObjectNode nestedObject && nestedObject.has("usage")) {
            target = nestedObject;
        }
        if (!usageIsSettled(target.get("usage"))) {
            return null;
        }
        Map<String, Long> counts = GatewayUsageExtractor.fromEnvelope(root);
        if (counts.isEmpty()) {
            return null;
        }
        OptionalDouble priced = priceSafely(counts, serviceTier);
        if (priced.isEmpty()) {
            return null;
        }
        JsonNode usageNode = target.get("usage");
        if (!(usageNode instanceof ObjectNode usageObject)) {
            return null;
        }
        usageObject.put("cost", priced.getAsDouble());
        usageObject.put("cost_currency", "USD");
        try {
            return objectMapper.writeValueAsBytes(root);
        } catch (IOException e) {
            return null;
        }
    }

    private byte[] enrichSseDataLine(String line, JsonNode event) {
        if (!(event instanceof ObjectNode blob)) {
            return null;
        }
        ObjectNode target = blob;
        JsonNode nested = blob.get("response");
        if (nested instanceof ObjectNode nestedObject) {
            target = nestedObject;
        }
        if (!usageIsSettled(target.get("usage"))) {
            return null;
        }
        Map<String, Long> counts = GatewayUsageExtractor.fromEnvelope(event);
        if (counts.isEmpty()) {
            return null;
        }
        String tier = GatewayUsageExtractor.serviceTier(event);
        OptionalDouble priced = priceSafely(counts, tier != null ? tier : serviceTier);
        if (priced.isEmpty()) {
            return null;
        }
        JsonNode usageNode = target.get("usage");
        if (!(usageNode instanceof ObjectNode usageObject)) {
            return null;
        }
        usageObject.put("cost", priced.getAsDouble());
        usageObject.put("cost_currency", "USD");
        try {
            String prefix = line.startsWith("data:") ? line.substring(0, 5) : "data:";
            String spacing = line.length() > 5 && line.charAt(5) == ' ' ? " " : "";
            // Preserve a trailing CR when the upstream used CRLF line endings.
            String suffix = line.endsWith("\r") ? "\r" : "";
            return (prefix + spacing + objectMapper.writeValueAsString(blob) + suffix)
                .getBytes(StandardCharsets.UTF_8);
        } catch (IOException e) {
            return null;
        }
    }

    private OptionalDouble priceSafely(Map<String, Long> counts, String tier) {
        try {
            return pricer.priceUsd(counts, tier);
        } catch (RuntimeException e) {
            return OptionalDouble.empty();
        }
    }

    /**
     * True when this usage object settles the input side too — same gate as the
     * orchestrator's streaming enricher, so a mid-stream output-only delta is
     * not priced alone.
     */
    static boolean usageIsSettled(JsonNode usage) {
        if (usage == null || !usage.isObject() || usage.isEmpty()) {
            return false;
        }
        for (String key : SETTLED_USAGE_KEYS) {
            if (usage.has(key)) {
                return true;
            }
        }
        return false;
    }
}
