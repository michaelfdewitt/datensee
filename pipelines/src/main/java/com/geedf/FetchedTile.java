package com.geedf;

import java.io.Serializable;

/**
 * Result of fetching a single tile from the EE High Volume API.
 *
 * <p>The raw image bytes are a GeoTIFF returned by the HV endpoint.
 * Downstream assembly reads these bytes and stitches them into a COG.
 */
public record FetchedTile(
    TileCoordinate coordinate,
    byte[] imageBytes,
    int widthPixels,
    int heightPixels
) implements Serializable {}
