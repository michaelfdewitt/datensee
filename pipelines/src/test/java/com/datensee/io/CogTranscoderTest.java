package com.datensee.io;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.zip.Deflater;
import org.junit.jupiter.api.Test;

/**
 * Tests for {@link CogTranscoder}.
 *
 * <p>The structural tests pin the layout invariants EE's
 * {@code Image.loadGeoTIFF} validator checks (first IFD at offset 8,
 * TileOffsets pointing past the IFD into pixel data).
 *
 * <p>The round-trip tests build synthetic GeoTIFFs that mimic what the
 * EE HV API returns, transcode them, and decode the result with a
 * test-only TIFF reader (deliberately independent of {@link CogTranscoder}'s
 * own decoder paths) to verify that pixel data round-trips exactly across
 * data types (uint8, uint16, int16, float32), band counts, and
 * compression schemes (none, deflate). LZW is intentionally not tested
 * end-to-end — the encoder is known broken (see
 * {@code DatensEEPipeline}'s "deflate default" comment) and is kept only
 * for future replacement.
 */
class CogTranscoderTest {

    // -----------------------------------------------------------------------
    // Structural invariants
    // -----------------------------------------------------------------------

    @Test
    void transcodedFileHasFirstIfdAtOffsetEight() throws Exception {
        byte[] raw = synthesizeUint8Geotiff(4, 4, 1);
        byte[] cog = CogTranscoder.transcode(raw, 4, "none");

        ByteBuffer hdr = ByteBuffer.wrap(cog).order(ByteOrder.LITTLE_ENDIAN);
        short magic = hdr.getShort(2);
        assertEquals(42, magic, "TIFF magic number");
        int ifdOffset = hdr.getInt(4);
        assertEquals(
            8,
            ifdOffset,
            "COG first IFD must immediately follow the 8-byte TIFF header; "
                + "EE rejects COGs where pixel data precedes the IFD.");
    }

    @Test
    void tileOffsetsPointPastTheIfdIntoPixelData() throws Exception {
        byte[] raw = synthesizeUint8Geotiff(4, 4, 1);
        byte[] cog = CogTranscoder.transcode(raw, 4, "none");

        TestTiffReader reader = new TestTiffReader(cog);
        long tileOffset = reader.tileOffsets()[0];
        int ifdSize = 2 + reader.entryCount() * 12 + 4;
        assertTrue(
            tileOffset >= 8 + ifdSize,
            "Pixel data offset (" + tileOffset
                + ") must land after the IFD (ends at " + (8 + ifdSize) + ")");
    }

    @Test
    void outputDeclaresTileLayoutAndDropsStripTags() throws Exception {
        byte[] raw = synthesizeUint8Geotiff(8, 8, 1);
        byte[] cog = CogTranscoder.transcode(raw, 8, "none");

        TestTiffReader reader = new TestTiffReader(cog);
        assertEquals(8, reader.intTag(322), "TileWidth");
        assertEquals(8, reader.intTag(323), "TileLength");
        assertNotNull(reader.entry(324), "TileOffsets must be present");
        assertNotNull(reader.entry(325), "TileByteCounts must be present");
        // Strip tags must be absent — the COG is tile-layout.
        org.junit.jupiter.api.Assertions.assertNull(
            reader.entry(273), "StripOffsets must be removed");
        org.junit.jupiter.api.Assertions.assertNull(
            reader.entry(279), "StripByteCounts must be removed");
        org.junit.jupiter.api.Assertions.assertNull(
            reader.entry(278), "RowsPerStrip must be removed");
    }

    // -----------------------------------------------------------------------
    // End-to-end pixel-integrity round-trips
    // -----------------------------------------------------------------------

    @Test
    void uint8SingleBandRoundTripsWithDeflate() throws Exception {
        int size = 16;
        byte[] pixels = sequentialBytes(size * size);
        byte[] raw = synthesizeGeotiff(size, size, 1, 8, /*sampleFormat=*/1, pixels);
        byte[] cog = CogTranscoder.transcode(raw, size, "deflate");

        byte[] decoded = decodeCogPixels(cog);
        assertArrayEquals(pixels, decoded, "uint8 pixel data must round-trip exactly");
    }

