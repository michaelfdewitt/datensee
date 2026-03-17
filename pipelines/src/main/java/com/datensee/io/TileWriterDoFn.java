package com.datensee.io;

import com.datensee.FetchedTile;
import com.google.cloud.storage.BlobId;
import com.google.cloud.storage.BlobInfo;
import com.google.cloud.storage.Storage;
import com.google.cloud.storage.StorageOptions;
import java.io.IOException;
import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import org.apache.beam.sdk.transforms.DoFn;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Writes a single fetched tile to either GCS or the local filesystem.
 *
 * <p>Routing: if {@code outputPath} starts with {@code gs://}, writes to GCS.
 * Otherwise, treats it as a local directory path (useful for Direct runner
 * local testing without requiring GCS credentials).
 *
 * <p>Each tile is written as an individual GeoTIFF named
 * {@code tile_r{row}_c{col}.tif}. Full COG stitching is handled in a
 * post-processing step (see {@code assemble.py} in the Python CLI).
 */
public final class TileWriterDoFn extends DoFn<FetchedTile, Void> {

    private static final Logger LOG = LoggerFactory.getLogger(TileWriterDoFn.class);

    private final String outputPath;

    private transient Storage storage;

    public TileWriterDoFn(String outputPath) {
        this.outputPath = outputPath;
    }

    @Setup
    public void setup() {
        if (outputPath.startsWith("gs://")) {
            storage = StorageOptions.getDefaultInstance().getService();
        }
    }

    @ProcessElement
    public void processElement(@Element FetchedTile tile) throws IOException {
        String tifName = String.format(
            "tile_r%04d_c%04d.tif",
            tile.coordinate().row(),
            tile.coordinate().col()
        );

        if (outputPath.startsWith("gs://")) {
            writeToGcs(tile, tifName);
        } else {
            writeToLocal(tile, tifName);
        }
    }

    private void writeToGcs(FetchedTile tile, String tifName) {
        URI gcsUri = URI.create(outputPath);
        String bucket = gcsUri.getHost();
        String prefix = gcsUri.getPath().replaceFirst("^/", "");
        String blobName = prefix.isEmpty() ? tifName : prefix + "/" + tifName;

        BlobId blobId = BlobId.of(bucket, blobName);
        BlobInfo blobInfo = BlobInfo.newBuilder(blobId)
            .setContentType("image/tiff")
            .build();

        storage.create(blobInfo, tile.imageBytes());
        LOG.info(
            "Wrote {} ({} bytes) to gs://{}/{}",
            tile.coordinate().id(), tile.imageBytes().length, bucket, blobName
        );
    }

    private void writeToLocal(FetchedTile tile, String tifName) throws IOException {
        Path outDir = Path.of(outputPath);
        Files.createDirectories(outDir);
        Path dest = outDir.resolve(tifName);
        Files.write(dest, tile.imageBytes());
        LOG.info(
            "Wrote {} ({} bytes) to {}",
            tile.coordinate().id(), tile.imageBytes().length, dest
        );
    }
}
