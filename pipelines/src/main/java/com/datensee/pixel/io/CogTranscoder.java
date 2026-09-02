package com.datensee.pixel.io;

import com.datensee.pixel.AffineTransform;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Transcodes raw GeoTIFF bytes into Cloud Optimized GeoTIFF format.
 *
 * <p>The EE High Volume API returns standard GeoTIFFs with a single-strip
 * layout and no compression. This transcoder rewrites the TIFF structure
 * to tiled layout with deflate (zlib) compression, producing a valid COG
 * that {@code ee.Image.loadGeoTIFF()} can read.
 *
 * <p>Approach: operates directly on TIFF bytes — no pixel decoding/encoding
 * via BufferedImage (which would lose float32 precision). Reads the IFD,
 * extracts raw pixel strips, deflate-compresses the pixel data, and writes
 * a new TIFF with tiled layout and all original GeoTIFF metadata tags
 * preserved.
 *
 * <p>Zero external dependencies — pure Java (zlib via {@code java.util.zip}).
 */
public final class CogTranscoder {

    private static final Logger LOG = LoggerFactory.getLogger(CogTranscoder.class);

    // TIFF tag constants
    private static final int TAG_IMAGE_WIDTH = 256;
    private static final int TAG_IMAGE_LENGTH = 257;
    private static final int TAG_BITS_PER_SAMPLE = 258;
    private static final int TAG_COMPRESSION = 259;
    private static final int TAG_STRIP_OFFSETS = 273;
    private static final int TAG_ROWS_PER_STRIP = 278;
    private static final int TAG_STRIP_BYTE_COUNTS = 279;
    private static final int TAG_TILE_WIDTH = 322;
    private static final int TAG_TILE_LENGTH = 323;
    private static final int TAG_TILE_OFFSETS = 324;
    private static final int TAG_TILE_BYTE_COUNTS = 325;
    private static final int TAG_SAMPLE_FORMAT = 339;
    private static final int TAG_PREDICTOR = 317;
    private static final int TAG_PLANAR_CONFIGURATION = 284;
    private static final int TAG_MODEL_PIXEL_SCALE = 33550;
    private static final int TAG_MODEL_TIEPOINT = 33922;
    private static final int TAG_GDAL_NODATA = 42113;

    // TIFF type sizes (in bytes)
    private static final int[] TYPE_SIZES = {0, 1, 1, 2, 4, 8, 1, 1, 2, 4, 8, 4, 8};

    // Compression values
    private static final int COMPRESS_NONE = 1;
    private static final int COMPRESS_DEFLATE = 8;

    private CogTranscoder() { }

    /**
     * Transcode raw GeoTIFF bytes to COG format.
     *
     * @param rawGeotiff  bytes from the EE HV API (standard GeoTIFF)
     * @param tileSize    tile edge size in pixels (used as COG block size)
     * @param compression compression algorithm ("deflate" or "none")
     * @return COG-encoded bytes
     * @throws IOException if transcoding fails
     */
    public static byte[] transcode(
        byte[] rawGeotiff,
        int tileSize,
        String compression
    ) throws IOException {
        return transcode(rawGeotiff, tileSize, compression, null);
    }

    /**
     * Variant of {@link #transcode(byte[], int, String)} that additionally
     * stamps a {@code GDAL_NODATA} tag (42113) when {@code nodata} is
     * non-null. EE's {@code computePixels} GeoTIFFs return masked pixels
     * as 0 with no mask channel, so callers who {@code unmask(sentinel)}
     * their expression can declare the sentinel here and downstream GIS
     * tools treat it as nodata instead of a real value.
     */
    public static byte[] transcode(
        byte[] rawGeotiff,
        int tileSize,
        String compression,
        Double nodata
    ) throws IOException {
        ParsedTiff parsed = readIfd(rawGeotiff);
        ByteOrder order = parsed.order;
        Map<Integer, IfdEntry> entries = parsed.entries;

        // PlanarConfiguration: 1=chunky (samples interleaved per pixel),
        // 2=planar (each sample in its own plane). Our extraction
        // concatenates strip/tile bytes verbatim, which only round-trips
        // for chunky. Reject planar before producing a misaligned COG.
        if (entries.containsKey(TAG_PLANAR_CONFIGURATION)) {
            int planar = getIntValue(entries, TAG_PLANAR_CONFIGURATION);
            if (planar != 1) {
                throw new IOException(
                    "Unsupported PlanarConfiguration=" + planar
                    + " (only chunky/1 is supported). EE HV API normally returns chunky;"
                    + " a planar response indicates an unexpected request shape."
                );
            }
        }

        int width = getIntValue(entries, TAG_IMAGE_WIDTH);
        int height = getIntValue(entries, TAG_IMAGE_LENGTH);
        int bitsPerSample = getIntValue(entries, TAG_BITS_PER_SAMPLE);
        int samplesPerPixel = entries.containsKey(277)
            ? getIntValue(entries, 277) : 1;  // 277 = SamplesPerPixel

        // Multi-tile invariant: the COG's internal block size is `tileSize`,
        // so the image dimensions must be exact multiples of it. Partial-edge
        // tiles aren't supported — the caller (TileFetchDoFn or two-tier assembler)
        // is responsible for producing aligned dimensions.
        if (width % tileSize != 0 || height % tileSize != 0) {
            throw new IOException(
                "Input image " + width + "x" + height
                + " is not a whole-number multiple of tileSize=" + tileSize
                + ". Edge-tile padding is not supported; use a tileSize that"
                + " evenly divides both dimensions."
            );
        }
        int tilesAcross = width / tileSize;
        int tilesDown = height / tileSize;

        // Extract pixel data from strips or tiles
        byte[] pixelData = extractPixelData(entries, rawGeotiff, order);

        // Slice the big buffer into per-block pixel chunks; partitionCompressBuild
        // does the compression + COG assembly.
        int bytesPerSample = bitsPerSample / 8;
        List<byte[]> blocks = new ArrayList<>(tilesAcross * tilesDown);
        for (int ty = 0; ty < tilesDown; ty++) {
            for (int tx = 0; tx < tilesAcross; tx++) {
                blocks.add(extractBlock(
                    pixelData, width, tileSize, tx, ty,
                    samplesPerPixel, bytesPerSample
                ));
            }
        }

        if (nodata != null) {
            entries.put(TAG_GDAL_NODATA,
                IfdEntry.asciiValue(TAG_GDAL_NODATA, formatNodata(nodata)));
        }
        return partitionCompressBuild(
            blocks, width, height, tileSize,
            bitsPerSample, samplesPerPixel,
            entries, order, compression
        );
    }

