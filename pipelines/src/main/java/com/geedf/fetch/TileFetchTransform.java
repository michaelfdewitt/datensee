package com.geedf.fetch;

import com.geedf.FetchedTile;
import com.geedf.TileCoordinate;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;

/**
 * Beam PTransform that maps tile coordinates to fetched image data.
 *
 * <p>Each tile is fetched independently via the EE High Volume API.
 * The {@code eeExpression} is passed through opaquely to the HV endpoint;
 * Earth Engine evaluates it per tile.
 */
public final class TileFetchTransform
    extends PTransform<PCollection<TileCoordinate>, PCollection<FetchedTile>> {

    private final String eeExpression;

    public TileFetchTransform(String eeExpression) {
        this.eeExpression = eeExpression;
    }

    @Override
    public PCollection<FetchedTile> expand(PCollection<TileCoordinate> input) {
        return input.apply(
            "FetchTileFromEE",
            ParDo.of(new TileFetchDoFn(eeExpression))
        );
    }
}
