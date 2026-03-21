package com.datensee.io;

import com.datensee.FetchedTile;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PDone;

/**
 * PTransform that writes fetched tiles as Cloud Optimized GeoTIFFs.
 *
 * <p>Each tile is transcoded from the raw GeoTIFF returned by the EE HV API
 * into COG format (internal tiling + compression) before writing to GCS
 * or local filesystem.
 */
public final class CogWriter extends PTransform<PCollection<FetchedTile>, PDone> {

    private final String outputPath;
    private final int tileSize;
    private final String compression;

    /**
     * @param outputPath  GCS URI or local directory
     * @param tileSize    tile edge size in pixels (used as COG block size)
     * @param compression COG compression algorithm ("lzw", "deflate", "none")
     */
    public CogWriter(String outputPath, int tileSize, String compression) {
        this.outputPath = outputPath;
        this.tileSize = tileSize;
        this.compression = compression;
    }

    @Override
    public PDone expand(PCollection<FetchedTile> input) {
        input.apply("WriteCogTile", ParDo.of(
            new TileWriterDoFn(outputPath, tileSize, compression)
        ));
        return PDone.in(input.getPipeline());
    }
}
