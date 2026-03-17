package com.datensee.fetch;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.datensee.TileCoordinate;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.lang.reflect.Method;
import org.junit.jupiter.api.Test;

/** Tests for TileFetchDoFn request body construction and EeApiException classification. */
class TileFetchDoFnTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private String buildRequestBody(String crs, TileCoordinate tile) throws Exception {
        TileFetchDoFn doFn = new TileFetchDoFn(
            "{\"result\":\"0\",\"values\":{}}",
            "test-project",
            256,
            crs,
            100.0,
            1
        );
        Method method = TileFetchDoFn.class.getDeclaredMethod(
            "buildRequestBody", TileCoordinate.class
        );
        method.setAccessible(true);
        return (String) method.invoke(doFn, tile);
    }

    @Test
    void requestBodyUsesCrsFromConstructor() throws Exception {
        TileCoordinate tile = new TileCoordinate(0, 0, 1, 1, 0, 0);
        String body = buildRequestBody("EPSG:32610", tile);

        JsonNode root = MAPPER.readTree(body);
        JsonNode grid = root.get("grid");
        assertNotNull(grid, "grid field must be present");
        assertEquals("EPSG:32610", grid.get("crsCode").asText());
    }

    @Test
    void requestBodyUsesEpsg4326() throws Exception {
        TileCoordinate tile = new TileCoordinate(-122.5, 37.5, -122.0, 38.0, 0, 0);
        String body = buildRequestBody("EPSG:4326", tile);

        JsonNode root = MAPPER.readTree(body);
        assertEquals("EPSG:4326", root.get("grid").get("crsCode").asText());
    }

    @Test
    void requestBodyContainsExpressionAndFormat() throws Exception {
        TileCoordinate tile = new TileCoordinate(0, 0, 1, 1, 0, 0);
        String body = buildRequestBody("EPSG:4326", tile);

        JsonNode root = MAPPER.readTree(body);
        assertNotNull(root.get("expression"), "expression field must be present");
        assertEquals("GEO_TIFF", root.get("fileFormat").asText());
    }

    @Test
    void requestBodyDimensionsMatchTileSize() throws Exception {
        TileCoordinate tile = new TileCoordinate(0, 0, 256, 256, 0, 0);
        String body = buildRequestBody("EPSG:32610", tile);

        JsonNode dims = MAPPER.readTree(body).get("grid").get("dimensions");
        assertEquals(256, dims.get("width").asInt());
        assertEquals(256, dims.get("height").asInt());
    }

    @Test
    void requestBodyAffineTransformUsesCoordinates() throws Exception {
        TileCoordinate tile = new TileCoordinate(500000, 4200000, 502560, 4202560, 0, 0);
        String body = buildRequestBody("EPSG:32610", tile);

        JsonNode affine = MAPPER.readTree(body).get("grid").get("affineTransform");
        assertEquals(500000.0, affine.get("translateX").asDouble());
        assertEquals(4202560.0, affine.get("translateY").asDouble());
        assertEquals(10.0, affine.get("scaleX").asDouble(), 0.001);
        assertEquals(-10.0, affine.get("scaleY").asDouble(), 0.001);
    }

    // --- EeApiException classification tests ---

    @Test
    void rateLimited429IsRetryable() {
        EeApiException ex = new EeApiException(429, "tile[r=0,c=0]", "rate limited");
        assertTrue(ex.isRetryable());
        assertEquals(429, ex.httpStatus());
    }

    @Test
    void serviceUnavailable503IsRetryable() {
        EeApiException ex = new EeApiException(503, "tile[r=0,c=0]", "unavailable");
        assertTrue(ex.isRetryable());
    }

    @Test
    void other5xxIsRetryable() {
        EeApiException ex = new EeApiException(502, "tile[r=0,c=0]", "bad gateway");
        assertTrue(ex.isRetryable());
    }

    @Test
    void badRequest400IsNotRetryable() {
        EeApiException ex = new EeApiException(400, "tile[r=0,c=0]", "bad request");
        assertFalse(ex.isRetryable());
    }

    @Test
    void forbidden403IsNotRetryable() {
        EeApiException ex = new EeApiException(403, "tile[r=0,c=0]", "forbidden");
        assertFalse(ex.isRetryable());
    }

    @Test
    void notFound404IsNotRetryable() {
        EeApiException ex = new EeApiException(404, "tile[r=0,c=0]", "not found");
        assertFalse(ex.isRetryable());
    }

    @Test
    void truncatesLongResponseBody() {
        String longBody = "x".repeat(1000);
        EeApiException ex = new EeApiException(500, "tile[r=0,c=0]", longBody);
        assertTrue(ex.truncatedBody().length() <= 500);
        assertTrue(ex.getMessage().length() < longBody.length());
    }
}
