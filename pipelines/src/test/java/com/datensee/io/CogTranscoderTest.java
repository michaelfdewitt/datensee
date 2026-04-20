package com.datensee.io;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import org.junit.jupiter.api.Test;

/**
 * Structural tests for {@link CogTranscoder}. Pin the layout invariants
 * EE's {@code Image.loadGeoTIFF} validator checks — specifically that
 * the first IFD lives at offset 8, which distinguishes a COG from a
 * plain tiled GeoTIFF.
 */
class CogTranscoderTest {

    @Test
    void transcodedFileHasFirstIfdAtOffsetEight() throws Exception {
        byte[] raw = synthesizeMinimalGeotiff(4, 4);
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
        byte[] raw = synthesizeMinimalGeotiff(4, 4);
        byte[] cog = CogTranscoder.transcode(raw, 4, "none");

        ByteBuffer buf = ByteBuffer.wrap(cog).order(ByteOrder.LITTLE_ENDIAN);
        int ifdOffset = buf.getInt(4);
        int entryCount = Short.toUnsignedInt(buf.getShort(ifdOffset));

        // Walk the IFD for TileOffsets (324). The value for a single-tile
        // file fits inline (4 bytes), so it's the 4-byte "value" field in
        // the 12-byte entry.
        int tileOffsetsValue = -1;
        for (int i = 0; i < entryCount; i++) {
            int entryPos = ifdOffset + 2 + i * 12;
            int tag = Short.toUnsignedInt(buf.getShort(entryPos));
            if (tag == 324) {
                tileOffsetsValue = buf.getInt(entryPos + 8);
                break;
            }
        }
        assertTrue(tileOffsetsValue > 0, "TileOffsets entry must be present");
        int ifdSize = 2 + entryCount * 12 + 4;
        assertTrue(
            tileOffsetsValue >= ifdOffset + ifdSize,
            "Pixel data offset (" + tileOffsetsValue
                + ") must land after the IFD (ends at " + (ifdOffset + ifdSize) + ")");
    }

    /**
     * Build a minimal valid TIFF mimicking what the EE HV API returns:
     * little-endian, strip layout, single strip, uncompressed uint8, one
     * sample per pixel. Enough for the transcoder to read and re-emit.
     */
    private static byte[] synthesizeMinimalGeotiff(int width, int height) {
        ByteOrder order = ByteOrder.LITTLE_ENDIAN;
        int pixelBytes = width * height;
        byte[] pixels = new byte[pixelBytes];
        for (int i = 0; i < pixelBytes; i++) pixels[i] = (byte) (i & 0xFF);

        // Layout: header (8) + strip data + IFD (at end, standard style).
        int stripOffset = 8;
        int ifdOffset = stripOffset + pixelBytes;
        if ((ifdOffset & 1) != 0) ifdOffset++;

        // IFD with the bare minimum tags.
        // 256 ImageWidth SHORT, 257 ImageLength SHORT, 258 BitsPerSample SHORT,
        // 259 Compression SHORT, 262 PhotometricInterpretation SHORT,
        // 273 StripOffsets LONG, 277 SamplesPerPixel SHORT,
        // 278 RowsPerStrip SHORT, 279 StripByteCounts LONG.
        int entryCount = 9;
        int ifdSize = 2 + entryCount * 12 + 4;

        ByteArrayOutputStream out = new ByteArrayOutputStream();
        ByteBuffer header = ByteBuffer.allocate(8).order(order);
        header.putShort((short) 0x4949);
        header.putShort((short) 42);
        header.putInt(ifdOffset);
        out.writeBytes(header.array());
        out.writeBytes(pixels);
        if (out.size() < ifdOffset) out.write(0); // pad

        ByteBuffer ifd = ByteBuffer.allocate(ifdSize).order(order);
        ifd.putShort((short) entryCount);
        writeShortEntry(ifd, 256, width);
        writeShortEntry(ifd, 257, height);
        writeShortEntry(ifd, 258, 8);
        writeShortEntry(ifd, 259, 1); // no compression
        writeShortEntry(ifd, 262, 1); // BlackIsZero
        writeLongEntry(ifd, 273, stripOffset);
        writeShortEntry(ifd, 277, 1);
        writeShortEntry(ifd, 278, height);
        writeLongEntry(ifd, 279, pixelBytes);
        ifd.putInt(0); // next-IFD = 0
        out.writeBytes(ifd.array());
        return out.toByteArray();
    }

    private static void writeShortEntry(ByteBuffer buf, int tag, int value) {
        buf.putShort((short) tag);
        buf.putShort((short) 3); // SHORT
        buf.putInt(1);
        buf.putShort((short) value);
        buf.putShort((short) 0); // pad
    }

    private static void writeLongEntry(ByteBuffer buf, int tag, int value) {
        buf.putShort((short) tag);
        buf.putShort((short) 4); // LONG
        buf.putInt(1);
        buf.putInt(value);
    }
}
