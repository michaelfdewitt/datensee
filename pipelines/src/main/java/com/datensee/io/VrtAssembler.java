package com.datensee.io;

import com.datensee.FetchedTile;
import com.datensee.TileCoordinate;
import com.google.cloud.storage.BlobId;
import com.google.cloud.storage.BlobInfo;
import com.google.cloud.storage.Storage;
import com.google.cloud.storage.StorageOptions;
import java.io.IOException;
import java.io.Serializable;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import org.apache.beam.sdk.transforms.Combine;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PDone;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Assembles a GDAL VRT manifest from successfully written tiles.
 *
 * <p>Collects tile metadata (coordinates, not pixels) via a global combine,
 * then writes a {@code mosaic.vrt} file to the output path. This lets users
 * open the VRT directly in GDAL or convert it to a single COG.
 *
 * <p>Only runs in Dataflow mode — local mode uses Python-side VRT assembly.
 */
public final class VrtAssembler
    extends PTransform<PCollection<FetchedTile>, PDone> {

    private final String outputPath;
    private final String crs;
    private final int bandCount;
    private final String dataType;
    private final int tileSizePixels;

    public VrtAssembler(
        String outputPath,
        String crs,
        int bandCount,
        String dataType,
        int tileSizePixels
    ) {
        this.outputPath = outputPath;
        this.crs = crs;
        this.bandCount = bandCount;
        this.dataType = dataType;
        this.tileSizePixels = tileSizePixels;
    }

    @Override
    public PDone expand(PCollection<FetchedTile> input) {
        input
            .apply("ExtractTileMetadata", ParDo.of(new ExtractMetadata()))
            .apply("CollectAllTiles", Combine.globally(new TileListCombiner()))
            .apply("WriteVrt", ParDo.of(new WriteVrtDoFn(
                outputPath, crs, bandCount, dataType, tileSizePixels
            )));
        return PDone.in(input.getPipeline());
    }

    /** Extracts the coordinate from a FetchedTile (drops the pixel data). */
    static final class ExtractMetadata extends DoFn<FetchedTile, TileCoordinate> {
        @ProcessElement
        public void processElement(
            @Element FetchedTile tile,
            OutputReceiver<TileCoordinate> out
        ) {
            out.output(tile.coordinate());
        }
    }

    /**
     * Accumulates tile coordinates into a list via Beam's Combine.
     *
     * <p>We only collect metadata (6 numbers per tile), not pixel data,
     * so memory is not a concern even for 100k+ tiles.
     */
    static final class TileListCombiner
        extends Combine.CombineFn<TileCoordinate, TileListCombiner.Accum, List<TileCoordinate>> {

        static final class Accum implements Serializable {
            final java.util.ArrayList<TileCoordinate> tiles = new java.util.ArrayList<>();
        }

        @Override
        public Accum createAccumulator() {
            return new Accum();
        }

        @Override
        public Accum addInput(Accum accum, TileCoordinate tile) {
            accum.tiles.add(tile);
            return accum;
        }

        @Override
        public Accum mergeAccumulators(Iterable<Accum> accums) {
            Accum merged = new Accum();
            for (Accum a : accums) {
                merged.tiles.addAll(a.tiles);
            }
            return merged;
        }

        @Override
        public List<TileCoordinate> extractOutput(Accum accum) {
            return List.copyOf(accum.tiles);
        }
    }

    /** Writes the VRT XML to the output path. */
    static final class WriteVrtDoFn extends DoFn<List<TileCoordinate>, Void> {

        private static final Logger LOG = LoggerFactory.getLogger(WriteVrtDoFn.class);
        private static final Map<String, String> GDAL_DATA_TYPES = Map.of(
            "float32", "Float32",
            "float64", "Float64",
            "int16", "Int16",
            "int32", "Int32",
            "uint8", "Byte",
            "uint16", "UInt16"
        );

        private final String outputPath;
        private final String crs;
        private final int bandCount;
        private final String dataType;
        private final int tileSizePixels;

        private transient Storage storage;

        WriteVrtDoFn(
            String outputPath,
            String crs,
            int bandCount,
            String dataType,
            int tileSizePixels
        ) {
            this.outputPath = outputPath;
            this.crs = crs;
            this.bandCount = bandCount;
            this.dataType = dataType;
            this.tileSizePixels = tileSizePixels;
        }

        @Setup
        public void setup() {
            if (outputPath.startsWith("gs://")) {
                storage = StorageOptions.getDefaultInstance().getService();
            }
        }

        @ProcessElement
        public void processElement(@Element List<TileCoordinate> tiles) throws IOException {
            if (tiles.isEmpty()) {
                LOG.warn("No tiles to assemble into VRT");
                return;
            }

            String vrtXml = buildVrt(tiles);

            if (outputPath.startsWith("gs://")) {
                writeToGcs(vrtXml);
            } else {
                writeToLocal(vrtXml);
            }
        }

        private String buildVrt(List<TileCoordinate> tiles) {
            List<TileCoordinate> sorted = tiles.stream()
                .sorted(Comparator.comparingInt(TileCoordinate::row)
                    .thenComparingInt(TileCoordinate::col))
                .toList();

            double globalXMin = sorted.stream()
                .mapToDouble(TileCoordinate::xMin).min().orElseThrow();
            double globalYMin = sorted.stream()
                .mapToDouble(TileCoordinate::yMin).min().orElseThrow();
            double globalXMax = sorted.stream()
                .mapToDouble(TileCoordinate::xMax).max().orElseThrow();
            double globalYMax = sorted.stream()
                .mapToDouble(TileCoordinate::yMax).max().orElseThrow();

            double tileWidth = sorted.getFirst().xMax() - sorted.getFirst().xMin();
            double tileHeight = sorted.getFirst().yMax() - sorted.getFirst().yMin();
            double pixelWidth = tileWidth / tileSizePixels;
            double pixelHeight = tileHeight / tileSizePixels;

            int rasterXSize = (int) Math.round((globalXMax - globalXMin) / pixelWidth);
            int rasterYSize = (int) Math.round((globalYMax - globalYMin) / pixelHeight);

            String gdalType = GDAL_DATA_TYPES.getOrDefault(dataType, "Float32");

            StringBuilder sb = new StringBuilder();
            sb.append("<VRTDataset rasterXSize=\"").append(rasterXSize)
                .append("\" rasterYSize=\"").append(rasterYSize).append("\">\n");
            sb.append("  <SRS>").append(crs).append("</SRS>\n");
            sb.append(String.format("  <GeoTransform>%f, %f, 0, %f, 0, %f</GeoTransform>%n",
                globalXMin, pixelWidth, globalYMax, -pixelHeight));

            for (int band = 1; band <= bandCount; band++) {
                sb.append(String.format("  <VRTRasterBand dataType=\"%s\" band=\"%d\">%n",
                    gdalType, band));

                for (TileCoordinate tile : sorted) {
                    int xOff = (int) Math.round((tile.xMin() - globalXMin) / pixelWidth);
                    int yOff = (int) Math.round((globalYMax - tile.yMax()) / pixelHeight);
                    String filename = String.format("tile_r%04d_c%04d.tif", tile.row(), tile.col());

                    sb.append("    <SimpleSource>\n");
                    sb.append(String.format(
                        "      <SourceFilename relativeToVRT=\"1\">%s</SourceFilename>%n",
                        filename));
                    sb.append(String.format("      <SourceBand>%d</SourceBand>%n", band));
                    sb.append(String.format(
                        "      <SrcRect xOff=\"0\" yOff=\"0\" xSize=\"%d\" ySize=\"%d\" />%n",
                        tileSizePixels, tileSizePixels));
                    sb.append(String.format(
                        "      <DstRect xOff=\"%d\" yOff=\"%d\" xSize=\"%d\" ySize=\"%d\" />%n",
                        xOff, yOff, tileSizePixels, tileSizePixels));
                    sb.append("    </SimpleSource>\n");
                }

                sb.append("  </VRTRasterBand>\n");
            }

            sb.append("</VRTDataset>\n");
            return sb.toString();
        }

        private void writeToGcs(String vrtXml) {
            URI gcsUri = URI.create(outputPath);
            String bucket = gcsUri.getHost();
            String prefix = gcsUri.getPath().replaceFirst("^/", "");
            String blobName = prefix.isEmpty() ? "mosaic.vrt" : prefix + "/mosaic.vrt";

            BlobId blobId = BlobId.of(bucket, blobName);
            BlobInfo blobInfo = BlobInfo.newBuilder(blobId)
                .setContentType("application/xml")
                .build();

            storage.create(blobInfo, vrtXml.getBytes(StandardCharsets.UTF_8));
            LOG.info("Wrote VRT manifest to gs://{}/{}", bucket, blobName);
        }

        private void writeToLocal(String vrtXml) throws IOException {
            Path outDir = Path.of(outputPath);
            Files.createDirectories(outDir);
            Path dest = outDir.resolve("mosaic.vrt");
            Files.writeString(dest, vrtXml, StandardCharsets.UTF_8);
            LOG.info("Wrote VRT manifest to {}", dest);
        }
    }
}
