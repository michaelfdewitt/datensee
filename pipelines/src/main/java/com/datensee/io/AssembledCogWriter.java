package com.datensee.io;

import com.datensee.FetchedTile;
import com.datensee.OutputTileKey;
import com.google.cloud.storage.BlobId;
import com.google.cloud.storage.BlobInfo;
import com.google.cloud.storage.Storage;
import com.google.cloud.storage.StorageOptions;
import java.io.IOException;
import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import org.apache.beam.sdk.coders.SerializableCoder;
import org.apache.beam.sdk.metrics.Counter;
import org.apache.beam.sdk.metrics.Metrics;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.GroupByKey;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.transforms.WithKeys;
import org.apache.beam.sdk.values.KV;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PDone;
import org.apache.beam.sdk.values.TypeDescriptor;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * M6 two-tier-tiling output writer.
 *
 * <p>Pipeline shape:
 * <pre>
 *   PCollection&lt;FetchedTile&gt;
 *     ─▶ KeyByOutputTile     (KV&lt;OutputTileKey, FetchedTile&gt;)
 *     ─▶ GroupByKey          (KV&lt;OutputTileKey, Iterable&lt;FetchedTile&gt;&gt;)
 *     ─▶ AssembleAndWriteDoFn
 *         ├─ allocate output buffer (outputTileSize × outputTileSize)
 *         ├─ extract pixels from each compute tile, copy into buffer
 *         ├─ CogTranscoder.transcodeFromAssembledPixels(...)
 *         └─ write COG to GCS or local
 * </pre>
 *
 * <p>The output COG's internal block size is the compute tile size, so
 * each input compute tile lands in exactly one inner TIFF block. This
 * means EE's {@code Image.loadGeoTIFF} reads remain efficient even for
 * large output COGs — the per-tile fetch granularity is preserved as
 * the COG's own internal random-access granularity.
 *
 * <p>Failure handling: today, an output tile is only emitted if
 * <em>all</em> its expected compute tiles successfully landed in the
 * group. If some compute tiles failed upstream and were dead-lettered,
 * the assembler emits a partially-populated COG by zero-filling the
 * missing blocks. Whether to skip vs zero-fill vs flag-as-failed for
 * partial groups is a design decision documented in
 * {@code docs/handoff.md}.
 */