    /**
     * Multi-tile entry point for the two-tier assembler.
     *
     * <p>Takes a list of per-tile pixel buffers in row-major order
     * ({@code [ty * tilesAcross + tx]}), one entry per inner COG block.
     * {@code null} entries are zero-filled (used when a compute tile in
     * the group failed upstream — the COG still tiles cleanly with a
     * blank block where the missing data would have lived).
     *
     * <p>The {@link AffineTransform} supplies the output tile's affine in
     * CRS units; the transcoder writes both the {@code ModelTiepointTag}
     * (from {@code translateX}/{@code translateY}) and the
     * {@code ModelPixelScaleTag} (from {@code |scaleX|}/{@code |scaleY|}),
     * so the COG is authoritatively georeferenced from the parent grid
     * rather than inheriting a possibly-stale {@code ModelPixelScale}
     * from the source compute tile. Other GeoTIFF tags
     * (GeoKeyDirectoryTag, GeoAsciiParams, etc.) are inherited from the
     * supplied source tile — every compute tile shares them.
     *
     * @param tilePixels             row-major list of per-block pixel
     *                               buffers (length = {@code (width/tileSize)
     *                               * (height/tileSize)}); each non-null
     *                               entry must be {@code tileSize *
     *                               tileSize * samples * bytes-per-sample}
     *                               bytes; null entries are zero-filled
     * @param width                  output image width in pixels
     *                               (multiple of tileSize)
     * @param height                 output image height in pixels
     *                               (multiple of tileSize)
     * @param tileSize               COG internal block size = compute tile size
     * @param sourceTiffForMetadata  any compute tile from the group; used
     *                               for sample structure and CRS metadata
     * @param outputAffine           output tile's affine transform — used
     *                               to write ModelTiepoint and
     *                               ModelPixelScale tags
     * @param compression            compression algorithm ("deflate" or "none")
     */
    public static byte[] transcodeFromTileBlocks(
        List<byte[]> tilePixels,
        int width, int height,
        int tileSize,
        byte[] sourceTiffForMetadata,
        AffineTransform outputAffine,
        String compression
    ) throws IOException {
        return transcodeFromTileBlocks(
            tilePixels, width, height, tileSize,
            sourceTiffForMetadata, outputAffine, compression, null
        );
    }

    /** Nodata-aware variant — see {@link #transcode(byte[], int, String, Double)}. */
    public static byte[] transcodeFromTileBlocks(
        List<byte[]> tilePixels,
        int width, int height,
        int tileSize,
        byte[] sourceTiffForMetadata,
        AffineTransform outputAffine,
        String compression,
        Double nodata
    ) throws IOException {
        if (width % tileSize != 0 || height % tileSize != 0) {
            throw new IOException(
                "Output image " + width + "x" + height
                + " is not a whole-number multiple of tileSize=" + tileSize
            );
        }

        // Parse sourceTiff to inherit sample structure + GeoTIFF tags.
        ParsedTiff parsed = readIfd(sourceTiffForMetadata);
        ByteOrder order = parsed.order;
        Map<Integer, IfdEntry> sourceEntries = parsed.entries;

        int bitsPerSample = getIntValue(sourceEntries, TAG_BITS_PER_SAMPLE);
        int samplesPerPixel = sourceEntries.containsKey(277)
            ? getIntValue(sourceEntries, 277) : 1;

        // Override ImageWidth/ImageLength + ModelTiepoint + ModelPixelScale
        // — everything else (GeoKeyDirectory, GeoAsciiParams, sample-
        // structure tags) is inherited via the copy loop in buildCogTiff.
        Map<Integer, IfdEntry> overridden = new LinkedHashMap<>(sourceEntries);
        overridden.put(TAG_IMAGE_WIDTH, IfdEntry.shortValue(TAG_IMAGE_WIDTH, width));
        overridden.put(TAG_IMAGE_LENGTH, IfdEntry.shortValue(TAG_IMAGE_LENGTH, height));
        // ModelPixelScale wants |scaleX|, |scaleY|, 0.0 — the GeoTIFF
        // convention's pixel-scale Y is positive (north-up sign comes
        // from ModelTiepoint + the implicit row-down raster axis).
        overridden.put(TAG_MODEL_PIXEL_SCALE, IfdEntry.doubleArray(
            TAG_MODEL_PIXEL_SCALE,
            new double[] {Math.abs(outputAffine.scaleX()), Math.abs(outputAffine.scaleY()), 0.0}
        ));
        overridden.put(TAG_MODEL_TIEPOINT, IfdEntry.doubleArray(TAG_MODEL_TIEPOINT, new double[] {
            0.0, 0.0, 0.0,
            outputAffine.translateX(), outputAffine.translateY(), 0.0
        }));
        if (nodata != null) {
            overridden.put(TAG_GDAL_NODATA,
                IfdEntry.asciiValue(TAG_GDAL_NODATA, formatNodata(nodata)));
        }

        return partitionCompressBuild(
            tilePixels, width, height, tileSize,
            bitsPerSample, samplesPerPixel,
            overridden, order, compression
        );
    }

