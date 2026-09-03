package com.datensee.pixel.io;

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
 * compression schemes (none, deflate).
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
        // Strip tags must be absent: the COG has tiled layout.
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

        byte[] decoded = TestTiffReader.decodeCogPixels(cog);
        assertArrayEquals(pixels, decoded, "uint8 pixel data must round-trip exactly");
    }

    @Test
    void uint8SingleBandRoundTripsWithNoCompression() throws Exception {
        int size = 8;
        byte[] pixels = sequentialBytes(size * size);
        byte[] raw = synthesizeGeotiff(size, size, 1, 8, 1, pixels);
        byte[] cog = CogTranscoder.transcode(raw, size, "none");

        byte[] decoded = TestTiffReader.decodeCogPixels(cog);
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

        byte[] decoded = TestTiffReader.decodeCogPixels(cog);
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

        byte[] decoded = TestTiffReader.decodeCogPixels(cog);
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

        byte[] decoded = TestTiffReader.decodeCogPixels(cog);
        assertArrayEquals(pixels, decoded, "float32 pixel data must round-trip exactly");
    }

    @Test
    void multiBandUint8RoundTripsWithDeflate() throws Exception {
        int size = 8;
        int bands = 3;
        byte[] pixels = sequentialBytes(size * size * bands);
        byte[] raw = synthesizeGeotiff(size, size, bands, 8, 1, pixels);
        byte[] cog = CogTranscoder.transcode(raw, size, "deflate");

        byte[] decoded = TestTiffReader.decodeCogPixels(cog);
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

        byte[] decoded = TestTiffReader.decodeCogPixels(cog);
        assertArrayEquals(pixels, decoded, "deflate-input pixel data must round-trip");
    }

    @Test
    void multiBlockCogWith256InnerTilesOf16x16RoundTripsExactly() throws Exception {
        // two-tier validation case: 256 inner tiles of 16x16 pixels arranged in a
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

        byte[] decoded = TestTiffReader.decodeCogPixels(cog);
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
        byte[] decoded = TestTiffReader.decodeCogPixels(cog);
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

    @Test
    void rejectsTruncatedUncompressedStripData() throws Exception {
        // Regression test for a real Dataflow worker crash: we passed a
        // TIFF whose IFD declared a 512×512 single-band float32 image
        // (1,048,576 bytes of pixels expected) but whose strip byte
        // counts summed to only 262,144 bytes. extractBlock then tried to
        // arraycopy past the end of the buffer and the JVM threw a bare
        // ArrayIndexOutOfBoundsException six frames deep into the
        // transcoder, on a Dataflow worker, where the only signal a
        // human got was "last source index 264192 out of bounds for
        // byte[262144]." The transcoder must catch IFD/pixel-buffer
        // mismatches at the boundary with a diagnosable message.
        int width = 512;
        int height = 512;
        int bps = 32;  // float32
        int sampleFormat = 3;  // floating point
        int bytesPerSample = bps / 8;
        int expected = width * height * 1 * bytesPerSample;
        byte[] truncated = sequentialBytes(expected / 4);

        byte[] tiff = synthesizeGeotiff(width, height, 1, bps, sampleFormat, truncated);

        IOException ex = assertThrows(
            IOException.class,
            () -> CogTranscoder.transcode(tiff, 512, "deflate"),
            "transcode must reject truncated pixel data with a clear "
                + "IOException, not crash deep in extractBlock"
        );
        assertTrue(
            ex.getMessage().contains(String.valueOf(truncated.length))
                && ex.getMessage().contains(String.valueOf(expected)),
            "error message should report the actual and expected byte counts: "
                + ex.getMessage()
        );
    }

    @Test
    void rejectsTruncatedDeflateStripData() throws Exception {
        // Same category of bug, but via the deflate-decompression path:
        // a TIFF whose Inflater output is shorter than the IFD says.
        int width = 512;
        int height = 512;
        int bps = 32;
        int sampleFormat = 3;
        int bytesPerSample = bps / 8;
        int expected = width * height * 1 * bytesPerSample;
        byte[] shortPixels = sequentialBytes(expected / 4);
        byte[] compressed = deflateBytes(shortPixels);

        byte[] tiff = synthesizeGeotiffWithCompression(
            width, height, 1, bps, sampleFormat, compressed, /*compression=*/8
        );

        IOException ex = assertThrows(
            IOException.class,
            () -> CogTranscoder.transcode(tiff, 512, "deflate"),
            "deflate-input path must also catch decompressed-vs-IFD mismatch"
        );
        assertTrue(
            ex.getMessage().contains("decoded to") && ex.getMessage().contains("expected"),
            "error should mention the decoded-vs-expected size mismatch: "
                + ex.getMessage()
        );
    }

    @Test
    void multiTileDeflateInputRoundTripsCorrectly() throws Exception {
        // EE HV's computePixels endpoint returns TILE-layout TIFFs with
        // 256×256 internal tiles and a separate zlib stream per tile.
        // Our pre-fix decoder fed the concatenation of all tiles' bytes
        // into a single Inflater, which stops at the first end-of-stream
        // marker and silently drops every tile after the first. This
        // crashed Dataflow workers downstream when extractBlock walked
        // past the end of a one-tile-sized buffer.
        // Synthesize a 4-tile (2×2) deflate float32 TIFF (matching the
        // shape EE returns for a 512×512 request) and verify that transcode
        // preserves all pixel values across round-trip decoding. Each pixel
        // encodes its (y, x) coordinates to ensure spatial alignment.
        int size = 512;
        int innerSize = 256;
        byte[] pixels = new byte[size * size * 4];
        ByteBuffer pb = ByteBuffer.wrap(pixels).order(ByteOrder.LITTLE_ENDIAN);
        for (int y = 0; y < size; y++) {
            for (int x = 0; x < size; x++) {
                pb.putFloat(y * 1000.0f + x);
            }
        }

        byte[] tiff = synthesizeTiledDeflateGeotiff(
            size, size, innerSize, innerSize,
            /*bands=*/1, /*bitsPerSample=*/32, /*sampleFormat=*/3, pixels
        );

        byte[] cog = CogTranscoder.transcode(tiff, size, "deflate");

        byte[] decoded = TestTiffReader.decodeCogPixels(cog);
        assertArrayEquals(
            pixels, decoded,
            "Multi-tile deflate input must round-trip pixel-perfectly. "
            + "If this fails, extractPixelData likely either re-introduced "
            + "the single-stream inflate bug (only first tile decompressed) "
            + "or scrambled tile-grid order vs row-major."
        );
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

        static FixedTag longArray(long[] v) {
            return new FixedTag(4, v.length, v.clone(), null);
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

    /**
     * Synthesize a tile-layout deflate-compressed GeoTIFF, mimicking
     * the actual response shape of EE's High Volume {@code computePixels}
     * endpoint for requests above 256×256: tile layout, deflate
     * compression (32946), each tile as its own zlib stream, tiles in
     * row-major order across the tile grid.
     *
     * <p>{@code pixels} is row-major image data; this method re-cuts
     * it into tile chunks, deflates each independently, and emits a
     * TIFF with TileOffsets / TileByteCounts / TileWidth / TileLength
     * tags pointing at those compressed tile blobs.
     */
    private static byte[] synthesizeTiledDeflateGeotiff(
        int width, int height, int tileWidth, int tileLength,
        int bands, int bitsPerSample, int sampleFormat, byte[] pixels
    ) {
        int bytesPerSample = bitsPerSample / 8;
        int rowBytes = width * bands * bytesPerSample;
        int tileRowBytes = tileWidth * bands * bytesPerSample;
        int tileBytesUncompressed = tileLength * tileRowBytes;
        int tilesAcross = (width + tileWidth - 1) / tileWidth;
        int tilesDown = (height + tileLength - 1) / tileLength;
        int numTiles = tilesAcross * tilesDown;

        // Cut row-major pixels into per-tile chunks (with edge padding
        // zeroed) and deflate each tile separately. Each compressed
        // chunk is an independent zlib stream.
        byte[][] compressedTiles = new byte[numTiles][];
        for (int ty = 0; ty < tilesDown; ty++) {
            for (int tx = 0; tx < tilesAcross; tx++) {
                byte[] tilePx = new byte[tileBytesUncompressed];
                int visibleWidth = Math.min(tileWidth, width - tx * tileWidth);
                int visibleHeight = Math.min(tileLength, height - ty * tileLength);
                int copyRowBytes = visibleWidth * bands * bytesPerSample;
                int dstColBytes = tx * tileWidth * bands * bytesPerSample;
                for (int dy = 0; dy < visibleHeight; dy++) {
                    int srcOff = (ty * tileLength + dy) * rowBytes + dstColBytes;
                    System.arraycopy(pixels, srcOff, tilePx, dy * tileRowBytes, copyRowBytes);
                }
                compressedTiles[ty * tilesAcross + tx] = deflateBytes(tilePx);
            }
        }

        // Layout: header (8) + per-tile compressed blobs back-to-back
        // + IFD + overflow data for any array-typed tags.
        long[] tileOffsets = new long[numTiles];
        long[] tileByteCounts = new long[numTiles];
        int cursor = 8;
        for (int i = 0; i < numTiles; i++) {
            tileOffsets[i] = cursor;
            tileByteCounts[i] = compressedTiles[i].length;
            cursor += compressedTiles[i].length;
        }
        int afterTiles = cursor;
        int ifdOffset = (afterTiles + 1) & ~1;

        Map<Integer, FixedTag> tags = new LinkedHashMap<>();
        tags.put(256, FixedTag.shortVal(width));
        tags.put(257, FixedTag.shortVal(height));
        tags.put(258, FixedTag.shortVal(bitsPerSample));
        tags.put(259, FixedTag.shortVal(/*Adobe Deflate=*/32946));
        tags.put(262, FixedTag.shortVal(bands == 1 ? 1 : 2));
        tags.put(277, FixedTag.shortVal(bands));
        tags.put(284, FixedTag.shortVal(/*chunky=*/1));
        tags.put(322, FixedTag.shortVal(tileWidth));
        tags.put(323, FixedTag.shortVal(tileLength));
        tags.put(324, FixedTag.longArray(tileOffsets));
        tags.put(325, FixedTag.longArray(tileByteCounts));
        tags.put(339, FixedTag.shortVal(sampleFormat));

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

        ByteOrder order = ByteOrder.LITTLE_ENDIAN;
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        ByteBuffer header = ByteBuffer.allocate(8).order(order);
        header.putShort((short) 0x4949);
        header.putShort((short) 42);
        header.putInt(ifdOffset);
        out.writeBytes(header.array());
        for (byte[] tile : compressedTiles) {
            out.writeBytes(tile);
        }
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

}
