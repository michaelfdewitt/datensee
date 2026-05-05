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
        String line = "{\"col_px\":256,\"row_px\":512,\"width_px\":256,"
            + "\"height_px\":256,\"row\":3,\"col\":5}";

        TileCoordinate tile = MAPPER.readValue(line, TileCoordinate.class);

        assertEquals(3, tile.row());
        assertEquals(5, tile.col());
        assertEquals(256, tile.colPx());
        assertEquals(512, tile.rowPx());
        assertEquals(256, tile.widthPx());
        assertEquals(256, tile.heightPx());
    }

    @Test
    void roundtripsViaJackson() throws Exception {
        TileCoordinate original = new TileCoordinate(0, 0, 512, 512, 7, 3);
        String json = MAPPER.writeValueAsString(original);
        TileCoordinate restored = MAPPER.readValue(json, TileCoordinate.class);

        assertEquals(original.row(), restored.row());
        assertEquals(original.col(), restored.col());
        assertEquals(original.colPx(), restored.colPx());
        assertEquals(original.rowPx(), restored.rowPx());
        assertEquals(original.widthPx(), restored.widthPx());
        assertEquals(original.heightPx(), restored.heightPx());
    }
}
