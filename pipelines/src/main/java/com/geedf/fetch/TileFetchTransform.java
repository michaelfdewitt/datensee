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
 * Earth Engine evaluates it per tile bounded to that tile's grid geometry.
 */
public final class TileFetchTransform
    extends PTransform<PCollection<TileCoordinate>, PCollection<FetchedTile>> {

    private final String eeExpression;
    private final String geeProject;
    private final int tileSizePixels;

    public TileFetchTransform(String eeExpression, String geeProject, int tileSizePixels) {
        this.eeExpression = eeExpression;
        this.geeProject = geeProject;
        this.tileSizePixels = tileSizePixels;
    }

    @Override
    public PCollection<FetchedTile> expand(PCollection<TileCoordinate> input) {
        return input.apply(
            "FetchTileFromEE",
            ParDo.of(new TileFetchDoFn(eeExpression, geeProject, tileSizePixels))
        );
    }
}
