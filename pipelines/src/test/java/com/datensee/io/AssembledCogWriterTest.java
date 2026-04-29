package com.datensee.io;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;

import com.datensee.FetchedTile;
import com.datensee.OutputTileKey;
import com.datensee.TileCoordinate;
import java.io.ByteArrayOutputStream;
import java.lang.reflect.Method;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import org.apache.beam.sdk.transforms.DoFn;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * Tests for {@link AssembledCogWriter}.
 *
 * <p>Validation case the user specified: 256 compute tiles of 16x16
 * pixels arranged as a 16x16 grid → a single 256x256 output COG. We
 * build synthetic FetchedTiles where each pixel encodes its
 * {@code (output_row, output_col, within_y, within_x)} so that any
 * mis-keying or off-by-one in the assembly path surfaces as a wrong
 * pixel at a deterministic location, not just a numerical mismatch.
 *
 * <p>The assembler is exercised by invoking the inner DoFn directly via
 * reflection rather than running a Beam pipeline; tests stay fast and
 * self-contained, and the assembled COG bytes are decoded with the
 * same independent {@code TestTiffReader}-style logic used by
 * {@link CogTranscoderTest} so writer bugs cannot self-mask.
 */
class AssembledCogWriterTest {

    @Test
    void assembles256ComputeTilesInto256x256OutputCog(@TempDir Path tempDir) throws Exception {
        int innerSize = 16;
        int tilesPerSide = 16;
        int outerSize = innerSize * tilesPerSide;  // 256
        int totalTiles = tilesPerSide * tilesPerSide;  // 256

        // Build the expected pixel grid first so we can construct each
        // compute tile's TIFF from the corresponding 16x16 slice of it.
        byte[] expectedPixels = new byte[outerSize * outerSize];
        for (int y = 0; y < outerSize; y++) {
            for (int x = 0; x < outerSize; x++) {
                int blockY = y / innerSize;
                int blockX = x / innerSize;
                int withinY = y % innerSize;
                int withinX = x % innerSize;
                expectedPixels[y * outerSize + x] = (byte) (
                    (blockY * 71 + blockX * 31 + withinY * 13 + withinX * 7) & 0xFF
                );
            }
        }

        // Output tile origin: must align to the snapped global output
        // grid (multiple of outputTileSize * pixelSize on each axis),
        // since the assembler snaps to that origin internally. Pick a
        // non-zero offset so ModelTiepoint isn't trivially zero, but
        // still on-grid: 16 * outputTileSizeNative = 16 * 256 * 30.
        double pixelSize = 30.0;
        double outputXMin = -16.0 * outerSize * pixelSize;  // -122880
        double outputYMax = 8.0 * outerSize * pixelSize;    //   61440

        // Fabricate 256 compute tiles. Each one's bbox lands it in the
        // right (out_row, out_col) cell, and its pixel data is the
        // 16x16 sub-block of expectedPixels at the corresponding
        // position. Compute tile (row, col) maps to within-output-tile
        // pixel offset (col * 16, row * 16) horizontally and from the
        // top of the output tile vertically.
        List<FetchedTile> computeTiles = new ArrayList<>();
        for (int outerRow = 0; outerRow < tilesPerSide; outerRow++) {
            for (int outerCol = 0; outerCol < tilesPerSide; outerCol++) {
                byte[] tilePixels = new byte[innerSize * innerSize];
                for (int dy = 0; dy < innerSize; dy++) {
                    int srcY = outerRow * innerSize + dy;
                    int srcOff = srcY * outerSize + outerCol * innerSize;
                    int dstOff = dy * innerSize;
                    System.arraycopy(expectedPixels, srcOff, tilePixels, dstOff, innerSize);
                }
                // bbox: x increases left→right with col, y increases
                // top→bottom (yMax decreases as row grows).
                double xMin = outputXMin + outerCol * innerSize * pixelSize;
                double xMax = xMin + innerSize * pixelSize;
                double yMax = outputYMax - outerRow * innerSize * pixelSize;
                double yMin = yMax - innerSize * pixelSize;

                byte[] rawTiff = synthesizeStripUint8Tiff(
                    innerSize, innerSize, tilePixels,
                    pixelSize, xMin, yMax
                );
                TileCoordinate coord = new TileCoordinate(
                    xMin, yMin, xMax, yMax,
                    outerRow, outerCol,  // local row/col
                    0, 0,                // out_row, out_col — all into one output tile
                    List.of()
                );
                computeTiles.add(new FetchedTile(coord, rawTiff, innerSize, innerSize));
            }
        }
        assertEquals(totalTiles, computeTiles.size());

        // Run the assembler DoFn directly.
        AssembledCogWriter.AssembleAndWriteDoFn doFn =
            new AssembledCogWriter.AssembleAndWriteDoFn(
                tempDir.toString(), innerSize, outerSize, "deflate"
            );
        invokeProcessElement(doFn, new OutputTileKey(0, 0), computeTiles);

        // The assembled COG should be at tempDir/tile_r0000_c0000.tif.
        Path cogPath = tempDir.resolve("tile_r0000_c0000.tif");
        byte[] cogBytes = Files.readAllBytes(cogPath);

        // Decode and assert pixel-perfect reconstruction.
        byte[] decoded = decodeAssembledCog(cogBytes, outerSize, outerSize, innerSize);
        assertArrayEquals(
            expectedPixels, decoded,
            "Assembled 256x256 COG must round-trip every pixel "
            + "(any mismatch indicates a tile keying / placement bug)"
        );
    }

