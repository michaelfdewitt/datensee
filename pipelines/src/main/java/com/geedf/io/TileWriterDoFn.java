package com.geedf.io;

import com.geedf.FetchedTile;
import com.geedf.PipelineConfig.OutputConfig;
import com.google.cloud.storage.BlobId;
import com.google.cloud.storage.BlobInfo;
import com.google.cloud.storage.Storage;
import com.google.cloud.storage.StorageOptions;
import java.net.URI;
import org.apache.beam.sdk.transforms.DoFn;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Writes a single fetched tile to GCS.
 *
 * <p>Each tile is written as an individual GeoTIFF at
 * {@code gcs_path/tile_r{row}_c{col}.tif}. Full COG stitching
 * is handled in a post-processing step (not yet implemented).
 */
public final class TileWriterDoFn extends DoFn<FetchedTile, Void> {

    private static final Logger LOG = LoggerFactory.getLogger(TileWriterDoFn.class);

    private final OutputConfig outputConfig;
    private transient Storage storage;

    public TileWriterDoFn(OutputConfig outputConfig) {
        this.outputConfig = outputConfig;
    }

    @Setup
    public void setup() {
        storage = StorageOptions.getDefaultInstance().getService();
    }

    @ProcessElement
    public void processElement(@Element FetchedTile tile) {
        String gcsPath = outputConfig.gcsPath();
        URI gcsUri = URI.create(gcsPath);
        String bucket = gcsUri.getHost();
        String prefix = gcsUri.getPath().replaceFirst("^/", "");
        String blobName = String.format(
            "%s/tile_r%04d_c%04d.tif",
            prefix,
            tile.coordinate().row(),
            tile.coordinate().col()
        );

        BlobId blobId = BlobId.of(bucket, blobName);
        BlobInfo blobInfo = BlobInfo.newBuilder(blobId)
            .setContentType("image/tiff")
            .build();

        storage.create(blobInfo, tile.imageBytes());
        LOG.info("Wrote {} ({} bytes) to gs://{}/{}", tile.coordinate().id(), tile.imageBytes().length, bucket, blobName);
    }
}
