package com.geedf;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.geedf.fetch.TileFetchTransform;
import com.geedf.io.CogWriter;
import com.geedf.options.GeeDataflowOptions;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import org.apache.beam.sdk.Pipeline;
import org.apache.beam.sdk.io.GenerateSequence;
import org.apache.beam.sdk.options.PipelineOptionsFactory;
import org.apache.beam.sdk.transforms.Create;
import org.apache.beam.sdk.values.PCollection;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Entry point for the GEE Dataflow pipeline.
 *
 * <p>Reads a {@code pipeline-config.json} describing the EE computation,
 * tile grid, and output destination; then orchestrates distributed tile
 * fetching via the Earth Engine High Volume API and COG assembly.
 */
public final class GeeDataflowPipeline {

    private static final Logger LOG = LoggerFactory.getLogger(GeeDataflowPipeline.class);
    private static final ObjectMapper MAPPER = new ObjectMapper()
        .registerModule(new JavaTimeModule());

    private GeeDataflowPipeline() {}

    public static void main(String[] args) throws IOException {
        GeeDataflowOptions options = PipelineOptionsFactory
            .fromArgs(args)
            .withValidation()
            .as(GeeDataflowOptions.class);

        run(options);
    }

    /**
     * Build and run the pipeline.
     *
     * @param options parsed pipeline options (includes path to config JSON)
     */
    static void run(GeeDataflowOptions options) throws IOException {
        PipelineConfig config = loadConfig(options.getConfigFile());
        LOG.info("Loaded pipeline config: {} tiles, output={}", config.tileCount(), config.output().gcsPath());

        Pipeline pipeline = Pipeline.create(options);

        PCollection<TileCoordinate> tiles = pipeline.apply(
            "CreateTiles",
            Create.of(config.tileGrid().tiles())
        );

        PCollection<FetchedTile> fetched = tiles.apply(
            "FetchTiles",
            new TileFetchTransform(config.eeExpression())
        );

        fetched.apply(
            "WriteCog",
            new CogWriter(config.output())
        );

        pipeline.run().waitUntilFinish();
    }

    private static PipelineConfig loadConfig(String configFile) throws IOException {
        String json = Files.readString(Path.of(configFile));
        return MAPPER.readValue(json, PipelineConfig.class);
    }
}