    @Test
    void smallerCaseFourComputeTilesInto32x32OutputCog(@TempDir Path tempDir) throws Exception {
        // Sanity-check 2x2 grid as a smaller variant — easier to debug
        // if the larger test ever fails.
        int innerSize = 16;
        int outerSize = 32;

        byte[] expectedPixels = new byte[outerSize * outerSize];
        for (int i = 0; i < expectedPixels.length; i++) {
            expectedPixels[i] = (byte) (i & 0xFF);
        }

        double pixelSize = 1.0;
        double outputXMin = 0.0;
        double outputYMax = 32.0;

        List<FetchedTile> tiles = new ArrayList<>();
        for (int row = 0; row < 2; row++) {
            for (int col = 0; col < 2; col++) {
                byte[] tilePixels = new byte[innerSize * innerSize];
                for (int dy = 0; dy < innerSize; dy++) {
                    int srcOff = (row * innerSize + dy) * outerSize + col * innerSize;
                    System.arraycopy(expectedPixels, srcOff, tilePixels, dy * innerSize, innerSize);
                }
                double xMin = outputXMin + col * innerSize * pixelSize;
                double xMax = xMin + innerSize * pixelSize;
                double yMax = outputYMax - row * innerSize * pixelSize;
                double yMin = yMax - innerSize * pixelSize;
                byte[] tiff = synthesizeStripUint8Tiff(
                    innerSize, innerSize, tilePixels, pixelSize, xMin, yMax
                );
                tiles.add(new FetchedTile(
                    new TileCoordinate(xMin, yMin, xMax, yMax, row, col, 0, 0, List.of()),
                    tiff, innerSize, innerSize
                ));
            }
        }

        AssembledCogWriter.AssembleAndWriteDoFn doFn =
            new AssembledCogWriter.AssembleAndWriteDoFn(
                tempDir.toString(), innerSize, outerSize, "deflate"
            );
        invokeProcessElement(doFn, new OutputTileKey(0, 0), tiles);

        byte[] cogBytes = Files.readAllBytes(tempDir.resolve("tile_r0000_c0000.tif"));
        byte[] decoded = decodeAssembledCog(cogBytes, outerSize, outerSize, innerSize);
        assertArrayEquals(expectedPixels, decoded);
    }

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

    /**
     * Invoke a DoFn's @ProcessElement method directly, bypassing Beam's
     * harness. Since this DoFn doesn't use OutputReceiver / windowing /
     * timers / state, a reflective call with a synthetic KV input is
     * sufficient for a unit test.
     */
    @SuppressWarnings("unchecked")
    private static void invokeProcessElement(
        DoFn<?, ?> doFn,
        OutputTileKey key,
        Iterable<FetchedTile> tiles
    ) throws Exception {
        // Trigger @Setup so transient fields are initialized.
        for (Method m : doFn.getClass().getDeclaredMethods()) {
            if (m.isAnnotationPresent(DoFn.Setup.class)) {
                m.setAccessible(true);
                m.invoke(doFn);
                break;
            }
        }
        org.apache.beam.sdk.values.KV<OutputTileKey, Iterable<FetchedTile>> kv =
            org.apache.beam.sdk.values.KV.of(key, tiles);
        for (Method m : doFn.getClass().getDeclaredMethods()) {
            if (m.isAnnotationPresent(DoFn.ProcessElement.class)) {
                m.setAccessible(true);
                m.invoke(doFn, kv);
                return;
            }
        }
        throw new IllegalStateException("No @ProcessElement method on " + doFn.getClass());
    }

