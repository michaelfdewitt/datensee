package com.datensee;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

/**
 * Tests for {@link DatensEEPipeline#validateConfig} and the
 * {@code pipeline_kind} discriminator handling — the config-shape
 * gatekeeping that decides whether a JSON blob is something this JAR
 * can run.
 *
 * <p>Legacy flat configs (pre-discriminator {@code tile_grid}/{@code
 * output} at the top level) must fail with the actionable
 * incompatible-client message, not a raw Jackson unknown-property
 * exception — the envelope tolerates unknown fields so the payload
 * check gets to speak.
 */
class DatensEEPipelineConfigValidationTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String NESTED_PIXEL_CONFIG = """
        {
          "pipeline_kind": "pixel",
          "ee_expression": "{}",
          "gee_project": "my-project",
          "pixel": {
            "tile_grid": {
              "pixel_grid": {
                "crs_code": "EPSG:4326",
                "affine_transform": {
                  "scale_x": 1.0, "shear_x": 0, "translate_x": 0,
                  "shear_y": 0, "scale_y": -1.0, "translate_y": 0
                },
                "dimensions": {"width": 32, "height": 32}
              },
              "tile_size_pixels": 16,
              "tiles": [
                {"col_px": 0, "row_px": 0, "width_px": 16, "height_px": 16,
                 "row": 0, "col": 0}
              ]
            },
            "output": {"output_path": "/tmp/out"}
          }
        }
        """;

    private static PipelineConfig parse(String json) throws Exception {
        return MAPPER.readValue(json, PipelineConfig.class);
    }

    @Test
    void nestedPixelConfigValidates() throws Exception {
        PipelineConfig config = parse(NESTED_PIXEL_CONFIG);
        assertDoesNotThrow(() -> DatensEEPipeline.validateConfig(config));
    }

    @Test
    void absentPipelineKindIsToleratedWhenPayloadPresent() throws Exception {
        PipelineConfig config = parse(NESTED_PIXEL_CONFIG.replace(
            "\"pipeline_kind\": \"pixel\",", ""
        ));
        assertDoesNotThrow(() -> DatensEEPipeline.validateConfig(config));
    }

    @Test
    void unsupportedPipelineKindIsRejected() throws Exception {
        PipelineConfig config = parse(NESTED_PIXEL_CONFIG.replace(
            "\"pipeline_kind\": \"pixel\"", "\"pipeline_kind\": \"vector\""
        ));
        IllegalArgumentException e = assertThrows(
            IllegalArgumentException.class,
            () -> DatensEEPipeline.validateConfig(config)
        );
        assertTrue(e.getMessage().contains("vector"));
        assertTrue(e.getMessage().contains("pipeline_kind"));
    }

    @Test
    void missingPixelPayloadIsRejectedWithActionableMessage() throws Exception {
        PipelineConfig config = parse("""
            {
              "pipeline_kind": "pixel",
              "ee_expression": "{}",
              "gee_project": "my-project"
            }
            """);
        IllegalArgumentException e = assertThrows(
            IllegalArgumentException.class,
            () -> DatensEEPipeline.validateConfig(config)
        );
        assertTrue(e.getMessage().contains("pixel"));
    }

    @Test
    void legacyFlatConfigFailsWithIncompatibleClientMessageNotJacksonError()
        throws Exception {
        // Pre-discriminator wire shape: tile_grid/output at the top level.
        // The envelope ignores the unknown fields so the payload validation
        // produces the actionable message instead of Jackson's
        // UnrecognizedPropertyException.
        PipelineConfig config = parse("""
            {
              "ee_expression": "{}",
              "gee_project": "my-project",
              "tile_grid": {"tile_size_pixels": 16},
              "output": {"output_path": "/tmp/out"}
            }
            """);
        IllegalArgumentException e = assertThrows(
            IllegalArgumentException.class,
            () -> DatensEEPipeline.validateConfig(config)
        );
        assertTrue(
            e.getMessage().contains("incompatible client"),
            "Expected the actionable incompatible-client message, got: " + e.getMessage()
        );
    }

    @Test
    void missingGeeProjectIsRejected() throws Exception {
        PipelineConfig config = parse(NESTED_PIXEL_CONFIG.replace(
            "\"gee_project\": \"my-project\",", ""
        ));
        IllegalArgumentException e = assertThrows(
            IllegalArgumentException.class,
            () -> DatensEEPipeline.validateConfig(config)
        );
        assertTrue(e.getMessage().contains("gee_project"));
    }

    @Test
    void emptyTileSourceIsRejected() throws Exception {
        PipelineConfig config = parse("""
            {
              "pipeline_kind": "pixel",
              "ee_expression": "{}",
              "gee_project": "my-project",
              "pixel": {
                "tile_grid": {
                  "pixel_grid": {
                    "crs_code": "EPSG:4326",
                    "affine_transform": {
                      "scale_x": 1.0, "shear_x": 0, "translate_x": 0,
                      "shear_y": 0, "scale_y": -1.0, "translate_y": 0
                    },
                    "dimensions": {"width": 32, "height": 32}
                  },
                  "tile_size_pixels": 16,
                  "tiles": []
                },
                "output": {"output_path": "/tmp/out"}
              }
            }
            """);
        assertThrows(
            IllegalArgumentException.class,
            () -> DatensEEPipeline.validateConfig(config)
        );
    }
}