    /**
     * Shared multi-block emit path: take a list of per-block uncompressed
     * pixel buffers (row-major, {@code null} for missing → zero-filled),
     * compress each block independently (TIFF spec requires self-contained
     * tiles), then assemble into the final COG layout.
     */
    private static byte[] partitionCompressBuild(
        List<byte[]> tilePixels,
        int width, int height,
        int tileSize,
        int bitsPerSample, int samplesPerPixel,
        Map<Integer, IfdEntry> entries,
        ByteOrder order,
        String compression
    ) throws IOException {
        int tilesAcross = width / tileSize;
        int tilesDown = height / tileSize;
        int expectedBlocks = tilesAcross * tilesDown;
        if (tilePixels.size() != expectedBlocks) {
            throw new IOException(
                "Expected " + expectedBlocks + " pixel blocks ("
                + tilesAcross + "x" + tilesDown + " for "
                + width + "x" + height + " at tileSize=" + tileSize
                + "), got " + tilePixels.size()
            );
        }

        LOG.debug(
            "Assembling: {}x{}, {}bps, {} samples, {}x{} blocks",
            width, height, bitsPerSample, samplesPerPixel,
            tilesAcross, tilesDown
        );

        int compressCode = switch (compression.toLowerCase()) {
            case "deflate" -> COMPRESS_DEFLATE;
            case "none" -> COMPRESS_NONE;
            default -> throw new IOException(
                "Unsupported COG compression '" + compression
                + "'. Supported: \"deflate\" (default), \"none\"."
            );
        };

        int bytesPerSample = bitsPerSample / 8;
        int blockBytes = tileSize * tileSize * samplesPerPixel * bytesPerSample;

        List<byte[]> compressedBlocks = new ArrayList<>(expectedBlocks);
        for (byte[] block : tilePixels) {
            if (block == null) {
                block = new byte[blockBytes];  // zero-fill missing tile
            } else if (block.length != blockBytes) {
                throw new IOException(
                    "Block pixel buffer is " + block.length
                    + " bytes; expected " + blockBytes
                    + " (tileSize=" + tileSize + ", samples=" + samplesPerPixel
                    + ", bytes/sample=" + bytesPerSample + ")"
                );
            }
            byte[] compressedBlock = compressCode == COMPRESS_DEFLATE
                ? deflateCompress(block)
                : block;
            compressedBlocks.add(compressedBlock);
        }

        return buildCogTiff(
            entries, compressedBlocks, order,
            width, height, tileSize, compressCode
        );
    }

    /**
     * Extract just the raw row-major pixel bytes from a TIFF, decompressing
     * if needed. Used by the two-tier assembler to read each compute tile's
     * pixels before stitching them into an output buffer.
     */
    public static byte[] extractPixelsFromTiff(byte[] rawTiff) throws IOException {
        ParsedTiff parsed = readIfd(rawTiff);
        return extractPixelData(parsed.entries, rawTiff, parsed.order);
    }

    /**
     * Pixel-buffer dimensions and sample structure of a TIFF, read from its
     * IFD without decoding any pixel data. Lets the two-tier assembler size and
     * stride its assembly canvas from the first tile in a group.
     */
    public record TiffPixelLayout(
        int width, int height, int samplesPerPixel, int bytesPerSample
    ) {
        /** Bytes per pixel across all samples (chunky layout). */
        public int bytesPerPixel() {
            return samplesPerPixel * bytesPerSample;
        }
    }

    /** Read the {@link TiffPixelLayout} of a TIFF from its IFD. */
    public static TiffPixelLayout readPixelLayout(byte[] rawTiff) throws IOException {
        ParsedTiff parsed = readIfd(rawTiff);
        Map<Integer, IfdEntry> entries = parsed.entries;
        int width = getIntValue(entries, TAG_IMAGE_WIDTH);
        int height = getIntValue(entries, TAG_IMAGE_LENGTH);
        int bitsPerSample = getIntValue(entries, TAG_BITS_PER_SAMPLE);
        int samplesPerPixel = entries.containsKey(277) ? getIntValue(entries, 277) : 1;
        return new TiffPixelLayout(width, height, samplesPerPixel, bitsPerSample / 8);
    }

    /**
     * Multi-block entry point over a fully-assembled pixel canvas.
     *
     * <p>Takes the complete row-major pixel buffer of the output image
     * (length {@code width * height * samples * bytes-per-sample}), slices
     * it into {@code tileSize}-square blocks, and emits the COG. Metadata
     * handling matches {@link #transcodeFromTileBlocks}: sample structure
     * and GeoTIFF tags are inherited from {@code sourceTiffForMetadata},
     * georeferencing is overridden from {@code outputAffine}.
     *
     * <p>This is the two-tier assembler's entry point: the assembler composes
     * baseline pixels (from a previously-written COG, when merging a retry
     * round) and freshly-fetched tiles — including sub-block quadtree
     * children — onto one canvas, so the transcoder never needs to know
     * about placement.
     */
    public static byte[] transcodeFromPixelBuffer(
        byte[] pixels,
        int width, int height,
        int tileSize,
        byte[] sourceTiffForMetadata,
        AffineTransform outputAffine,
        String compression
    ) throws IOException {
        return transcodeFromPixelBuffer(
            pixels, width, height, tileSize,
            sourceTiffForMetadata, outputAffine, compression, null
        );
    }

