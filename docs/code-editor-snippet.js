/*
 * DatensEE: export bundle for the Earth Engine Code Editor.
 *
 * The Code Editor is JavaScript and its only output is the Console, so this
 * prints a single JSON "bundle" carrying both the serialized computation and
 * the export region. Copy the printed object into one file (e.g. bundle.json)
 * and run:
 *
 *     datensee export bundle.json --project my-gcp-project \
 *       --output gs://my-bucket/my-export --scale 10 --crs EPSG:32610
 *
 * One file instead of two: no separate expression.json / region.geojson.
 *
 * Edit the two lines below to point at your own image and region. `image`
 * must be an ee.Image (reduce a collection first); `region` an ee.Geometry.
 */

var image  = ee.Image('USGS/SRTMGL1_003');            // <-- your ee.Image
var region = ee.Geometry.Rectangle([-122.6, 37.2, -122.0, 37.9]);  // <-- your ee.Geometry

// The serialized graph is a JSON string; parse it so the bundle is one clean
// object rather than a string-inside-a-string.
print('Copy the object below into bundle.json:');
print(JSON.stringify({
  expression: JSON.parse(ee.Serializer.encode(image)),
  region: region.toGeoJSON()
}));

/*
 * Tips:
 *  - Exact grid instead of --scale? Pass --crs-transform / --dimensions to
 *    datensee (mirrors Export.image's crsTransform + dimensions) to pin the
 *    output grid pixel-for-pixel. You can read an asset's native grid in the
 *    Code Editor with print(image.projection().getInfo()).
 *  - The bundle may also carry a "region" that is a Polygon/MultiPolygon
 *    GeoJSON; Points and lines are rejected.
 */
