package com.geedf;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;

/**
 * Top-level configuration passed from the Python CLI to the Beam pipeline.
 *
 * <p>Deserializes from the JSON file written by the Python CLI. The
 * {@code eeExpression} field is opaque — the pipeline never interprets it.
 */
public record PipelineConfig(
    @JsonProperty("ee_expression") String eeExpression,
    @JsonProperty("tile_grid") TileGridConfig tileGrid,
    @JsonProperty("output") OutputConfig output,
    @JsonProperty("runner") RunnerConfig runner
) {

    /** Convenience method for logging. */
    public int tileCount() {
        return tileGrid != null && tileGrid.tiles() != null ? tileGrid.tiles().size() : 0;
    }

    /** Tile grid configuration. */
    public record TileGridConfig(
        String crs,
        @JsonProperty("scale_meters") double scaleMeters,
        List<TileCoordinate> tiles
    ) {}

    /** Output destination and COG parameters. */
    public record OutputConfig(
        @JsonProperty("gcs_path") String gcsPath,
        CogConfig cog
    ) {}

    /** Cloud Optimized GeoTIFF parameters. */
    public record CogConfig(
        @JsonProperty("overview_levels") List<Integer> overviewLevels,
        int blocksize,
        String compress,
        int predictor
    ) {}

    /** Runner mode. */
    public record RunnerConfig(
        String mode,
        @JsonProperty("dataflow") DataflowConfig dataflow
    ) {}

    /** Dataflow-specific runner options. */
    public record DataflowConfig(
        String project,
        String region,
        @JsonProperty("temp_location") String tempLocation,
        @JsonProperty("staging_location") String stagingLocation,
        @JsonProperty("machine_type") String machineType,
        @JsonProperty("max_workers") int maxWorkers
    ) {}
}
