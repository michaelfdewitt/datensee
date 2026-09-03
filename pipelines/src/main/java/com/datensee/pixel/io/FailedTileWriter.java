package com.datensee.pixel.io;

import com.datensee.pixel.FailedTileRecord;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import org.apache.beam.sdk.metrics.Counter;
import org.apache.beam.sdk.metrics.Metrics;
import org.apache.beam.sdk.transforms.DoFn;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Serializes a {@link FailedTileRecord} as one NDJSON line for the failures
 * journal.
 *
 * <p>Output is written via {@code TextIO.write()} to
 * {@code {outputPath}/_failures.json}. The schema is a strict superset of
 * {@code TileCoordinate}: a future {@code datensee retry --journal} command
 * can feed the journal back through the existing {@code tiles_file} input
 * path because {@code TileCoordinate} ignores the extra fields.
 *
 * <p>{@link com.datensee.pixel.fetch.TileFetchDoFn} populates the {@code error_kind},
 * {@code http_status}, and {@code error_message} fields from the underlying
 * {@code EeApiException} when one is available; otherwise the kind defaults
 * to {@code UNKNOWN}. The retry CLI uses {@code error_kind} to decide whether
 * to split the tile into quadrants or retry it as-is.
 */
public final class FailedTileWriter extends DoFn<FailedTileRecord, String> {

    private static final Logger LOG = LoggerFactory.getLogger(FailedTileWriter.class);
    private static final ObjectMapper MAPPER = new ObjectMapper()
        .registerModule(new JavaTimeModule());

    /**
     * Per-worker failure threshold above which this worker logs a warning.
     * Strictly a per-worker signal because Beam DoFns cannot inspect
     * pipeline-wide counter aggregates during execution. System-level
     * monitoring should rely on the {@code failures_written} metric.
     */
    private static final long PER_WORKER_FAILURE_WARN_THRESHOLD = 1000L;

    private final Counter failuresWritten = Metrics.counter("datensee", "failures_written");
    private long localCount;

    @ProcessElement
    public void processElement(
        @Element FailedTileRecord record,
        OutputReceiver<String> out
    ) throws com.fasterxml.jackson.core.JsonProcessingException {
        String json = MAPPER.writeValueAsString(record);
        LOG.warn("Tile failed permanently ({}): {}", record.errorKind(), json);
        out.output(json);
        failuresWritten.inc();
        localCount++;
        if (localCount == PER_WORKER_FAILURE_WARN_THRESHOLD) {
            LOG.error(
                "FailedTileWriter: THIS WORKER has serialized {} failures. "
                + "Pipeline-wide rate is in the 'failures_written' Beam counter. "
                + "A single worker hitting this threshold often means a "
                + "systemic problem on its slice (credentials, asset access).",
                localCount
            );
        }
    }
}
