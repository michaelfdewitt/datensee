package com.datensee;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.datensee.options.DatensEEOptions;
import com.datensee.pixel.FailedTileRecord;
import com.datensee.pixel.FetchedTile;
import com.datensee.pixel.PixelGrid;
import com.datensee.pixel.PixelOutputTransform;
import com.datensee.pixel.TileCoordinate;
import com.datensee.pixel.fetch.TileFetchDoFn;
import com.datensee.pixel.fetch.TileFetchTransform;
import com.datensee.pixel.io.FailedTileWriter;
import com.datensee.pixel.io.TileCoordinateParser;
import java.io.IOException;
import java.io.InputStream;
import java.net.URI;
import java.nio.channels.Channels;
import java.nio.file.Path;
import org.apache.beam.sdk.Pipeline;
import org.apache.beam.sdk.io.FileSystems;
import org.apache.beam.sdk.io.TextIO;
import org.apache.beam.sdk.io.fs.MatchResult;
import org.apache.beam.sdk.options.PipelineOptionsFactory;
import org.apache.beam.sdk.transforms.Create;
import org.apache.beam.sdk.transforms.Flatten;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionList;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Entry point for the DatensEE pipeline.
 *
 * <p>Reads a {@code pipeline-config.json} describing the EE computation,
 * tile grid, and output destination; then orchestrates distributed tile
 * fetching via the Earth Engine High Volume API and COG assembly.
 *
 * <p>Partial-failure model: fetch and write failures dead-letter into the
 * failures journal instead of failing the job; {@code datensee retry}
 * feeds the journal back in. Tiles arrive inline or via a tiles file.
 */
public final class DatensEEPipeline {

    private static final Logger LOG = LoggerFactory.getLogger(DatensEEPipeline.class);
    private static final ObjectMapper MAPPER = new ObjectMapper()
        .registerModule(new JavaTimeModule());

    private DatensEEPipeline() { }

    public static void main(String[] args) throws IOException {
        DatensEEOptions options = PipelineOptionsFactory
            .fromArgs(args)
            .withValidation()
            .as(DatensEEOptions.class);

        run(options);
    }

    /**
     * Build and run the pipeline.
     *
     * @param options parsed pipeline options (includes path to config JSON)
     */
    static void run(DatensEEOptions options) throws IOException {
        // Register filesystems so loadConfig can resolve gs:// URIs (Flex
        // Template launches stage the config to GCS rather than a local path).
        FileSystems.setDefaultPipelineOptions(options);

        PipelineConfig config = loadConfig(options.getConfigFile());
        validateConfig(config);

        int tileSize = config.tileGrid().effectiveTileSize();
        PixelGrid parentGrid = config.tileGrid().pixelGrid();
        String crs = config.tileGrid().crs();

        LOG.info(
            "Pipeline config: project={}, tiles={}, tileSize={}px, crs={}, output={}",
            config.geeProject(),
            config.tileGrid().hasExternalTiles() ? "(file)" : config.tileCount(),
            tileSize,
            crs,
            config.output().outputPath()
        );

        Pipeline pipeline = Pipeline.create(options);

        // --- Tile source: inline or file-based ---
        PCollection<TileCoordinate> tiles;
        if (config.tileGrid().hasExternalTiles()) {
            LOG.info("Reading tiles from file: {}", config.tileGrid().tilesFile());
            tiles = pipeline
                .apply("ReadTileFile", TextIO.read().from(config.tileGrid().tilesFile()))
                .apply("ParseTileCoordinates", ParDo.of(new TileCoordinateParser()));
        } else {
            tiles = pipeline.apply(
                "CreateTiles",
                Create.of(config.tileGrid().tiles())
            );
        }

        // --- Fetch tiles with dead-letter support ---
        PCollectionTuple fetchResult = tiles.apply(
            "FetchTiles",
            new TileFetchTransform(
                config.eeExpression(), config.geeProject(), parentGrid
            )
        );

        PCollection<FetchedTile> fetched = fetchResult.get(TileFetchDoFn.SUCCESS_TAG);
        PCollection<FailedTileRecord> fetchFailed = fetchResult.get(TileFetchDoFn.FAILED_TAG);

        // --- Write successful tiles as COGs ---
        // The terminal raster write stage lives in com.datensee.pixel so the
        // shell here stays runner- and shape-agnostic. two-tier routing
        // (output_tile_size_pixels > tile_size_pixels → assemble; otherwise
        // one COG per compute tile) is decided inside the transform. Write
        // failures dead-letter instead of failing the job, symmetric with
        // the fetch stage.
        PCollection<FailedTileRecord> writeFailed = fetched.apply(
            "WritePixelOutput",
            PixelOutputTransform.fromConfig(config, parentGrid, tileSize)
        );

        // --- Write failure report (fetch + write stages + carryover) ---
        writeFailuresJournal(
            pipeline,
            PCollectionList.of(fetchFailed).and(writeFailed)
                .apply("FlattenFailures", Flatten.pCollections()),
            config.hasCarryover() ? config.carryoverFile() : null,
            failuresOutputPath(config.output().outputPath())
        );

        var result = pipeline.run();

        boolean isDataflow = config.runner() != null
            && "dataflow".equals(config.runner().mode());
        if (isDataflow) {
            // DataflowPipelineJob is a runtime-only dep; extract job ID
            // via reflection to keep the compile-time dep on beam-core only.
            try {
                String jobId = result.getClass()
                    .getMethod("getJobId")
                    .invoke(result)
                    .toString();
                LOG.info("Dataflow job submitted: {}", jobId);
                System.out.println("DATENSEE_JOB_ID=" + jobId);
                System.out.flush();
            } catch (ReflectiveOperationException e) {
                LOG.warn("Could not extract Dataflow job ID: {}", e.toString());
            }
        } else {
            result.waitUntilFinish();
        }
    }