    /** Nodata-aware variant — see {@link #transcode(byte[], int, String, Double)}. */
    public static byte[] transcodeFromPixelBuffer(
        byte[] pixels,
        int width, int height,
        int tileSize,
        byte[] sourceTiffForMetadata,
        AffineTransform outputAffine,
        String compression,
        Double nodata
    ) throws IOException {
        TiffPixelLayout source = readPixelLayout(sourceTiffForMetadata);
        long expected = (long) width * height * source.bytesPerPixel();
        if (pixels.length != expected) {
            throw new IOException(
                "Pixel canvas is " + pixels.length + " bytes; expected " + expected
                + " (" + width + "x" + height + " x " + source.samplesPerPixel()
                + " sample(s) x " + source.bytesPerSample() + " byte(s)/sample)"
            );
        }
        int tilesAcross = width / tileSize;
        int tilesDown = height / tileSize;
        List<byte[]> blocks = new ArrayList<>(tilesAcross * tilesDown);
        for (int ty = 0; ty < tilesDown; ty++) {
            for (int tx = 0; tx < tilesAcross; tx++) {
                blocks.add(extractBlock(
                    pixels, width, tileSize, tx, ty,
                    source.samplesPerPixel(), source.bytesPerSample()
                ));
            }
        }
        return transcodeFromTileBlocks(
            blocks, width, height, tileSize,
            sourceTiffForMetadata, outputAffine, compression, nodata
        );
    }

    /**
     * Canonical ASCII form of a nodata value for the GDAL_NODATA tag.
     * GDAL parses this with atof(): "nan" for NaN, integral values
     * without a trailing ".0" (matches what GDAL itself writes), plain
     * {@code Double.toString} otherwise.
     */
    private static String formatNodata(double nodata) {
        if (Double.isNaN(nodata)) {
            return "nan";
        }
        if (nodata == Math.rint(nodata) && Math.abs(nodata) < 1e15) {
            return Long.toString((long) nodata);
        }
        return Double.toString(nodata);
    }

    /** Byte order + parsed IFD entries of a TIFF. */
    private record ParsedTiff(Map<Integer, IfdEntry> entries, ByteOrder order) { }

    /** Parse a TIFF header + first IFD; shared preamble of every entry point. */
    private static ParsedTiff readIfd(byte[] rawTiff) throws IOException {
        ByteBuffer buf = ByteBuffer.wrap(rawTiff);
        short bom = buf.getShort(0);
        ByteOrder order = (bom == 0x4D4D) ? ByteOrder.BIG_ENDIAN : ByteOrder.LITTLE_ENDIAN;
        buf.order(order);
        short magic = buf.getShort(2);
        if (magic != 42) {
            throw new IOException("Not a TIFF file (magic=" + magic + ")");
        }
        int ifdOffset = buf.getInt(4);
        buf.position(ifdOffset);
        int entryCount = Short.toUnsignedInt(buf.getShort());
        Map<Integer, IfdEntry> entries = new LinkedHashMap<>();
        for (int i = 0; i < entryCount; i++) {
            IfdEntry entry = IfdEntry.read(buf, order, rawTiff);
            entries.put(entry.tag, entry);
        }
        return new ParsedTiff(entries, order);
    }

    /**
     * Copy one {@code tileSize × tileSize} block out of a row-major pixel
     * buffer. The block is itself laid out row-major (within-block).
     *
     * @param pixelData       full image pixel bytes (W × H × samples × bytes)
     * @param width           full image width in pixels
     * @param tileSize        block edge length in pixels
     * @param tx              block column (0-based, from left)
     * @param ty              block row (0-based, from top)
     * @param samplesPerPixel number of samples (band count for chunky layout)
     * @param bytesPerSample  bytes per sample
     */
    private static byte[] extractBlock(
        byte[] pixelData,
        int width,
        int tileSize,
        int tx,
        int ty,
        int samplesPerPixel,
        int bytesPerSample
    ) {
        int rowBytes = width * samplesPerPixel * bytesPerSample;
        int blockRowBytes = tileSize * samplesPerPixel * bytesPerSample;
        byte[] block = new byte[tileSize * blockRowBytes];
        int srcXOff = tx * blockRowBytes;
        for (int dy = 0; dy < tileSize; dy++) {
            int srcOff = (ty * tileSize + dy) * rowBytes + srcXOff;
            int dstOff = dy * blockRowBytes;
            System.arraycopy(pixelData, srcOff, block, dstOff, blockRowBytes);
        }
        return block;
    }