public final class AssembledCogWriter
    extends PTransform<PCollection<FetchedTile>, PDone> {

    private static final Logger LOG = LoggerFactory.getLogger(AssembledCogWriter.class);

    private final String outputPath;
    private final int computeTileSize;
    private final int outputTileSize;
    private final String compression;

    public AssembledCogWriter(
        String outputPath,
        int computeTileSize,
        int outputTileSize,
        String compression
    ) {
        // The (outputTileSize % computeTileSize == 0) invariant is enforced
        // at the wire boundary by PipelineConfig's pydantic validator and
        // again at COG-emit time by transcodeFromTileBlocks; no need for a
        // third check here.
        this.outputPath = outputPath;
        this.computeTileSize = computeTileSize;
        this.outputTileSize = outputTileSize;
        this.compression = compression;
    }

    @Override
    public PDone expand(PCollection<FetchedTile> input) {
        input
            .apply("KeyByOutputTile", WithKeys.of(
                (FetchedTile t) -> new OutputTileKey(t.coordinate().outRow(), t.coordinate().outCol())
            ).withKeyType(TypeDescriptor.of(OutputTileKey.class)))
            .setCoder(org.apache.beam.sdk.coders.KvCoder.of(
                SerializableCoder.of(OutputTileKey.class),
                SerializableCoder.of(FetchedTile.class)
            ))
            .apply("GroupByOutputTile", GroupByKey.create())
            .apply("AssembleAndWrite", ParDo.of(new AssembleAndWriteDoFn(
                outputPath, computeTileSize, outputTileSize, compression
            )));
        return PDone.in(input.getPipeline());
    }

    static final class AssembleAndWriteDoFn
        extends DoFn<KV<OutputTileKey, Iterable<FetchedTile>>, Void> {

        private final String outputPath;
        private final int computeTileSize;
        private final int outputTileSize;
        private final String compression;

        private transient Storage storage;
        private final Counter outputTilesWritten = Metrics.counter("datensee", "output_tiles_written");
        private final Counter computeTilesAssembled = Metrics.counter("datensee", "compute_tiles_assembled");

        AssembleAndWriteDoFn(
            String outputPath,
            int computeTileSize,
            int outputTileSize,
            String compression
        ) {
            this.outputPath = outputPath;
            this.computeTileSize = computeTileSize;
            this.outputTileSize = outputTileSize;
            this.compression = compression;
        }

        @Setup
        public void setup() {
            if (outputPath.startsWith("gs://")) {
                storage = StorageOptions.getDefaultInstance().getService();
            }
        }

        @ProcessElement
        public void processElement(
            @Element KV<OutputTileKey, Iterable<FetchedTile>> element
        ) throws IOException {
            process(element);
        }

        /**
         * The actual @ProcessElement body — exposed package-private so
         * tests can drive the DoFn with a synthetic {@code KV} input
         * without going through Beam's harness or reflection.
         */
        void process(KV<OutputTileKey, Iterable<FetchedTile>> element) throws IOException {
            OutputTileKey key = element.getKey();
            List<FetchedTile> computeTiles = new ArrayList<>();
            for (FetchedTile t : element.getValue()) {
                computeTiles.add(t);
            }
            if (computeTiles.isEmpty()) {
                LOG.warn("Output tile {} has no compute tiles — skipping", key);
                return;
            }

            // All compute tiles in a group share CRS, scale, sample
            // structure (they came from the same EE expression at the
            // same scale). The first tile is a representative.
            FetchedTile first = computeTiles.getFirst();
            byte[] sourceTiff = first.imageBytes();

            // Pixel size in CRS units, derived from the compute tile bbox.
            double pixelNative =
                (first.coordinate().xMax() - first.coordinate().xMin()) / computeTileSize;
            double outputTileSizeNative = pixelNative * outputTileSize;

            // Snap output tile origin to the global output grid (anchored
            // at CRS origin (0,0) — same convention as Python tiling).
            double outputXMin = Math.floor(
                first.coordinate().xMin() / outputTileSizeNative
            ) * outputTileSizeNative;
            double outputYMin = Math.floor(
                first.coordinate().yMin() / outputTileSizeNative
            ) * outputTileSizeNative;
            double outputYMax = outputYMin + outputTileSizeNative;

            // Each compute tile becomes one inner COG block at index
            // (ty * tilesAcross + tx). Build a row-major list of pixel
            // buffers; null entries (missing compute tiles, e.g. dead-
            // lettered upstream) get zero-filled by the transcoder.
            int tilesAcross = outputTileSize / computeTileSize;
            int tilesDown = outputTileSize / computeTileSize;
            List<byte[]> tilePixels = new ArrayList<>(tilesAcross * tilesDown);
            for (int i = 0; i < tilesAcross * tilesDown; i++) {
                tilePixels.add(null);
            }

            for (FetchedTile t : computeTiles) {
                int localXPx = (int) Math.round(
                    (t.coordinate().xMin() - outputXMin) / pixelNative
                );
                int localYPx = (int) Math.round(
                    (outputYMax - t.coordinate().yMax()) / pixelNative
                );
                if (localXPx % computeTileSize != 0
                    || localYPx % computeTileSize != 0) {
                    throw new IOException(
                        "Compute tile " + t.coordinate().id()
                        + " (origin " + localXPx + "," + localYPx
                        + " px within output tile " + key + ") is not"
                        + " block-aligned to computeTileSize=" + computeTileSize
                    );
                }
                int tx = localXPx / computeTileSize;
                int ty = localYPx / computeTileSize;
                if (tx < 0 || ty < 0 || tx >= tilesAcross || ty >= tilesDown) {
                    throw new IOException(
                        "Compute tile " + t.coordinate().id()
                        + " (block " + tx + "," + ty + ") falls outside"
                        + " output tile " + key
                        + " (" + tilesAcross + "x" + tilesDown + " blocks)"
                    );
                }
                tilePixels.set(
                    ty * tilesAcross + tx,
                    CogTranscoder.extractPixelsFromTiff(t.imageBytes())
                );
                computeTilesAssembled.inc();
            }

            byte[] cog = CogTranscoder.transcodeFromTileBlocks(
                tilePixels,
                outputTileSize, outputTileSize,
                computeTileSize,
                sourceTiff,
                outputXMin, outputYMax,
                compression
            );

            String filename = key.filename();
            String destination;
            if (outputPath.startsWith("gs://")) {
                destination = writeToGcs(filename, cog);
            } else {
                destination = writeToLocal(filename, cog);
            }
            LOG.info(
                "Wrote output tile {} ({} compute tiles, {} bytes) to {}",
                key, computeTiles.size(), cog.length, destination
            );
            outputTilesWritten.inc();
        }

        private String writeToGcs(String filename, byte[] data) {
            URI gcsUri = URI.create(outputPath);
            String bucket = gcsUri.getHost();
            String prefix = gcsUri.getPath().replaceAll("^/+|/+$", "");
            String blobName = prefix.isEmpty() ? filename : prefix + "/" + filename;
            BlobId blobId = BlobId.of(bucket, blobName);
            BlobInfo blobInfo = BlobInfo.newBuilder(blobId)
                .setContentType("image/tiff")
                .build();
            storage.create(blobInfo, data);
            return "gs://" + bucket + "/" + blobName;
        }

        private String writeToLocal(String filename, byte[] data) throws IOException {
            Path outDir = Path.of(outputPath);
            Files.createDirectories(outDir);
            Path dest = outDir.resolve(filename);
            Files.write(dest, data);
            return dest.toString();
        }
    }
}
