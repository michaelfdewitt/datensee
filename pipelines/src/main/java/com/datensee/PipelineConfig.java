package com.datensee;

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
    @JsonProperty("gee_project") String geeProject,
    @JsonProperty("tile_grid") TileGridConfig tileGrid,
    @JsonProperty("output") OutputConfig output,
    @JsonProperty("runner") RunnerConfig runner,
    @JsonProperty("rate_limit") RateLimitConfig rateLimit
) {

    /** Convenience method for logging. */
    public int tileCount() {
        return tileGrid != null && tileGrid.tiles() != null ? tileGrid.tiles().size() : 0;
    }

    /** Returns effective rate limit config with defaults. */
    public RateLimitConfig effectiveRateLimit() {
        return rateLimit != null ? rateLimit : new RateLimitConfig(100);
    }

    /** Tile grid configuration. */
    public record TileGridConfig(
        String crs,
        @JsonProperty("scale_meters") double scaleMeters,
        @JsonProperty("tile_size_pixels") int tileSizePixels,
        List<TileCoordinate> tiles,
        @JsonProperty("tiles_file") String tilesFile
    ) {
        /** Returns tile size in pixels, defaulting to 512 if not set. */
        public int effectiveTileSize() {
            return tileSizePixels > 0 ? tileSizePixels : 512;
        }

        /** Whether tiles are provided via external file rather than inline. */
        public boolean hasExternalTiles() {
            return tilesFile != null && !tilesFile.isBlank();
        }
    }

    /** Output destination and COG parameters. */
    public record OutputConfig(
        @JsonProperty("output_path") String outputPath,
        @JsonProperty("band_count") int bandCount,
        @JsonProperty("data_type") String dataType,
        @JsonProperty("output_tile_size_pixels") Integer outputTileSizePixels,
        CogConfig cog
    ) {
        /** Returns band count, defaulting to 1 if not set. */
        public int effectiveBandCount() {
            return bandCount > 0 ? bandCount : 1;
        }

        /** Returns data type, defaulting to float32 if not set. */
        public String effectiveDataType() {
            return dataType != null && !dataType.isBlank() ? dataType : "float32";
        }

        /**
         * Returns the output COG edge size in pixels, defaulting to the
         * compute tile size when M6 two-tier tiling is disabled.
         */
        public int effectiveOutputTileSizePixels(int computeTileSize) {
            return outputTileSizePixels != null && outputTileSizePixels > 0
                ? outputTileSizePixels
                : computeTileSize;
        }

        /**
         * Returns the configured COG compression name, defaulting to
         * {@code "deflate"} when no {@code cog} block is present.
         */
        public String effectiveCompression() {
            return cog != null && cog.compress() != null && !cog.compress().isBlank()
                ? cog.compress() : "deflate";
        }
    }

    /** Cloud Optimized GeoTIFF parameters. */
    public record CogConfig(
        @JsonProperty("overview_levels") List<Integer> overviewLevels,
        int blocksize,
        String compress,
        int predictor
    ) { }

    /** Rate limiting configuration. */
    public record RateLimitConfig(
        @JsonProperty("max_qps") int maxQps
    ) {
        /** Returns QPS, defaulting to 100 if not set. */
        public int effectiveMaxQps() {
            return maxQps > 0 ? maxQps : 100;
        }
    }

    /** Runner mode. */
    public record RunnerConfig(
        String mode,
        @JsonProperty("dataflow") DataflowConfig dataflow
    ) { }

    /** Dataflow-specific runner options. */
    public record DataflowConfig(
        String project,
        String region,
        @JsonProperty("temp_location") String tempLocation,
        @JsonProperty("staging_location") String stagingLocation,
        @JsonProperty("machine_type") String machineType,
        @JsonProperty("max_workers") int maxWorkers,
        @JsonProperty("labels") java.util.Map<String, String> labels
    ) { }
}
