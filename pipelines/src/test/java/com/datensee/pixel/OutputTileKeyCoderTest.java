package com.datensee.pixel;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import org.apache.beam.sdk.coders.Coder;
import org.junit.jupiter.api.Test;

/**
 * Pins the deterministic coder for {@link OutputTileKey}.
 *
 * <p>{@link OutputTileKey.OutputTileKeyCoder} replaces {@code SerializableCoder}
 * for the two-tier GroupByKey because Dataflow's {@code GroupByKey} requires
 * deterministic key coders, which default Java serialization does not guarantee.
 */
class OutputTileKeyCoderTest {

    @Test
    void roundTripsKey() throws Exception {
        OutputTileKey original = new OutputTileKey(7, 13);
        Coder<OutputTileKey> coder = OutputTileKey.OutputTileKeyCoder.of();

        ByteArrayOutputStream out = new ByteArrayOutputStream();
        coder.encode(original, out);
        OutputTileKey restored = coder.decode(new ByteArrayInputStream(out.toByteArray()));

        assertEquals(original, restored);
    }

    @Test
    void encodingIsByteIdenticalForEqualKeys() throws Exception {
        // Determinism in Beam's sense: equal values produce equal bytes.
        OutputTileKey a = new OutputTileKey(3, 5);
        OutputTileKey b = new OutputTileKey(3, 5);
        Coder<OutputTileKey> coder = OutputTileKey.OutputTileKeyCoder.of();

        ByteArrayOutputStream outA = new ByteArrayOutputStream();
        ByteArrayOutputStream outB = new ByteArrayOutputStream();
        coder.encode(a, outA);
        coder.encode(b, outB);

        assertArrayEquals(outA.toByteArray(), outB.toByteArray());
    }

    @Test
    void verifyDeterministicDoesNotThrow() throws Exception {
        OutputTileKey.OutputTileKeyCoder.of().verifyDeterministic();
    }
}