    private static String failuresOutputPath(String outputPath) {
        if (outputPath.endsWith("/")) {
            return outputPath + "_failures";
        }
        return outputPath + "/_failures";
    }

    /**
     * Serialize this run's failure records and union them with the staged
     * carryover journal (when {@code carryoverFile} is non-null) before
     * writing {@code _failures.json}.
     *
     * <p>Carryover lines are the previous round's no-progress records
     * (terminal kinds, depth-capped splits), already stamped with their
     * {@code journal_reason} by the retry CLI. Performing the union inside
     * the pipeline keeps the journal complete on every runner.
     *
     * <p>Package-private and pipeline-shaped so a {@code TestPipeline}
     * can drive it directly against temp files.
     */
    static void writeFailuresJournal(
        Pipeline pipeline,
        PCollection<FailedTileRecord> failed,
        String carryoverFile,
        String failuresPath
    ) {
        PCollection<String> fresh = failed
            .apply("FormatFailedTiles", ParDo.of(new FailedTileWriter()));
        PCollection<String> lines;
        if (carryoverFile != null) {
            LOG.info("Merging carryover journal from {}", carryoverFile);
            PCollection<String> carryover = pipeline
                .apply("ReadCarryover", TextIO.read().from(carryoverFile));
            lines = PCollectionList.of(fresh).and(carryover)
                .apply("UnionCarryover", Flatten.pCollections());
        } else {
            lines = fresh;
        }
        lines.apply("WriteFailures", TextIO.write()
            .to(failuresPath)
            .withoutSharding()
            .withSuffix(".json"));
    }

    private static PipelineConfig loadConfig(String configFile) throws IOException {
        // Resolve through Beam's FileSystems so gs:// and local paths share
        // a code path. Avoids hard-coding GCS-vs-local branching here.
        MatchResult.Metadata metadata = FileSystems.matchSingleFileSpec(configFile);
        try (InputStream in = Channels.newInputStream(FileSystems.open(metadata.resourceId()))) {
            return MAPPER.readValue(in, PipelineConfig.class);
        }
    }

    // Package-private for direct test coverage.
    static void validateConfig(PipelineConfig config) {
        if (config.geeProject() == null || config.geeProject().isBlank()) {
            throw new IllegalArgumentException(
                "gee_project is required. Set it to your GCP project ID "
                + "with the Earth Engine API enabled."
            );
        }
        // The discriminator gates which payload we expect. Today "pixel"
        // is the only kind; vector will land later as a sibling payload.
        String kind = config.pipelineKind();
        if (kind != null && !kind.isBlank() && !"pixel".equals(kind)) {
            throw new IllegalArgumentException(
                "Unsupported pipeline_kind='" + kind + "'. This pipeline JAR "
                + "only handles pipeline_kind='pixel'."
            );
        }
        if (config.pixel() == null) {
            throw new IllegalArgumentException(
                "pipeline_kind='pixel' requires a 'pixel' payload "
                + "(tile_grid + output). The Python CLI builds this; the "
                + "config JSON looks corrupt or was written by an "
                + "incompatible client."
            );
        }
        boolean hasInlineTiles = config.tileGrid() != null
            && config.tileGrid().tiles() != null
            && !config.tileGrid().tiles().isEmpty();
        boolean hasFileTiles = config.tileGrid() != null
            && config.tileGrid().hasExternalTiles();
        if (!hasInlineTiles && !hasFileTiles) {
            throw new IllegalArgumentException(
                "pixel.tile_grid must contain either inline tiles or a tiles_file path. "
                + "Check that the region intersects the tile grid."
            );
        }
        if (config.output() == null || config.output().outputPath() == null
            || config.output().outputPath().isBlank()) {
            throw new IllegalArgumentException(
                "pixel.output.output_path is required. Provide a GCS URI (gs://…) "
                + "or a local directory path."
            );
        }
        if (config.tileGrid().pixelGrid() == null
            || config.tileGrid().pixelGrid().crsCode() == null
            || config.tileGrid().pixelGrid().crsCode().isBlank()) {
            throw new IllegalArgumentException(
                "pixel.tile_grid.pixel_grid.crs_code is required. Provide an EPSG code "
                + "(e.g. 'EPSG:4326') or a proj string."
            );
        }
        if (config.tileGrid().pixelGrid().affineTransform() == null) {
            throw new IllegalArgumentException(
                "pixel.tile_grid.pixel_grid.affine_transform is required."
            );
        }
        if (config.tileGrid().pixelGrid().dimensions() == null) {
            throw new IllegalArgumentException(
                "pixel.tile_grid.pixel_grid.dimensions is required."
            );
        }
    }
}
