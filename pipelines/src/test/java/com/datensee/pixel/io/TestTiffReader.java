package com.datensee.pixel.io;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Independent TIFF/COG reader used only by the test suite.
 *
 * <p>Deliberately decoupled from {@link CogTranscoder}'s own decoder
 * paths so a writer bug can't mask itself by being read back through
 * the same broken assumptions. {@code docs/handoff.md} explicitly notes
 * that this reader must not be collapsed into the production helpers —
 * the independence is the point.
 *
 * <p>Supports just enough of TIFF to walk the IFD, locate tile data,
 * decompress (deflate / none), reverse a horizontal predictor, and
 * return raw pixel bytes from a multi-block COG.
 */
final class TestTiffReader {

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

    // -----------------------------------------------------------------------
    // High-level COG decoder + helpers
    // -----------------------------------------------------------------------

    /**
     * Decode a multi-block COG back to row-major pixel bytes. Derives
     * width/height/sample structure from the IFD; supports
     * uncompressed and deflate, and reverses a horizontal predictor
     * if one is set.
     */
    static byte[] decodeCogPixels(byte[] cog) throws IOException {
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
                    throw new IOException(
                        "Unsupported compression in test reader: " + compression);
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

    static byte[] inflateBytes(byte[] data, int expectedBytes) throws IOException {
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

    static byte[] reverseHorizontalPredictor(
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
}
