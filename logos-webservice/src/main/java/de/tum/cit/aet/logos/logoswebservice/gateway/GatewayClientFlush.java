package de.tum.cit.aet.logos.logoswebservice.gateway;

import java.io.FilterOutputStream;
import java.io.IOException;
import java.io.OutputStream;

import org.springframework.http.ResponseEntity;
import org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody;

import jakarta.servlet.http.HttpServletResponse;

/**
 * Makes a relay's {@code flush()} reach the client.
 *
 * <p>Spring hands a {@link StreamingResponseBody} an output stream whose
 * {@code flush()} does nothing (see
 * {@code ServletServerHttpResponse.FLUSH_ENABLED_PROPERTY_NAME}), so the bytes
 * of a streamed answer would wait in the servlet container's response buffer
 * until it fills or the body ends. The stream given to the relay here flushes
 * the servlet response itself, which is the flush Spring recommends; the
 * relays decide when to flush.
 */
final class GatewayClientFlush {

    private GatewayClientFlush() {
    }

    /** The same response, with a body whose {@code flush()} sends what was written so far. */
    static ResponseEntity<StreamingResponseBody> flushingTo(
            HttpServletResponse servletResponse, ResponseEntity<StreamingResponseBody> response) {
        StreamingResponseBody body = response.getBody();
        if (body == null) {
            return response;
        }
        StreamingResponseBody flushing = out -> body.writeTo(new ClientFlushingOutputStream(out, servletResponse));
        return ResponseEntity.status(response.getStatusCode()).headers(response.getHeaders()).body(flushing);
    }

    private static final class ClientFlushingOutputStream extends FilterOutputStream {

        private final HttpServletResponse servletResponse;

        ClientFlushingOutputStream(OutputStream out, HttpServletResponse servletResponse) {
            super(out);
            this.servletResponse = servletResponse;
        }

        @Override
        public void write(byte[] b, int off, int len) throws IOException {
            // FilterOutputStream would forward this byte by byte.
            out.write(b, off, len);
        }

        @Override
        public void flush() throws IOException {
            out.flush();
            servletResponse.flushBuffer();
        }

        @Override
        public void close() throws IOException {
            // The relays flush when they finish. FilterOutputStream would flush
            // again here, and after a failed flush that repeats the failure while
            // the relay is closing its streams.
            out.close();
        }
    }
}
