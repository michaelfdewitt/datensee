package com.datensee.pixel.fetch;

import com.datensee.pixel.PixelGrid;
import com.datensee.pixel.TileCoordinate;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
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
 *   <li>{@link TileFetchDoFn#SUCCESS_TAG} — successfully fetched tiles
 *   <li>{@link TileFetchDoFn#FAILED_TAG} — tiles that exhausted all retries
 * </ul>
 */
public final class TileFetchTransform
    extends PTransform<PCollection<TileCoordinate>, PCollectionTuple> {

    private final String eeExpression;
    private final String geeProject;
    private final PixelGrid parentGrid;
    private final String impersonateSa;

    public TileFetchTransform(
        String eeExpression,
        String geeProject,
        PixelGrid parentGrid,
        String impersonateSa
    ) {
        this.eeExpression = eeExpression;
        this.geeProject = geeProject;
        this.parentGrid = parentGrid;
        this.impersonateSa = impersonateSa;
    }

    @Override
    public PCollectionTuple expand(PCollection<TileCoordinate> input) {
        return input.apply(
            "FetchTileFromEE",
            ParDo.of(new TileFetchDoFn(eeExpression, geeProject, parentGrid, impersonateSa))
                .withOutputTags(TileFetchDoFn.SUCCESS_TAG,
                    TupleTagList.of(TileFetchDoFn.FAILED_TAG))
        );
    }
}
