package com.datensee.io;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.datensee.FailedTileRecord;
import com.datensee.TileCoordinate;
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
 * <p>{@link FailedTileWriter} is a thin wrapper that calls
 * {@code MAPPER.writeValueAsString(FailedTileRecord.fromTile(tile))}; we
 * exercise the same code without going through Beam's DoFn harness so
 * the test stays fast and self-contained.
 */
class FailedTileWriterTest {

    private static final ObjectMapper MAPPER = new ObjectMapper()
        .registerModule(new JavaTimeModule());

    private static String emit(TileCoordinate tile) throws Exception {
        return MAPPER.writeValueAsString(FailedTileRecord.fromTile(tile));
    }

    @Test
    void emitsRichSchemaWithBboxOutTileLineageAndErrorMetadata() throws Exception {
        TileCoordinate tile = new TileCoordinate(10.0, 20.0, 11.0, 21.0, 3, 5);
        String json = emit(tile);
        JsonNode parsed = MAPPER.readTree(json);

        // Bounding box + indices (read by tiles_file consumer).
        assertEquals(10.0, parsed.get("x_min").asDouble());
        assertEquals(20.0, parsed.get("y_min").asDouble());
        assertEquals(11.0, parsed.get("x_max").asDouble());
        assertEquals(21.0, parsed.get("y_max").asDouble());
        assertEquals(3, parsed.get("row").asInt());
        assertEquals(5, parsed.get("col").asInt());
        // M6 / two-tier defaults — out_row/out_col mirror row/col when unset.
        assertEquals(3, parsed.get("out_row").asInt());
        assertEquals(5, parsed.get("out_col").asInt());
        // Lineage: empty list for a root compute tile.
        assertTrue(parsed.get("lineage").isArray(), "lineage must be array");
        assertEquals(0, parsed.get("lineage").size(), "root tile has empty lineage");
        // Error metadata (placeholder values until the classifier is wired).
        assertEquals("UNKNOWN", parsed.get("error_kind").asText());
        assertEquals(0, parsed.get("attempts").asInt());
        assertNotNull(parsed.get("first_seen"), "first_seen must be present");
        assertNotNull(parsed.get("last_seen"), "last_seen must be present");
    }

    @Test
    void emittedJsonIsSingleLineNdjson() throws Exception {
        TileCoordinate tile = new TileCoordinate(0.5, 1.5, 2.5, 3.5, 0, 0);
        String json = emit(tile);
        assertEquals(-1, json.indexOf('\n'), "no embedded newlines: " + json);
    }

    @Test
    void roundTripsBackThroughTileCoordinateParser() throws Exception {
        // The journal record must be valid TileCoordinate input so a future
        // retry can read failures.json back in via the tiles_file path.
        // TileCoordinate's @JsonIgnoreProperties(ignoreUnknown=true) plus
        // its @JsonCreator default-defending constructor handle the extras.
        TileCoordinate tile = new TileCoordinate(-122.5, 37.5, -122.0, 38.0, 7, 11);
        String json = emit(tile);

        TileCoordinate parsed = MAPPER.readValue(json, TileCoordinate.class);
        assertEquals(tile.xMin(), parsed.xMin());
        assertEquals(tile.yMin(), parsed.yMin());
        assertEquals(tile.xMax(), parsed.xMax());
        assertEquals(tile.yMax(), parsed.yMax());
        assertEquals(tile.row(), parsed.row());
        assertEquals(tile.col(), parsed.col());
        assertEquals(tile.row(), parsed.outRow());
        assertEquals(tile.col(), parsed.outCol());
        assertEquals(0, parsed.lineage().size());
    }
}