    @Test
    void uint8SingleBandRoundTripsWithNoCompression() throws Exception {
        int size = 8;
        byte[] pixels = sequentialBytes(size * size);
        byte[] raw = synthesizeGeotiff(size, size, 1, 8, 1, pixels);
        byte[] cog = CogTranscoder.transcode(raw, size, "none");

        byte[] decoded = decodeCogPixels(cog);
        assertArrayEquals(pixels, decoded, "uncompressed pixel data must round-trip");
    }

    @Test
    void uint16SingleBandRoundTripsWithDeflate() throws Exception {
        int size = 8;
        byte[] pixels = new byte[size * size * 2];
        ByteBuffer pb = ByteBuffer.wrap(pixels).order(ByteOrder.LITTLE_ENDIAN);
        for (int i = 0; i < size * size; i++) {
            pb.putShort((short) (i * 257));
        }
        byte[] raw = synthesizeGeotiff(size, size, 1, 16, /*sampleFormat=*/1, pixels);
        byte[] cog = CogTranscoder.transcode(raw, size, "deflate");

        byte[] decoded = decodeCogPixels(cog);
        assertArrayEquals(pixels, decoded, "uint16 pixel data must round-trip exactly");
    }

    @Test
    void int16SingleBandRoundTripsWithDeflate() throws Exception {
        int size = 4;
        byte[] pixels = new byte[size * size * 2];
        ByteBuffer pb = ByteBuffer.wrap(pixels).order(ByteOrder.LITTLE_ENDIAN);
        for (int i = 0; i < size * size; i++) {
            pb.putShort((short) (i * 100 - 800));
        }
        byte[] raw = synthesizeGeotiff(size, size, 1, 16, /*sampleFormat=*/2, pixels);
        byte[] cog = CogTranscoder.transcode(raw, size, "deflate");

        byte[] decoded = decodeCogPixels(cog);
        assertArrayEquals(pixels, decoded, "int16 pixel data must round-trip exactly");
    }

    @Test
    void float32SingleBandRoundTripsWithDeflate() throws Exception {
        int size = 8;
        byte[] pixels = new byte[size * size * 4];
        ByteBuffer pb = ByteBuffer.wrap(pixels).order(ByteOrder.LITTLE_ENDIAN);
        for (int i = 0; i < size * size; i++) {
            pb.putFloat(i * 0.125f - 4.0f);
        }
        byte[] raw = synthesizeGeotiff(size, size, 1, 32, /*sampleFormat=*/3, pixels);
        byte[] cog = CogTranscoder.transcode(raw, size, "deflate");

        byte[] decoded = decodeCogPixels(cog);
        assertArrayEquals(pixels, decoded, "float32 pixel data must round-trip exactly");
    }

    @Test
    void multiBandUint8RoundTripsWithDeflate() throws Exception {
        int size = 8;
        int bands = 3;
        byte[] pixels = sequentialBytes(size * size * bands);
        byte[] raw = synthesizeGeotiff(size, size, bands, 8, 1, pixels);
        byte[] cog = CogTranscoder.transcode(raw, size, "deflate");

        byte[] decoded = decodeCogPixels(cog);
        assertArrayEquals(pixels, decoded, "multi-band uint8 must round-trip");
    }

    @Test
    void deflateInputRoundTripsToCog() throws Exception {
        // EE HV may return deflate-compressed inputs in some configurations.
        // Verify the input-decompression path produces correct pixels.
        int size = 8;
        byte[] pixels = sequentialBytes(size * size);
        byte[] compressedInput = deflateBytes(pixels);
        byte[] raw = synthesizeGeotiffWithCompression(
            size, size, 1, 8, 1, compressedInput, /*compression=*/8 // deflate
        );
        byte[] cog = CogTranscoder.transcode(raw, size, "deflate");

        byte[] decoded = decodeCogPixels(cog);
        assertArrayEquals(pixels, decoded, "deflate-input pixel data must round-trip");
    }

