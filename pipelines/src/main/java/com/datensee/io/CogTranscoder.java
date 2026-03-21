package com.datensee.io;

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
 * layout and no compression. This transcoder rewrites the TIFF structure to
 * use tiled layout and LZW compression, producing a valid COG that
 * {@code ee.Image.loadGeoTIFF()} can read.
 *
 * <p>Approach: operates directly on TIFF bytes — no pixel decoding/encoding
 * via BufferedImage (which would lose float32 precision). Reads the IFD,
 * extracts raw pixel strips, LZW-compresses the pixel data, and writes a
 * new TIFF with tiled layout and all original GeoTIFF metadata tags preserved.
 *
 * <p>Zero external dependencies — pure Java.
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

    // TIFF type sizes (in bytes)
    private static final int[] TYPE_SIZES = {0, 1, 1, 2, 4, 8, 1, 1, 2, 4, 8, 4, 8};

    // Compression values
    private static final int COMPRESS_NONE = 1;
    private static final int COMPRESS_LZW = 5;
    private static final int COMPRESS_DEFLATE = 8;

    // Predictor values
    private static final int PREDICTOR_NONE = 1;
    private static final int PREDICTOR_HORIZONTAL = 2;

    private CogTranscoder() { }

    /**
     * Transcode raw GeoTIFF bytes to COG format.
     *
     * @param rawGeotiff  bytes from the EE HV API (standard GeoTIFF)
     * @param tileSize    tile edge size in pixels (used as COG block size)
     * @param compression compression algorithm ("lzw", "deflate", "none")
     * @return COG-encoded bytes
     * @throws IOException if transcoding fails
     */
    public static byte[] transcode(
        byte[] rawGeotiff,
        int tileSize,
        String compression
    ) throws IOException {
        ByteBuffer buf = ByteBuffer.wrap(rawGeotiff);

        // Read byte order
        short bom = buf.getShort(0);
        ByteOrder order = (bom == 0x4D4D) ? ByteOrder.BIG_ENDIAN : ByteOrder.LITTLE_ENDIAN;
        buf.order(order);

        short magic = buf.getShort(2);
        if (magic != 42) {
            throw new IOException("Not a TIFF file (magic=" + magic + ")");
        }

        int ifdOffset = buf.getInt(4);
        buf.position(ifdOffset);

        // Read IFD entries
        int entryCount = Short.toUnsignedInt(buf.getShort());
        Map<Integer, IfdEntry> entries = new LinkedHashMap<>();
        for (int i = 0; i < entryCount; i++) {
            IfdEntry entry = IfdEntry.read(buf, order, rawGeotiff);
            entries.put(entry.tag, entry);
        }

        // Extract pixel data from strips
        byte[] pixelData = extractStripData(entries, rawGeotiff, order);

        int width = getIntValue(entries, TAG_IMAGE_WIDTH);
        int height = getIntValue(entries, TAG_IMAGE_LENGTH);
        int bitsPerSample = getIntValue(entries, TAG_BITS_PER_SAMPLE);
        int samplesPerPixel = entries.containsKey(277)
            ? getIntValue(entries, 277) : 1;  // 277 = SamplesPerPixel

        LOG.debug(
            "Input: {}x{}, {}bps, {} samples, {} bytes pixel data",
            width, height, bitsPerSample, samplesPerPixel, pixelData.length
        );

        // Determine compression
        int compressCode;
        int predictorCode = PREDICTOR_NONE;
        switch (compression.toLowerCase()) {
            case "lzw" -> {
                compressCode = COMPRESS_LZW;
                // Use horizontal differencing for integer types (>= 8bps),
                // skip for float (sample format 3) as it can hurt compression.
                int sampleFormat = entries.containsKey(TAG_SAMPLE_FORMAT)
                    ? getIntValue(entries, TAG_SAMPLE_FORMAT) : 1;
                if (sampleFormat != 3) {  // not floating point
                    predictorCode = PREDICTOR_HORIZONTAL;
                }
            }
            case "deflate" -> compressCode = COMPRESS_DEFLATE;
            case "none" -> compressCode = COMPRESS_NONE;
            default -> compressCode = COMPRESS_LZW;
        }

        // Compress the pixel data
        byte[] compressedData;
        if (compressCode == COMPRESS_LZW) {
            if (predictorCode == PREDICTOR_HORIZONTAL) {
                byte[] predicted = applyHorizontalPredictor(
                    pixelData, width, height, samplesPerPixel, bitsPerSample / 8
                );
                compressedData = lzwCompress(predicted);
            } else {
                compressedData = lzwCompress(pixelData);
            }
        } else if (compressCode == COMPRESS_DEFLATE) {
            compressedData = deflateCompress(pixelData);
        } else {
            compressedData = pixelData;
        }

        // Build new TIFF with tile layout
        return buildCogTiff(
            entries, compressedData, order,
            width, height, tileSize,
            compressCode, predictorCode
        );
    }

    /**
     * Extract raw pixel bytes from strip-based layout.
     */
    private static byte[] extractStripData(
        Map<Integer, IfdEntry> entries,
        byte[] raw,
        ByteOrder order
    ) throws IOException {
        long[] offsets = getLongArray(entries, TAG_STRIP_OFFSETS);
        long[] counts = getLongArray(entries, TAG_STRIP_BYTE_COUNTS);

        if (offsets.length != counts.length) {
            throw new IOException(
                "StripOffsets count (" + offsets.length
                + ") != StripByteCounts count (" + counts.length + ")"
            );
        }

        // Check if the input is already uncompressed
        int inputCompression = entries.containsKey(TAG_COMPRESSION)
            ? getIntValue(entries, TAG_COMPRESSION) : 1;

        int totalBytes = 0;
        for (long c : counts) {
            totalBytes += (int) c;
        }

        byte[] data = new byte[totalBytes];
        int pos = 0;
        for (int i = 0; i < offsets.length; i++) {
            int off = (int) offsets[i];
            int len = (int) counts[i];
            System.arraycopy(raw, off, data, pos, len);
            pos += len;
        }

        // If the source was compressed, we need to decompress first
        if (inputCompression != COMPRESS_NONE) {
            throw new IOException(
                "Input GeoTIFF is already compressed (compression="
                + inputCompression + "). Expected uncompressed input from EE HV API."
            );
        }

        return data;
    }

    /**
     * Build a new TIFF file with tile-based layout.
     */
    private static byte[] buildCogTiff(
        Map<Integer, IfdEntry> originalEntries,
        byte[] tileData,
        ByteOrder order,
        int width,
        int height,
        int tileSize,
        int compression,
        int predictor
    ) throws IOException {
        var out = new ByteArrayOutputStream();

        // TIFF header (8 bytes)
        ByteBuffer header = ByteBuffer.allocate(8).order(order);
        header.putShort(order == ByteOrder.BIG_ENDIAN ? (short) 0x4D4D : (short) 0x4949);
        header.putShort((short) 42);
        // IFD offset — we'll put pixel data first, then IFD after
        // Placeholder — will be filled after we know the data size
        header.putInt(0);
        byte[] headerBytes = header.array();

        // Pixel data starts at offset 8
        int pixelDataOffset = 8;
        int pixelDataLength = tileData.length;

        // IFD starts after header + pixel data
        int ifdStart = pixelDataOffset + pixelDataLength;
        // Align to word boundary
        if (ifdStart % 2 != 0) {
            ifdStart++;
        }

        // Update IFD offset in header
        ByteBuffer.wrap(headerBytes, 4, 4).order(order).putInt(ifdStart);

        // Build IFD entries — copy originals, replace strip→tile tags
        List<IfdEntry> newEntries = new ArrayList<>();
        for (var entry : originalEntries.values()) {
            // Skip strip-related and compression/predictor tags
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

        // Add tile tags
        newEntries.add(IfdEntry.shortValue(TAG_COMPRESSION, compression));
        if (predictor != PREDICTOR_NONE) {
            newEntries.add(IfdEntry.shortValue(TAG_PREDICTOR, predictor));
        }
        newEntries.add(IfdEntry.shortValue(TAG_TILE_WIDTH, tileSize));
        newEntries.add(IfdEntry.shortValue(TAG_TILE_LENGTH, tileSize));

        // Single tile — one offset, one byte count
        newEntries.add(IfdEntry.longValue(TAG_TILE_OFFSETS, pixelDataOffset));
        newEntries.add(IfdEntry.longValue(TAG_TILE_BYTE_COUNTS, pixelDataLength));

        // Sort by tag number (TIFF spec requirement)
        newEntries.sort((a, b) -> Integer.compare(a.tag, b.tag));

        // Serialize IFD
        int entryCount = newEntries.size();
        // IFD: 2 bytes count + 12 bytes per entry + 4 bytes next-IFD pointer
        int ifdSize = 2 + entryCount * 12 + 4;
        int overflowStart = ifdStart + ifdSize;

        var ifdBuf = new ByteArrayOutputStream();
        var overflowBuf = new ByteArrayOutputStream();

        ByteBuffer countBuf = ByteBuffer.allocate(2).order(order);
        countBuf.putShort((short) entryCount);
        ifdBuf.write(countBuf.array());

        for (IfdEntry entry : newEntries) {
            byte[] serialized = entry.serialize(order, overflowStart + overflowBuf.size());
            ifdBuf.write(serialized);
            byte[] overflow = entry.overflowData(order);
            if (overflow != null) {
                overflowBuf.write(overflow);
            }
        }

        // Next IFD pointer = 0 (no more IFDs)
        ByteBuffer nextIfd = ByteBuffer.allocate(4).order(order);
        nextIfd.putInt(0);
        ifdBuf.write(nextIfd.array());

        // Assemble the file
        out.write(headerBytes);
        out.write(tileData);
        // Pad to align IFD
        int padNeeded = ifdStart - (pixelDataOffset + pixelDataLength);
        for (int i = 0; i < padNeeded; i++) {
            out.write(0);
        }
        out.write(ifdBuf.toByteArray());
        out.write(overflowBuf.toByteArray());

        return out.toByteArray();
    }

    // -----------------------------------------------------------------------
    // LZW compression (TIFF-compatible: MSB-first, with clear/EOI codes)
    // -----------------------------------------------------------------------

    private static byte[] lzwCompress(byte[] input) {
        var out = new ByteArrayOutputStream();
        var bitWriter = new BitWriter(out);

        final int clearCode = 256;
        final int eoiCode = 257;
        int nextCode = 258;
        int codeSize = 9;

        // Initialize table with single-byte entries
        Map<List<Byte>, Integer> table = new LinkedHashMap<>();
        for (int i = 0; i < 256; i++) {
            table.put(List.of((byte) i), i);
        }

        bitWriter.write(clearCode, codeSize);

        List<Byte> w = new ArrayList<>();
        for (byte b : input) {
            List<Byte> wc = new ArrayList<>(w);
            wc.add(b);

            if (table.containsKey(wc)) {
                w = wc;
            } else {
                bitWriter.write(table.get(w), codeSize);

                if (nextCode < 4094) {
                    table.put(wc, nextCode++);
                    if (nextCode > (1 << codeSize)) {
                        codeSize++;
                    }
                } else {
                    // Table full — emit clear code and reset
                    bitWriter.write(clearCode, codeSize);
                    table.clear();
                    for (int i = 0; i < 256; i++) {
                        table.put(List.of((byte) i), i);
                    }
                    nextCode = 258;
                    codeSize = 9;
                }

                w = new ArrayList<>();
                w.add(b);
            }
        }

        if (!w.isEmpty()) {
            bitWriter.write(table.get(w), codeSize);
        }
        bitWriter.write(eoiCode, codeSize);
        bitWriter.flush();

        return out.toByteArray();
    }

    /**
     * MSB-first bit writer for TIFF LZW.
     */
    private static final class BitWriter {
        private final ByteArrayOutputStream out;
        private int buffer;
        private int bitsInBuffer;

        BitWriter(ByteArrayOutputStream out) {
            this.out = out;
        }

        void write(int code, int numBits) {
            buffer = (buffer << numBits) | code;
            bitsInBuffer += numBits;
            while (bitsInBuffer >= 8) {
                bitsInBuffer -= 8;
                out.write((buffer >> bitsInBuffer) & 0xFF);
            }
        }

        void flush() {
            if (bitsInBuffer > 0) {
                out.write((buffer << (8 - bitsInBuffer)) & 0xFF);
                bitsInBuffer = 0;
            }
        }
    }

    // -----------------------------------------------------------------------
    // Deflate compression
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

    // -----------------------------------------------------------------------
    // Horizontal differencing predictor
    // -----------------------------------------------------------------------

    private static byte[] applyHorizontalPredictor(
        byte[] data,
        int width,
        int height,
        int samplesPerPixel,
        int bytesPerSample
    ) {
        byte[] result = data.clone();
        int rowBytes = width * samplesPerPixel * bytesPerSample;

        for (int row = 0; row < height; row++) {
            int rowStart = row * rowBytes;
            // Process right-to-left to avoid overwriting needed values
            for (int x = width - 1; x >= 1; x--) {
                for (int s = 0; s < samplesPerPixel; s++) {
                    int currOff = rowStart + (x * samplesPerPixel + s) * bytesPerSample;
                    int prevOff = rowStart + ((x - 1) * samplesPerPixel + s) * bytesPerSample;
                    for (int b = 0; b < bytesPerSample; b++) {
                        result[currOff + b] = (byte) (data[currOff + b] - data[prevOff + b]);
                    }
                }
            }
        }
        return result;
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

        static IfdEntry read(ByteBuffer buf, ByteOrder order, byte[] raw) {
            int tag = Short.toUnsignedInt(buf.getShort());
            int type = Short.toUnsignedInt(buf.getShort());
            int count = buf.getInt();
            int valueOffset = buf.position();

            int typeSize = (type > 0 && type < TYPE_SIZES.length) ? TYPE_SIZES[type] : 1;
            int totalBytes = count * typeSize;

            long[] values;
            byte[] overflow = null;
            int dataPos;

            if (totalBytes <= 4) {
                dataPos = valueOffset;
            } else {
                dataPos = buf.getInt(valueOffset);
                overflow = new byte[totalBytes];
                System.arraycopy(raw, dataPos, overflow, 0, totalBytes);
            }

            // Skip past the 4-byte value/offset field
            buf.position(valueOffset + 4);

            // Parse values
            ByteBuffer dataBuf = ByteBuffer.wrap(raw).order(order);
            dataBuf.position(dataPos);
            values = new long[count];

            for (int i = 0; i < count; i++) {
                values[i] = switch (type) {
                    case 1, 7 -> Byte.toUnsignedLong(dataBuf.get());     // BYTE, UNDEFINED
                    case 2 -> Byte.toUnsignedLong(dataBuf.get());        // ASCII
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

            return new IfdEntry(tag, type, count, values, overflow);
        }

        byte[] serialize(ByteOrder order, int overflowOffset) {
            ByteBuffer buf = ByteBuffer.allocate(12).order(order);
            buf.putShort((short) tag);
            buf.putShort((short) type);
            buf.putInt(count);

            int typeSize = (type > 0 && type < TYPE_SIZES.length) ? TYPE_SIZES[type] : 1;
            int totalBytes = count * typeSize;

            if (totalBytes <= 4) {
                // Inline value
                int pos = buf.position();
                writeValues(buf, order);
                // Pad to 4 bytes
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
                    default -> buf.put((byte) val);
                }
            }
        }
    }
}
