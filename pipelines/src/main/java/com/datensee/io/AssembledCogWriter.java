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
 * <p>Failure handling: when one or more compute tiles in a group are
 * absent (either dropped to the dead-letter PCollection upstream, or
 * never present because the compute tile didn't intersect the export
 * region), the assembler still emits the output COG with zero-filled
 * blocks at the missing positions — partial data is more useful than
 * no data. To make the holes <em>visible</em> rather than silent, the
 * assembler:
 *
 * <ul>
 *   <li>increments the {@code output_tiles_partial} Beam counter for each
 *       affected output tile;</li>
 *   <li>logs a {@code WARN} listing the missing block positions; and</li>
 *   <li>writes a sidecar {@code <filename>.partial.json} alongside the
 *       COG enumerating the missing block coordinates so a downstream
 *       reader can cross-reference against the failures journal.</li>
 * </ul>
 *
 * <p>The number of compute tiles in a complete group depends on whether
 * the output tile lies on the region edge, so the assembler can't
 * distinguish "edge tile, fewer compute tiles by design" from "interior
 * tile with fetch failures" on its own — that disambiguation is the
 * caller's job (cross-reference partial sidecars with {@code _failures.json}).
 */
public final class AssembledCogWriter
    extends PTransform<PCollection<FetchedTile>, PDone> {

    private static final Logger LOG = LoggerFactory.getLogger(AssembledCogWriter.class);

    /**
     * How far a compute-tile's bbox-derived block origin may drift, in
     * pixels, from a perfect block boundary before the assembler treats
     * it as a misaligned tile. With CRS coordinates in millions
     * (UTM/equal-area projections at small scales), {@code Math.round}
     * still produces stable integer block indices for any drift well
     * below 1 pixel — but small ULP differences from float arithmetic
     * shouldn't trip the alignment guard. 0.001 px = ~3 cm at 30 m/px
     * resolution, which is comfortably tighter than any real-world
     * misalignment but loose enough to absorb double-precision noise.
     */
    static final double TILE_ALIGNMENT_TOLERANCE_PX = 1e-3;

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
        // The (outputTileSize % computeTileSize == 0) invariant is also
        // enforced at the wire boundary by PipelineConfig's pydantic
        // validator. We re-check here because Java has multiple potential
        // clients (CLI-driven pipelines today; Cloud Run / FoundrEE bridge
        // tomorrow) and the assembler's block math assumes a clean
        // multiple — `transcodeFromTileBlocks` only checks the weaker
        // (width % tileSize == 0) invariant.
        if (computeTileSize <= 0) {
            throw new IllegalArgumentException(
                "computeTileSize must be positive, got " + computeTileSize
            );
        }
        if (outputTileSize <= 0 || outputTileSize % computeTileSize != 0) {
            throw new IllegalArgumentException(
                "outputTileSize (" + outputTileSize + ") must be a positive multiple of "
                + "computeTileSize (" + computeTileSize + "); got remainder "
                + (outputTileSize % computeTileSize)
            );
        }
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
        private final Counter outputTilesPartial = Metrics.counter("datensee", "output_tiles_partial");
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
                // Compute the bbox-derived position in pixels and verify
                // both that the float drift from a perfect integer is
                // within tolerance (catches off-grid bboxes) and that
                // the rounded position is a clean multiple of the
                // compute tile size (catches off-block bboxes).
                double rawXPx = (t.coordinate().xMin() - outputXMin) / pixelNative;
                double rawYPx = (outputYMax - t.coordinate().yMax()) / pixelNative;
                int localXPx = (int) Math.round(rawXPx);
                int localYPx = (int) Math.round(rawYPx);
                if (Math.abs(rawXPx - localXPx) > TILE_ALIGNMENT_TOLERANCE_PX
                    || Math.abs(rawYPx - localYPx) > TILE_ALIGNMENT_TOLERANCE_PX) {
                    throw new IOException(
                        "Compute tile " + t.coordinate().id()
                        + " bbox is not pixel-aligned within output tile " + key
                        + " (raw position " + rawXPx + "," + rawYPx + " px;"
                        + " tolerance " + TILE_ALIGNMENT_TOLERANCE_PX + " px)."
                        + " Check that scale and CRS match the original export."
                    );
                }
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

            // Surface zero-filled blocks so partial groups are visible
            // rather than silent. We can't tell "edge tile (legitimately
            // sparse)" from "interior tile with fetch failures" here —
            // that's a downstream cross-reference against _failures.json.
            List<int[]> missingBlocks = new ArrayList<>();
            for (int ty = 0; ty < tilesDown; ty++) {
                for (int tx = 0; tx < tilesAcross; tx++) {
                    if (tilePixels.get(ty * tilesAcross + tx) == null) {
                        missingBlocks.add(new int[] {tx, ty});
                    }
                }
            }

            String filename = key.filename();
            String destination;
            if (outputPath.startsWith("gs://")) {
                destination = writeToGcs(filename, cog);
            } else {
                destination = writeToLocal(filename, cog);
            }
            if (missingBlocks.isEmpty()) {
                LOG.info(
                    "Wrote output tile {} ({} compute tiles, {} bytes) to {}",
                    key, computeTiles.size(), cog.length, destination
                );
            } else {
                LOG.warn(
                    "Wrote PARTIAL output tile {} ({} compute tiles, {} bytes, "
                    + "{} blocks zero-filled) to {}; see {}.partial.json",
                    key, computeTiles.size(), cog.length, missingBlocks.size(),
                    destination, filename
                );
                outputTilesPartial.inc();
                writePartialSidecar(filename, key, missingBlocks);
            }
            outputTilesWritten.inc();
        }

        private void writePartialSidecar(
            String filename, OutputTileKey key, List<int[]> missingBlocks
        ) throws IOException {
            StringBuilder json = new StringBuilder();
            json.append("{\"output_tile\":\"").append(filename).append("\",")
                .append("\"out_row\":").append(key.outRow()).append(",")
                .append("\"out_col\":").append(key.outCol()).append(",")
                .append("\"missing_blocks\":[");
            for (int i = 0; i < missingBlocks.size(); i++) {
                if (i > 0) {
                    json.append(",");
                }
                int[] tc = missingBlocks.get(i);
                json.append("{\"tx\":").append(tc[0]).append(",\"ty\":").append(tc[1]).append("}");
            }
            json.append("]}\n");
            byte[] data = json.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8);
            String sidecarName = filename + ".partial.json";
            if (outputPath.startsWith("gs://")) {
                writeToGcs(sidecarName, data);
            } else {
                writeToLocal(sidecarName, data);
            }
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
