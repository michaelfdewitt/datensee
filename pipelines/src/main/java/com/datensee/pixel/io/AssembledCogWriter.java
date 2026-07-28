package com.datensee.pixel.io;

import com.datensee.fetch.EeErrorKind;
import com.datensee.pixel.AffineTransform;
import com.datensee.pixel.FailedTileRecord;
import com.datensee.pixel.FetchedTile;
import com.datensee.pixel.OutputTileKey;
import com.datensee.pixel.PixelGrid;
import com.google.cloud.storage.Blob;
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
import org.apache.beam.sdk.values.TypeDescriptor;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * two-tier-tiling output writer.
 *
 * <p>Pipeline shape:
 * <pre>
 *   PCollection&lt;FetchedTile&gt;
 *     ─▶ KeyByOutputTile     (KV&lt;OutputTileKey, FetchedTile&gt;)
 *     ─▶ GroupByKey          (KV&lt;OutputTileKey, Iterable&lt;FetchedTile&gt;&gt;)
 *     ─▶ AssembleAndWriteDoFn
 *         ├─ read existing output COG as the baseline canvas (retry merge)
 *         ├─ overlay each fetched tile's pixels at its integer offset
 *         ├─ CogTranscoder.transcodeFromPixelBuffer(...)
 *         └─ write COG to GCS or local
 *     ─▶ PCollection&lt;FailedTileRecord&gt;   (write-stage dead letters)
 * </pre>
 *
 * <p>The output COG's internal block size is the compute tile size, so
 * EE's {@code Image.loadGeoTIFF} reads remain efficient even for large
 * output COGs.
 *
 * <p><strong>Origin contract:</strong> the Python tiler snaps the parent
 * {@link PixelGrid} origin to <em>output</em>-tile boundaries whenever two-tier
 * mode is on, and assigns {@code outRow}/{@code outCol} as
 * {@code rowPx / outputTileSize} / {@code colPx / outputTileSize} in local
 * parent-grid coordinates. The output tile {@code (outRow, outCol)}
 * therefore occupies exactly the local pixel rect
 * {@code [outCol·S, outRow·S, S, S]} with {@code S = outputTileSize}, and
 * this writer derives every group's origin from its key — independent of
 * group iteration order. Tiles outside their key's rect indicate a broken
 * tiler and dead-letter the group.
 *
 * <p><strong>Retry merge:</strong> when the destination COG already exists
 * (a {@code datensee retry} round re-fetching part of a previously-written
 * output tile), its pixels are decoded and used as the baseline canvas, so
 * re-fetched tiles — including sub-block quadtree split children — overlay
 * the existing data instead of replacing the whole file with zero-filled
 * gaps. Fresh exports start from a zero canvas; fully-missing blocks are
 * therefore zero-filled, counted on the {@code output_tiles_partial}
 * counter, and logged. {@code _failures.json} is the canonical record of
 * missing data — there is no per-file sidecar.
 *
 * <p><strong>Failure handling:</strong> any exception while assembling or
 * writing a group emits one {@link FailedTileRecord} per member tile
 * (kind {@code UNKNOWN}, message prefixed {@code write-stage:}) instead of
 * failing the bundle, keeping partial-failure semantics symmetric with the
 * fetch stage.
 */
