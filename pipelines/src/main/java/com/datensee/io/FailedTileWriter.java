package com.datensee.io;

import com.datensee.FailedTileRecord;
import com.datensee.TileCoordinate;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import org.apache.beam.sdk.transforms.DoFn;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Converts a failed {@link TileCoordinate} to a JSON line for the failures journal.
 *
 * <p>Output lines are NDJSON, one per tile, written via {@code TextIO.write()}
 * to {@code {outputPath}/_failures.json}. The schema is
 * {@link FailedTileRecord}, a superset of {@link TileCoordinate} that
 * carries error classification and retry metadata. The journal is
 * intentionally consumable as a {@code tiles_file} input: a future
 * {@code datensee retry --journal} command can feed failures back in
 * directly because {@code TileCoordinate} ignores the extra fields.
 *
 * <p>Sketch only at present — the dead-letter side output upstream still
 * emits raw {@code TileCoordinate}, so {@code error_kind}, {@code attempts},
 * and the timestamps are placeholders. Wiring the classifier is a
 * follow-up. The on-disk format is stable now.
 */
public final class FailedTileWriter extends DoFn<TileCoordinate, String> {

    private static final Logger LOG = LoggerFactory.getLogger(FailedTileWriter.class);
    private static final ObjectMapper MAPPER = new ObjectMapper()
        .registerModule(new JavaTimeModule());

    @ProcessElement
    public void processElement(
        @Element TileCoordinate tile,
        OutputReceiver<String> out
    ) throws com.fasterxml.jackson.core.JsonProcessingException {
        FailedTileRecord record = FailedTileRecord.fromTile(tile);
        String json = MAPPER.writeValueAsString(record);
        LOG.warn("Tile failed permanently: {}", json);
        out.output(json);
    }
}