    @Test
    void multiBlockCogWith256InnerTilesOf16x16RoundTripsExactly() throws Exception {
        // M6 validation case: 256 inner tiles of 16x16 pixels arranged in a
        // 16x16 grid → a 256x256 multi-block COG. Pixels are constructed so
        // each compute tile is identifiable: (out_row, out_col, ix, iy) →
        // a deterministic byte. Any block-ordering bug would surface as a
        // checkerboarded mismatch.
        int innerSize = 16;
        int tilesPerSide = 16;
        int outerSize = innerSize * tilesPerSide;  // 256
        int totalTiles = tilesPerSide * tilesPerSide;  // 256

        byte[] pixels = new byte[outerSize * outerSize];
        for (int y = 0; y < outerSize; y++) {
            for (int x = 0; x < outerSize; x++) {
                int blockY = y / innerSize;
                int blockX = x / innerSize;
                int withinBlockY = y % innerSize;
                int withinBlockX = x % innerSize;
                // Pack a 4-field signature into one byte so each pixel is unique
                // within its block, and each block's signature differs from its
                // neighbors.
                pixels[y * outerSize + x] = (byte) (
                    (blockY * 71 + blockX * 31 + withinBlockY * 13 + withinBlockX * 7) & 0xFF
                );
            }
        }

        byte[] raw = synthesizeGeotiff(outerSize, outerSize, 1, 8, /*sampleFormat=*/1, pixels);
        byte[] cog = CogTranscoder.transcode(raw, innerSize, "deflate");

        TestTiffReader reader = new TestTiffReader(cog);
        assertEquals(innerSize, reader.intTag(322), "TileWidth must equal compute tile size");
        assertEquals(innerSize, reader.intTag(323), "TileLength must equal compute tile size");
        assertEquals(totalTiles, reader.entry(324).count, "TileOffsets must have one entry per inner block");
        assertEquals(totalTiles, reader.entry(325).count, "TileByteCounts must have one entry per inner block");

        byte[] decoded = decodeCogPixels(cog);
        assertArrayEquals(
            pixels, decoded,
            "All 256x256 pixels must round-trip through the multi-block COG"
        );
    }

    @Test
    void multiBlockCogFloat32RoundTripsExactly() throws Exception {
        // Smaller multi-block float32 case to pin the predictor=NONE path
        // for non-integer types alongside the larger uint8 case.
        int innerSize = 8;
        int tilesAcross = 4;
        int tilesDown = 2;
        int width = innerSize * tilesAcross;   // 32
        int height = innerSize * tilesDown;    // 16

        byte[] pixels = new byte[width * height * 4];
        ByteBuffer pb = ByteBuffer.wrap(pixels).order(ByteOrder.LITTLE_ENDIAN);
        for (int y = 0; y < height; y++) {
            for (int x = 0; x < width; x++) {
                pb.putFloat(x * 0.125f - y * 0.0625f);
            }
        }

        byte[] raw = synthesizeGeotiff(width, height, 1, 32, /*sampleFormat=*/3, pixels);
        byte[] cog = CogTranscoder.transcode(raw, innerSize, "deflate");

        TestTiffReader reader = new TestTiffReader(cog);
        assertEquals(tilesAcross * tilesDown, reader.entry(324).count, "block count");
        byte[] decoded = decodeCogPixels(cog);
        assertArrayEquals(pixels, decoded, "float32 multi-block COG must round-trip exactly");
    }

    @Test
    void preservesGeoTiffMetadataTags() throws Exception {
        // Build a TIFF that includes ModelPixelScale (33550) + ModelTiepoint (33922) —
        // these are the bare-minimum tags Earth Engine looks for to georeference
        // the COG via loadGeoTIFF.
        int size = 4;
        byte[] pixels = sequentialBytes(size * size);
        double[] pixelScale = {30.0, 30.0, 0.0};
        double[] tiepoint = {0, 0, 0, -120.5, 37.5, 0};
        byte[] raw = synthesizeGeotiffWithGeoTags(size, size, pixels, pixelScale, tiepoint);

        byte[] cog = CogTranscoder.transcode(raw, size, "deflate");
        TestTiffReader reader = new TestTiffReader(cog);
        double[] outScale = reader.doubleArrayTag(33550);
        double[] outTie = reader.doubleArrayTag(33922);
        assertArrayEquals(pixelScale, outScale, 0.0, "ModelPixelScale must round-trip");
        assertArrayEquals(tiepoint, outTie, 0.0, "ModelTiepoint must round-trip");
    }

    // -----------------------------------------------------------------------
    // Error paths
    // -----------------------------------------------------------------------

