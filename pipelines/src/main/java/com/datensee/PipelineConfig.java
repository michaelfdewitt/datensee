package com.datensee;

import com.datensee.pixel.PixelGrid;
import com.datensee.pixel.TileCoordinate;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;

/**
 * Top-level configuration passed from the Python CLI to the Beam pipeline.
 *
 * <p>The envelope holds runner-agnostic submission fields (auth, snapshot
 * pin, runner selection, rate limit) plus a {@code pipelineKind}
 * discriminator and the kind-specific payload. Today only
 * {@code pipelineKind="pixel"} is supported and the payload lives under
 * {@link #pixel}; a future vector pipeline will plug a sibling field
 * onto the same envelope without touching the pixel side.
 *
 * <p>The {@code eeExpression} field is opaque — the pipeline never
 * interprets it.
 *
 * <p>Back-compat: {@link #tileGrid()} and {@link #output()} are
 * convenience accessors that delegate into {@link #pixel}, so consumers
 * inside {@code DatensEEPipeline} don't churn for the Phase 2 wire-shape
 * change.
 */
@com.fasterxml.jackson.annotation.JsonIgnoreProperties(ignoreUnknown = true)
public record PipelineConfig(
    @JsonProperty("pipeline_kind") String pipelineKind,
    @JsonProperty("ee_expression") String eeExpression,
    @JsonProperty("gee_project") String geeProject,
    @JsonProperty("runner") RunnerConfig runner,
    @JsonProperty("snapshot_time") Long snapshotTime,
    @JsonProperty("carryover_file") String carryoverFile,
    @JsonProperty("pixel") PixelPayload pixel
) {

    /**
     * Whether a carryover journal was staged for this run. Set by
     * {@code datensee retry}: records that made no progress last round
     * (terminal kinds, depth-capped splits) are written to
     * {@code {output}/_carryover.json} and unioned with this run's fresh
     * failures when the pipeline writes {@code _failures.json} — so the
     * journal stays the complete view of stuck tiles on every runner,
     * with no Python post-step racing the async writer.
     */
    public boolean hasCarryover() {
        return carryoverFile != null && !carryoverFile.isBlank();
    }

    /** Convenience method for logging. */
    public int tileCount() {
        TileGridConfig tg = tileGrid();
        return tg != null && tg.tiles() != null ? tg.tiles().size() : 0;
    }

    /**
     * Pixel-pipeline payload — selected when {@code pipelineKind == "pixel"}.
     *
     * <p>Carries the raster-specific work-unit description: the parent
     * tile grid and the output config that says where COGs land and how
     * they're packed.
     */
    public record PixelPayload(
        @JsonProperty("tile_grid") TileGridConfig tileGrid,
        @JsonProperty("output") OutputConfig output
    ) { }

    /** Pixel payload's tile grid, or {@code null} if the payload is absent. */
    public TileGridConfig tileGrid() {
        return pixel != null ? pixel.tileGrid() : null;
    }

    /** Pixel payload's output config, or {@code null} if the payload is absent. */
    public OutputConfig output() {
        return pixel != null ? pixel.output() : null;
    }

    /** Tile grid configuration. */
    public record TileGridConfig(
        @JsonProperty("pixel_grid") PixelGrid pixelGrid,
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

        /** CRS code — delegates to {@code pixelGrid.crsCode()}. */
        public String crs() {
            return pixelGrid != null ? pixelGrid.crsCode() : null;
        }
    }

    /**
     * Output destination and COG parameters.
     *
     * <p>{@code bandCount} and {@code dataType} are <strong>informational
     * metadata</strong> describing what the EE expression is expected to
     * return, not contracts the pipeline enforces. The transcoder reads
     * the actual sample structure from each TIFF returned by EE; if EE
     * yields a different shape from what the config claims, the COG is
     * built around the TIFF's truth and the config's claim is silently
     * superseded. Treat these fields as documentation for humans and
     * downstream tools — never as authority for runtime behavior.
     */
    public record OutputConfig(
        @JsonProperty("output_path") String outputPath,
        @JsonProperty("band_count") int bandCount,
        @JsonProperty("data_type") String dataType,
        @JsonProperty("output_tile_size_pixels") Integer outputTileSizePixels,
        @JsonProperty("merge_existing_output") Boolean mergeExistingOutput,
        @JsonProperty("nodata") Double nodata,
        @JsonProperty("compression") String compression
    ) {
        /**
         * Whether the two-tier assembler should merge this run's tiles into an
         * already-existing output COG instead of replacing it. Set by
         * {@code datensee retry} so re-fetched tiles (including quadtree
         * split children) overlay the blocks that already succeeded.
         * Defaults to false: a fresh export replaces stale files rather
         * than silently blending old pixels into failed blocks.
         */
        public boolean effectiveMergeExistingOutput() {
            return Boolean.TRUE.equals(mergeExistingOutput);
        }

        /**
         * Returns the configured band count (informational; see class
         * javadoc), defaulting to 1 when not set.
         */
        public int effectiveBandCount() {
            return bandCount > 0 ? bandCount : 1;
        }

        /**
         * Returns the configured data type (informational; see class
         * javadoc), defaulting to float32 when not set.
         */
        public String effectiveDataType() {
            return dataType != null && !dataType.isBlank() ? dataType : "float32";
        }

        /**
         * Returns the output COG edge size in pixels, defaulting to the
         * compute tile size when two-tier tiling is disabled.
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
            return compression != null && !compression.isBlank() ? compression : "deflate";
        }
    }

    /** Runner mode. */
    public record RunnerConfig(
        String mode,
        @JsonProperty("dataflow") DataflowConfig dataflow
    ) { }

    /**
     * Dataflow-specific runner options.
     *
     * <p>The worker-pool knobs ({@code numWorkers}, {@code maxWorkers},
     * {@code autoscalingAlgorithm}, {@code numberOfWorkerHarnessThreads})
     * are read by the Python CLI into the Flex Template launch payload's
     * {@code environment} block; the Java pipeline never inspects them
     * because Beam's runner consumes them as pipeline options before
     * {@code main()} runs. They live here so the persisted
     * {@code _pipeline-config.json} captures the full submission shape
     * for diagnostics.
     */
    @com.fasterxml.jackson.annotation.JsonIgnoreProperties(ignoreUnknown = true)
    public record DataflowConfig(
        String project,
        String region,
        @JsonProperty("temp_location") String tempLocation,
        @JsonProperty("staging_location") String stagingLocation,
        @JsonProperty("machine_type") String machineType,
        @JsonProperty("num_workers") Integer numWorkers,
        @JsonProperty("max_workers") int maxWorkers,
        @JsonProperty("autoscaling_algorithm") String autoscalingAlgorithm,
        @JsonProperty("number_of_worker_harness_threads") Integer numberOfWorkerHarnessThreads,
        @JsonProperty("labels") java.util.Map<String, String> labels
    ) { }
}
