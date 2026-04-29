package com.datensee.io;

import com.datensee.FailedTileRecord;
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
 * <p>{@link com.datensee.fetch.TileFetchDoFn} populates the {@code error_kind},
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
     * Failure-count threshold above which the writer logs a per-worker
     * "catastrophic-rate" warning. The downstream {@code _failures.json}
     * is written {@code .withoutSharding()} by the pipeline, so a flood
     * of failures funnels through a single worker and becomes a wall-time
     * bottleneck — operators want to know the rate is high so they can
     * decide whether to abort early (e.g. auth credentials revoked).
     */
    private static final long FAILURE_COUNT_WARN_THRESHOLD = 1000L;

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
        if (localCount == FAILURE_COUNT_WARN_THRESHOLD) {
            LOG.error(
                "FailedTileWriter: this worker has serialized {} failures so far. "
                + "_failures.json is written without sharding, so a sustained "
                + "high failure rate will become the pipeline's long pole. "
                + "Consider aborting (e.g. credentials revoked, region change) "
                + "rather than letting the journal grow unbounded.",
                localCount
            );
        }
    }
}
