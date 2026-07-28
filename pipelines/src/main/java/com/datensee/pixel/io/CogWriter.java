package com.datensee.pixel.io;

import com.datensee.pixel.FailedTileRecord;
import com.datensee.pixel.FetchedTile;
import org.apache.beam.sdk.coders.SerializableCoder;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;

/**
 * PTransform that writes fetched tiles as Cloud Optimized GeoTIFFs.
 *
 * <p>Each tile is transcoded from the raw GeoTIFF returned by the EE HV API
 * into COG format (internal tiling + compression) before writing to GCS
 * or local filesystem. Returns the write-stage dead letters (tiles whose
 * transcode or write failed) so the pipeline can union them into the
 * failures journal alongside fetch failures.
 */
public final class CogWriter
    extends PTransform<PCollection<FetchedTile>, PCollection<FailedTileRecord>> {

    private final String outputPath;
    private final int tileSize;
    private final String compression;
    private final Double nodata;

    /**
     * @param outputPath  GCS URI or local directory
     * @param tileSize    tile edge size in pixels (used as COG block size)
     * @param compression COG compression algorithm ("deflate" or "none")
     * @param nodata      optional GDAL_NODATA value stamped on every COG
     */
    public CogWriter(String outputPath, int tileSize, String compression, Double nodata) {
        this.outputPath = outputPath;
        this.tileSize = tileSize;
        this.compression = compression;
        this.nodata = nodata;
    }

    @Override
    public PCollection<FailedTileRecord> expand(PCollection<FetchedTile> input) {
        return input
            .apply("WriteCogTile", ParDo.of(
                new TileWriterDoFn(outputPath, tileSize, compression, nodata)
            ))
            .setCoder(SerializableCoder.of(FailedTileRecord.class));
    }
}
