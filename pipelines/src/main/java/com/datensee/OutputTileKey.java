package com.datensee;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.Serializable;
import org.apache.beam.sdk.coders.AtomicCoder;
import org.apache.beam.sdk.coders.Coder;
import org.apache.beam.sdk.coders.VarIntCoder;

/**
 * Composite key for the M6 GroupByKey: which output tile a compute tile
 * belongs to.
 *
 * <p>Compute tiles with the same {@code (outRow, outCol)} share an output
 * COG. The Beam {@code GroupByKey} step shuffles compute tiles together
 * by this key so the assembler can stitch them into a single
 * multi-block COG.
 *
 * <p>The {@link OutputTileKeyCoder} below is registered as the default
 * coder for this type because Dataflow's {@code GroupByKey} validates
 * key-coder determinism — Java's {@code SerializableCoder} declines to
 * make that promise (different JVM/JDK pairs can re-order field bytes
 * inside a serialized record), and Dataflow refuses to launch with a
 * non-deterministic key coder. Two {@code VarInt}s round-trip cleanly
 * across JVMs and are smaller on the wire than Java serialization.
 */
@org.apache.beam.sdk.coders.DefaultCoder(OutputTileKey.OutputTileKeyCoder.class)
public record OutputTileKey(
    @JsonProperty("out_row") int outRow,
    @JsonProperty("out_col") int outCol
) implements Serializable, Comparable<OutputTileKey> {

    @Override
    public int compareTo(OutputTileKey other) {
        int rowCmp = Integer.compare(this.outRow, other.outRow);
        return rowCmp != 0 ? rowCmp : Integer.compare(this.outCol, other.outCol);
    }

    /** Filename for the output COG, e.g. {@code tile_r0003_c0007.tif}. */
    public String filename() {
        return String.format("tile_r%04d_c%04d.tif", outRow, outCol);
    }

    /** Deterministic coder — two VarInts in fixed order. */
    public static final class OutputTileKeyCoder extends AtomicCoder<OutputTileKey> {
        private static final long serialVersionUID = 1L;
        private static final OutputTileKeyCoder INSTANCE = new OutputTileKeyCoder();
        private static final VarIntCoder INT_CODER = VarIntCoder.of();

        public static OutputTileKeyCoder of() {
            return INSTANCE;
        }

        @Override
        public void encode(OutputTileKey value, OutputStream out) throws IOException {
            INT_CODER.encode(value.outRow(), out);
            INT_CODER.encode(value.outCol(), out);
        }

        @Override
        public OutputTileKey decode(InputStream in) throws IOException {
            int outRow = INT_CODER.decode(in);
            int outCol = INT_CODER.decode(in);
            return new OutputTileKey(outRow, outCol);
        }

        @Override
        public void verifyDeterministic() { }
    }
}