    @Test
    void rejectsImageDimensionsThatArentAMultipleOfTileSize() throws Exception {
        // 12x12 image with tileSize=8 would need partial-edge tile padding,
        // which we don't implement. Reject with a clear error.
        byte[] raw = synthesizeUint8Geotiff(12, 12, 1);
        IOException ex = assertThrows(
            IOException.class,
            () -> CogTranscoder.transcode(raw, 8, "deflate"),
            "transcode must reject dimensions that aren't a multiple of tileSize");
        assertTrue(
            ex.getMessage().contains("not a whole-number multiple of tileSize"),
            "error message should explain the dimension mismatch: " + ex.getMessage());
    }

    @Test
    void rejectsPlanarConfiguration() throws Exception {
        int size = 4;
        byte[] pixels = sequentialBytes(size * size * 3);
        byte[] raw = synthesizeGeotiffWithPlanarConfig(size, size, 3, pixels, /*planar=*/2);
        IOException ex = assertThrows(
            IOException.class,
            () -> CogTranscoder.transcode(raw, size, "deflate"),
            "transcode must reject planar configuration");
        assertTrue(
            ex.getMessage().contains("PlanarConfiguration"),
            "error message should mention PlanarConfiguration: " + ex.getMessage());
    }

    @Test
    void rejectsNonTiffInput() {
        byte[] notTiff = new byte[]{'P', 'N', 'G', 0, 0, 0, 0, 0, 0, 0, 0, 0};
        assertThrows(IOException.class, () -> CogTranscoder.transcode(notTiff, 8, "none"));
    }

    // -----------------------------------------------------------------------
    // Synthetic GeoTIFF builders
    // -----------------------------------------------------------------------

    private static byte[] synthesizeUint8Geotiff(int width, int height, int bands) {
        return synthesizeGeotiff(
            width, height, bands, 8, 1, sequentialBytes(width * height * bands)
        );
    }

    private static byte[] sequentialBytes(int n) {
        byte[] out = new byte[n];
        for (int i = 0; i < n; i++) {
            out[i] = (byte) (i & 0xFF);
        }
        return out;
    }

    /** Mirrors the EE HV API output: little-endian, single-strip, chunky. */
    private static byte[] synthesizeGeotiff(
        int width, int height, int bands, int bitsPerSample, int sampleFormat, byte[] pixels
    ) {
        return synthesizeGeotiffWithCompression(
            width, height, bands, bitsPerSample, sampleFormat, pixels, /*compression=*/1
        );
    }

    private static byte[] synthesizeGeotiffWithCompression(
        int width, int height, int bands, int bitsPerSample, int sampleFormat,
        byte[] stripBytes, int compression
    ) {
        return buildSyntheticTiff(
            width, height, bands, bitsPerSample, sampleFormat, stripBytes,
            compression, /*planar=*/1, /*pixelScale=*/null, /*tiepoint=*/null
        );
    }

    private static byte[] synthesizeGeotiffWithPlanarConfig(
        int width, int height, int bands, byte[] pixels, int planar
    ) {
        return buildSyntheticTiff(
            width, height, bands, 8, 1, pixels, 1, planar, null, null
        );
    }

    private static byte[] synthesizeGeotiffWithGeoTags(
        int width, int height, byte[] pixels, double[] pixelScale, double[] tiepoint
    ) {
        return buildSyntheticTiff(
            width, height, 1, 8, 1, pixels, 1, 1, pixelScale, tiepoint
        );
    }

