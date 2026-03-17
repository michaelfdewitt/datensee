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
        int tileSize = config.tileGrid().effectiveTileSize();

        LOG.info(
            "Pipeline config: project={}, tiles={}, tileSize={}px, output={}",
            config.geeProject(),
            config.tileCount(),
            tileSize,
            config.output().outputPath()
        );

        Pipeline pipeline = Pipeline.create(options);

        PCollection<TileCoordinate> tiles = pipeline.apply(
            "CreateTiles",
            Create.of(config.tileGrid().tiles())
        );

        PCollection<FetchedTile> fetched = tiles.apply(
            "FetchTiles",
            new TileFetchTransform(config.eeExpression(), config.geeProject(), tileSize)
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
}
