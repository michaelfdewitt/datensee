package com.datensee;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.datensee.fetch.TileFetchDoFn;
import com.datensee.fetch.TileFetchTransform;
import com.datensee.io.AssembledCogWriter;
import com.datensee.io.CogWriter;
import com.datensee.io.FailedTileWriter;
import com.datensee.io.TileCoordinateParser;
import com.datensee.io.VrtAssembler;
import com.datensee.options.DatensEEOptions;
import com.google.auth.oauth2.AccessToken;
import com.google.auth.oauth2.GoogleCredentials;
import java.io.IOException;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.apache.beam.sdk.Pipeline;
import org.apache.beam.sdk.extensions.gcp.options.GcpOptions;
import org.apache.beam.sdk.io.TextIO;
import org.apache.beam.sdk.options.PipelineOptionsFactory;
import org.apache.beam.sdk.transforms.Create;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Entry point for the DatensEE pipeline.
 *
 * <p>Reads a {@code pipeline-config.json} describing the EE computation,
 * tile grid, and output destination; then orchestrates distributed tile
 * fetching via the Earth Engine High Volume API and COG assembly.
 *
 * <p>M3 features: partial failure tolerance (dead-letter), per-worker rate
 * limiting, smart retry classification, file-based tile input, VRT assembly.
 */
public final class DatensEEPipeline {

    private static final Logger LOG = LoggerFactory.getLogger(DatensEEPipeline.class);
    private static final ObjectMapper MAPPER = new ObjectMapper()
        .registerModule(new JavaTimeModule());

    private DatensEEPipeline() { }

    public static void main(String[] args) throws IOException {
        DatensEEOptions options = PipelineOptionsFactory
            .fromArgs(args)
            .withValidation()
            .as(DatensEEOptions.class);

        run(options);
    }

