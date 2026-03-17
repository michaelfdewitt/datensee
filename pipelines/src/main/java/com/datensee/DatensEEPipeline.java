package com.datensee;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.datensee.fetch.TileFetchTransform;
import com.datensee.io.CogWriter;
import com.datensee.options.DatensEEOptions;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import org.apache.beam.sdk.Pipeline;
import org.apache.beam.sdk.options.PipelineOptionsFactory;
import org.apache.beam.sdk.transforms.Create;
import org.apache.beam.sdk.values.PCollection;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Entry point for the DatensEE pipeline.
 *
 * <p>Reads a {@code pipeline-config.json} describing the EE computation,
 * tile grid, and output destination; then orchestrates distributed tile
 * fetching via the Earth Engine High Volume API and COG assembly.
 */
public final class DatensEEPipeline {

    private static final Logger LOG = LoggerFactory.getLogger(DatensEEPipeline.class);
    private static final ObjectMapper MAPPER = new ObjectMapper()
        .registerModule(new JavaTimeModule());

    private DatensEEPipeline() {}

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
        PipelineConfig config = loadConfig(options.getConfigFile());
        validateConfig(config);

        int tileSize = config.tileGrid().effectiveTileSize();
        String crs = config.tileGrid().crs();

        LOG.info(
            "Pipeline config: project={}, tiles={}, tileSize={}px, crs={}, output={}",
            config.geeProject(),
            config.tileCount(),
            tileSize,
            crs,
            config.output().outputPath()
        );

        Pipeline pipeline = Pipeline.create(options);

        PCollection<TileCoordinate> tiles = pipeline.apply(
            "CreateTiles",
            Create.of(config.tileGrid().tiles())
        );

        PCollection<FetchedTile> fetched = tiles.apply(
            "FetchTiles",
            new TileFetchTransform(
                config.eeExpression(), config.geeProject(), tileSize, crs
            )
        );

        fetched.apply(
            "WriteTiles",
            new CogWriter(config.output().outputPath())
        );

        pipeline.run().waitUntilFinish();
    }

    private static PipelineConfig loadConfig(String configFile) throws IOException {
        String json = Files.readString(Path.of(configFile));
        return MAPPER.readValue(json, PipelineConfig.class);
    }

    private static void validateConfig(PipelineConfig config) {
        if (config.geeProject() == null || config.geeProject().isBlank()) {
            throw new IllegalArgumentException(
                "gee_project is required. Set it to your GCP project ID "
                + "with the Earth Engine API enabled."
            );
        }
        if (config.tileGrid() == null || config.tileGrid().tiles() == null
            || config.tileGrid().tiles().isEmpty()) {
            throw new IllegalArgumentException(
                "tile_grid must contain at least one tile. Check that the region "
                + "intersects the tile grid."
            );
        }
        if (config.output() == null || config.output().outputPath() == null
            || config.output().outputPath().isBlank()) {
            throw new IllegalArgumentException(
                "output.output_path is required. Provide a GCS URI (gs://…) "
                + "or a local directory path."
            );
        }
        if (config.tileGrid().crs() == null || config.tileGrid().crs().isBlank()) {
            throw new IllegalArgumentException(
                "tile_grid.crs is required. Provide an EPSG code (e.g. 'EPSG:4326') "
                + "or a proj string."
            );
        }
    }
}
