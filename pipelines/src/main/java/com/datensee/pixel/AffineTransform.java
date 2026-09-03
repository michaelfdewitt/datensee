package com.datensee.pixel;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.io.Serializable;

/**
 * 6-tuple geo-affine in the EE / GDAL / GeoTIFF convention.
 *
 * <p>For axis-aligned grids, {@code shearX} and {@code shearY} are zero,
 * {@code scaleX} is positive, and {@code scaleY} is negative, placing
 * {@code (translateX, translateY)} at the NW corner of the top-left pixel
 * (PixelIsArea convention).
 *
 * <p>Sent verbatim in the EE {@code computePixels} request body as the
 * {@code grid.affineTransform} object.
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record AffineTransform(
    @JsonProperty("scale_x") double scaleX,
    @JsonProperty("shear_x") double shearX,
    @JsonProperty("translate_x") double translateX,
    @JsonProperty("shear_y") double shearY,
    @JsonProperty("scale_y") double scaleY,
    @JsonProperty("translate_y") double translateY
) implements Serializable { }