    /**
     * Build and run the pipeline.
     *
     * @param options parsed pipeline options (includes path to config JSON)
     */
    static void run(DatensEEOptions options) throws IOException {
        applyUserCredentials(options);

        PipelineConfig config = loadConfig(options.getConfigFile());
        validateConfig(config);

        int tileSize = config.tileGrid().effectiveTileSize();
        String crs = config.tileGrid().crs();
        PipelineConfig.RateLimitConfig rateLimit = config.effectiveRateLimit();
        int maxWorkers = resolveMaxWorkers(config);

        LOG.info(
            "Pipeline config: project={}, tiles={}, tileSize={}px, crs={}, "
            + "output={}, maxQps={}, maxWorkers={}",
            config.geeProject(),
            config.tileGrid().hasExternalTiles() ? "(file)" : config.tileCount(),
            tileSize,
            crs,
            config.output().outputPath(),
            rateLimit.effectiveMaxQps(),
            maxWorkers
        );

        Pipeline pipeline = Pipeline.create(options);

        // --- Tile source: inline or file-based ---
        PCollection<TileCoordinate> tiles;
        if (config.tileGrid().hasExternalTiles()) {
            LOG.info("Reading tiles from file: {}", config.tileGrid().tilesFile());
            tiles = pipeline
                .apply("ReadTileFile", TextIO.read().from(config.tileGrid().tilesFile()))
                .apply("ParseTileCoordinates", ParDo.of(new TileCoordinateParser()));
        } else {
            tiles = pipeline.apply(
                "CreateTiles",
                Create.of(config.tileGrid().tiles())
            );
        }

        // --- Fetch tiles with dead-letter support ---
        PCollectionTuple fetchResult = tiles.apply(
            "FetchTiles",
            new TileFetchTransform(
                config.eeExpression(), config.geeProject(), tileSize, crs,
                rateLimit.effectiveMaxQps(), maxWorkers
            )
        );

        PCollection<FetchedTile> fetched = fetchResult.get(TileFetchDoFn.SUCCESS_TAG);
        PCollection<FailedTileRecord> failed = fetchResult.get(TileFetchDoFn.FAILED_TAG);

        // --- Write successful tiles as COGs ---
        // Default: deflate (zlib). Our hand-rolled LZW encoder in
        // CogTranscoder produces output that strict TIFF-LZW decoders
        // (GDAL, imagecodecs, EE's loader) reject with "Corrupted tile:
        // failed to decompress using scheme LZW". Until the LZW encoder
        // is rewritten against a canonical test vector, keep the default
        // at deflate — it's a java.util.zip.Deflater pass-through so
        // there's nothing to get wrong. Explicit `compress: "lzw"` still
        // works for callers that want to experiment.
        String compression = config.output().cog() != null
            && config.output().cog().compress() != null
            ? config.output().cog().compress() : "deflate";

        // M6 routing: when output_tile_size_pixels is set and larger
        // than the compute tile size, group compute tiles by output tile
        // and assemble. Otherwise fall through to one COG per compute tile.
        int outputTileSize = config.output().effectiveOutputTileSizePixels(tileSize);
        boolean twoTier = outputTileSize > tileSize;
        if (twoTier) {
            LOG.info(
                "M6 two-tier tiling: output tile size = {}px (= {}x{} compute tiles per output COG)",
                outputTileSize, outputTileSize / tileSize, outputTileSize / tileSize
            );
            fetched.apply(
                "AssembleAndWriteOutputTiles",
                new AssembledCogWriter(
                    config.output().outputPath(), tileSize, outputTileSize, compression
                )
            );
        } else {
            fetched.apply(
                "WriteTiles",
                new CogWriter(config.output().outputPath(), tileSize, compression)
            );
        }

        // --- Write failure report ---
        String failuresPath = failuresOutputPath(config.output().outputPath());
        failed
            .apply("FormatFailedTiles", ParDo.of(new FailedTileWriter()))
            .apply("WriteFailures", TextIO.write()
                .to(failuresPath)
                .withoutSharding()
                .withSuffix(".json"));

        // --- VRT assembly ---
        // VRT references the actual on-disk files. In two-tier mode,
        // those are output tiles (tile size = outputTileSize); otherwise
        // they're compute tiles (tile size = tileSize).
        fetched.apply(
            "AssembleVrt",
            new VrtAssembler(
                config.output().outputPath(),
                crs,
                config.output().effectiveBandCount(),
                config.output().effectiveDataType(),
                outputTileSize,
                twoTier
            )
        );

        var result = pipeline.run();

        boolean isDataflow = config.runner() != null
            && "dataflow".equals(config.runner().mode());
        if (isDataflow) {
            // DataflowPipelineJob is a runtime-only dep; extract job ID
            // via reflection to keep the compile-time dep on beam-core only.
            try {
                String jobId = result.getClass()
                    .getMethod("getJobId")
                    .invoke(result)
                    .toString();
                LOG.info("Dataflow job submitted: {}", jobId);
                System.out.println("DATENSEE_JOB_ID=" + jobId);
                System.out.flush();
            } catch (ReflectiveOperationException e) {
                LOG.warn("Could not extract Dataflow job ID: {}", e.toString());
            }
        } else {
            result.waitUntilFinish();
        }
    }