    /**
     * Extract raw row-major pixel bytes from a TIFF, decompressing per
     * strip/tile and assembling tile-layout inputs into row order.
     *
     * <p>EE's High Volume {@code computePixels} endpoint returns
     * <em>tile-layout</em> TIFFs for any request larger than 256×256
     * (with 256×256 internal tile size), and each strip/tile is its
     * own self-contained zlib stream per the TIFF spec. Two implications:
     * <ul>
     *   <li>We must {@link #inflateData} each chunk independently —
     *       feeding the concatenated compressed bytes into a single
     *       {@code Inflater} stops at the first end-of-stream marker
     *       and silently drops everything past the first chunk.
     *   <li>For tile layout we must place each decompressed tile at its
     *       row-major position in the output buffer, not concatenate
     *       in tile order. EE's tile-layout responses use row-major
     *       tile indexing (TIFF spec).
     * </ul>
     */
    private static byte[] extractPixelData(
        Map<Integer, IfdEntry> entries,
        byte[] raw,
        ByteOrder order
    ) throws IOException {
        long[] offsets;
        long[] counts;
        boolean tileLayout;

        if (entries.containsKey(TAG_STRIP_OFFSETS)) {
            offsets = getLongArray(entries, TAG_STRIP_OFFSETS);
            counts = getLongArray(entries, TAG_STRIP_BYTE_COUNTS);
            tileLayout = false;
        } else if (entries.containsKey(TAG_TILE_OFFSETS)) {
            offsets = getLongArray(entries, TAG_TILE_OFFSETS);
            counts = getLongArray(entries, TAG_TILE_BYTE_COUNTS);
            tileLayout = true;
        } else {
            throw new IOException(
                "Input GeoTIFF has neither StripOffsets (273) nor TileOffsets (324)"
            );
        }

        if (offsets.length != counts.length) {
            throw new IOException(
                "Offsets count (" + offsets.length
                + ") != ByteCounts count (" + counts.length + ")"
            );
        }

        int inputCompression = entries.containsKey(TAG_COMPRESSION)
            ? getIntValue(entries, TAG_COMPRESSION) : 1;

        int width = getIntValue(entries, TAG_IMAGE_WIDTH);
        int height = getIntValue(entries, TAG_IMAGE_LENGTH);
        int bitsPerSample = getIntValue(entries, TAG_BITS_PER_SAMPLE);
        int samplesPerPixel = entries.containsKey(277) ? getIntValue(entries, 277) : 1;
        int bytesPerSample = bitsPerSample / 8;
        int rowBytes = width * samplesPerPixel * bytesPerSample;
        byte[] decoded = new byte[height * rowBytes];

        if (!tileLayout) {
            // STRIP LAYOUT: concatenate decompressed strip pixels in
            // strip order — strip data is row-major within each strip
            // and strips themselves are top-down, so the concatenation
            // is already row-major image data.
            int rowsPerStrip = entries.containsKey(TAG_ROWS_PER_STRIP)
                ? getIntValue(entries, TAG_ROWS_PER_STRIP) : height;
            int dstPos = 0;
            for (int i = 0; i < offsets.length; i++) {
                int rowsInThisStrip = Math.min(rowsPerStrip, height - i * rowsPerStrip);
                int stripExpected = rowsInThisStrip * rowBytes;
                byte[] stripBytes = readChunk(
                    raw, (int) offsets[i], (int) counts[i],
                    inputCompression, stripExpected
                );
                if (stripBytes.length != stripExpected) {
                    throw new IOException(
                        "Strip " + i + " decoded to " + stripBytes.length
                        + " bytes; expected " + stripExpected
                        + " (" + rowsInThisStrip + " rows × " + rowBytes + " bytes/row)"
                    );
                }
                System.arraycopy(stripBytes, 0, decoded, dstPos, stripBytes.length);
                dstPos += stripBytes.length;
            }
        } else {
            // TILE LAYOUT: each tile holds tileWidth × tileLength row-
            // major pixels, and tiles are arranged in row-major order
            // across the tile grid. Edge tiles still occupy a full
            // tileWidth × tileLength block on disk (with possibly-padded
            // pixels past the visible region) — we copy only the
            // visible portion into the output buffer.
            int tileWidth = getIntValue(entries, TAG_TILE_WIDTH);
            int tileLength = getIntValue(entries, TAG_TILE_LENGTH);
            int tilesAcross = (width + tileWidth - 1) / tileWidth;
            int tilesDown = (height + tileLength - 1) / tileLength;
            int tileExpected = tileWidth * tileLength * samplesPerPixel * bytesPerSample;
            int tileRowBytes = tileWidth * samplesPerPixel * bytesPerSample;

            if (offsets.length != tilesAcross * tilesDown) {
                throw new IOException(
                    "Tile count " + offsets.length + " ≠ " + tilesAcross
                    + "×" + tilesDown + " = " + (tilesAcross * tilesDown)
                    + " expected for " + width + "×" + height + " image with "
                    + tileWidth + "×" + tileLength + " tiles"
                );
            }

            for (int ty = 0; ty < tilesDown; ty++) {
                for (int tx = 0; tx < tilesAcross; tx++) {
                    int idx = ty * tilesAcross + tx;
                    byte[] tileBytes = readChunk(
                        raw, (int) offsets[idx], (int) counts[idx],
                        inputCompression, tileExpected
                    );
                    if (tileBytes.length != tileExpected) {
                        throw new IOException(
                            "Tile (" + tx + "," + ty + ") decoded to "
                            + tileBytes.length + " bytes; expected " + tileExpected
                            + " (" + tileWidth + "×" + tileLength + " × "
                            + samplesPerPixel + " sample(s) × " + bytesPerSample
                            + " byte(s)/sample)"
                        );
                    }
                    int visibleWidth = Math.min(tileWidth, width - tx * tileWidth);
                    int visibleHeight = Math.min(tileLength, height - ty * tileLength);
                    int copyRowBytes = visibleWidth * samplesPerPixel * bytesPerSample;
                    int dstColBytes = tx * tileWidth * samplesPerPixel * bytesPerSample;
                    for (int dy = 0; dy < visibleHeight; dy++) {
                        int srcOff = dy * tileRowBytes;
                        int dstOff = (ty * tileLength + dy) * rowBytes + dstColBytes;
                        System.arraycopy(tileBytes, srcOff, decoded, dstOff, copyRowBytes);
                    }
                }
            }
        }
        return decoded;
    }

