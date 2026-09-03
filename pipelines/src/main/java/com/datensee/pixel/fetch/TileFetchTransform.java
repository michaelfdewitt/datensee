package com.datensee.pixel.fetch;

import com.datensee.pixel.PixelGrid;
import com.datensee.pixel.TileCoordinate;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.transforms.Redistribute;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.apache.beam.sdk.values.TupleTagList;

/**
 * Beam PTransform that maps tile coordinates to fetched image data.
 *
 * <p>Each tile is fetched independently via the EE High Volume API.
 * The {@code eeExpression} is passed through opaquely to the HV endpoint;
 * Earth Engine evaluates it per tile bounded to that tile's grid geometry.
 *
 * <p>Returns a {@link PCollectionTuple} with two branches:
 * <ul>
 *   <li>{@link TileFetchDoFn#SUCCESS_TAG}: successfully fetched tiles
 *   <li>{@link TileFetchDoFn#FAILED_TAG}: tiles that exhausted all retries
 * </ul>
 */
public final class TileFetchTransform
    extends PTransform<PCollection<TileCoordinate>, PCollectionTuple> {

    private final String eeExpression;
    private final String geeProject;
    private final PixelGrid parentGrid;

    public TileFetchTransform(
        String eeExpression,
        String geeProject,
        PixelGrid parentGrid
    ) {
        this.eeExpression = eeExpression;
        this.geeProject = geeProject;
        this.parentGrid = parentGrid;
    }

    @Override
    public PCollectionTuple expand(PCollection<TileCoordinate> input) {
        // Break fusion: tile coordinate inputs originate from single-file or
        // in-memory sources. Without redistribution, Dataflow fuses tile fetches
        // into the source read, constraining parallelism to the initial shard count.
        // Redistribute ensures coordinates fan out across available workers.
        return input
            .apply("FanOutTiles", Redistribute.<TileCoordinate>arbitrarily())
            .apply(
            "FetchTileFromEE",
            ParDo.of(new TileFetchDoFn(eeExpression, geeProject, parentGrid))
                .withOutputTags(TileFetchDoFn.SUCCESS_TAG,
                    TupleTagList.of(TileFetchDoFn.FAILED_TAG))
        );
    }
}
