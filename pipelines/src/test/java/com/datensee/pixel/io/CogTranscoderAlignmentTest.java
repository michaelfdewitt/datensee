package com.datensee.pixel.io;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import org.junit.jupiter.api.Test;

/**
 * TIFF 6.0 requires every out-of-line tag value to start on a word
 * boundary ("The value offset must be an even number"). The transcoder
 * packs overflow chunks back-to-back, so an odd-length chunk — e.g. an
 * ASCII tag with an odd count, which GeoAsciiParams routinely is — must
 * be padded or every subsequent offset goes odd. Strict validators
 * (including EE's loader) may reject such files.
 */
class CogTranscoderAlignmentTest {

    @Test
    void overflowValuesAfterOddLengthAsciiTagStayWordAligned() throws Exception {
        int size = 16;
        byte[] pixels = new byte[size * size];
        for (int i = 0; i < pixels.length; i++) {
            pixels[i] = (byte) (i & 0xFF);
        }
        byte[] input = synthesizeTiffWithOddAsciiTag(size, size, pixels);

        byte[] cog = CogTranscoder.transcode(input, size, "deflate");

        // Walk the output IFD: every entry whose value doesn't fit inline
        // must point at an even offset.
        ByteBuffer buf = ByteBuffer.wrap(cog).order(ByteOrder.LITTLE_ENDIAN);
        assertEquals(8, buf.getInt(4), "First IFD must be at offset 8");
        buf.position(8);
        int entryCount = Short.toUnsignedInt(buf.getShort());
        int[] typeSizes = {0, 1, 1, 2, 4, 8, 1, 1, 2, 4, 8, 4, 8};
        boolean sawOverflowEntry = false;
        for (int i = 0; i < entryCount; i++) {
            int tag = Short.toUnsignedInt(buf.getShort());
            int type = Short.toUnsignedInt(buf.getShort());
            int count = buf.getInt();
            int valuePos = buf.position();
            int typeSize = (type > 0 && type < typeSizes.length) ? typeSizes[type] : 1;
            if (count * typeSize > 4) {
                sawOverflowEntry = true;
                int offset = buf.getInt(valuePos);
                assertEquals(
                    0, offset & 1,
                    "Tag " + tag + " overflow value at odd offset " + offset
                );
            }
            buf.position(valuePos + 4);
        }
        assertTrue(sawOverflowEntry, "Test input must produce overflow entries");

        // And the file still round-trips.
        assertArrayEquals(pixels, TestTiffReader.decodeCogPixels(cog));
    }

    /**
     * Minimal strip-layout uint8 TIFF carrying an ASCII tag with an ODD
     * byte count (ImageDescription, tag 270 — sorts BEFORE the double-array
     * GeoTIFF tags in the output IFD), so unpadded packing would misalign
     * everything after it.
     */
    private static byte[] synthesizeTiffWithOddAsciiTag(
        int width, int height, byte[] pixels
    ) {
        ByteOrder order = ByteOrder.LITTLE_ENDIAN;
        byte[] ascii = "WGS 84|\0".getBytes(java.nio.charset.StandardCharsets.US_ASCII);
        // Make the count odd on purpose.
        byte[] oddAscii = new byte[9];
        System.arraycopy(ascii, 0, oddAscii, 0, 8);
        oddAscii[8] = 0;

        int stripOffset = 8;
        int afterStrip = stripOffset + pixels.length;
        int ifdOffset = (afterStrip + 1) & ~1;

        int entryCount = 12;
        int ifdSize = 2 + entryCount * 12 + 4;
        int overflowStart = ifdOffset + ifdSize;
        int asciiOffset = overflowStart;
        int pixelScaleOffset = asciiOffset + oddAscii.length;   // deliberately odd
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
        putShortEntry(ifd, 256, width);
        putShortEntry(ifd, 257, height);
        putShortEntry(ifd, 258, 8);
        putShortEntry(ifd, 259, 1);
        putShortEntry(ifd, 262, 1);
        putOffsetEntry(ifd, 270, 2, oddAscii.length, asciiOffset);
        putLongEntry(ifd, 273, stripOffset);
        putShortEntry(ifd, 277, 1);
        putShortEntry(ifd, 278, height);
        putLongEntry(ifd, 279, pixels.length);
        putOffsetEntry(ifd, 33550, 12, 3, pixelScaleOffset);
        putOffsetEntry(ifd, 33922, 12, 6, tiepointOffset);
        ifd.putInt(0);
        out.writeBytes(ifd.array());

        out.writeBytes(oddAscii);
        ByteBuffer doubles = ByteBuffer.allocate(24 + 48).order(order);
        doubles.putDouble(1.0).putDouble(1.0).putDouble(0.0);
        doubles.putDouble(0.0).putDouble(0.0).putDouble(0.0)
            .putDouble(100.0).putDouble(200.0).putDouble(0.0);
        out.writeBytes(doubles.array());
        return out.toByteArray();
    }

    private static void putShortEntry(ByteBuffer buf, int tag, int value) {
        buf.putShort((short) tag).putShort((short) 3).putInt(1);
        buf.putShort((short) value).putShort((short) 0);
    }

    private static void putLongEntry(ByteBuffer buf, int tag, int value) {
        buf.putShort((short) tag).putShort((short) 4).putInt(1).putInt(value);
    }

    private static void putOffsetEntry(
        ByteBuffer buf, int tag, int type, int count, int offset
    ) {
        buf.putShort((short) tag).putShort((short) type).putInt(count).putInt(offset);
    }
}