    /**
     * Read one strip/tile chunk's compressed (or uncompressed) bytes
     * from the raw TIFF and return decoded bytes. Each chunk in a
     * compressed TIFF is its own zlib stream, so this must be called
     * once per chunk — never on a concatenation.
     */
    private static byte[] readChunk(
        byte[] raw, int offset, int length,
        int compression, int expectedBytes
    ) throws IOException {
        byte[] compressed = new byte[length];
        System.arraycopy(raw, offset, compressed, 0, length);
        if (compression == COMPRESS_NONE) {
            return compressed;
        }
        if (compression == COMPRESS_DEFLATE || compression == 32946) {
            // 32946 = Adobe Deflate (zlib), 8 = standard Deflate — both use zlib.
            return inflateData(compressed, expectedBytes);
        }
        throw new IOException(
            "Unsupported input compression (compression=" + compression
            + "). Supported: none (1), Deflate (8, 32946)."
        );
    }

    /**
     * Build a new TIFF file with COG layout:
     *
     * <pre>
     *   0-7        TIFF header (byte order, magic, IFD offset=8)
     *   8..        First IFD (count + entries + next-IFD pointer)
     *   ..         Overflow tag data (GeoKeys, pixel scale, tie points, etc.)
     *   ..         Pixel data (one tile)
     * </pre>
     *
     * <p>The Cloud Optimized GeoTIFF spec — and EE's validator, which
     * emits "The first IFD does not immediately follow the TIFF header
     * or the header ghost area is malformed" — requires the IFD to live
     * at offset 8, not at the end of the file the way a vanilla
     * GeoTIFF writer would put it. Putting pixel data first makes this
     * a perfectly-valid TIFF but not a COG.
     *
     * <p>The layout is computed in two passes because the
     * {@link #TAG_TILE_OFFSETS} value has to point at the pixel data,
     * which in turn depends on the size of the IFD+overflow preceding
     * it. We size everything first, then serialize.
     */
    private static byte[] buildCogTiff(
        Map<Integer, IfdEntry> originalEntries,
        List<byte[]> compressedBlocks,
        ByteOrder order,
        int width,
        int height,
        int tileSize,
        int compression
    ) throws IOException {
        int numBlocks = compressedBlocks.size();
        int expectedBlocks = (width / tileSize) * (height / tileSize);
        if (numBlocks != expectedBlocks) {
            throw new IOException(
                "compressedBlocks size " + numBlocks + " != expected "
                + expectedBlocks + " (" + (width / tileSize) + "x"
                + (height / tileSize) + " blocks for " + width + "x" + height
                + " image at tileSize=" + tileSize + ")"
            );
        }

        long[] perTileByteCounts = new long[numBlocks];
        long totalCompressedBytes = 0;
        for (int i = 0; i < numBlocks; i++) {
            perTileByteCounts[i] = compressedBlocks.get(i).length;
            totalCompressedBytes += perTileByteCounts[i];
        }

        // Build IFD entries — copy originals, replace strip→tile tags.
        // TAG_PREDICTOR is excluded from the inheritance set even though
        // we never write one ourselves: the transcoder doesn't apply
        // predictor encoding, so a stale Predictor tag from the source
        // would mis-describe our output.
        List<IfdEntry> newEntries = new ArrayList<>();
        for (var entry : originalEntries.values()) {
            if (entry.tag == TAG_STRIP_OFFSETS
                || entry.tag == TAG_STRIP_BYTE_COUNTS
                || entry.tag == TAG_ROWS_PER_STRIP
                || entry.tag == TAG_TILE_WIDTH
                || entry.tag == TAG_TILE_LENGTH
                || entry.tag == TAG_TILE_OFFSETS
                || entry.tag == TAG_TILE_BYTE_COUNTS
                || entry.tag == TAG_COMPRESSION
                || entry.tag == TAG_PREDICTOR) {
                continue;
            }
            newEntries.add(entry);
        }
        newEntries.add(IfdEntry.shortValue(TAG_COMPRESSION, compression));
        newEntries.add(IfdEntry.shortValue(TAG_TILE_WIDTH, tileSize));
        newEntries.add(IfdEntry.shortValue(TAG_TILE_LENGTH, tileSize));

        // TileOffsets and TileByteCounts: arrays sized to numBlocks, one entry
        // per inner tile in row-major order. TileOffsets values are filled in
        // after we know where pixel data lands; TileByteCounts is known up front.
        IfdEntry tileOffsets = IfdEntry.longArray(TAG_TILE_OFFSETS, new long[numBlocks]);
        newEntries.add(tileOffsets);
        newEntries.add(IfdEntry.longArray(TAG_TILE_BYTE_COUNTS, perTileByteCounts));

        // Sort by tag number (TIFF spec requirement).
        newEntries.sort((a, b) -> Integer.compare(a.tag, b.tag));

        // Layout pass 1: figure out IFD + overflow sizes so we know
        // where the pixel data will land.
        final int ifdStart = 8; // immediately after the TIFF header
        int entryCount = newEntries.size();
        int ifdSize = 2 + entryCount * 12 + 4;
        int overflowStart = ifdStart + ifdSize;

        int overflowSize = 0;
        for (IfdEntry e : newEntries) {
            int typeSize = (e.type > 0 && e.type < TYPE_SIZES.length) ? TYPE_SIZES[e.type] : 1;
            int totalBytes = e.count * typeSize;
            if (totalBytes > 4) {
                // TIFF 6.0 requires value offsets to be even ("word
                // boundary"), so odd-length chunks (e.g. ASCII tags with
                // odd counts) are padded by one byte. Pass 2 pads
                // identically — the two must stay in lockstep.
                overflowSize += totalBytes + (totalBytes & 1);
            }
        }
        int pixelDataOffset = overflowStart + overflowSize;
        // Align to word boundary.
        if ((pixelDataOffset & 1) != 0) {
            pixelDataOffset++;
        }

        // Now fill in the per-block offsets into the TileOffsets entry.
        long cursor = pixelDataOffset;
        for (int i = 0; i < numBlocks; i++) {
            tileOffsets.values[i] = cursor;
            cursor += perTileByteCounts[i];
        }

        // Layout pass 2: serialize IFD + overflow streaming in parallel.
        var ifdBuf = new ByteArrayOutputStream(ifdSize);
        var overflowBuf = new ByteArrayOutputStream(overflowSize);

        ByteBuffer countBuf = ByteBuffer.allocate(2).order(order);
        countBuf.putShort((short) entryCount);
        ifdBuf.write(countBuf.array());

        for (IfdEntry entry : newEntries) {
            byte[] serialized = entry.serialize(order, overflowStart + overflowBuf.size());
            ifdBuf.write(serialized);
            byte[] overflow = entry.overflowData(order);
            if (overflow != null) {
                overflowBuf.write(overflow);
                if ((overflow.length & 1) != 0) {
                    overflowBuf.write(0); // word-align the next entry's offset
                }
            }
        }

        // Next-IFD pointer = 0 (no more IFDs in this file).
        ByteBuffer nextIfd = ByteBuffer.allocate(4).order(order);
        nextIfd.putInt(0);
        ifdBuf.write(nextIfd.array());

        // Header — now that pixel data offset is known, the IFD offset
        // is just the constant 8.
        ByteBuffer header = ByteBuffer.allocate(8).order(order);
        header.putShort(order == ByteOrder.BIG_ENDIAN ? (short) 0x4D4D : (short) 0x4949);
        header.putShort((short) 42);
        header.putInt(ifdStart);

        // Assemble.
        var out = new ByteArrayOutputStream(
            8 + ifdSize + overflowSize + (int) totalCompressedBytes
        );
        out.write(header.array());
        out.write(ifdBuf.toByteArray());
        out.write(overflowBuf.toByteArray());
        // Pad to pixel-data alignment.
        int pad = pixelDataOffset - (overflowStart + overflowBuf.size());
        for (int i = 0; i < pad; i++) {
            out.write(0);
        }
        for (byte[] block : compressedBlocks) {
            out.write(block);
        }
        return out.toByteArray();
    }