    /**
     * Build a valid uncompressed/deflate strip TIFF with optional GeoTIFF tags.
     * Layout: header (8) + strip data + IFD + (optional) double-array overflow.
     */
    private static byte[] buildSyntheticTiff(
        int width, int height, int bands, int bitsPerSample, int sampleFormat,
        byte[] stripBytes, int compression, int planar,
        double[] pixelScale, double[] tiepoint
    ) {
        ByteOrder order = ByteOrder.LITTLE_ENDIAN;
        int stripOffset = 8;
        int afterStrip = stripOffset + stripBytes.length;
        int ifdOffset = (afterStrip + 1) & ~1;

        Map<Integer, FixedTag> tags = new LinkedHashMap<>();
        tags.put(256, FixedTag.shortVal(width));
        tags.put(257, FixedTag.shortVal(height));
        tags.put(258, FixedTag.shortVal(bitsPerSample));
        tags.put(259, FixedTag.shortVal(compression));
        tags.put(262, FixedTag.shortVal(bands == 1 ? 1 : 2));
        tags.put(273, FixedTag.longVal(stripOffset));
        tags.put(277, FixedTag.shortVal(bands));
        tags.put(278, FixedTag.shortVal(height));
        tags.put(279, FixedTag.longVal(stripBytes.length));
        tags.put(284, FixedTag.shortVal(planar));
        tags.put(339, FixedTag.shortVal(sampleFormat));

        if (pixelScale != null) {
            tags.put(33550, FixedTag.doubleArray(pixelScale));
        }
        if (tiepoint != null) {
            tags.put(33922, FixedTag.doubleArray(tiepoint));
        }

        // First pass: figure out IFD + overflow size to compute overflow offsets.
        int entryCount = tags.size();
        int ifdSize = 2 + entryCount * 12 + 4;
        int overflowStart = ifdOffset + ifdSize;
        int overflowCursor = overflowStart;
        for (FixedTag t : tags.values()) {
            if (t.overflowBytes() > 4) {
                t.overflowOffset = overflowCursor;
                overflowCursor += t.overflowBytes();
            }
        }

        ByteArrayOutputStream out = new ByteArrayOutputStream();
        ByteBuffer header = ByteBuffer.allocate(8).order(order);
        header.putShort((short) 0x4949);
        header.putShort((short) 42);
        header.putInt(ifdOffset);
        out.writeBytes(header.array());
        out.writeBytes(stripBytes);
        while (out.size() < ifdOffset) {
            out.write(0);
        }

        ByteBuffer ifd = ByteBuffer.allocate(ifdSize).order(order);
        ifd.putShort((short) entryCount);
        for (Map.Entry<Integer, FixedTag> e : tags.entrySet()) {
            e.getValue().writeEntry(ifd, e.getKey(), order);
        }
        ifd.putInt(0);
        out.writeBytes(ifd.array());

        for (FixedTag t : tags.values()) {
            if (t.overflowBytes() > 4) {
                out.writeBytes(t.overflowBytes(order));
            }
        }
        return out.toByteArray();
    }

    /** A simple holder for an IFD entry being written into a synthetic TIFF. */
    private static final class FixedTag {
        final int type;
        final int count;
        final long[] longValues;
        final double[] doubleValues;
        int overflowOffset;

        private FixedTag(int type, int count, long[] longValues, double[] doubleValues) {
            this.type = type;
            this.count = count;
            this.longValues = longValues;
            this.doubleValues = doubleValues;
        }

        static FixedTag shortVal(int v) {
            return new FixedTag(3, 1, new long[]{v & 0xFFFFL}, null);
        }

        static FixedTag longVal(long v) {
            return new FixedTag(4, 1, new long[]{v}, null);
        }

        static FixedTag doubleArray(double[] v) {
            return new FixedTag(12, v.length, null, v.clone());
        }

        int overflowBytes() {
            return type == 12 ? count * 8 : (type == 4 ? count * 4 : count * 2);
        }

        void writeEntry(ByteBuffer buf, int tag, ByteOrder order) {
            buf.putShort((short) tag);
            buf.putShort((short) type);
            buf.putInt(count);
            int total = overflowBytes();
            if (total <= 4) {
                int start = buf.position();
                if (type == 3) {
                    buf.putShort((short) longValues[0]);
                    buf.putShort((short) 0);
                } else if (type == 4) {
                    buf.putInt((int) longValues[0]);
                } else {
                    while (buf.position() < start + 4) {
                        buf.put((byte) 0);
                    }
                }
            } else {
                buf.putInt(overflowOffset);
            }
        }

        byte[] overflowBytes(ByteOrder order) {
            ByteBuffer buf = ByteBuffer.allocate(overflowBytes()).order(order);
            if (type == 12) {
                for (double d : doubleValues) {
                    buf.putDouble(d);
                }
            } else if (type == 4) {
                for (long v : longValues) {
                    buf.putInt((int) v);
                }
            } else {
                for (long v : longValues) {
                    buf.putShort((short) v);
                }
            }
            return buf.array();
        }
    }

