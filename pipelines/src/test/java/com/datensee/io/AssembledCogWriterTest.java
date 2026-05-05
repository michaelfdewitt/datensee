package com.datensee.io;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;

import com.datensee.AffineTransform;
import com.datensee.FetchedTile;
import com.datensee.GridDimensions;
import com.datensee.OutputTileKey;
import com.datensee.PixelGrid;
import com.datensee.TileCoordinate;
import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import org.apache.beam.sdk.values.KV;
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
 * <p>The assembler is exercised by invoking the inner DoFn's
 * package-private {@code process(KV)} method directly rather than
 * running a Beam pipeline; tests stay fast and self-contained, and the
 * assembled COG bytes are decoded with the same independent
 * {@code TestTiffReader}-style logic used by {@link CogTranscoderTest}
 * so writer bugs cannot self-mask.
 */
class AssembledCogWriterTest {

    private static PixelGrid parentGrid(double pixelSize, double translateX, double translateY,
                                        int width, int height) {
        return new PixelGrid(
            "EPSG:32610",
            new AffineTransform(pixelSize, 0.0, translateX, 0.0, -pixelSize, translateY),
            new GridDimensions(width, height)
        );
    }

    @Test
    void assembles256ComputeTilesInto256x256OutputCog(@TempDir Path tempDir) throws Exception {
        int innerSize = 16;
        int tilesPerSide = 16;
        int outerSize = innerSize * tilesPerSide;  // 256
        int totalTiles = tilesPerSide * tilesPerSide;  // 256

        byte[] expectedPixels = new byte[outerSize * outerSize];
        for (int y = 0; y < outerSize; y++) {
            for (int x = 0; x < outerSize; x++) {
                expectedPixels[y * outerSize + x] = (byte) (
                    ((y / innerSize) * 71 + (x / innerSize) * 31
                        + (y % innerSize) * 13 + (x % innerSize) * 7) & 0xFF
                );
            }
        }

        // Output tile origin: must align to the snapped global output
        // grid (multiple of outputTileSize * pixelSize on each axis),
        // since the assembler snaps to that origin internally. Pick a
        // non-zero offset so ModelTiepoint isn't trivially zero.
        double pixelSize = 30.0;
        double outputXMin = -16.0 * outerSize * pixelSize;  // -122880
        double outputYMax = 8.0 * outerSize * pixelSize;    //   61440

        // Parent grid covers exactly this output tile. Tiles inside have
        // colPx/rowPx local to the parent — i.e. starting at (0, 0).
        PixelGrid parent = parentGrid(pixelSize, outputXMin, outputYMax, outerSize, outerSize);

        // Fabricate 256 compute tiles. Each one's pixel offsets land it
        // in the right block, and its pixel data is the 16x16 sub-block
        // of expectedPixels.
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
                int colPx = outerCol * innerSize;
                int rowPx = outerRow * innerSize;
                double xMin = outputXMin + colPx * pixelSize;
                double yMax = outputYMax - rowPx * pixelSize;

                byte[] rawTiff = synthesizeStripUint8Tiff(
                    innerSize, innerSize, tilePixels,
                    pixelSize, xMin, yMax
                );
                TileCoordinate coord = new TileCoordinate(
                    colPx, rowPx, innerSize, innerSize,
                    outerRow, outerCol,  // local row/col
                    0, 0,                // out_row, out_col — all into one output tile
                    List.of()
                );
                computeTiles.add(new FetchedTile(coord, rawTiff, innerSize, innerSize));
            }
        }
        assertEquals(totalTiles, computeTiles.size());

        AssembledCogWriter.AssembleAndWriteDoFn doFn =
            new AssembledCogWriter.AssembleAndWriteDoFn(
                tempDir.toString(), parent, innerSize, outerSize, "deflate"
            );
        doFn.setup();
        doFn.process(KV.of(new OutputTileKey(0, 0), computeTiles));

        Path cogPath = tempDir.resolve("tile_r0000_c0000.tif");
        byte[] cogBytes = Files.readAllBytes(cogPath);

        byte[] decoded = TestTiffReader.decodeCogPixels(cogBytes);
        assertArrayEquals(
            expectedPixels, decoded,
            "Assembled 256x256 COG must round-trip every pixel "
            + "(any mismatch indicates a tile keying / placement bug)"
        );
    }

    @Test
    void smallerCaseFourComputeTilesInto32x32OutputCog(@TempDir Path tempDir) throws Exception {
        int innerSize = 16;
        int outerSize = 32;

        byte[] expectedPixels = new byte[outerSize * outerSize];
        for (int i = 0; i < expectedPixels.length; i++) {
            expectedPixels[i] = (byte) (i & 0xFF);
        }

        double pixelSize = 1.0;
        double outputXMin = 0.0;
        double outputYMax = 32.0;
        PixelGrid parent = parentGrid(pixelSize, outputXMin, outputYMax, outerSize, outerSize);

        List<FetchedTile> tiles = new ArrayList<>();
        for (int row = 0; row < 2; row++) {
            for (int col = 0; col < 2; col++) {
                byte[] tilePixels = new byte[innerSize * innerSize];
                for (int dy = 0; dy < innerSize; dy++) {
                    int srcOff = (row * innerSize + dy) * outerSize + col * innerSize;
                    System.arraycopy(expectedPixels, srcOff, tilePixels, dy * innerSize, innerSize);
                }
                int colPx = col * innerSize;
                int rowPx = row * innerSize;
                double xMin = outputXMin + colPx * pixelSize;
                double yMax = outputYMax - rowPx * pixelSize;
                byte[] tiff = synthesizeStripUint8Tiff(
                    innerSize, innerSize, tilePixels, pixelSize, xMin, yMax
                );
                tiles.add(new FetchedTile(
                    new TileCoordinate(
                        colPx, rowPx, innerSize, innerSize, row, col, 0, 0, List.of()
                    ),
                    tiff, innerSize, innerSize
                ));
            }
        }

        AssembledCogWriter.AssembleAndWriteDoFn doFn =
            new AssembledCogWriter.AssembleAndWriteDoFn(
                tempDir.toString(), parent, innerSize, outerSize, "deflate"
            );
        doFn.setup();
        doFn.process(KV.of(new OutputTileKey(0, 0), tiles));

        byte[] cogBytes = Files.readAllBytes(tempDir.resolve("tile_r0000_c0000.tif"));
        byte[] decoded = TestTiffReader.decodeCogPixels(cogBytes);
        assertArrayEquals(expectedPixels, decoded);
    }

    @Test
    void partialGroupEmitsSidecarWithMissingBlocks(@TempDir Path tempDir) throws Exception {
        int innerSize = 16;
        int outerSize = 32;
        double pixelSize = 1.0;
        double outputXMin = 0.0;
        double outputYMax = 32.0;
        PixelGrid parent = parentGrid(pixelSize, outputXMin, outputYMax, outerSize, outerSize);

        List<FetchedTile> tiles = new ArrayList<>();
        for (int row = 0; row < 2; row++) {
            for (int col = 0; col < 2; col++) {
                if (row == 1 && col == 1) {
                    continue;  // drop the bottom-right block
                }
                byte[] tilePixels = new byte[innerSize * innerSize];
                java.util.Arrays.fill(tilePixels, (byte) 0xAA);
                int colPx = col * innerSize;
                int rowPx = row * innerSize;
                double xMin = outputXMin + colPx * pixelSize;
                double yMax = outputYMax - rowPx * pixelSize;
                byte[] tiff = synthesizeStripUint8Tiff(
                    innerSize, innerSize, tilePixels, pixelSize, xMin, yMax
                );
                tiles.add(new FetchedTile(
                    new TileCoordinate(
                        colPx, rowPx, innerSize, innerSize, row, col, 0, 0, List.of()
                    ),
                    tiff, innerSize, innerSize
                ));
            }
        }

        AssembledCogWriter.AssembleAndWriteDoFn doFn =
            new AssembledCogWriter.AssembleAndWriteDoFn(
                tempDir.toString(), parent, innerSize, outerSize, "deflate"
            );
        doFn.setup();
        doFn.process(KV.of(new OutputTileKey(0, 0), tiles));

        Path cogPath = tempDir.resolve("tile_r0000_c0000.tif");
        org.junit.jupiter.api.Assertions.assertTrue(
            Files.exists(cogPath),
            "Partial group must still emit the COG (zero-filled at the gap)"
        );

        Path sidecarPath = tempDir.resolve("tile_r0000_c0000.tif.partial.json");
        org.junit.jupiter.api.Assertions.assertTrue(
            Files.exists(sidecarPath),
            "Partial group must emit a sidecar listing missing blocks"
        );
        String sidecar = Files.readString(sidecarPath);
        org.junit.jupiter.api.Assertions.assertTrue(
            sidecar.contains("\"tx\":1") && sidecar.contains("\"ty\":1"),
            "Sidecar must enumerate the missing block at (tx=1, ty=1); got: " + sidecar
        );
    }

    @Test
    void completeGroupWritesNoSidecar(@TempDir Path tempDir) throws Exception {
        int innerSize = 16;
        int outerSize = 32;
        byte[] expectedPixels = new byte[outerSize * outerSize];
        for (int i = 0; i < expectedPixels.length; i++) {
            expectedPixels[i] = (byte) (i & 0xFF);
        }
        double pixelSize = 1.0;
        PixelGrid parent = parentGrid(pixelSize, 0.0, 32.0, outerSize, outerSize);

        List<FetchedTile> tiles = new ArrayList<>();
        for (int row = 0; row < 2; row++) {
            for (int col = 0; col < 2; col++) {
                byte[] tilePixels = new byte[innerSize * innerSize];
                for (int dy = 0; dy < innerSize; dy++) {
                    int srcOff = (row * innerSize + dy) * outerSize + col * innerSize;
                    System.arraycopy(expectedPixels, srcOff, tilePixels, dy * innerSize, innerSize);
                }
                int colPx = col * innerSize;
                int rowPx = row * innerSize;
                double xMin = colPx * pixelSize;
                double yMax = 32.0 - rowPx * pixelSize;
                byte[] tiff = synthesizeStripUint8Tiff(
                    innerSize, innerSize, tilePixels, pixelSize, xMin, yMax
                );
                tiles.add(new FetchedTile(
                    new TileCoordinate(
                        colPx, rowPx, innerSize, innerSize, row, col, 0, 0, List.of()
                    ),
                    tiff, innerSize, innerSize
                ));
            }
        }

        AssembledCogWriter.AssembleAndWriteDoFn doFn =
            new AssembledCogWriter.AssembleAndWriteDoFn(
                tempDir.toString(), parent, innerSize, outerSize, "deflate"
            );
        doFn.setup();
        doFn.process(KV.of(new OutputTileKey(0, 0), tiles));

        org.junit.jupiter.api.Assertions.assertFalse(
            Files.exists(tempDir.resolve("tile_r0000_c0000.tif.partial.json")),
            "Complete group must NOT emit a partial sidecar"
        );
    }

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

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
        ifd.putInt(0);
        out.writeBytes(ifd.array());

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
        buf.putShort((short) 3);
        buf.putInt(1);
        buf.putShort((short) value);
        buf.putShort((short) 0);
    }

    private static void writeLongInline(ByteBuffer buf, int tag, int value) {
        buf.putShort((short) tag);
        buf.putShort((short) 4);
        buf.putInt(1);
        buf.putInt(value);
    }

    private static void writeDoubleArrayOffset(ByteBuffer buf, int tag, int count, int offset) {
        buf.putShort((short) tag);
        buf.putShort((short) 12);
        buf.putInt(count);
        buf.putInt(offset);
    }
}
