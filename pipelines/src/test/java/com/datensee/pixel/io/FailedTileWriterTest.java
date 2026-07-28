package com.datensee.pixel.io;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.datensee.pixel.FailedTileRecord;
import com.datensee.pixel.TileCoordinate;
import com.datensee.fetch.EeErrorKind;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import org.junit.jupiter.api.Test;

/**
 * Pins the JSON schema of {@code _failures.json}.
 *
 * <p>The journal is a wire contract: a future
 * {@code datensee retry --journal failures.json} must be able to read it
 * back via the existing {@code tiles_file} input path. If the schema
 * changes, change it here on purpose.
 *
 * <p>{@link FailedTileWriter} is a thin wrapper around
 * {@code MAPPER.writeValueAsString(record)}; we exercise that by
 * constructing a {@link FailedTileRecord} and round-tripping it
 * through Jackson.
 */
class FailedTileWriterTest {

    private static final ObjectMapper MAPPER = new ObjectMapper()
        .registerModule(new JavaTimeModule());

    private static String emit(FailedTileRecord record) throws Exception {
        return MAPPER.writeValueAsString(record);
    }

    @Test
    void emitsRichSchemaWithPixelOffsetOutTileLineageAndErrorMetadata() throws Exception {
        TileCoordinate tile = new TileCoordinate(256, 512, 256, 256, 3, 5);
        FailedTileRecord record = FailedTileRecord.fromTileWithError(
            tile, EeErrorKind.MEMORY_EXCEEDED,
            "User memory limit exceeded.", 400, 5
        );
        String json = emit(record);
        JsonNode parsed = MAPPER.readTree(json);

        // Pixel offsets + dimensions + indices (read by tiles_file consumer).
        assertEquals(256, parsed.get("col_px").asInt());
        assertEquals(512, parsed.get("row_px").asInt());
        assertEquals(256, parsed.get("width_px").asInt());
        assertEquals(256, parsed.get("height_px").asInt());
        assertEquals(3, parsed.get("row").asInt());
        assertEquals(5, parsed.get("col").asInt());
        // two-tier / two-tier defaults — out_row/out_col mirror row/col when unset.
        assertEquals(3, parsed.get("out_row").asInt());
        assertEquals(5, parsed.get("out_col").asInt());
        // Lineage: empty list for a root compute tile.
        assertTrue(parsed.get("lineage").isArray(), "lineage must be array");
        assertEquals(0, parsed.get("lineage").size(), "root tile has empty lineage");
        // Error metadata.
        assertEquals("MEMORY_EXCEEDED", parsed.get("error_kind").asText());
        assertEquals("User memory limit exceeded.", parsed.get("error_message").asText());
        assertEquals(400, parsed.get("http_status").asInt());
        assertEquals(5, parsed.get("attempts").asInt());
        assertNotNull(parsed.get("first_seen"), "first_seen must be present");
        assertNotNull(parsed.get("last_seen"), "last_seen must be present");
        // Pipeline-emitted records always start with journal_reason="failed".
        // The retry CLI re-stamps to depth_cap/terminal/unknown_kind for
        // carryover. Pinned because downstream readers (jq, the next
        // retry round) rely on this string being stable.
        assertEquals(
            FailedTileRecord.JOURNAL_REASON_FAILED,
            parsed.get("journal_reason").asText()
        );
    }

    @Test
    void emittedJsonIsSingleLineNdjson() throws Exception {
        TileCoordinate tile = new TileCoordinate(0, 0, 16, 16, 0, 0);
        String json = emit(FailedTileRecord.fromTile(tile));
        assertEquals(-1, json.indexOf('\n'), "no embedded newlines: " + json);
    }

    @Test
    void roundTripsBackThroughTileCoordinateParser() throws Exception {
        // The journal record must be valid TileCoordinate input so a future
        // retry can read failures.json back in via the tiles_file path.
        // TileCoordinate's @JsonIgnoreProperties(ignoreUnknown=true) plus
        // its @JsonCreator default-defending constructor handle the extras.
        TileCoordinate tile = new TileCoordinate(1024, 2048, 512, 512, 7, 11);
        FailedTileRecord record = FailedTileRecord.fromTileWithError(
            tile, EeErrorKind.COMPUTATION_TIMEOUT, "Computation timed out.", 400, 5
        );
        String json = emit(record);

        TileCoordinate parsed = MAPPER.readValue(json, TileCoordinate.class);
        assertEquals(tile.colPx(), parsed.colPx());
        assertEquals(tile.rowPx(), parsed.rowPx());
        assertEquals(tile.widthPx(), parsed.widthPx());
        assertEquals(tile.heightPx(), parsed.heightPx());
        assertEquals(tile.row(), parsed.row());
        assertEquals(tile.col(), parsed.col());
        assertEquals(tile.row(), parsed.outRow());
        assertEquals(tile.col(), parsed.outCol());
        assertEquals(0, parsed.lineage().size());
    }

    @Test
    void unknownKindWhenNoErrorContext() throws Exception {
        TileCoordinate tile = new TileCoordinate(0, 0, 16, 16, 0, 0);
        String json = emit(FailedTileRecord.fromTile(tile));
        JsonNode parsed = MAPPER.readTree(json);
        assertEquals("UNKNOWN", parsed.get("error_kind").asText());
        assertEquals(0, parsed.get("attempts").asInt());
    }
}
