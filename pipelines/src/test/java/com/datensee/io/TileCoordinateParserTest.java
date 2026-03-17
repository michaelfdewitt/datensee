package com.datensee.io;

import static org.junit.jupiter.api.Assertions.assertEquals;

import com.datensee.TileCoordinate;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

/** Tests for TileCoordinateParser DoFn. */
class TileCoordinateParserTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    @Test
    void parsesNdjsonLine() throws Exception {
        String line = "{\"x_min\":10.0,\"y_min\":20.0,\"x_max\":11.0,\"y_max\":21.0,\"row\":3,\"col\":5}";

        TileCoordinate tile = MAPPER.readValue(line, TileCoordinate.class);

        assertEquals(3, tile.row());
        assertEquals(5, tile.col());
        assertEquals(10.0, tile.xMin());
        assertEquals(20.0, tile.yMin());
        assertEquals(11.0, tile.xMax());
        assertEquals(21.0, tile.yMax());
    }

    @Test
    void roundtripsViaJackson() throws Exception {
        TileCoordinate original = new TileCoordinate(500000, 4200000, 502560, 4202560, 7, 3);
        String json = MAPPER.writeValueAsString(original);
        TileCoordinate restored = MAPPER.readValue(json, TileCoordinate.class);

        assertEquals(original.row(), restored.row());
        assertEquals(original.col(), restored.col());
        assertEquals(original.xMin(), restored.xMin());
        assertEquals(original.yMax(), restored.yMax());
    }
}