    /**
     * Install a caller-supplied OAuth access token as the pipeline's GCP
     * credential, if one was passed via {@code --userTokenFd=<N>}. When
     * absent, the pipeline falls back to application default credentials,
     * preserving the standalone-CLI flow.
     *
     * <p>Security: the token is a bearer credential. The parent side
     * (Python {@code datensee.submit}) creates an {@code os.pipe()}, writes
     * the token onto the write-end, closes the write-end, and marks the
     * read-end inheritable before spawning this JVM. We read the inherited
     * FD via {@code /proc/self/fd/<N>}, parse the token, construct the
     * credential, and zero the intermediate byte buffer. The token never
     * appears on argv ({@code /proc/<pid>/cmdline}) or the environment
     * ({@code /proc/<pid>/environ}), so the only on-disk exposure is the
     * FD symlink itself, and the pipe is at EOF before any other code in
     * this process runs.
     *
     * <p>Linux-only: we rely on {@code /proc/self/fd/<N>} to reopen the
     * inherited FD as a regular {@code Path}.
     */
    private static void applyUserCredentials(DatensEEOptions options) throws IOException {
        Integer fd = options.getUserTokenFd();
        if (fd == null || fd < 0) {
            LOG.info("No --userTokenFd set — falling back to application default credentials.");
            return;
        }

        Path fdPath = Path.of("/proc/self/fd/" + fd);
        byte[] buf = Files.readAllBytes(fdPath);
        try {
            // Trim trailing whitespace (parent may or may not newline-terminate)
            // without materializing a String copy longer than necessary.
            int end = buf.length;
            while (end > 0 && Character.isWhitespace((char) (buf[end - 1] & 0xFF))) {
                end--;
            }
            if (end == 0) {
                throw new IOException(
                    "Received empty access token on --userTokenFd=" + fd
                );
            }
            String token = new String(buf, 0, end, StandardCharsets.UTF_8);
            try {
                // Attach the target GCP project as the quota project. Without
                // this, google-api-client sends the API call with no
                // x-goog-user-project header, and Google attributes quota +
                // API-enablement checks to the OAuth client's implicit project
                // (the FoundrEE app project), which doesn't have Dataflow
                // enabled and should never be billed for user jobs. With it
                // set, quota lands on the user's own project — which is the
                // same project the Dataflow job runs in, so enablement and
                // billing line up.
                //
                // We wrap the bare AccessToken credential in a subclass that
                // forces x-goog-user-project into `getRequestMetadata(URI)`.
                // `GoogleCredentials.createWithQuotaProject` alone does not
                // propagate the header through that code path in this version
                // of google-auth-library — empirically confirmed by dumping
                // the metadata map from a deployed pipeline. HttpCredentialsAdapter
                // (Beam's Dataflow client) only calls the URI variant, so we
                // have to override that method directly.
                String quotaProject = options.as(GcpOptions.class).getProject();
                GoogleCredentials credentials = new QuotaProjectUserAccessTokenCredentials(
                    new AccessToken(token, null), quotaProject
                );
                options.as(GcpOptions.class).setGcpCredential(credentials);
                LOG.info(
                    "Installed caller-supplied access token from --userTokenFd={} as pipeline GCP credential (quotaProject={}).",
                    fd, quotaProject
                );
                try {
                    Map<String, List<String>> md =
                        credentials.getRequestMetadata(URI.create("https://dataflow.googleapis.com/"));
                    List<String> redacted = new ArrayList<>(md.keySet());
                    Collections.sort(redacted);
                    List<String> userProj = md.get("x-goog-user-project");
                    LOG.info(
                        "Credential request metadata header keys={}, x-goog-user-project={}",
                        redacted,
                        userProj != null ? userProj : "(absent)"
                    );
                } catch (IOException mdEx) {
                    LOG.warn("Failed to dump credential request metadata: {}", mdEx.toString());
                }
            } finally {
                // String contents are immutable in the JVM, so we can't zero
                // `token`; we just drop the local ref and let GC reclaim it.
                // The intermediate byte buffer — which we *do* own — is
                // wiped below.
                token = null;
            }
        } finally {
            Arrays.fill(buf, (byte) 0);
        }
    }

    private static int resolveMaxWorkers(PipelineConfig config) {
        if (config.runner() != null
            && config.runner().dataflow() != null
            && config.runner().dataflow().maxWorkers() > 0) {
            return config.runner().dataflow().maxWorkers();
        }
        // Local runner: single JVM, typically 1 effective worker
        return 1;
    }

    private static String failuresOutputPath(String outputPath) {
        if (outputPath.endsWith("/")) {
            return outputPath + "_failures";
        }
        return outputPath + "/_failures";
    }

