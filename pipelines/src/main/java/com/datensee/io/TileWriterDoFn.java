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
 * <p>Each tile is transcoded from the raw GeoTIFF returned by the EE HV API
 * into a Cloud Optimized GeoTIFF (internal tiling + compression) before
 * writing. This makes output tiles compatible with
 * {@code ee.Image.loadGeoTIFF()} for direct visualization.
 *
 * <p>Routing: if {@code outputPath} starts with {@code gs://}, writes to GCS.
 * Otherwise, treats it as a local directory path.
 */
public final class TileWriterDoFn extends DoFn<FetchedTile, Void> {

    private static final Logger LOG = LoggerFactory.getLogger(TileWriterDoFn.class);

    private final String outputPath;
    private final int tileSize;
    private final String compression;

    private transient Storage storage;

    /**
     * @param outputPath  GCS URI or local directory
     * @param tileSize    tile edge size in pixels (used as COG block size)
     * @param compression COG compression algorithm ("lzw", "deflate", "none")
     */
    public TileWriterDoFn(String outputPath, int tileSize, String compression) {
        this.outputPath = outputPath;
        this.tileSize = tileSize;
        this.compression = compression;
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

        byte[] cogBytes = CogTranscoder.transcode(
            tile.imageBytes(), tileSize, compression
        );

        if (outputPath.startsWith("gs://")) {
            writeToGcs(tile, tifName, cogBytes);
        } else {
            writeToLocal(tile, tifName, cogBytes);
        }
    }

    private void writeToGcs(FetchedTile tile, String tifName, byte[] data) {
        URI gcsUri = URI.create(outputPath);
        String bucket = gcsUri.getHost();
        String prefix = gcsUri.getPath().replaceFirst("^/", "");
        String blobName = prefix.isEmpty() ? tifName : prefix + "/" + tifName;

        BlobId blobId = BlobId.of(bucket, blobName);
        BlobInfo blobInfo = BlobInfo.newBuilder(blobId)
            .setContentType("image/tiff")
            .build();

        storage.create(blobInfo, data);
        LOG.info(
            "Wrote {} ({} bytes, COG) to gs://{}/{}",
            tile.coordinate().id(), data.length, bucket, blobName
        );
    }

    private void writeToLocal(FetchedTile tile, String tifName, byte[] data) throws IOException {
        Path outDir = Path.of(outputPath);
        Files.createDirectories(outDir);
        Path dest = outDir.resolve(tifName);
        Files.write(dest, data);
        LOG.info(
            "Wrote {} ({} bytes, COG) to {}",
            tile.coordinate().id(), data.length, dest
        );
    }
}