public final class AssembledCogWriter
    extends PTransform<PCollection<FetchedTile>, PCollection<FailedTileRecord>> {

    private static final Logger LOG = LoggerFactory.getLogger(AssembledCogWriter.class);

    private final String outputPath;
    private final PixelGrid parentGrid;
    private final int computeTileSize;
    private final int outputTileSize;
    private final String compression;
    private final boolean mergeExistingOutput;
    private final Double nodata;

    public AssembledCogWriter(
        String outputPath,
        PixelGrid parentGrid,
        int computeTileSize,
        int outputTileSize,
        String compression,
        boolean mergeExistingOutput,
        Double nodata
    ) {
        // The (outputTileSize % computeTileSize == 0) invariant is also
        // enforced at the wire boundary by PipelineConfig's pydantic
        // validator. We re-check here because Java has multiple potential
        // clients and the assembler's block math assumes a clean multiple.
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
        this.parentGrid = parentGrid;
        this.computeTileSize = computeTileSize;
        this.outputTileSize = outputTileSize;
        this.compression = compression;
        this.mergeExistingOutput = mergeExistingOutput;
        this.nodata = nodata;
    }

    @Override
    public PCollection<FailedTileRecord> expand(PCollection<FetchedTile> input) {
        return input
            .apply("KeyByOutputTile", WithKeys.of(
                (FetchedTile t) -> new OutputTileKey(t.coordinate().outRow(), t.coordinate().outCol())
            ).withKeyType(TypeDescriptor.of(OutputTileKey.class)))
            // Dataflow's GroupByKey requires a deterministic key coder;
            // Java's SerializableCoder is non-deterministic by default. We
            // pair our deterministic OutputTileKeyCoder with the standard
            // SerializableCoder for the FetchedTile value (records carry
            // raw GeoTIFF bytes, which are already byte-stable).
            .setCoder(org.apache.beam.sdk.coders.KvCoder.of(
                OutputTileKey.OutputTileKeyCoder.of(),
                SerializableCoder.of(FetchedTile.class)
            ))
            .apply("GroupByOutputTile", GroupByKey.create())
            .apply("AssembleAndWrite", ParDo.of(new AssembleAndWriteDoFn(
                outputPath, parentGrid, computeTileSize, outputTileSize, compression,
                mergeExistingOutput, nodata
            )))
            .setCoder(SerializableCoder.of(FailedTileRecord.class));
    }

    static final class AssembleAndWriteDoFn
        extends DoFn<KV<OutputTileKey, Iterable<FetchedTile>>, FailedTileRecord> {

        private final String outputPath;
        private final PixelGrid parentGrid;
        private final int computeTileSize;
        private final int outputTileSize;
        private final String compression;
        private final boolean mergeExistingOutput;
        private final Double nodata;

        private transient Storage storage;
        private final Counter outputTilesWritten = Metrics.counter("datensee", "output_tiles_written");
        private final Counter outputTilesPartial = Metrics.counter("datensee", "output_tiles_partial");
        private final Counter outputTilesMerged = Metrics.counter("datensee", "output_tiles_merged");
        private final Counter computeTilesAssembled = Metrics.counter("datensee", "compute_tiles_assembled");
        private final Counter writeFailures = Metrics.counter("datensee", "write_failures");

        AssembleAndWriteDoFn(
            String outputPath,
            PixelGrid parentGrid,
            int computeTileSize,
            int outputTileSize,
            String compression,
            boolean mergeExistingOutput,
            Double nodata
        ) {
            this.outputPath = outputPath;
            this.parentGrid = parentGrid;
            this.computeTileSize = computeTileSize;
            this.outputTileSize = outputTileSize;
            this.compression = compression;
            this.mergeExistingOutput = mergeExistingOutput;
            this.nodata = nodata;
        }

        @Setup
        public void setup() {
            if (outputPath.startsWith("gs://")) {
                storage = StorageOptions.getDefaultInstance().getService();
            }
        }

        @ProcessElement
        public void processElement(
            @Element KV<OutputTileKey, Iterable<FetchedTile>> element,
            OutputReceiver<FailedTileRecord> out
        ) {
            for (FailedTileRecord record : process(element)) {
                out.output(record);
            }
        }

        /**
         * The actual @ProcessElement body — exposed package-private so
         * tests can drive the DoFn with a synthetic {@code KV} input
         * without going through Beam's harness or reflection. Returns the
         * write-stage dead letters for the group (empty on success).
         */
        List<FailedTileRecord> process(KV<OutputTileKey, Iterable<FetchedTile>> element) {
            OutputTileKey key = element.getKey();
            List<FetchedTile> computeTiles = new ArrayList<>();
            for (FetchedTile t : element.getValue()) {
                computeTiles.add(t);
            }
            if (computeTiles.isEmpty()) {
                LOG.warn("Output tile {} has no compute tiles — skipping", key);
                return List.of();
            }
            try {
                assembleAndWrite(key, computeTiles);
                return List.of();
            } catch (Exception e) {
                writeFailures.inc();
                LOG.error(
                    "Output tile {}: write stage failed, dead-lettering {} compute tile(s): {}",
                    key, computeTiles.size(), e.toString()
                );
                List<FailedTileRecord> records = new ArrayList<>(computeTiles.size());
                for (FetchedTile t : computeTiles) {
                    records.add(FailedTileRecord.fromTileWithError(
                        t.coordinate(),
                        EeErrorKind.UNKNOWN,
                        "write-stage: " + e,
                        null,
                        1
                    ));
                }
                return records;
            }
        }

        private void assembleAndWrite(OutputTileKey key, List<FetchedTile> computeTiles)
            throws IOException {
            // All compute tiles in a group share CRS, scale, sample
            // structure (they came from the same EE expression at the
            // same scale). The first tile is a representative for
            // metadata and pixel layout.
            FetchedTile first = computeTiles.getFirst();
            byte[] sourceTiff = first.imageBytes();
            CogTranscoder.TiffPixelLayout layout = CogTranscoder.readPixelLayout(sourceTiff);
            int bytesPerPixel = layout.bytesPerPixel();

            // Origin from the key — see the class-level origin contract.
            int outputColPx = key.outCol() * outputTileSize;
            int outputRowPx = key.outRow() * outputTileSize;

            // Baseline canvas: the existing output COG when present (retry
            // merge), a zero canvas otherwise.
            int canvasRowBytes = outputTileSize * bytesPerPixel;
            byte[] canvas;
            byte[] existing = mergeExistingOutput ? readExisting(key.filename()) : null;
            boolean merged = existing != null;
            if (merged) {
                CogTranscoder.TiffPixelLayout existingLayout =
                    CogTranscoder.readPixelLayout(existing);
                if (existingLayout.width() != outputTileSize
                    || existingLayout.height() != outputTileSize
                    || existingLayout.bytesPerPixel() != bytesPerPixel) {
                    throw new IOException(
                        "Existing output COG " + key.filename() + " has layout "
                        + existingLayout + " but this run produces "
                        + outputTileSize + "x" + outputTileSize + " at "
                        + bytesPerPixel + " bytes/pixel. The retry's shape args "
                        + "don't match the export that wrote the file — check "
                        + "_export_meta.json, or remove the stale file."
                    );
                }
                canvas = CogTranscoder.extractPixelsFromTiff(existing);
            } else {
                canvas = new byte[outputTileSize * canvasRowBytes];
            }

            // Overlay each fetched tile at its integer pixel offset. Root
            // tiles cover whole blocks; quadtree split children cover
            // aligned sub-rectangles of one block. Coverage is tracked
            // per-pixel-row-area only to report fully-empty blocks below.
            long[] blockCoveredPixels = new long[
                (outputTileSize / computeTileSize) * (outputTileSize / computeTileSize)
            ];
            int tilesAcross = outputTileSize / computeTileSize;
            for (FetchedTile t : computeTiles) {
                int localX = t.coordinate().colPx() - outputColPx;
                int localY = t.coordinate().rowPx() - outputRowPx;
                int w = t.coordinate().widthPx();
                int h = t.coordinate().heightPx();
                if (localX < 0 || localY < 0
                    || localX + w > outputTileSize || localY + h > outputTileSize) {
                    throw new IOException(
                        "Compute tile " + t.coordinate().id() + " (local " + localX
                        + "," + localY + " size " + w + "x" + h + ") falls outside"
                        + " output tile " + key + " (" + outputTileSize + "px)."
                        + " The parent grid origin is not output-tile-aligned —"
                        + " re-export with a current datensee CLI."
                    );
                }
                byte[] tilePixels = CogTranscoder.extractPixelsFromTiff(t.imageBytes());
                long expected = (long) w * h * bytesPerPixel;
                if (tilePixels.length != expected) {
                    throw new IOException(
                        "Compute tile " + t.coordinate().id() + " decoded to "
                        + tilePixels.length + " bytes; expected " + expected
                        + " (" + w + "x" + h + " x " + bytesPerPixel + " bytes/pixel)"
                    );
                }
                int tileRowBytes = w * bytesPerPixel;
                for (int dy = 0; dy < h; dy++) {
                    System.arraycopy(
                        tilePixels, dy * tileRowBytes,
                        canvas, (localY + dy) * canvasRowBytes + localX * bytesPerPixel,
                        tileRowBytes
                    );
                }
                // Attribute covered area to the (single) block the rect
                // falls in — children never straddle blocks because they
                // subdivide exactly one root tile.
                int bx = localX / computeTileSize;
                int by = localY / computeTileSize;
                blockCoveredPixels[by * tilesAcross + bx] += (long) w * h;
                computeTilesAssembled.inc();
            }

            AffineTransform p = parentGrid.affineTransform();
            AffineTransform outputAffine = new AffineTransform(
                p.scaleX(),
                p.shearX(),
                p.translateX() + outputColPx * p.scaleX() + outputRowPx * p.shearX(),
                p.shearY(),
                p.scaleY(),
                p.translateY() + outputColPx * p.shearY() + outputRowPx * p.scaleY()
            );

            byte[] cog = CogTranscoder.transcodeFromPixelBuffer(
                canvas,
                outputTileSize, outputTileSize,
                computeTileSize,
                sourceTiff,
                outputAffine,
                compression,
                nodata
            );

            // Fully-uncovered blocks in a fresh (non-merge) write are
            // zero-filled holes: count + log them so partial groups are
            // visible. _failures.json is the canonical record of exactly
            // which tiles are missing — cross-reference there.
            int emptyBlocks = 0;
            if (!merged) {
                for (long covered : blockCoveredPixels) {
                    if (covered == 0) {
                        emptyBlocks++;
                    }
                }
            }

            String filename = key.filename();
            String destination = outputPath.startsWith("gs://")
                ? writeToGcs(filename, cog)
                : writeToLocal(filename, cog);
            if (merged) {
                outputTilesMerged.inc();
                LOG.info(
                    "Merged {} fetched tile(s) into existing output tile {} ({} bytes) at {}",
                    computeTiles.size(), key, cog.length, destination
                );
            } else if (emptyBlocks > 0) {
                outputTilesPartial.inc();
                LOG.warn(
                    "Wrote PARTIAL output tile {} ({} compute tiles, {} bytes, "
                    + "{} block(s) zero-filled) to {}; see _failures.json for the "
                    + "missing tiles",
                    key, computeTiles.size(), cog.length, emptyBlocks, destination
                );
            } else {
                LOG.info(
                    "Wrote output tile {} ({} compute tiles, {} bytes) to {}",
                    key, computeTiles.size(), cog.length, destination
                );
            }
            outputTilesWritten.inc();
        }

        /** Read the existing destination COG, or {@code null} if absent. */
        private byte[] readExisting(String filename) throws IOException {
            if (outputPath.startsWith("gs://")) {
                URI gcsUri = URI.create(outputPath);
                String prefix = gcsUri.getPath().replaceAll("^/+|/+$", "");
                String blobName = prefix.isEmpty() ? filename : prefix + "/" + filename;
                Blob blob = storage.get(BlobId.of(gcsUri.getHost(), blobName));
                return blob != null ? blob.getContent() : null;
            }
            Path dest = Path.of(outputPath).resolve(filename);
            return Files.exists(dest) ? Files.readAllBytes(dest) : null;
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
