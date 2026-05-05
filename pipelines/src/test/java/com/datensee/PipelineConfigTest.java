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
                "pixel_grid": {
                  "crs_code": "EPSG:4326",
                  "affine_transform": {
                    "scale_x": 0.000269458,
                    "shear_x": 0,
                    "translate_x": 0,
                    "shear_y": 0,
                    "scale_y": -0.000269458,
                    "translate_y": 1
                  },
                  "dimensions": {"width": 512, "height": 512}
                },
                "tile_size_pixels": 512,
                "tiles": [
                  {"col_px": 0, "row_px": 0, "width_px": 512, "height_px": 512, "row": 0, "col": 0}
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
        assertEquals("EPSG:4326", config.tileGrid().pixelGrid().crsCode());
        assertEquals(512, config.tileGrid().effectiveTileSize());
        assertEquals(1, config.tileGrid().tiles().size());
        assertEquals(0, config.tileGrid().tiles().get(0).colPx());
        assertEquals(512, config.tileGrid().tiles().get(0).widthPx());
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
                "pixel_grid": {
                  "crs_code": "EPSG:32610",
                  "affine_transform": {
                    "scale_x": 10.0,
                    "shear_x": 0,
                    "translate_x": 500000,
                    "shear_y": 0,
                    "scale_y": -10.0,
                    "translate_y": 4202560
                  },
                  "dimensions": {"width": 512, "height": 256}
                },
                "tile_size_pixels": 256,
                "tiles": [
                  {"col_px": 0,   "row_px": 0, "width_px": 256, "height_px": 256, "row": 0, "col": 0},
                  {"col_px": 256, "row_px": 0, "width_px": 256, "height_px": 256, "row": 0, "col": 1}
                ]
              },
              "output": {
                "output_path": "gs://my-bucket/output",
                "band_count": 3,
                "data_type": "uint8",
                "cog": {
                  "overview_levels": [2, 4, 8],
                  "blocksize": 256,
                  "compress": "deflate",
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
        assertEquals(10.0, config.tileGrid().pixelGrid().affineTransform().scaleX());
        assertEquals(500000.0, config.tileGrid().pixelGrid().affineTransform().translateX());
        assertEquals(256, config.tileGrid().effectiveTileSize());
        assertEquals(2, config.tileGrid().tiles().size());
        assertEquals(3, config.output().effectiveBandCount());
        assertEquals("uint8", config.output().effectiveDataType());
        assertNotNull(config.output().cog());
        assertEquals("deflate", config.output().cog().compress());
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
                "pixel_grid": {
                  "crs_code": "EPSG:4326",
                  "affine_transform": {
                    "scale_x": 1, "shear_x": 0, "translate_x": 0,
                    "shear_y": 0, "scale_y": -1, "translate_y": 1
                  },
                  "dimensions": {"width": 1, "height": 1}
                },
                "tile_size_pixels": 1,
                "tiles": [
                  {"col_px": 0, "row_px": 0, "width_px": 1, "height_px": 1, "row": 0, "col": 0},
                  {"col_px": 1, "row_px": 0, "width_px": 1, "height_px": 1, "row": 0, "col": 1},
                  {"col_px": 0, "row_px": 1, "width_px": 1, "height_px": 1, "row": 1, "col": 0}
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
                "pixel_grid": {
                  "crs_code": "EPSG:4326",
                  "affine_transform": {
                    "scale_x": 1, "shear_x": 0, "translate_x": 0,
                    "shear_y": 0, "scale_y": -1, "translate_y": 1
                  },
                  "dimensions": {"width": 1, "height": 1}
                },
                "tile_size_pixels": 512,
                "tiles": [
                  {"col_px": 0, "row_px": 0, "width_px": 1, "height_px": 1, "row": 0, "col": 0}
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
                "pixel_grid": {
                  "crs_code": "EPSG:4326",
                  "affine_transform": {
                    "scale_x": 1, "shear_x": 0, "translate_x": 0,
                    "shear_y": 0, "scale_y": -1, "translate_y": 1
                  },
                  "dimensions": {"width": 1, "height": 1}
                },
                "tiles": [
                  {"col_px": 0, "row_px": 0, "width_px": 1, "height_px": 1, "row": 0, "col": 0}
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
                "pixel_grid": {
                  "crs_code": "EPSG:4326",
                  "affine_transform": {
                    "scale_x": 1, "shear_x": 0, "translate_x": 0,
                    "shear_y": 0, "scale_y": -1, "translate_y": 1
                  },
                  "dimensions": {"width": 1, "height": 1}
                },
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
