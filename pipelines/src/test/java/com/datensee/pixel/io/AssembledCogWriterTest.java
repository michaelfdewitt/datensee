package com.datensee.pixel.io;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.datensee.fetch.EeErrorKind;
import com.datensee.pixel.AffineTransform;
import com.datensee.pixel.FailedTileRecord;
import com.datensee.pixel.FetchedTile;
import com.datensee.pixel.GridDimensions;
import com.datensee.pixel.OutputTileKey;
import com.datensee.pixel.PixelGrid;
import com.datensee.pixel.TileCoordinate;
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
                tempDir.toString(), parent, innerSize, outerSize, "deflate", false, null
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
                tempDir.toString(), parent, innerSize, outerSize, "deflate", false, null
            );
        doFn.setup();
        doFn.process(KV.of(new OutputTileKey(0, 0), tiles));

        byte[] cogBytes = Files.readAllBytes(tempDir.resolve("tile_r0000_c0000.tif"));
        byte[] decoded = TestTiffReader.decodeCogPixels(cogBytes);
        assertArrayEquals(expectedPixels, decoded);
    }

    @Test
    void partialGroupZeroFillsMissingBlockAndWritesNoSidecar(@TempDir Path tempDir)
        throws Exception {
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
                tempDir.toString(), parent, innerSize, outerSize, "deflate", false, null
            );
        doFn.setup();
        List<FailedTileRecord> failures =
            doFn.process(KV.of(new OutputTileKey(0, 0), tiles));
        assertTrue(failures.isEmpty(), "Partial group is not a write failure");

        Path cogPath = tempDir.resolve("tile_r0000_c0000.tif");
        assertTrue(
            Files.exists(cogPath),
            "Partial group must still emit the COG (zero-filled at the gap)"
        );
        byte[] decoded = TestTiffReader.decodeCogPixels(Files.readAllBytes(cogPath));
        for (int y = innerSize; y < outerSize; y++) {
            for (int x = innerSize; x < outerSize; x++) {
                assertEquals(0, decoded[y * outerSize + x], "Missing block must be zero-filled");
            }
        }
        assertEquals((byte) 0xAA, decoded[0], "Present blocks keep their data");

        // _failures.json is the canonical record of missing data; the
        // assembler must not emit a per-file sidecar.
        assertFalse(Files.exists(tempDir.resolve("tile_r0000_c0000.tif.partial.json")));
    }

    @Test
    void nonZeroOutputKeyDerivesOriginFromKeyNotGroupOrder(@TempDir Path tempDir)
        throws Exception {
        // Output tile (1, 2) in a parent grid spanning 3x2 output tiles.
        // Pre-fix, the origin was snapped from whichever tile GroupByKey
        // yielded first; now it must come from the key itself. Feed the
        // tiles in an order whose FIRST element is NOT the block-(0,0)
        // tile to pin order-independence.
        int innerSize = 16;
        int outerSize = 32;
        double pixelSize = 2.0;
        double parentXMin = 1000.0;
        double parentYMax = 5000.0;
        PixelGrid parent = parentGrid(pixelSize, parentXMin, parentYMax, 96, 64);

        int originColPx = 2 * outerSize;  // out_col = 2
        int originRowPx = 1 * outerSize;  // out_row = 1

        byte[] expectedPixels = new byte[outerSize * outerSize];
        for (int i = 0; i < expectedPixels.length; i++) {
            expectedPixels[i] = (byte) ((i * 17) & 0xFF);
        }

        List<FetchedTile> tiles = new ArrayList<>();
        for (int row = 0; row < 2; row++) {
            for (int col = 0; col < 2; col++) {
                byte[] tilePixels = new byte[innerSize * innerSize];
                for (int dy = 0; dy < innerSize; dy++) {
                    int srcOff = (row * innerSize + dy) * outerSize + col * innerSize;
                    System.arraycopy(expectedPixels, srcOff, tilePixels, dy * innerSize, innerSize);
                }
                int colPx = originColPx + col * innerSize;
                int rowPx = originRowPx + row * innerSize;
                byte[] tiff = synthesizeStripUint8Tiff(
                    innerSize, innerSize, tilePixels, pixelSize,
                    parentXMin + colPx * pixelSize, parentYMax - rowPx * pixelSize
                );
                tiles.add(new FetchedTile(
                    new TileCoordinate(
                        colPx, rowPx, innerSize, innerSize, row, col, 1, 2, List.of()
                    ),
                    tiff, innerSize, innerSize
                ));
            }
        }
        // Rotate so the first tile in the iterable is the block-(1,1) tile.
        java.util.Collections.rotate(tiles, 1);

        AssembledCogWriter.AssembleAndWriteDoFn doFn =
            new AssembledCogWriter.AssembleAndWriteDoFn(
                tempDir.toString(), parent, innerSize, outerSize, "deflate", false, null
            );
        doFn.setup();
        List<FailedTileRecord> failures =
            doFn.process(KV.of(new OutputTileKey(1, 2), tiles));
        assertTrue(failures.isEmpty());

        byte[] cogBytes = Files.readAllBytes(tempDir.resolve("tile_r0001_c0002.tif"));
        assertArrayEquals(expectedPixels, TestTiffReader.decodeCogPixels(cogBytes));

        // ModelTiepoint must anchor at the key-derived origin.
        double[] tiepoint = new TestTiffReader(cogBytes).doubleArrayTag(33922);
        assertEquals(parentXMin + originColPx * pixelSize, tiepoint[3], 1e-9);
        assertEquals(parentYMax - originRowPx * pixelSize, tiepoint[4], 1e-9);
    }

    @Test
    void quadtreeSplitChildrenAssembleIntoTheirBlock(@TempDir Path tempDir) throws Exception {
        // One block of the output tile is covered by four 8x8 split
        // children instead of a single 16x16 root tile — the retry path
        // after a MEMORY_EXCEEDED split. All four children must land at
        // their sub-block offsets.
        int innerSize = 16;
        int outerSize = 32;
        int half = innerSize / 2;
        double pixelSize = 1.0;
        PixelGrid parent = parentGrid(pixelSize, 0.0, 32.0, outerSize, outerSize);

        byte[] expectedPixels = new byte[outerSize * outerSize];
        for (int i = 0; i < expectedPixels.length; i++) {
            expectedPixels[i] = (byte) ((i * 29) & 0xFF);
        }

        List<FetchedTile> tiles = new ArrayList<>();
        for (int row = 0; row < 2; row++) {
            for (int col = 0; col < 2; col++) {
                if (row == 0 && col == 1) {
                    // This block arrives as 4 split children.
                    for (int qy = 0; qy < 2; qy++) {
                        for (int qx = 0; qx < 2; qx++) {
                            int colPx = col * innerSize + qx * half;
                            int rowPx = row * innerSize + qy * half;
                            byte[] childPixels = new byte[half * half];
                            for (int dy = 0; dy < half; dy++) {
                                int srcOff = (rowPx + dy) * outerSize + colPx;
                                System.arraycopy(
                                    expectedPixels, srcOff, childPixels, dy * half, half
                                );
                            }
                            byte[] tiff = synthesizeStripUint8Tiff(
                                half, half, childPixels, pixelSize,
                                colPx * pixelSize, 32.0 - rowPx * pixelSize
                            );
                            tiles.add(new FetchedTile(
                                new TileCoordinate(
                                    colPx, rowPx, half, half, row, col, 0, 0,
                                    List.of(qy * 2 + qx)
                                ),
                                tiff, half, half
                            ));
                        }
                    }
                    continue;
                }
                byte[] tilePixels = new byte[innerSize * innerSize];
                for (int dy = 0; dy < innerSize; dy++) {
                    int srcOff = (row * innerSize + dy) * outerSize + col * innerSize;
                    System.arraycopy(expectedPixels, srcOff, tilePixels, dy * innerSize, innerSize);
                }
                int colPx = col * innerSize;
                int rowPx = row * innerSize;
                byte[] tiff = synthesizeStripUint8Tiff(
                    innerSize, innerSize, tilePixels, pixelSize,
                    colPx * pixelSize, 32.0 - rowPx * pixelSize
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
                tempDir.toString(), parent, innerSize, outerSize, "deflate", false, null
            );
        doFn.setup();
        List<FailedTileRecord> failures =
            doFn.process(KV.of(new OutputTileKey(0, 0), tiles));
        assertTrue(failures.isEmpty());

        byte[] cogBytes = Files.readAllBytes(tempDir.resolve("tile_r0000_c0000.tif"));
        assertArrayEquals(expectedPixels, TestTiffReader.decodeCogPixels(cogBytes));
    }

    @Test
    void retryRoundMergesIntoExistingCogInsteadOfZeroFilling(@TempDir Path tempDir)
        throws Exception {
        // Round 1: three of four blocks succeed → partial COG on disk.
        // Round 2 (retry): ONLY the previously-missing tile arrives. With
        // merge_existing_output set (as `datensee retry` does), the
        // assembler must merge it into the existing file — the three good
        // blocks from round 1 must survive.
        int innerSize = 16;
        int outerSize = 32;
        double pixelSize = 1.0;
        PixelGrid parent = parentGrid(pixelSize, 0.0, 32.0, outerSize, outerSize);

        byte[] expectedPixels = new byte[outerSize * outerSize];
        for (int i = 0; i < expectedPixels.length; i++) {
            expectedPixels[i] = (byte) ((i * 13 + 5) & 0xFF);
        }

        AssembledCogWriter.AssembleAndWriteDoFn doFn =
            new AssembledCogWriter.AssembleAndWriteDoFn(
                tempDir.toString(), parent, innerSize, outerSize, "deflate", true, null
            );
        doFn.setup();

        List<FetchedTile> round1 = new ArrayList<>();
        FetchedTile missing = null;
        for (int row = 0; row < 2; row++) {
            for (int col = 0; col < 2; col++) {
                byte[] tilePixels = new byte[innerSize * innerSize];
                for (int dy = 0; dy < innerSize; dy++) {
                    int srcOff = (row * innerSize + dy) * outerSize + col * innerSize;
                    System.arraycopy(expectedPixels, srcOff, tilePixels, dy * innerSize, innerSize);
                }
                int colPx = col * innerSize;
                int rowPx = row * innerSize;
                byte[] tiff = synthesizeStripUint8Tiff(
                    innerSize, innerSize, tilePixels, pixelSize,
                    colPx * pixelSize, 32.0 - rowPx * pixelSize
                );
                FetchedTile tile = new FetchedTile(
                    new TileCoordinate(
                        colPx, rowPx, innerSize, innerSize, row, col, 0, 0, List.of()
                    ),
                    tiff, innerSize, innerSize
                );
                if (row == 1 && col == 0) {
                    missing = tile;  // fails in round 1, retried in round 2
                } else {
                    round1.add(tile);
                }
            }
        }

        doFn.process(KV.of(new OutputTileKey(0, 0), round1));
        doFn.process(KV.of(new OutputTileKey(0, 0), List.of(missing)));

        byte[] cogBytes = Files.readAllBytes(tempDir.resolve("tile_r0000_c0000.tif"));
        assertArrayEquals(
            expectedPixels, TestTiffReader.decodeCogPixels(cogBytes),
            "Retry merge must preserve round-1 blocks and fill the retried block"
        );
    }

    @Test
    void nonTwoTierMergeOverlaysSplitChildrenIntoExistingSingleTileCog(@TempDir Path tempDir)
        throws Exception {
        // Non-two-tier retry: output tile == compute tile (single-block COG).
        // Round 1 wrote the full tile; round 2 re-fetches two quadtree
        // children (8x8 within the 16px tile) that must overlay in place —
        // the untouched quadrants keep round-1 data.
        int size = 16;
        int half = size / 2;
        double pixelSize = 1.0;
        PixelGrid parent = parentGrid(pixelSize, 0.0, 16.0, size, size);

        byte[] round1Pixels = new byte[size * size];
        java.util.Arrays.fill(round1Pixels, (byte) 0x11);

        AssembledCogWriter.AssembleAndWriteDoFn doFn =
            new AssembledCogWriter.AssembleAndWriteDoFn(
                tempDir.toString(), parent, size, size, "deflate", true, null
            );
        doFn.setup();

        FetchedTile root = new FetchedTile(
            new TileCoordinate(0, 0, size, size, 0, 0, 0, 0, List.of()),
            synthesizeStripUint8Tiff(size, size, round1Pixels, pixelSize, 0.0, 16.0),
            size, size
        );
        doFn.process(KV.of(new OutputTileKey(0, 0), List.of(root)));

        // Round 2: children for quadrants q1 (x-high/y-low → col 8, row 8)
        // and q2 (x-low/y-high → col 0, row 0), new value 0x77.
        byte[] childPixels = new byte[half * half];
        java.util.Arrays.fill(childPixels, (byte) 0x77);
        List<FetchedTile> children = List.of(
            new FetchedTile(
                new TileCoordinate(half, half, half, half, 0, 0, 0, 0, List.of(1)),
                synthesizeStripUint8Tiff(half, half, childPixels, pixelSize, 8.0, 8.0),
                half, half
            ),
            new FetchedTile(
                new TileCoordinate(0, 0, half, half, 0, 0, 0, 0, List.of(2)),
                synthesizeStripUint8Tiff(half, half, childPixels, pixelSize, 0.0, 16.0),
                half, half
            )
        );
        List<FailedTileRecord> failures =
            doFn.process(KV.of(new OutputTileKey(0, 0), children));
        assertTrue(failures.isEmpty());

        byte[] decoded = TestTiffReader.decodeCogPixels(
            Files.readAllBytes(tempDir.resolve("tile_r0000_c0000.tif"))
        );
        for (int y = 0; y < size; y++) {
            for (int x = 0; x < size; x++) {
                boolean covered = (x >= half && y >= half) || (x < half && y < half);
                byte expected = covered ? (byte) 0x77 : (byte) 0x11;
                assertEquals(expected, decoded[y * size + x],
                    "pixel (" + x + "," + y + ")");
            }
        }
    }

    @Test
    void nodataValueIsStampedAsGdalNodataTag(@TempDir Path tempDir) throws Exception {
        int size = 16;
        byte[] pixels = new byte[size * size];
        PixelGrid parent = parentGrid(1.0, 0.0, 16.0, size, size);

        AssembledCogWriter.AssembleAndWriteDoFn doFn =
            new AssembledCogWriter.AssembleAndWriteDoFn(
                tempDir.toString(), parent, size, size, "deflate", false, -9999.0
            );
        doFn.setup();
        doFn.process(KV.of(new OutputTileKey(0, 0), List.of(new FetchedTile(
            new TileCoordinate(0, 0, size, size, 0, 0, 0, 0, List.of()),
            synthesizeStripUint8Tiff(size, size, pixels, 1.0, 0.0, 16.0),
            size, size
        ))));

        byte[] cog = Files.readAllBytes(tempDir.resolve("tile_r0000_c0000.tif"));
        String tagValue = readAsciiTag(cog, 42113);
        assertEquals("-9999", tagValue,
            "GDAL_NODATA (42113) must carry the configured nodata value");
    }

    /** Read a TIFF ASCII tag's value (without the NUL terminator). */
    private static String readAsciiTag(byte[] tiff, int wantedTag) {
        ByteBuffer buf = ByteBuffer.wrap(tiff).order(ByteOrder.LITTLE_ENDIAN);
        buf.position(buf.getInt(4));
        int n = Short.toUnsignedInt(buf.getShort());
        for (int i = 0; i < n; i++) {
            int tag = Short.toUnsignedInt(buf.getShort());
            int type = Short.toUnsignedInt(buf.getShort());
            int count = buf.getInt();
            int valuePos = buf.position();
            if (tag == wantedTag && type == 2) {
                int offset = count <= 4 ? valuePos : buf.getInt(valuePos);
                StringBuilder sb = new StringBuilder();
                for (int b = 0; b < count - 1 && tiff[offset + b] != 0; b++) {
                    sb.append((char) tiff[offset + b]);
                }
                return sb.toString();
            }
            buf.position(valuePos + 4);
        }
        throw new AssertionError("Tag " + wantedTag + " not found");
    }

    @Test
    void corruptTileDeadLettersInsteadOfThrowing(@TempDir Path tempDir) {
        int innerSize = 16;
        int outerSize = 32;
        PixelGrid parent = parentGrid(1.0, 0.0, 32.0, outerSize, outerSize);

        FetchedTile corrupt = new FetchedTile(
            new TileCoordinate(0, 0, innerSize, innerSize, 0, 0, 0, 0, List.of()),
            new byte[] {1, 2, 3, 4},  // not a TIFF
            innerSize, innerSize
        );

        AssembledCogWriter.AssembleAndWriteDoFn doFn =
            new AssembledCogWriter.AssembleAndWriteDoFn(
                tempDir.toString(), parent, innerSize, outerSize, "deflate", false, null
            );
        doFn.setup();
        List<FailedTileRecord> failures =
            doFn.process(KV.of(new OutputTileKey(0, 0), List.of(corrupt)));

        assertEquals(1, failures.size(), "Write failure must dead-letter, not throw");
        assertEquals(EeErrorKind.UNKNOWN, failures.getFirst().errorKind());
        assertTrue(failures.getFirst().errorMessage().startsWith("write-stage:"));
        assertFalse(Files.exists(tempDir.resolve("tile_r0000_c0000.tif")));
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