    private static byte[] deflateBytes(byte[] input) {
        Deflater deflater = new Deflater();
        deflater.setInput(input);
        deflater.finish();
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] tmp = new byte[8192];
        while (!deflater.finished()) {
            int n = deflater.deflate(tmp);
            out.write(tmp, 0, n);
        }
        deflater.end();
        return out.toByteArray();
    }

    // -----------------------------------------------------------------------
    // TestTiffReader — independent reader for COG output verification.
    //
    // Deliberately decoupled from CogTranscoder's own reader so a bug there
    // can't mask itself by rewriting and re-reading the same broken layout.
    // Supports just enough of TIFF to walk the IFD, locate tile data,
    // decompress (deflate / none), reverse a horizontal predictor, and
    // return raw pixel bytes.
    // -----------------------------------------------------------------------

    private static byte[] decodeCogPixels(byte[] cog) throws IOException {
        TestTiffReader r = new TestTiffReader(cog);
        long[] tileOffsets = r.tileOffsets();
        long[] tileByteCounts = r.tileByteCounts();
        int width = r.intTag(256);
        int height = r.intTag(257);
        int bps = r.intTag(258);
        int spp = r.intTagOrDefault(277, 1);
        int tileWidth = r.intTag(322);
        int tileHeight = r.intTag(323);
        int compression = r.intTagOrDefault(259, 1);
        int predictor = r.intTagOrDefault(317, 1);
        int bytesPerSample = bps / 8;
        int rowBytes = width * spp * bytesPerSample;
        int tileRowBytes = tileWidth * spp * bytesPerSample;
        int blockSize = tileHeight * tileRowBytes;
        int tilesAcross = width / tileWidth;
        int tilesDown = height / tileHeight;

        byte[] full = new byte[height * rowBytes];

        for (int ty = 0; ty < tilesDown; ty++) {
            for (int tx = 0; tx < tilesAcross; tx++) {
                int idx = ty * tilesAcross + tx;
                long off = tileOffsets[idx];
                long count = tileByteCounts[idx];
                byte[] compressed = new byte[(int) count];
                System.arraycopy(cog, (int) off, compressed, 0, (int) count);

                byte[] block;
                if (compression == 1) {
                    block = compressed;
                } else if (compression == 8 || compression == 32946) {
                    block = inflateBytes(compressed, blockSize);
                } else {
                    throw new IOException("Unsupported compression in test reader: " + compression);
                }
                if (predictor == 2) {
                    block = reverseHorizontalPredictor(
                        block, tileWidth, tileHeight, spp, bytesPerSample
                    );
                }
                int dstX = tx * tileRowBytes;
                for (int dy = 0; dy < tileHeight; dy++) {
                    int dstOff = (ty * tileHeight + dy) * rowBytes + dstX;
                    int srcOff = dy * tileRowBytes;
                    System.arraycopy(block, srcOff, full, dstOff, tileRowBytes);
                }
            }
        }
        return full;
    }

    private static byte[] inflateBytes(byte[] data, int expectedBytes) throws IOException {
        java.util.zip.Inflater inflater = new java.util.zip.Inflater();
        inflater.setInput(data);
        ByteArrayOutputStream out = new ByteArrayOutputStream(expectedBytes);
        byte[] tmp = new byte[8192];
        try {
            while (!inflater.finished()) {
                int n = inflater.inflate(tmp);
                if (n == 0 && inflater.needsInput()) {
                    break;
                }
                out.write(tmp, 0, n);
            }
        } catch (java.util.zip.DataFormatException e) {
            throw new IOException("test inflate failed: " + e.getMessage(), e);
        } finally {
            inflater.end();
        }
        return out.toByteArray();
    }

    private static byte[] reverseHorizontalPredictor(
        byte[] data, int width, int height, int samplesPerPixel, int bytesPerSample
    ) {
        byte[] result = data.clone();
        int rowBytes = width * samplesPerPixel * bytesPerSample;
        for (int row = 0; row < height; row++) {
            int rowStart = row * rowBytes;
            for (int x = 1; x < width; x++) {
                for (int s = 0; s < samplesPerPixel; s++) {
                    int curr = rowStart + (x * samplesPerPixel + s) * bytesPerSample;
                    int prev = rowStart + ((x - 1) * samplesPerPixel + s) * bytesPerSample;
                    for (int b = 0; b < bytesPerSample; b++) {
                        result[curr + b] = (byte) (result[curr + b] + result[prev + b]);
                    }
                }
            }
        }
        return result;
    }

    /**
     * Bare-bones little-endian TIFF IFD walker for tests. Reads tags into a map
     * keyed by tag number; values stored either inline (≤4 bytes) or via
     * an overflow pointer.
     */
    private static final class TestTiffReader {
        private final byte[] raw;
        private final ByteOrder order;
        private final Map<Integer, Entry> entries = new LinkedHashMap<>();

        TestTiffReader(byte[] raw) throws IOException {
            this.raw = raw;
            ByteBuffer buf = ByteBuffer.wrap(raw);
            short bom = buf.getShort(0);
            this.order = (bom == 0x4D4D) ? ByteOrder.BIG_ENDIAN : ByteOrder.LITTLE_ENDIAN;
            buf.order(order);
            short magic = buf.getShort(2);
            if (magic != 42) {
                throw new IOException("Not a TIFF (magic=" + magic + ")");
            }
            int ifdOffset = buf.getInt(4);
            buf.position(ifdOffset);
            int n = Short.toUnsignedInt(buf.getShort());
            for (int i = 0; i < n; i++) {
                int tag = Short.toUnsignedInt(buf.getShort());
                int type = Short.toUnsignedInt(buf.getShort());
                int count = buf.getInt();
                int valuePos = buf.position();
                entries.put(tag, new Entry(type, count, valuePos));
                buf.position(valuePos + 4);
            }
        }

        int entryCount() {
            return entries.size();
        }

        Entry entry(int tag) {
            return entries.get(tag);
        }

        int intTag(int tag) throws IOException {
            Entry e = entries.get(tag);
            if (e == null) {
                throw new IOException("Missing tag: " + tag);
            }
            return (int) readScalar(e);
        }

        int intTagOrDefault(int tag, int defaultValue) {
            Entry e = entries.get(tag);
            if (e == null) {
                return defaultValue;
            }
            return (int) readScalar(e);
        }

        long[] tileOffsets() throws IOException {
            return readLongArray(324);
        }

        long[] tileByteCounts() throws IOException {
            return readLongArray(325);
        }

        double[] doubleArrayTag(int tag) throws IOException {
            Entry e = entries.get(tag);
            if (e == null) {
                throw new IOException("Missing tag: " + tag);
            }
            int dataStart = e.count * 8 <= 4 ? e.valuePos
                : ByteBuffer.wrap(raw).order(order).getInt(e.valuePos);
            ByteBuffer db = ByteBuffer.wrap(raw).order(order);
            db.position(dataStart);
            double[] result = new double[e.count];
            for (int i = 0; i < e.count; i++) {
                result[i] = db.getDouble();
            }
            return result;
        }

        private long readScalar(Entry e) {
            ByteBuffer db = ByteBuffer.wrap(raw).order(order);
            db.position(e.valuePos);
            return switch (e.type) {
                case 3 -> Short.toUnsignedLong(db.getShort());
                case 4 -> Integer.toUnsignedLong(db.getInt());
                default -> Byte.toUnsignedLong(db.get());
            };
        }

        private long[] readLongArray(int tag) throws IOException {
            Entry e = entries.get(tag);
            if (e == null) {
                throw new IOException("Missing tag: " + tag);
            }
            int typeSize = (e.type == 4) ? 4 : (e.type == 3 ? 2 : 1);
            int total = e.count * typeSize;
            int dataStart = total <= 4 ? e.valuePos
                : ByteBuffer.wrap(raw).order(order).getInt(e.valuePos);
            ByteBuffer db = ByteBuffer.wrap(raw).order(order);
            db.position(dataStart);
            long[] out = new long[e.count];
            for (int i = 0; i < e.count; i++) {
                out[i] = (e.type == 4)
                    ? Integer.toUnsignedLong(db.getInt())
                    : Short.toUnsignedLong(db.getShort());
            }
            return out;
        }

        static final class Entry {
            final int type;
            final int count;
            final int valuePos;

            Entry(int type, int count, int valuePos) {
                this.type = type;
                this.count = count;
                this.valuePos = valuePos;
            }
        }
    }
}
