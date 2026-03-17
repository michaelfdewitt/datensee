package com.datensee;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import org.junit.jupiter.api.Test;

/** Tests for PipelineConfig JSON deserialization. */
class PipelineConfigTest {

    private static final ObjectMapper MAPPER = new ObjectMapper()
        .registerModule(new JavaTimeModule());

    @Test
    void deserializesMinimalConfig() throws Exception {
        String json = """
            {
              "ee_expression": "{\\"result\\":\\"0\\",\\"values\\":{}}",
              "gee_project": "test-project",
              "tile_grid": {
                "crs": "EPSG:4326",
                "scale_meters": 30.0,
                "tile_size_pixels": 512,
                "tiles": [
                  {"x_min": 0, "y_min": 0, "x_max": 1, "y_max": 1, "row": 0, "col": 0}
                ]
              },
              "output": {
                "output_path": "/tmp/test",
                "band_count": 1,
                "data_type": "float32"
              },
              "runner": { "mode": "local" }
            }
            """;

        PipelineConfig config = MAPPER.readValue(json, PipelineConfig.class);

        assertEquals("test-project", config.geeProject());
        assertEquals("EPSG:4326", config.tileGrid().crs());
        assertEquals(512, config.tileGrid().effectiveTileSize());
        assertEquals(1, config.tileGrid().tiles().size());
        assertEquals("/tmp/test", config.output().outputPath());
        assertEquals(1, config.output().effectiveBandCount());
        assertEquals("float32", config.output().effectiveDataType());
        assertEquals("local", config.runner().mode());
        assertFalse(config.tileGrid().hasExternalTiles());
    }

    @Test
    void deserializesAllFields() throws Exception {
        String json = """
            {
              "ee_expression": "{\\"result\\":\\"0\\",\\"values\\":{}}",
              "gee_project": "my-project",
              "tile_grid": {
                "crs": "EPSG:32610",
                "scale_meters": 10.0,
                "tile_size_pixels": 256,
                "tiles": [
                  {"x_min": 500000, "y_min": 4200000, "x_max": 502560, "y_max": 4202560, "row": 0, "col": 0},
                  {"x_min": 502560, "y_min": 4200000, "x_max": 505120, "y_max": 4202560, "row": 0, "col": 1}
                ]
              },
              "output": {
                "output_path": "gs://my-bucket/output",
                "band_count": 3,
                "data_type": "uint8",
                "cog": {
                  "overview_levels": [2, 4, 8],
                  "blocksize": 256,
                  "compress": "zstd",
                  "predictor": 1
                }
              },
              "runner": {
                "mode": "dataflow",
                "dataflow": {
                  "project": "my-project",
                  "region": "us-central1",
                  "temp_location": "gs://my-bucket/tmp",
                  "staging_location": "gs://my-bucket/staging",
                  "machine_type": "n2-standard-8",
                  "max_workers": 50
                }
              },
              "rate_limit": {
                "max_qps": 200
              }
            }
            """;

        PipelineConfig config = MAPPER.readValue(json, PipelineConfig.class);

        assertEquals("EPSG:32610", config.tileGrid().crs());
        assertEquals(10.0, config.tileGrid().scaleMeters());
        assertEquals(256, config.tileGrid().effectiveTileSize());
        assertEquals(2, config.tileGrid().tiles().size());
        assertEquals(3, config.output().effectiveBandCount());
        assertEquals("uint8", config.output().effectiveDataType());
        assertNotNull(config.output().cog());
        assertEquals("zstd", config.output().cog().compress());
        assertEquals("dataflow", config.runner().mode());
        assertNotNull(config.runner().dataflow());
        assertEquals(50, config.runner().dataflow().maxWorkers());
        assertEquals(200, config.rateLimit().effectiveMaxQps());
    }

    @Test
    void tileCountReflectsTileList() throws Exception {
        String json = """
            {
              "ee_expression": "{}",
              "gee_project": "p",
              "tile_grid": {
                "crs": "EPSG:4326",
                "scale_meters": 30.0,
                "tile_size_pixels": 512,
                "tiles": [
                  {"x_min": 0, "y_min": 0, "x_max": 1, "y_max": 1, "row": 0, "col": 0},
                  {"x_min": 1, "y_min": 0, "x_max": 2, "y_max": 1, "row": 0, "col": 1},
                  {"x_min": 0, "y_min": 1, "x_max": 1, "y_max": 2, "row": 1, "col": 0}
                ]
              },
              "output": { "output_path": "/tmp/out", "band_count": 1, "data_type": "float32" },
              "runner": { "mode": "local" }
            }
            """;

        PipelineConfig config = MAPPER.readValue(json, PipelineConfig.class);
        assertEquals(3, config.tileCount());
    }

    @Test
    void defaultsForMissingBandFields() throws Exception {
        String json = """
            {
              "ee_expression": "{}",
              "gee_project": "p",
              "tile_grid": {
                "crs": "EPSG:4326",
                "scale_meters": 30.0,
                "tile_size_pixels": 512,
                "tiles": [
                  {"x_min": 0, "y_min": 0, "x_max": 1, "y_max": 1, "row": 0, "col": 0}
                ]
              },
              "output": { "output_path": "/tmp/out" },
              "runner": { "mode": "local" }
            }
            """;

        PipelineConfig config = MAPPER.readValue(json, PipelineConfig.class);
        assertEquals(1, config.output().effectiveBandCount());
        assertEquals("float32", config.output().effectiveDataType());
    }

    @Test
    void defaultsForMissingRateLimit() throws Exception {
        String json = """
            {
              "ee_expression": "{}",
              "gee_project": "p",
              "tile_grid": {
                "crs": "EPSG:4326",
                "scale_meters": 30.0,
                "tiles": [
                  {"x_min": 0, "y_min": 0, "x_max": 1, "y_max": 1, "row": 0, "col": 0}
                ]
              },
              "output": { "output_path": "/tmp/out" },
              "runner": { "mode": "local" }
            }
            """;

        PipelineConfig config = MAPPER.readValue(json, PipelineConfig.class);
        assertNull(config.rateLimit());
        assertEquals(100, config.effectiveRateLimit().effectiveMaxQps());
    }

    @Test
    void deserializesFileTilesConfig() throws Exception {
        String json = """
            {
              "ee_expression": "{}",
              "gee_project": "p",
              "tile_grid": {
                "crs": "EPSG:4326",
                "scale_meters": 30.0,
                "tiles_file": "gs://bucket/tiles.ndjson"
              },
              "output": { "output_path": "gs://bucket/output" },
              "runner": { "mode": "local" }
            }
            """;

        PipelineConfig config = MAPPER.readValue(json, PipelineConfig.class);
        assertTrue(config.tileGrid().hasExternalTiles());
        assertEquals("gs://bucket/tiles.ndjson", config.tileGrid().tilesFile());
        assertNull(config.tileGrid().tiles());
    }
}
