package com.datensee.pixel;

import com.datensee.PipelineConfig;
import com.datensee.pixel.io.AssembledCogWriter;
import com.datensee.pixel.io.CogWriter;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.values.PCollection;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Terminal pixel-pipeline write stage: fetched tiles → COGs on GCS / disk.
 *
 * <p>Selects between the two raster output modes based on two-tier
 * tiling configuration:
 *
 * <ul>
 *   <li>{@code output_tile_size_pixels > tile_size_pixels} — group compute
 *       tiles by output tile and assemble into multi-block COGs via
 *       {@link AssembledCogWriter}.
 *   <li>Otherwise — one COG per compute tile via {@link CogWriter}.
 * </ul>
 *
 * <p>Returns the write-stage dead letters (tiles whose transcode, assembly,
 * or upload failed) so the pipeline shell can union them with fetch-stage
 * failures into a single {@code _failures.json} — a write error dead-letters
 * the affected tiles rather than failing the whole job.
 *
 * <p>Lives in {@code com.datensee.pixel} so {@code DatensEEPipeline} (the
 * runner-agnostic shell) doesn't have to know about COG assembly. A future
 * vector pipeline plugs its own terminal write transform in the same
 * position.
 */
public final class PixelOutputTransform
    extends PTransform<PCollection<FetchedTile>, PCollection<FailedTileRecord>> {

    private static final Logger LOG = LoggerFactory.getLogger(PixelOutputTransform.class);

    private final String outputPath;
    private final PixelGrid parentGrid;
    private final int tileSize;
    private final int outputTileSize;
    private final String compression;
    private final boolean mergeExistingOutput;
    private final Double nodata;

    public PixelOutputTransform(
        String outputPath,
        PixelGrid parentGrid,
        int tileSize,
        int outputTileSize,
        String compression,
        boolean mergeExistingOutput,
        Double nodata
    ) {
        this.outputPath = outputPath;
        this.parentGrid = parentGrid;
        this.tileSize = tileSize;
        this.outputTileSize = outputTileSize;
        this.compression = compression;
        this.mergeExistingOutput = mergeExistingOutput;
        this.nodata = nodata;
    }

    /**
     * Convenience constructor that pulls the relevant fields off a
     * {@link PipelineConfig}. Keeps the call site in {@code DatensEEPipeline}
     * a one-liner without leaking the two-tier decision back into the shell.
     */
    public static PixelOutputTransform fromConfig(
        PipelineConfig config,
        PixelGrid parentGrid,
        int tileSize
    ) {
        int outputTileSize = config.output().effectiveOutputTileSizePixels(tileSize);
        return new PixelOutputTransform(
            config.output().outputPath(),
            parentGrid,
            tileSize,
            outputTileSize,
            config.output().effectiveCompression(),
            config.output().effectiveMergeExistingOutput(),
            config.output().nodata()
        );
    }

    @Override
    public PCollection<FailedTileRecord> expand(PCollection<FetchedTile> input) {
        boolean twoTier = outputTileSize > tileSize;
        // Merge mode (retry rounds) always routes through the assembler,
        // even when output tile == compute tile: the canvas merge is what
        // lets re-fetched tiles — including sub-block quadtree split
        // children — overlay an existing COG instead of replacing it.
        // Fresh non-two-tier exports keep the direct writer: routing them
        // through GroupByKey would shuffle the entire raster for nothing.
        if (twoTier || mergeExistingOutput) {
            if (twoTier) {
                LOG.info(
                    "two-tier tiling: output tile size = {}px (= {}x{} compute tiles per output COG)",
                    outputTileSize, outputTileSize / tileSize, outputTileSize / tileSize
                );
            } else {
                LOG.info("Merge mode: routing per-tile writes through the assembler");
            }
            return input.apply(
                "AssembleAndWriteOutputTiles",
                new AssembledCogWriter(
                    outputPath, parentGrid, tileSize, outputTileSize, compression,
                    mergeExistingOutput, nodata
                )
            );
        }
        return input.apply(
            "WriteTiles",
            new CogWriter(outputPath, tileSize, compression, nodata)
        );
    }
}