    // -----------------------------------------------------------------------
    // Deflate compression / decompression (zlib via java.util.zip)
    // -----------------------------------------------------------------------

    private static byte[] deflateCompress(byte[] input) {
        var deflater = new java.util.zip.Deflater();
        deflater.setInput(input);
        deflater.finish();
        var out = new ByteArrayOutputStream();
        byte[] tmp = new byte[8192];
        while (!deflater.finished()) {
            int n = deflater.deflate(tmp);
            out.write(tmp, 0, n);
        }
        deflater.end();
        return out.toByteArray();
    }

    private static byte[] inflateData(byte[] compressed, int expectedBytes) throws IOException {
        var inflater = new java.util.zip.Inflater();
        inflater.setInput(compressed);
        var out = new ByteArrayOutputStream(expectedBytes);
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
            throw new IOException("Failed to inflate pixel data: " + e.getMessage(), e);
        } finally {
            inflater.end();
        }
        return out.toByteArray();
    }

    // -----------------------------------------------------------------------
    // IFD entry
    // -----------------------------------------------------------------------

    private static int getIntValue(Map<Integer, IfdEntry> entries, int tag) throws IOException {
        IfdEntry e = entries.get(tag);
        if (e == null) {
            throw new IOException("Missing required TIFF tag: " + tag);
        }
        return (int) e.values[0];
    }

    private static long[] getLongArray(Map<Integer, IfdEntry> entries, int tag) throws IOException {
        IfdEntry e = entries.get(tag);
        if (e == null) {
            throw new IOException("Missing required TIFF tag: " + tag);
        }
        return e.values;
    }

    /**
     * A parsed TIFF IFD entry.
     */
    static final class IfdEntry {
        final int tag;
        final int type;    // TIFF type (1=BYTE, 3=SHORT, 4=LONG, etc.)
        final int count;
        final long[] values;
        final byte[] rawOverflow;  // non-null if data didn't fit in 4 bytes

        IfdEntry(int tag, int type, int count, long[] values, byte[] rawOverflow) {
            this.tag = tag;
            this.type = type;
            this.count = count;
            this.values = values;
            this.rawOverflow = rawOverflow;
        }

        static IfdEntry shortValue(int tag, int value) {
            return new IfdEntry(tag, 3, 1, new long[]{value}, null);
        }

        static IfdEntry longValue(int tag, long value) {
            return new IfdEntry(tag, 4, 1, new long[]{value}, null);
        }

        static IfdEntry longArray(int tag, long[] values) {
            return new IfdEntry(tag, 4, values.length, values, null);
        }

        static IfdEntry asciiValue(int tag, String value) {
            // TIFF ASCII values are NUL-terminated; count includes the NUL.
            byte[] bytes = new byte[value.length() + 1];
            System.arraycopy(
                value.getBytes(java.nio.charset.StandardCharsets.US_ASCII),
                0, bytes, 0, value.length()
            );
            return new IfdEntry(tag, 2, bytes.length, new long[0], bytes);
        }

        static IfdEntry doubleArray(int tag, double[] values) {
            long[] bits = new long[values.length];
            for (int i = 0; i < values.length; i++) {
                bits[i] = Double.doubleToRawLongBits(values[i]);
            }
            return new IfdEntry(tag, 12, values.length, bits, null);
        }

        static IfdEntry read(ByteBuffer buf, ByteOrder order, byte[] raw) {
            int tag = Short.toUnsignedInt(buf.getShort());
            int type = Short.toUnsignedInt(buf.getShort());
            int count = buf.getInt();
            int valueOffset = buf.position();

            int typeSize = (type > 0 && type < TYPE_SIZES.length) ? TYPE_SIZES[type] : 1;
            int totalBytes = count * typeSize;

            int dataPos;
            byte[] rawBytes = null;
            if (totalBytes <= 4) {
                dataPos = valueOffset;
            } else {
                dataPos = buf.getInt(valueOffset);
                rawBytes = new byte[totalBytes];
                System.arraycopy(raw, dataPos, rawBytes, 0, totalBytes);
            }

            // Skip past the 4-byte value/offset field
            buf.position(valueOffset + 4);

            // Parse numeric values per-type. ASCII (type 2) is byte-oriented
            // and never inspected numerically; we capture its bytes verbatim
            // below and leave values[] empty so no part of the round-trip
            // relies on a pseudo-numerical interpretation of character data.
            long[] values;
            if (type == 2) {
                if (rawBytes == null) {
                    rawBytes = new byte[totalBytes];
                    System.arraycopy(raw, dataPos, rawBytes, 0, totalBytes);
                }
                values = new long[0];
            } else {
                ByteBuffer dataBuf = ByteBuffer.wrap(raw).order(order);
                dataBuf.position(dataPos);
                values = new long[count];
                for (int i = 0; i < count; i++) {
                    values[i] = switch (type) {
                        case 1, 7 -> Byte.toUnsignedLong(dataBuf.get());     // BYTE, UNDEFINED
                        case 3 -> Short.toUnsignedLong(dataBuf.getShort());  // SHORT
                        case 4 -> Integer.toUnsignedLong(dataBuf.getInt());  // LONG
                        case 5 -> {  // RATIONAL
                            long num = Integer.toUnsignedLong(dataBuf.getInt());
                            long den = Integer.toUnsignedLong(dataBuf.getInt());
                            yield (den == 0) ? 0 : num / den;
                        }
                        case 11 -> Float.floatToRawIntBits(dataBuf.getFloat()); // FLOAT
                        case 12 -> Double.doubleToRawLongBits(dataBuf.getDouble()); // DOUBLE
                        default -> Byte.toUnsignedLong(dataBuf.get());
                    };
                }
            }

            return new IfdEntry(tag, type, count, values, rawBytes);
        }

        byte[] serialize(ByteOrder order, int overflowOffset) {
            ByteBuffer buf = ByteBuffer.allocate(12).order(order);
            buf.putShort((short) tag);
            buf.putShort((short) type);
            buf.putInt(count);

            int typeSize = (type > 0 && type < TYPE_SIZES.length) ? TYPE_SIZES[type] : 1;
            int totalBytes = count * typeSize;

            if (totalBytes <= 4) {
                // Inline value. ASCII tags are byte-oriented and were read
                // verbatim into rawOverflow; for those, write the raw bytes
                // directly (rather than going through the numeric writeValues
                // path) and pad to the 4-byte field width.
                int pos = buf.position();
                if (type == 2 && rawOverflow != null) {
                    buf.put(rawOverflow);
                } else {
                    writeValues(buf, order);
                }
                while (buf.position() < pos + 4) {
                    buf.put((byte) 0);
                }
            } else {
                buf.putInt(overflowOffset);
            }

            return buf.array();
        }

        byte[] overflowData(ByteOrder order) {
            int typeSize = (type > 0 && type < TYPE_SIZES.length) ? TYPE_SIZES[type] : 1;
            int totalBytes = count * typeSize;
            if (totalBytes <= 4) {
                return null;
            }

            if (rawOverflow != null) {
                return rawOverflow;
            }

            // Synthesized entry — serialize values
            ByteBuffer buf = ByteBuffer.allocate(totalBytes).order(order);
            writeValues(buf, order);
            return buf.array();
        }

        /**
         * Serialize numeric values per TIFF type. Never called for ASCII
         * (type 2) — those are byte-oriented and pass through {@code
         * rawOverflow} on both read and write.
         */
        private void writeValues(ByteBuffer buf, ByteOrder order) {
            for (long val : values) {
                switch (type) {
                    case 1, 7 -> buf.put((byte) val);
                    case 3 -> buf.putShort((short) val);
                    case 4 -> buf.putInt((int) val);
                    case 5 -> {
                        buf.putInt((int) val);
                        buf.putInt(1);
                    }
                    case 11 -> buf.putFloat(Float.intBitsToFloat((int) val));
                    case 12 -> buf.putDouble(Double.longBitsToDouble(val));
                    default -> throw new IllegalStateException(
                        "Cannot serialize TIFF tag " + tag + " of unknown type " + type
                    );
                }
            }
        }
    }
}
