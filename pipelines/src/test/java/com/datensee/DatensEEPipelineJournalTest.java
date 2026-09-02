package com.datensee;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.datensee.fetch.EeErrorKind;
import com.datensee.pixel.FailedTileRecord;
import com.datensee.pixel.TileCoordinate;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import org.apache.beam.sdk.Pipeline;
import org.apache.beam.sdk.coders.SerializableCoder;
import org.apache.beam.sdk.transforms.Create;
import org.apache.beam.sdk.values.PCollection;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * Pipeline-level tests for the failures-journal write: this round's
 * dead letters unioned with the staged carryover journal. Runs on the DirectRunner against
 * temp files — the same code path Dataflow executes, which is the whole
 * point: no Python post-step, no race against the async writer.
 */
class DatensEEPipelineJournalTest {

    private static FailedTileRecord freshFailure() {
        return FailedTileRecord.fromTileWithError(
            new TileCoordinate(0, 0, 512, 512, 0, 0),
            EeErrorKind.RATE_LIMITED,
            "quota exceeded",
            429,
            6
        );
    }

    @Test
    void carryoverLinesAreUnionedWithFreshFailures(@TempDir Path tmp) throws Exception {
        Path carryover = tmp.resolve("_carryover.json");
        String terminalLine =
            "{\"col_px\":512,\"row_px\":0,\"width_px\":512,\"height_px\":512,"
            + "\"row\":0,\"col\":1,\"error_kind\":\"AUTH_ERROR\","
            + "\"journal_reason\":\"terminal\"}";
        String depthCapLine =
            "{\"col_px\":0,\"row_px\":512,\"width_px\":128,\"height_px\":128,"
            + "\"row\":1,\"col\":0,\"lineage\":[0,1],\"error_kind\":\"MEMORY_EXCEEDED\","
            + "\"journal_reason\":\"depth_cap\"}";
        Files.write(carryover, List.of(terminalLine, depthCapLine));

        Pipeline pipeline = Pipeline.create();
        PCollection<FailedTileRecord> failed = pipeline
            .apply(Create.of(freshFailure())
                .withCoder(SerializableCoder.of(FailedTileRecord.class)));
        DatensEEPipeline.writeFailuresJournal(
            pipeline, failed, carryover.toString(),
            tmp.resolve("_failures").toString()
        );
        pipeline.run().waitUntilFinish();

        List<String> lines = Files.readAllLines(tmp.resolve("_failures.json")).stream()
            .filter(l -> !l.isBlank())
            .toList();
        assertEquals(3, lines.size(), "fresh + 2 carryover lines expected: " + lines);
        assertTrue(lines.stream().anyMatch(l -> l.contains("\"journal_reason\":\"terminal\"")),
            "carryover terminal record must pass through verbatim");
        assertTrue(lines.stream().anyMatch(l -> l.contains("\"journal_reason\":\"depth_cap\"")),
            "carryover depth-cap record must pass through verbatim");
        assertTrue(lines.stream().anyMatch(l -> l.contains("RATE_LIMITED")),
            "this round's fresh failure must be present");
    }

    @Test
    void withoutCarryoverJournalHoldsFreshFailuresOnly(@TempDir Path tmp) throws Exception {
        Pipeline pipeline = Pipeline.create();
        PCollection<FailedTileRecord> failed = pipeline
            .apply(Create.of(freshFailure())
                .withCoder(SerializableCoder.of(FailedTileRecord.class)));
        DatensEEPipeline.writeFailuresJournal(
            pipeline, failed, null, tmp.resolve("_failures").toString()
        );
        pipeline.run().waitUntilFinish();

        List<String> lines = Files.readAllLines(tmp.resolve("_failures.json")).stream()
            .filter(l -> !l.isBlank())
            .toList();
        assertEquals(1, lines.size());
        assertTrue(lines.getFirst().contains("RATE_LIMITED"));
    }

    @Test
    void emptyRoundStillMergesCarryover(@TempDir Path tmp) throws Exception {
        Path carryover = tmp.resolve("_carryover.json");
        Files.write(carryover, List.of(
            "{\"col_px\":0,\"row_px\":0,\"width_px\":512,\"height_px\":512,"
            + "\"row\":0,\"col\":0,\"error_kind\":\"AUTH_ERROR\","
            + "\"journal_reason\":\"terminal\"}"
        ));

        Pipeline pipeline = Pipeline.create();
        PCollection<FailedTileRecord> failed = pipeline
            .apply(Create.empty(SerializableCoder.of(FailedTileRecord.class)));
        DatensEEPipeline.writeFailuresJournal(
            pipeline, failed, carryover.toString(),
            tmp.resolve("_failures").toString()
        );
        pipeline.run().waitUntilFinish();

        List<String> lines = Files.readAllLines(tmp.resolve("_failures.json")).stream()
            .filter(l -> !l.isBlank())
            .toList();
        assertEquals(1, lines.size(), "carryover survives a round with no fresh failures");
        assertTrue(lines.getFirst().contains("terminal"));
    }
}
