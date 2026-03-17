package com.datensee.io;

import com.datensee.FetchedTile;
import com.datensee.PipelineConfig.OutputConfig;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PDone;

/**
 * PTransform that writes fetched tiles to GCS as a Cloud Optimized GeoTIFF.
 *
 * <p>Current implementation writes individual tile GeoTIFFs to GCS as a
 * staging step. Full COG assembly (stitching + overview pyramid) is a TODO.
 *
 * <p>Tradeoffs:
 * <ul>
 *   <li>Option A: GDAL-based assembly — most complete COG support, requires
 *       GDAL on workers (Docker image or native install).
 *   <li>Option B: Pure-Java tile writing — simpler worker setup, limited
 *       COG feature set.
 * </ul>
 * Starting with Option A (GDAL) as it's the production-quality path.
 */
public final class CogWriter extends PTransform<PCollection<FetchedTile>, PDone> {

    private final OutputConfig outputConfig;

    public CogWriter(OutputConfig outputConfig) {
        this.outputConfig = outputConfig;
    }

    @Override
    public PDone expand(PCollection<FetchedTile> input) {
        input.apply("WriteTileToGcs", ParDo.of(new TileWriterDoFn(outputConfig)));
        return PDone.in(input.getPipeline());
    }
}
