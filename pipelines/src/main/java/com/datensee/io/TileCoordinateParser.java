package com.datensee.io;

import com.datensee.TileCoordinate;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import org.apache.beam.sdk.transforms.DoFn;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Parses NDJSON lines into {@link TileCoordinate} records.
 *
 * <p>Used when tile coordinates are provided via a file (for large tile counts)
 * instead of inlined in the pipeline config.
 */
public final class TileCoordinateParser extends DoFn<String, TileCoordinate> {

    private static final Logger LOG = LoggerFactory.getLogger(TileCoordinateParser.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    @ProcessElement
    public void processElement(
        @Element String line,
        OutputReceiver<TileCoordinate> out
    ) throws IOException {
        TileCoordinate tile = MAPPER.readValue(line, TileCoordinate.class);
        out.output(tile);
    }
}
