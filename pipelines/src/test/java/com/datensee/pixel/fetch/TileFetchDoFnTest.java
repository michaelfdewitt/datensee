package com.datensee.pixel.fetch;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.datensee.fetch.EeApiException;
import com.datensee.fetch.EeErrorKind;
import com.datensee.pixel.AffineTransform;
import com.datensee.pixel.GridDimensions;
import com.datensee.pixel.PixelGrid;
import com.datensee.pixel.TileCoordinate;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.lang.reflect.Method;
import org.junit.jupiter.api.Test;

/** Tests for TileFetchDoFn request body construction and EeApiException classification. */
class TileFetchDoFnTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static PixelGrid pixelGrid(
        String crs, double scale, double translateX, double translateY,
        int width, int height
    ) {
        return new PixelGrid(
            crs,
            new AffineTransform(scale, 0.0, translateX, 0.0, -scale, translateY),
            new GridDimensions(width, height)
        );
    }

    private String buildRequestBody(PixelGrid parentGrid, TileCoordinate tile) throws Exception {
        TileFetchDoFn doFn = new TileFetchDoFn(
            "{\"result\":\"0\",\"values\":{}}",
            "test-project",
            parentGrid
        );
        Method method = TileFetchDoFn.class.getDeclaredMethod(
            "buildRequestBody", TileCoordinate.class
        );
        method.setAccessible(true);
        return (String) method.invoke(doFn, tile);
    }

    @Test
    void requestBodyUsesCrsFromParentGrid() throws Exception {
        // UTM 10N, 10 m/px, parent grid origin at (500_000, 4_202_560).
        PixelGrid parent = pixelGrid("EPSG:32610", 10.0, 500_000.0, 4_202_560.0, 256, 256);
        TileCoordinate tile = new TileCoordinate(0, 0, 256, 256, 0, 0);
        String body = buildRequestBody(parent, tile);

        JsonNode root = MAPPER.readTree(body);
        JsonNode grid = root.get("grid");
        assertNotNull(grid, "grid field must be present");
        assertEquals("EPSG:32610", grid.get("crsCode").asText());
    }

    @Test
    void requestBodyUsesEpsg4326() throws Exception {
        PixelGrid parent = pixelGrid(
            "EPSG:4326", 30.0 / 111_320.0, -122.5, 38.0, 1024, 1024
        );
        TileCoordinate tile = new TileCoordinate(0, 0, 512, 512, 0, 0);
        String body = buildRequestBody(parent, tile);

        JsonNode root = MAPPER.readTree(body);
        assertEquals("EPSG:4326", root.get("grid").get("crsCode").asText());
    }

    @Test
    void requestBodyContainsExpressionAndFormat() throws Exception {
        PixelGrid parent = pixelGrid("EPSG:4326", 0.0001, 0.0, 1.0, 16, 16);
        TileCoordinate tile = new TileCoordinate(0, 0, 16, 16, 0, 0);
        String body = buildRequestBody(parent, tile);

        JsonNode root = MAPPER.readTree(body);
        assertNotNull(root.get("expression"), "expression field must be present");
        assertEquals("GEO_TIFF", root.get("fileFormat").asText());
    }

    @Test
    void requestBodyDimensionsMatchTileSize() throws Exception {
        PixelGrid parent = pixelGrid("EPSG:32610", 10.0, 500_000.0, 4_202_560.0, 256, 256);
        TileCoordinate tile = new TileCoordinate(0, 0, 256, 256, 0, 0);
        String body = buildRequestBody(parent, tile);

        JsonNode dims = MAPPER.readTree(body).get("grid").get("dimensions");
        assertEquals(256, dims.get("width").asInt());
        assertEquals(256, dims.get("height").asInt());
    }

    @Test
    void requestBodyAffineTransformDerivedFromParentAndOffset() throws Exception {
        // Parent grid at (500_000, 4_202_560), 10 m/px, tile shifted 256 px
        // east = 2_560 m east, NW corner stays at translateY.
        PixelGrid parent = pixelGrid("EPSG:32610", 10.0, 500_000.0, 4_202_560.0, 512, 256);
        TileCoordinate tile = new TileCoordinate(256, 0, 256, 256, 0, 1);
        String body = buildRequestBody(parent, tile);

        JsonNode affine = MAPPER.readTree(body).get("grid").get("affineTransform");
        assertEquals(502_560.0, affine.get("translateX").asDouble(), 0.001);
        assertEquals(4_202_560.0, affine.get("translateY").asDouble(), 0.001);
        assertEquals(10.0, affine.get("scaleX").asDouble(), 0.001);
        assertEquals(-10.0, affine.get("scaleY").asDouble(), 0.001);
    }

    @Test
    void requestBodyAffineTranslatesAcrossRowOffset() throws Exception {
        // Tile shifted 1 row south = -10 m in CRS y (scaleY < 0).
        PixelGrid parent = pixelGrid("EPSG:32610", 10.0, 500_000.0, 4_202_560.0, 256, 512);
        TileCoordinate tile = new TileCoordinate(0, 256, 256, 256, 1, 0);
        String body = buildRequestBody(parent, tile);

        JsonNode affine = MAPPER.readTree(body).get("grid").get("affineTransform");
        // translateY = parent.translateY + rowPx * scaleY = 4_202_560 + 256 * (-10)
        //            = 4_200_000.
        assertEquals(4_200_000.0, affine.get("translateY").asDouble(), 0.001);
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

    @Test
    void classifyFailureUnwrapsEeApiExceptionAndPopulatesKind() {
        TileCoordinate tile = new TileCoordinate(0, 0, 256, 256, 0, 0);
        // Simulate the retry loop's wrapper: IOException(EeApiException(...)).
        EeApiException root = new EeApiException(
            400, tile.id(), "User memory limit exceeded."
        );
        java.io.IOException wrapper = new java.io.IOException(
            "Failed after 5 retries: " + root.getMessage(), root
        );

        com.datensee.pixel.FailedTileRecord r = TileFetchDoFn.classifyFailure(
            tile, wrapper, null, "p"
        );
        assertEquals(EeErrorKind.MEMORY_EXCEEDED, r.errorKind());
        assertEquals(400, r.httpStatus());
        assertTrue(r.errorMessage().contains("memory limit"));
        assertEquals(6, r.attempts());
        // Pixel offsets + indices preserved from the failed TileCoordinate.
        assertEquals(0, r.colPx());
        assertEquals(256, r.widthPx());
        assertEquals(0, r.row());
    }

    @Test
    void classifyFailureFallsBackToUnknownForNonEeException() {
        TileCoordinate tile = new TileCoordinate(0, 0, 256, 256, 0, 0);
        java.io.IOException ioe = new java.io.IOException("connection reset");

        com.datensee.pixel.FailedTileRecord r = TileFetchDoFn.classifyFailure(
            tile, ioe, null, "p"
        );
        assertEquals(EeErrorKind.UNKNOWN, r.errorKind());
        assertEquals(null, r.httpStatus());
        assertTrue(r.errorMessage().contains("connection reset"));
    }

    @Test
    void classifyFailureRewritesAuthErrorMessageWithRemediation() {
        TileCoordinate tile = new TileCoordinate(0, 0, 256, 256, 0, 0);
        EeApiException root = new EeApiException(
            403, tile.id(), "Permission denied: missing credential."
        );
        java.io.IOException wrapper = new java.io.IOException(
            "Failed after 5 retries: " + root.getMessage(), root
        );

        com.datensee.pixel.FailedTileRecord r = TileFetchDoFn.classifyFailure(
            tile, wrapper, "worker@p.iam.gserviceaccount.com", "ee-project"
        );
        assertEquals(EeErrorKind.AUTH_ERROR, r.errorKind());
        assertEquals(403, r.httpStatus());
        assertTrue(
            r.errorMessage().contains("worker@p.iam.gserviceaccount.com"),
            "remediation message should name the worker SA, got: " + r.errorMessage()
        );
        assertTrue(
            r.errorMessage().contains("roles/earthengine.viewer"),
            "remediation message should include the role to grant"
        );
        assertTrue(
            r.errorMessage().contains("ee-project"),
            "remediation message should name the EE project"
        );
        assertTrue(
            r.errorMessage().contains("Permission denied"),
            "original EE body should still be preserved at the tail"
        );
    }

    @Test
    void classifyFailureAuthErrorWithoutSaUsesFallbackPhrase() {
        TileCoordinate tile = new TileCoordinate(0, 0, 256, 256, 0, 0);
        EeApiException root = new EeApiException(
            403, tile.id(), "Permission denied: insufficient authentication scopes."
        );
        java.io.IOException wrapper = new java.io.IOException("retries exhausted", root);

        com.datensee.pixel.FailedTileRecord r = TileFetchDoFn.classifyFailure(
            tile, wrapper, null, "p"
        );
        assertEquals(EeErrorKind.AUTH_ERROR, r.errorKind());
        assertTrue(
            r.errorMessage().contains("Dataflow worker service account"),
            "fallback phrase used when SA isn't discoverable"
        );
    }
}
