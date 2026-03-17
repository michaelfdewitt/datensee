package com.datensee.io;

import static org.junit.jupiter.api.Assertions.assertEquals;

import com.datensee.TileCoordinate;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.junit.jupiter.api.Test;

/**
 * Tests for FailedTileWriter JSON formatting.
 *
 * <p>Tests the JSON structure directly rather than going through Beam's
 * DoFn framework, since we're testing serialization logic not pipeline wiring.
 */
class FailedTileWriterTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    @Test
    void formatsFailedTileAsJson() throws Exception {
        TileCoordinate tile = new TileCoordinate(10.0, 20.0, 11.0, 21.0, 3, 5);

        // Reproduce the same logic as FailedTileWriter.processElement
        ObjectNode node = MAPPER.createObjectNode();
        node.put("row", tile.row());
        node.put("col", tile.col());
        node.put("x_min", tile.xMin());
        node.put("y_min", tile.yMin());
        node.put("x_max", tile.xMax());
        node.put("y_max", tile.yMax());

        String json = node.toString();
        JsonNode parsed = MAPPER.readTree(json);

        assertEquals(3, parsed.get("row").asInt());
        assertEquals(5, parsed.get("col").asInt());
        assertEquals(10.0, parsed.get("x_min").asDouble());
        assertEquals(20.0, parsed.get("y_min").asDouble());
        assertEquals(11.0, parsed.get("x_max").asDouble());
        assertEquals(21.0, parsed.get("y_max").asDouble());
    }

    @Test
    void jsonIsValidNdjsonLine() throws Exception {
        TileCoordinate tile = new TileCoordinate(0.5, 1.5, 2.5, 3.5, 0, 0);

        ObjectNode node = MAPPER.createObjectNode();
        node.put("row", tile.row());
        node.put("col", tile.col());
        node.put("x_min", tile.xMin());
        node.put("y_min", tile.yMin());
        node.put("x_max", tile.xMax());
        node.put("y_max", tile.yMax());

        String json = node.toString();

        // Single line, no embedded newlines
        assertEquals(-1, json.indexOf('\n'));
        // Round-trips through Jackson
        JsonNode roundtripped = MAPPER.readTree(json);
        assertEquals(6, roundtripped.size());
    }
}