    /**
     * Build a minimal strip-layout uint8 single-band GeoTIFF with
     * ModelPixelScale + ModelTiepoint so the assembler can read CRS
     * metadata. Pixel data is row-major, no compression.
     */
    private static byte[] synthesizeStripUint8Tiff(
        int width, int height, byte[] pixels,
        double pixelSize, double xMin, double yMax
    ) {
        ByteOrder order = ByteOrder.LITTLE_ENDIAN;
        int stripOffset = 8;
        int afterStrip = stripOffset + pixels.length;
        int ifdOffset = (afterStrip + 1) & ~1;

        // Tag plan (sorted):
        // 256 ImageWidth SHORT, 257 ImageLength SHORT, 258 BitsPerSample SHORT,
        // 259 Compression SHORT (1), 262 Photometric SHORT (1),
        // 273 StripOffsets LONG, 277 SamplesPerPixel SHORT,
        // 278 RowsPerStrip SHORT, 279 StripByteCounts LONG,
        // 284 PlanarConfiguration SHORT (1), 339 SampleFormat SHORT (1),
        // 33550 ModelPixelScale DOUBLE[3] (overflow),
        // 33922 ModelTiepoint DOUBLE[6] (overflow).
        int entryCount = 13;
        int ifdSize = 2 + entryCount * 12 + 4;
        int overflowStart = ifdOffset + ifdSize;
        int pixelScaleOffset = overflowStart;
        int tiepointOffset = pixelScaleOffset + 24;

        ByteArrayOutputStream out = new ByteArrayOutputStream();
        ByteBuffer header = ByteBuffer.allocate(8).order(order);
        header.putShort((short) 0x4949);
        header.putShort((short) 42);
        header.putInt(ifdOffset);
        out.writeBytes(header.array());
        out.writeBytes(pixels);
        while (out.size() < ifdOffset) {
            out.write(0);
        }

        ByteBuffer ifd = ByteBuffer.allocate(ifdSize).order(order);
        ifd.putShort((short) entryCount);
        writeShortInline(ifd, 256, width);
        writeShortInline(ifd, 257, height);
        writeShortInline(ifd, 258, 8);
        writeShortInline(ifd, 259, 1);
        writeShortInline(ifd, 262, 1);
        writeLongInline(ifd, 273, stripOffset);
        writeShortInline(ifd, 277, 1);
        writeShortInline(ifd, 278, height);
        writeLongInline(ifd, 279, pixels.length);
        writeShortInline(ifd, 284, 1);
        writeShortInline(ifd, 339, 1);
        writeDoubleArrayOffset(ifd, 33550, 3, pixelScaleOffset);
        writeDoubleArrayOffset(ifd, 33922, 6, tiepointOffset);
        ifd.putInt(0);  // next IFD = 0
        out.writeBytes(ifd.array());

        // Overflow: pixel scale + tiepoint.
        ByteBuffer overflow = ByteBuffer.allocate(24 + 48).order(order);
        overflow.putDouble(pixelSize);
        overflow.putDouble(pixelSize);
        overflow.putDouble(0.0);
        overflow.putDouble(0.0);
        overflow.putDouble(0.0);
        overflow.putDouble(0.0);
        overflow.putDouble(xMin);
        overflow.putDouble(yMax);
        overflow.putDouble(0.0);
        out.writeBytes(overflow.array());

        return out.toByteArray();
    }

    private static void writeShortInline(ByteBuffer buf, int tag, int value) {
        buf.putShort((short) tag);
        buf.putShort((short) 3);  // SHORT
        buf.putInt(1);
        buf.putShort((short) value);
        buf.putShort((short) 0);
    }

    private static void writeLongInline(ByteBuffer buf, int tag, int value) {
        buf.putShort((short) tag);
        buf.putShort((short) 4);  // LONG
        buf.putInt(1);
        buf.putInt(value);
    }