    private static PipelineConfig loadConfig(String configFile) throws IOException {
        String json = Files.readString(Path.of(configFile));
        return MAPPER.readValue(json, PipelineConfig.class);
    }

    private static void validateConfig(PipelineConfig config) {
        if (config.geeProject() == null || config.geeProject().isBlank()) {
            throw new IllegalArgumentException(
                "gee_project is required. Set it to your GCP project ID "
                + "with the Earth Engine API enabled."
            );
        }
        boolean hasInlineTiles = config.tileGrid() != null
            && config.tileGrid().tiles() != null
            && !config.tileGrid().tiles().isEmpty();
        boolean hasFileTiles = config.tileGrid() != null
            && config.tileGrid().hasExternalTiles();
        if (!hasInlineTiles && !hasFileTiles) {
            throw new IllegalArgumentException(
                "tile_grid must contain either inline tiles or a tiles_file path. "
                + "Check that the region intersects the tile grid."
            );
        }
        if (config.output() == null || config.output().outputPath() == null
            || config.output().outputPath().isBlank()) {
            throw new IllegalArgumentException(
                "output.output_path is required. Provide a GCS URI (gs://…) "
                + "or a local directory path."
            );
        }
        if (config.tileGrid().crs() == null || config.tileGrid().crs().isBlank()) {
            throw new IllegalArgumentException(
                "tile_grid.crs is required. Provide an EPSG code (e.g. 'EPSG:4326') "
                + "or a proj string."
            );
        }
    }

    /**
     * GoogleCredentials subclass that wraps a bare OAuth access token and
     * forces {@code x-goog-user-project} into every request metadata map
     * regardless of which {@code getRequestMetadata} variant the HTTP
     * initializer calls. The stock
     * {@link GoogleCredentials#createWithQuotaProject(String)} path only
     * injects the header in the no-arg {@code getRequestMetadata()} in this
     * version of google-auth-library, but Beam's Dataflow client goes
     * through {@link com.google.auth.http.HttpCredentialsAdapter} which
     * calls the URI-taking variant — so the header never makes it onto the
     * Dataflow {@code createJob} request, and the API enablement check
     * lands on the OAuth client's implicit project instead of the user's.
     *
     * <p>We can't refresh a bearer-only credential, so we override the
     * superclass's refresh machinery to a no-op: the caller has already
     * guaranteed the token is live, and if it expires mid-pipeline the
     * rest of the stack will surface a 401.
     */
    private static final class QuotaProjectUserAccessTokenCredentials extends GoogleCredentials {
        private static final long serialVersionUID = 1L;
        private final String quotaProjectId;

        QuotaProjectUserAccessTokenCredentials(AccessToken token, String quotaProjectId) {
            super(token);
            this.quotaProjectId = quotaProjectId;
        }

        @Override
        public String getQuotaProjectId() {
            return quotaProjectId;
        }

        @Override
        public AccessToken refreshAccessToken() {
            // Bearer-only; caller guarantees freshness. No refresh path.
            return getAccessToken();
        }

        @Override
        public Map<String, List<String>> getRequestMetadata(URI uri) throws IOException {
            return injectQuotaProject(super.getRequestMetadata(uri));
        }

        @Override
        public Map<String, List<String>> getRequestMetadata() throws IOException {
            return injectQuotaProject(super.getRequestMetadata());
        }

        private Map<String, List<String>> injectQuotaProject(Map<String, List<String>> base) {
            if (quotaProjectId == null || quotaProjectId.isBlank()) {
                return base;
            }
            if (base != null && base.containsKey("x-goog-user-project")) {
                return base;
            }
            Map<String, List<String>> merged = new LinkedHashMap<>();
            if (base != null) {
                merged.putAll(base);
            }
            merged.put("x-goog-user-project", Collections.singletonList(quotaProjectId));
            return Collections.unmodifiableMap(merged);
        }
    }
}
