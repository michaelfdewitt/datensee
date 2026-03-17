package com.datensee.io;

import com.datensee.FetchedTile;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PDone;

/**
 * PTransform that writes fetched tiles to GCS or local filesystem.
 *
 * <p>Current implementation writes individual tile GeoTIFFs as a staging
 * step. Full COG assembly (stitching + overview pyramid) is a TODO for M3.
 */
public final class CogWriter extends PTransform<PCollection<FetchedTile>, PDone> {

    private final String outputPath;

    public CogWriter(String outputPath) {
        this.outputPath = outputPath;
    }

    @Override
    public PDone expand(PCollection<FetchedTile> input) {
        input.apply("WriteTileToOutput", ParDo.of(new TileWriterDoFn(outputPath)));
        return PDone.in(input.getPipeline());
    }
}
