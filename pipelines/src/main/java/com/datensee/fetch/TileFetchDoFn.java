package com.datensee.fetch;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.datensee.FetchedTile;
import com.datensee.TileCoordinate;
import com.google.auth.oauth2.GoogleCredentials;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.Collections;
import org.apache.beam.sdk.transforms.DoFn;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * DoFn that fetches a single tile from the EE High Volume API.
 *
 * <p>Auth: uses Application Default Credentials with the Earth Engine scope.
 * Credentials are initialized once per worker in {@code @Setup} and refreshed
 * as needed before each request.
 *
 * <p>Rate limiting: each worker uses exponential backoff with jitter on 429/503
 * responses. Coordinated global rate limiting (Beam state or token bucket) is
 * a TODO for M3 once we have observed real quota pressure.
 */
public final class TileFetchDoFn extends DoFn<TileCoordinate, FetchedTile> {

    private static final Logger LOG = LoggerFactory.getLogger(TileFetchDoFn.class);
    private static final String HV_ENDPOINT =
        "https://earthengine-highvolume.googleapis.com/v1/projects/%s/image:computePixels";
    private static final String EE_SCOPE = "https://www.googleapis.com/auth/earthengine";
    private static final int MAX_RETRIES = 5;
    private static final Duration INITIAL_BACKOFF = Duration.ofSeconds(1);

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final String eeExpression;
    private final String geeProject;
    private final int tileSizePixels;

    // Transient: not serialized by Beam; recreated on each worker in @Setup.
    private transient HttpClient httpClient;
    private transient GoogleCredentials credentials;

    public TileFetchDoFn(String eeExpression, String geeProject, int tileSizePixels) {
        this.eeExpression = eeExpression;
        this.geeProject = geeProject;
        this.tileSizePixels = tileSizePixels;
    }

    @Setup
    public void setup() throws IOException {
        credentials = GoogleCredentials.getApplicationDefault()
            .createScoped(Collections.singleton(EE_SCOPE));
        httpClient = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(30))
            .build();
        LOG.info("Worker setup complete: ADC credentials initialized for project={}", geeProject);
    }

    @ProcessElement
    public void processElement(
        @Element TileCoordinate tile,
        OutputReceiver<FetchedTile> out
    ) throws IOException, InterruptedException {
        byte[] imageBytes = fetchWithRetry(tile);
        out.output(new FetchedTile(tile, imageBytes, tileSizePixels, tileSizePixels));
    }

    private byte[] fetchWithRetry(TileCoordinate tile)
        throws IOException, InterruptedException {
        Duration backoff = INITIAL_BACKOFF;
        IOException lastException = null;

        for (int attempt = 1; attempt <= MAX_RETRIES; attempt++) {
            try {
                return fetchTile(tile);
            } catch (IOException e) {
                lastException = e;
                LOG.warn(
                    "{}: attempt {}/{} failed: {}",
                    tile.id(), attempt, MAX_RETRIES, e.getMessage()
                );
                if (attempt < MAX_RETRIES) {
                    long jitter = (long) (Math.random() * backoff.toMillis() * 0.2);
                    Thread.sleep(backoff.toMillis() + jitter);
                    backoff = backoff.multipliedBy(2);
                }
            }
        }

        throw new IOException(
            String.format("All %d fetch attempts failed for %s", MAX_RETRIES, tile.id()),
            lastException
        );
    }

    private byte[] fetchTile(TileCoordinate tile) throws IOException, InterruptedException {
        credentials.refreshIfExpired();
        String token = credentials.getAccessToken().getTokenValue();
        String requestBody = buildRequestBody(tile);

        HttpRequest request = HttpRequest.newBuilder()
            .uri(URI.create(String.format(HV_ENDPOINT, geeProject)))
            .header("Content-Type", "application/json")
            .header("Authorization", "Bearer " + token)
            .header("x-goog-user-project", geeProject)
            .POST(HttpRequest.BodyPublishers.ofString(requestBody))
            .timeout(Duration.ofSeconds(120))
            .build();

        HttpResponse<byte[]> response = httpClient.send(
            request,
            HttpResponse.BodyHandlers.ofByteArray()
        );

        int status = response.statusCode();
        if (status == 429 || status == 503) {
            // Surface as IOException so retry logic handles it.
            throw new IOException(
                String.format("EE HV API rate-limited (HTTP %d) for %s", status, tile.id())
            );
        }
        if (status != 200) {
            String body = new String(response.body());
            throw new IOException(
                String.format(
                    "EE HV API returned HTTP %d for %s: %s",
                    status, tile.id(), body.length() > 200 ? body.substring(0, 200) : body
                )
            );
        }

        LOG.debug("{}: fetched {} bytes", tile.id(), response.body().length);
        return response.body();
    }

    /**
     * Build the EE computePixels JSON request body using Jackson.
     *
     * <p>The {@code eeExpression} is a JSON string containing the serialized
     * EE computation graph. We parse it into a {@link JsonNode} and embed it
     * as the {@code expression} field — the HV API expects a JSON object there,
     * not a quoted string.
     */
    private String buildRequestBody(TileCoordinate tile) throws IOException {
        JsonNode expressionNode = MAPPER.readTree(eeExpression);

        double pixelWidth = (tile.xMax() - tile.xMin()) / tileSizePixels;
        double pixelHeight = (tile.yMax() - tile.yMin()) / tileSizePixels;

        ObjectNode affine = MAPPER.createObjectNode();
        affine.put("scaleX", pixelWidth);
        affine.put("shearX", 0.0);
        affine.put("translateX", tile.xMin());
        affine.put("shearY", 0.0);
        affine.put("scaleY", -pixelHeight);
        affine.put("translateY", tile.yMax());

        ObjectNode dimensions = MAPPER.createObjectNode();
        dimensions.put("width", tileSizePixels);
        dimensions.put("height", tileSizePixels);

        ObjectNode grid = MAPPER.createObjectNode();
        grid.set("dimensions", dimensions);
        grid.set("affineTransform", affine);
        grid.put("crsCode", "EPSG:4326");

        ObjectNode request = MAPPER.createObjectNode();
        request.set("expression", expressionNode);
        request.put("fileFormat", "GEO_TIFF");
        request.set("grid", grid);

        return MAPPER.writeValueAsString(request);
    }
}