    private static void writeDoubleArrayOffset(ByteBuffer buf, int tag, int count, int offset) {
        buf.putShort((short) tag);
        buf.putShort((short) 12);  // DOUBLE
        buf.putInt(count);
        buf.putInt(offset);
    }

    /**
     * Decode a multi-block COG of known dimensions back into row-major
     * pixel bytes, independent of CogTranscoder's own decoder. Mirrors
     * the test reader in {@link CogTranscoderTest}.
     */
    private static byte[] decodeAssembledCog(
        byte[] cog, int width, int height, int innerSize
    ) throws Exception {
        ByteBuffer buf = ByteBuffer.wrap(cog).order(ByteOrder.LITTLE_ENDIAN);
        int ifdOffset = buf.getInt(4);
        buf.position(ifdOffset);
        int entryCount = Short.toUnsignedInt(buf.getShort());

        long[] tileOffsets = null;
        long[] tileByteCounts = null;
        int compression = 1;
        int predictor = 1;

        for (int i = 0; i < entryCount; i++) {
            int tag = Short.toUnsignedInt(buf.getShort());
            int type = Short.toUnsignedInt(buf.getShort());
            int count = buf.getInt();
            int valuePos = buf.position();
            if (tag == 324 || tag == 325) {
                int total = count * 4;  // LONG
                int dataStart = total <= 4 ? valuePos
                    : ByteBuffer.wrap(cog).order(ByteOrder.LITTLE_ENDIAN).getInt(valuePos);
                ByteBuffer db = ByteBuffer.wrap(cog).order(ByteOrder.LITTLE_ENDIAN);
                db.position(dataStart);
                long[] arr = new long[count];
                for (int j = 0; j < count; j++) {
                    arr[j] = Integer.toUnsignedLong(db.getInt());
                }
                if (tag == 324) {
                    tileOffsets = arr;
                } else {
                    tileByteCounts = arr;
                }
            } else if (tag == 259) {
                compression = Short.toUnsignedInt(ByteBuffer.wrap(cog)
                    .order(ByteOrder.LITTLE_ENDIAN).getShort(valuePos));
            } else if (tag == 317) {
                predictor = Short.toUnsignedInt(ByteBuffer.wrap(cog)
                    .order(ByteOrder.LITTLE_ENDIAN).getShort(valuePos));
            }
            buf.position(valuePos + 4);
        }

        int tilesAcross = width / innerSize;
        int tilesDown = height / innerSize;
        int tileBytes = innerSize * innerSize;  // uint8 single band
        byte[] full = new byte[width * height];

        for (int ty = 0; ty < tilesDown; ty++) {
            for (int tx = 0; tx < tilesAcross; tx++) {
                int idx = ty * tilesAcross + tx;
                int off = (int) tileOffsets[idx];
                int len = (int) tileByteCounts[idx];
                byte[] compressed = new byte[len];
                System.arraycopy(cog, off, compressed, 0, len);

                byte[] block;
                if (compression == 1) {
                    block = compressed;
                } else if (compression == 8 || compression == 32946) {
                    java.util.zip.Inflater inflater = new java.util.zip.Inflater();
                    inflater.setInput(compressed);
                    ByteArrayOutputStream o = new ByteArrayOutputStream(tileBytes);
                    byte[] tmp = new byte[8192];
                    while (!inflater.finished()) {
                        int n = inflater.inflate(tmp);
                        if (n == 0 && inflater.needsInput()) {
                            break;
                        }
                        o.write(tmp, 0, n);
                    }
                    inflater.end();
                    block = o.toByteArray();
                } else {
                    throw new RuntimeException("unsupported compression " + compression);
                }
                if (predictor == 2) {
                    for (int y = 0; y < innerSize; y++) {
                        int rowStart = y * innerSize;
                        for (int x = 1; x < innerSize; x++) {
                            block[rowStart + x] = (byte) (block[rowStart + x] + block[rowStart + x - 1]);
                        }
                    }
                }

                for (int dy = 0; dy < innerSize; dy++) {
                    int dstOff = (ty * innerSize + dy) * width + tx * innerSize;
                    int srcOff = dy * innerSize;
                    System.arraycopy(block, srcOff, full, dstOff, innerSize);
                }
            }
        }
        return full;
    }
}
