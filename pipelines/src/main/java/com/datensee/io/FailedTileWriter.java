package com.datensee.io;

import com.datensee.TileCoordinate;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.apache.beam.sdk.transforms.DoFn;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Converts a failed {@link TileCoordinate} to a JSON line for the failures report.
 *
 * <p>Output lines are NDJSON, one per tile. Downstream, these are written via
 * {@code TextIO.write()} to {@code {outputPath}/_failures.json}.
 */
public final class FailedTileWriter extends DoFn<TileCoordinate, String> {

    private static final Logger LOG = LoggerFactory.getLogger(FailedTileWriter.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    @ProcessElement
    public void processElement(
        @Element TileCoordinate tile,
        OutputReceiver<String> out
    ) {
        ObjectNode node = MAPPER.createObjectNode();
        node.put("row", tile.row());
        node.put("col", tile.col());
        node.put("x_min", tile.xMin());
        node.put("y_min", tile.yMin());
        node.put("x_max", tile.xMax());
        node.put("y_max", tile.yMax());

        String json = node.toString();
        LOG.warn("Tile failed permanently: {}", json);
        out.output(json);
    }
}
