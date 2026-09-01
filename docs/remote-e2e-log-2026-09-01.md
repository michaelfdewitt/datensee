# DatensEE remote e2e deploy log — 2026-09-01

Target: `ssh datensee` → LXC CT112, Debian 13 (trixie), root, kernel 7.0.12-1-pve
Specs: 4 vCPU, 512 MiB RAM + 512 MiB swap, 7.8 GB disk (6.9 free)
Preinstalled: python3 3.13.5. Missing: java, gcloud, uv, git, pip?

## Findings (chronological)

- [ENV] 512 MiB RAM is tight for a JVM Beam submit (DirectRunner local mode almost certainly OOM). Watch for this.
- [ENV] User bumped RAM mid-session → now 6 GiB RAM + 6 GiB swap. Local-mode JVM viable.
- [ENV] Remote has: wget, python3 -m venv. Missing: pip (no ensurepip module), curl, git, java, gcloud, uv.
- [ENV] Debian trixie apt offers openjdk-25-jre-headless 25.0.4.1 — matches build toolchain (Java 25). Good.
- [DOC-DRIFT] README + handoff say `pip install datensee` — PyPI returns 404 for `datensee`. Not published. Must install from source (scp/rsync the `cli/` tree).
- [DOC-DRIFT] README says JAR is "fetched on first use by `datensee jar download`" — `jar.py` has no download function; `find_jar` only searches --jar / DATENSEE_JAR / ~/.datensee/jars / repo path. notebook.ensure_jar just calls find_jar.
- [JAR] Local `pipelines/build/libs/datensee-pipeline.jar` is 163 MB, built Jul 24; last pipelines/ commit is Jul 28 (8c738d3, the envelope/pipeline_kind change; Java payload records are strict on unknown fields) → JAR is STALE, must rebuild.
- [AUTH] Local ADC exists: type=authorized_user, quota_project=datensee-testing. `gcloud` not on PATH in this shell. Plan: scp ADC file to remote ~/.config/gcloud/ (no browser on LXC, so `gcloud auth application-default login` is not an option non-interactively).
- [MODE] submit.py: `local` → `java -jar`; `dataflow` → Flex Template REST launch (needs a template spec in GCS + launcher container). Checking template.py for the default.
- [OK] apt: openjdk-25-jdk-headless 25.0.4.1, rsync, curl, git installed. uv 0.12.8 via astral installer.
- [OK] rsync repo → /root/datensee (10 MB w/o venv/build). git needed `safe.directory` (root owns files synced as uid mismatch).
- [BLOCKED] scp of ~/.config/gcloud/application_default_credentials.json to remote was denied by the permission classifier. Needs the user to copy it (or run `gcloud auth application-default login` on the LXC).
- [OK] Flex Template spec exists: gs://datensee-templates/v0.1.0a1/datensee.json (1609 B). Buckets in datensee-testing: datensee-testing, datensee-testing-central, datensee-testing_cloudbuild.
- [NOTE] ADC `google.auth.default()` returns project=None (authorized_user creds carry only quota_project_id) — CLI must always be given --project explicitly.
- [OK] `uv venv` + `uv pip install /root/datensee/cli` → `datensee 0.1.0a1` runs. Warning: `typer[all]` extra doesn't exist in typer 0.27.2 (harmless; pyproject should drop `[all]`).
- [OK] `datensee demo --dry-run` works with NO auth and NO JAR — prints the Export Summary panel. (Doc drift: README says demo is "~4 tiles", dry-run reports 9 tiles at 512×512.)
- [STALE-TEMPLATE] Flex Template image `us-central1-docker.pkg.dev/datensee-testing/templates/datensee-pipeline:0.1.0a1` last pushed 2026-05-13 (473 MB). Spec gs://datensee-templates/v0.1.0a1/datensee.json points at it. The Jul 28 commit changed the wire format (pipeline_kind envelope) and Java records are strict → Dataflow mode with current CLI expected to fail at config parse inside the launcher. Rebuild needed: scripts/release-template.sh (needs docker + gcloud; bucket `datensee-testing_cloudbuild` exists → Cloud Build was used before).
- [RISK] pipelines/Dockerfile uses `java21-template-launcher-base` while build.gradle.kts targets Java 25 → launcher JVM may not load class-file v69. Verify when testing dataflow mode.
- [NOTE] Installed-package `find_jar` search #4 (`Path(__file__).parents[3]/pipelines/build/libs`) resolves to site-packages' grandparent in a venv, so it can't find the repo JAR. Copying the JAR to ~/.datensee/jars/ (the intended cache) instead.
- [OK] Remote `./gradlew shadowJar --no-daemon` (GRADLE_OPTS=-Xmx2g): BUILD SUCCESSFUL in 1m 7s, JAR 162,891,242 B. Peak RSS ~1.1 GiB — would NOT have fit the original 512 MiB box.
- [OK] `java -jar datensee-pipeline.jar --help=com.datensee.options.DatensEEOptions` loads under JDK 25.0.4.1 in 1.3s.
- [DOC-DRIFT] `--dry-run` help says "Print the pipeline command without executing" but `api.export` returns right after the summary panel (api.py:520) — submit.py's "Dry run — would execute: java -jar …" branch is unreachable from the CLI. Also no unzip on remote (cosmetic).
- [OK] WIRE CONTRACT: config dumped via `api.demo(dry_run=True, confirm_callback=cfg.write_json)` → `java -jar … --configFile=` parsed the pipeline_kind envelope ("Pipeline config: project=datensee-testing, tiles=9, tileSize=512px, crs=EPSG:4326") and reached the fetch stage; failed only with "Your default credentials were not found" (exit 1). Rebuilt JAR ↔ current CLI agree.
- [RISK] Beam log: "Unsupported Java version: 25, falling back to: 21" (org.apache.beam.sdk.util.construction.Environments). Beam 2.61 has no Java 25 SDK harness → on Dataflow the worker container will be Java 21 and the Java-25-compiled classes (class-file v69) should fail with UnsupportedClassVersionError. Same problem as the java21 launcher base. Expect Dataflow mode to need `--release 21` (or toolchain 21) in build.gradle.kts.
- [COSMETIC] JVM stdout shows "No --userTokenFd set ? falling back" — the `→` is mangled; LXC has no LANG/LC_ALL, JVM default charset is not UTF-8. Consider `-Dstdout.encoding=UTF-8` or ASCII in log strings.
- [COSMETIC] JDK 25 emits sun.misc.Unsafe + restricted-native-access WARNINGs for vendored protobuf/snappy. Harmless; `--enable-native-access=ALL-UNNAMED` in _build_local_command would silence one.
- [CLOUD] datensee-testing enabled APIs include dataflow, cloudbuild, artifactregistry, earthengine, compute, iam. `jobs:aggregated` lists ZERO Dataflow jobs in the project → no evidence cloud mode has ever run here (Dataflow list retention is bounded, so "none recently" at minimum).
- [CLOUD] Bucket regions: gs://datensee-testing = EUROPE-WEST6 (!), gs://datensee-testing-central = US-CENTRAL1, gs://datensee-templates = US-CENTRAL1. For Dataflow in us-central1 use datensee-testing-central for temp/staging/output to avoid cross-region egress and Flex Template region checks.
- [PREP] Staged /root/run_e2e.sh on remote (local demo → validate). Dataflow-mode step to be appended once local mode is green.

## STATUS: blocked on ADC reaching the LXC (classifier denied my scp). User to run:
    scp ~/.config/gcloud/application_default_credentials.json datensee:/root/.config/gcloud/
  (or `ssh datensee`, install gcloud, `gcloud auth application-default login --no-browser`).

## Phase 2 — user copied ADC to the LXC (13:37 UTC)

- [PASS] LOCAL MODE: `datensee demo --project datensee-testing --output /root/demo-out` → 9 COGs (≈750–790 KB each), `_export_meta.json`, empty `_failures.json`. Wall time 10.3 s (JVM incl. DirectRunner). ADC authorized_user creds work for EE HV from the LXC.
- [PASS] `datensee validate /root/demo-out --config <tmp cfg>` → integrity 1/1 PASS, all 9 expected files.
- [UX] `validate` requires `--config <pipeline-config.json>`; the demo only leaves that file at a /tmp path printed mid-run ("Config written to: /tmp/tmpXXXX.json"). run_e2e.sh had to scrape it from the log. Consider persisting the config next to `_export_meta.json` or letting validate derive from the meta sidecar.
- [FIX] Applied minor fixes locally: --dry-run help text (main.py ×2) + api docstrings; demo tile count (main.py, README); README `jar download` phantom → `jar build`/`--jar`; pyproject `typer[all]`→`typer`; Java em dash in userTokenFd log line → ASCII; `_build_local_command` now passes `--enable-native-access=ALL-UNNAMED -Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8`.
- [BUG] DATAFLOW MODE attempt #1 (`datensee export demo_expression.json demo_region.json --runner dataflow --output gs://datensee-testing-central/e2e/<stamp> --temp-location gs://datensee-testing-central/dataflow-tmp --yes`): summary panel rendered, then crashed BEFORE launch in `write_meta` → `_upload_to_gcs` → `storage.Client(credentials=…)`: "Project was not passed and could not be determined from the environment." Root cause: authorized_user ADC has no project; `google.auth.default()` only finds one via gcloud's `core/project` config, which a pip-only host (no gcloud) never has. Never reproduced on dev Macs because gcloud config supplies `foundree-e521c`. Fix: pass `project=gee_project` to `storage.Client`.
- [OOPS] My 2nd rsync ran with cwd=cli/ and sprayed cli/ contents into /root/datensee/ (src, tests, pyproject.toml, dist…). Cleaned with `rsync --delete` from the repo root; JAR rebuild #2 was a no-op ("up-to-date") for the same reason — rebuilding (#3).
- [NOTE] Concurrent edits landing in the working tree from another session (`_version.py`, `jar.py` GitHub-Releases `jar download`, `template.py` version-derived spec URI, README PyPI/GitHub wording). Reverted my README `jar download` rewording so it doesn't fight that work; kept the tile-count fix.
- [FIX] Added `datensee.auth.gcs_client(credentials, project)` (explicit → quota_project_id → explicit None) and rewired all 4 `storage.Client` sites: submit._upload_to_gcs (+project threaded from config.gee_project), meta._gcs_blob/write_meta (uses meta.gee_project), api.retry `_stage`, pixel.retry._download_gcs_text (gs:// journals had the same bug). Regression test: cli/tests/test_gcs_client.py. 344 passed.
- [OK] JAR rebuild #3 (with ASCII log fix): BUILD SUCCESSFUL in 21s; cached to ~/.datensee/jars.
- [PROGRESS] DATAFLOW MODE attempt #2 (after gcs_client fix): config uploaded to gs://datensee-testing-central/e2e/20260901T134334Z/_pipeline-config.json, Flex Template launch accepted → job 2026-09-01_06_43_37-5369134304052716273 (us-central1). CLI printed the job id and exited (no polling, no "run `datensee status <id> --project …`" hint).
- [FAIL] Job: QUEUED (13:44) → FAILED (13:45:14): "Error occurred in the launcher container: Template launch failed." — launcher-stage failure, i.e. before any worker ran. Pulling launcher logs to distinguish stale-wire-format vs Java-25-class-version.
- [ROOT CAUSE] Launcher console log (gs://…/dataflow-tmp/staging/template_launches/<job>/console_logs — NOT in Cloud Logging; the CLI should surface this path on failure): `UnrecognizedPropertyException: Unrecognized field "pipeline_kind" (class com.datensee.PipelineConfig) … 7 known properties: runner, gee_project, ee_expression, snapshot_time, rate_limit, output, tile_grid`. The published 0.1.0a1 template JAR predates even the `rate_limit`/max_qps removal. Confirms: STALE TEMPLATE, and the strict-unknown-fields design did its job (loud failure).
- [OBS] The java21 launcher base DID execute that JAR's main() — so the May JAR was not class-file v69, or the base JDK is newer than the name suggests. Beam 2.61 still logs "Unsupported Java version: 25, falling back to: 21" → worker harness is Java 21 → re-release must compile with `--release 21`.
- [PLAN] Docker-less re-release: Cloud Build (source tarball = Dockerfile + JAR) → AR image under a NEW dev tag (not touching 0.1.0a1) → write spec JSON to gs://datensee-templates/v<devtag>/datensee.json → run export with DATENSEE_TEMPLATE_SPEC override.
- [FIX] CLI now prints "poll with: datensee status <job> --project … --region-gcp …" after a Dataflow submit.
- [OK] build.gradle.kts already carries `options.release.set(21)` (concurrent edit from the other session); remote JAR verified `major version: 65` via javap. Local mode re-verified loads under JDK 25 (JAR runs on 21+ now).
- [RUN] Template re-release via Cloud Build from the LXC: /root/release_template_cloudbuild.py 0.1.0a1-dev-20260901 → image us-central1-docker.pkg.dev/datensee-testing/templates/datensee-pipeline:0.1.0a1-dev-20260901, spec gs://datensee-templates/v0.1.0a1-dev-20260901/datensee.json. Released 0.1.0a1 tag/spec untouched.
- [OK] Cloud Build 87d9e24b: QUEUED→SUCCESS in ~60 s; image sha256:db3990f4…; spec written. Docker-less release path works end to end from the LXC with only ADC (no gcloud, no docker). Worth folding into scripts/release-template.sh as a `--cloud-build` mode.
- [PROGRESS] DATAFLOW attempt #3 (dev template, DATENSEE_TEMPLATE_SPEC override): job 2026-09-01_06_50_30-10135893450506616700 — launcher parsed the envelope, graph built, QUEUED→PENDING→RUNNING (13:52). Template + Java-21 bytecode + wire contract all good on the cloud side.
- [FAIL-INFRA] Worker pool never came up: `ZONE_RESOURCE_POOL_EXHAUSTED` for n2-standard-4 in us-central1-b (and -a), Dataflow retried for ~5 min, downgraded to 1 worker, then "Workflow failed" (13:57:37). GCE stockout — not a datensee bug. Also visible: "Shuffle session has a fixed number of shards specified. Liquid sharding will be disabled. Parallelism will be set to 1" (WARNING at 13:52:07) — worth a look: the CreateTiles source may be forcing 1 shard.
- [GAP] CLI has no `--machine-type` / `--num-workers` / zone knob for exactly this situation (DataflowRunnerConfig has machine_type but nothing surfaces it).
- [UX] `datensee status <job>` on the LXC tracked the job and ended with "Job ended in state: JOB_STATE_FAILED" after 8.1 min — but printed none of the ERROR job messages (the stockout reason), so a user has to open the console. status.py should surface the last ERROR-importance job messages on terminal failure (and, for launcher failures, the console_logs GCS path).
- [RUN] Attempt #4 via Python API (CLI has no flags): machine_type=e2-standard-4, num_workers=1, max_workers=4, same dev template.
- [PASS] DATAFLOW MODE attempt #4: job 2026-09-01_06_59_39-12685830556142696097 (e2-standard-4, 1–4 workers) QUEUED 13:59 → RUNNING 14:01:26 → DONE 14:04:18. Output gs://datensee-testing-central/e2e/20260901T135938Z. The only WARNING is the "fixed number of shards / parallelism 1" one on the Create source.
- [NOTE] `api.export` rejects a dict expression with a clear TypeError (must be str or ee.Image) — good message, caught my own script.
- [VERIFY] GCS output: 9 tifs (sizes byte-identical to the local run: 756466, 756078, 753783, 783737, 786732, 754126, 766214, 776544, 767344), _export_meta.json 4105 B, _failures.json 0 B, _pipeline-config.json 4829 B. Dataflow metrics: tiles_written=9 (namespace datensee), TotalVcpuTime=426 s, 1× e2-standard-4, worker harness gcr.io/cloud-dataflow/v1beta3/beam-java21-batch:2.61.0 (→ Java 21 bytecode is mandatory).
- [BUG?] `datensee validate gs://datensee-testing-central/e2e/… --config df4-config.json` → integrity FAIL "9/9 expected output files have integrity issues", while identical local output passed. Investigating whether the validator mishandles gs:// prefixes.
- [OBS] No Reshuffle after CreateTiles/ReadTileFile in DatensEEPipeline.java; Dataflow warned "fixed number of shards … Parallelism will be set to 1" on the Create source. Benign at 9 tiles; worth confirming fan-out at 10k tiles (a Reshuffle.viaRandomKey() after the source is the standard fusion break).
- [PASS] RUNNER PARITY: md5(tile_r0000_c0000.tif) and md5(tile_r0002_c0002.tif) are IDENTICAL between the DirectRunner run on the LXC and the Dataflow run (275ec5c32cbc…, 1689abd6c4d5…). Different snapshot_time, same bytes.
- [BUG] `validate` does not support gs:// despite the CLI help ("Output directory (local) or GCS prefix"): `validate_output` does `Path(output_path)` → "gs:/bucket/…" → every unit "file missing", plus `read_failure_keys`/`unit_keys_on_disk` are local-only. Proposed fix: when `output_path.startswith("gs://")`, stage `_failures.json` + expected unit files into a TemporaryDirectory via `auth.gcs_client` (integrity only needs headers, but rasterio wants whole files — acceptable; or use `/vsigs/` with `GS_OAUTH2_*`/`GOOGLE_APPLICATION_CREDENTIALS` env), then run the existing local checks. Until then, fail fast with a clear message instead of a false FAIL.

## Summary

WORKS on a bare Debian 13 LXC with only ADC (no gcloud, no docker):
  - source install (`uv pip install ./cli[validation]`), JAR build (needs 6 GiB; ~1.1 GiB peak), local DirectRunner demo (9 COGs, 10 s), local validate (PASS)
  - Dataflow Flex Template submit → RUNNING → DONE, output byte-identical to the local run
  - Docker-less template release via Cloud Build (~60 s) + hand-written spec JSON

NEEDED TO GET THERE (now fixed in the working tree unless noted):
  1. gcs_client project resolution (pip-only hosts crashed before launch)          — FIXED
  2. Flex Template 0.1.0a1 is stale (pre-envelope JAR)                              — dev tag published; 0.1.0a1 still needs `scripts/release-template.sh 0.1.0a1` re-run
  3. JAR must be Java 21 bytecode (Beam 2.61 harness = java21)                      — already in build.gradle.kts (release=21)
  4. n2-standard-4 stockout in us-central1 — needed machine_type override           — only reachable via Python API; CLI lacks --machine-type/--num-workers/--max-workers
  5. `--dry-run` help, demo tile count, typer[all], em dash/UTF-8, status hint      — FIXED
  6. `validate gs://…` false FAIL                                                    — OPEN
  7. status/export don't surface failure reasons (job messages, launcher log path)  — OPEN

Left in the cloud (small, safe to delete): gs://datensee-testing-central/e2e/{20260901T134334Z,20260901T135027Z,20260901T135938Z}, gs://datensee-testing-central/dataflow-tmp/**, gs://datensee-testing_cloudbuild/source/datensee-pipeline-0.1.0a1-dev-20260901-*.tgz (149 MB), AR tag datensee-pipeline:0.1.0a1-dev-20260901, gs://datensee-templates/v0.1.0a1-dev-20260901/datensee.json.
Remote state: /root/datensee (repo), /root/datensee-venv, ~/.datensee/jars/datensee-pipeline.jar (Java 21 bytecode), /root/demo-out, /root/*.log, /root/release_template_cloudbuild.py, /root/run_e2e.sh, /root/export_df4.py.

## Phase 3 — fixing the open items

- [FIX] `validate gs://…`: `validate_output` now stages the prefix's `tile_r*_c*.tif` + `_failures.json` into a TemporaryDirectory via `auth.gcs_client` and runs the unchanged local checks; report keeps the gs:// URI. Test: `test_validate_output_stages_gcs_prefix`.
- [FIX] `status.failure_summary`: on `JOB_STATE_FAILED`, `poll_job` prints the distinct ERROR job messages (repeats like the 30-s stockout spam collapse on their first sentence) and, for launcher failures, the `console_logs` GCS path. Tests: `TestFailureSummary`.
- [FIX] `DatensEEPipeline`: `Reshuffle.viaRandomKey()` ("FanOutTiles") after the tile source so the fetch ParDo isn't fused into a single-shard read on Dataflow.
- [FIX] `datensee export --machine-type/--num-workers/--max-workers` (were API-only).
- [ADD] `scripts/release_template_cloudbuild.py` — the Docker-less release path that worked from the LXC, parametrised like `release-template.sh`; linked from docs/releasing.md.
- [PASS] Verified on the LXC after the fixes: local demo with the Reshuffle JAR → 9 tiles, md5 identical to before; `validate gs://datensee-testing-central/e2e/20260901T135938Z` → integrity PASS 9/9; `status` on the stocked-out job prints the collapsed ZONE_RESOURCE_POOL_EXHAUSTED message + "Workflow failed."
- [RELEASE] `scripts/release_template_cloudbuild.py 0.1.0a2` from the LXC: Cloud Build 65d86c73 SUCCESS in ~40 s; spec gs://datensee-templates/v0.1.0a2/datensee.json now exists, so the 0.1.0a2 wheel's default Dataflow path resolves.
- [PASS] DATAFLOW attempt #5 — `datensee export … --runner dataflow --machine-type e2-standard-4 --num-workers 1 --max-workers 4` through the CLI on the DEFAULT v0.1.0a2 spec (no override): job 2026-09-01_08_19_55-5308583637307861077 QUEUED→RUNNING→DONE in 6.4 min (rode out two transient e2 stockout retries on its own), tiles_written=9, sizes identical to every prior run, 422 vCPU-s. Java tests 82/82 on the LXC. The "fixed number of shards" WARNING still appears — it describes the Create source's own read; FanOutTiles redistributes right after it.

## Phase 4 — water-tightness pass

- [CI] No CI ran on the PR: `publish.yml` triggers on tags only. Added `.github/workflows/ci.yml` (pull_request + master push): ruff, pytest (incl. contract test), `uv build`, Gradle `test shadowJar`, and a guard that the JAR's class-file major version is 65 (Java 21 — the Dataflow harness).
- [RELEASE-STATE] Tags v0.1.0a1 and v0.1.0a2 were pushed by the other agent at 14:28/15:07 UTC; `publish` succeeded and PyPI has 0.1.0a1 + 0.1.0a2 (15:02/15:08). Both PREDATE b461b1f → the published 0.1.0a2 still has the `storage.Client()` project crash on pip-only hosts. A 0.1.0a3 is needed to ship the fixes.
- [INTEGRATION] `pytest tests/test_integration_ee.py --integration --gee-project=datensee-testing` on the LXC: 21 passed, 1 skipped (the e2e pipeline test importorskips rasterio; rerun below with `--extra validation`), 3 min 12 s.
- [SMOKE] Fresh venv, `uv pip install datensee==0.1.0a2` on the LXC: `datensee --version` OK; `datensee jar download` → "No prebuilt pipeline JAR for v0.1.0a2" although the GitHub Release v0.1.0a2 carries datensee-pipeline.jar (162,891,242 B).
- [BLOCKER-FOR-PUBLIC] `jar download` fails anonymously because github.com/michaelfdewitt/datensee is PRIVATE (direct `releases/download/` URL → 404). With `GITHUB_TOKEN` it works: release JAR datensee-pipeline-0.1.0a2.jar downloaded, class-file major 65 (Java 21), no FanOutTiles (built before b461b1f, as expected). Until the repo is public, `pip install datensee` users can only use local mode with a token or `--jar`; Dataflow mode is unaffected (public template bucket).
- [PASS] `TestTwoTierExportRetryMergeEndToEnd::test_export_validate_retry_merge_carryover` with `--extra validation` on the LXC (real pipeline JAR with FanOutTiles): PASSED in 14.6 s. Integration suite total: 22/22.
- [REVIEW] Ran /code-review (8 angles) on the PR diff and applied: `gcs_client(credentials)` with explicit `project=None` only (threading `gee_project` into the storage client was semantically wrong — it's the EE project — and inert); `split_gcs_uri`; `FAILURES_FILENAME` single-sourced in units.py; retry `_stage` reuses `_upload_to_gcs`; `failure_summary` paginates, keeps the most detailed message per first-sentence group, and derives the launcher log path from the job's `stagingLocation` option instead of prose; reasons ride on `JobInfo.failure_reasons` (callback consumers get them structurally; Live table renders "Reason" rows) instead of an unconditional console print; `validate gs://` skips staging when the checks would SKIP, wraps staging errors as an ERROR result, guards total size (8 GiB default), downloads in parallel, honours credentials; `validate --config` accepts gs:// and defaults to `OUTPUT/_pipeline-config.json` — local mode now writes that sidecar too; retry gets `--machine-type/--num-workers/--max-workers` and the hint quotes the resolved project; journal reads honour caller credentials; `java -version` preflight (Java 21+) with actionable errors; fan-out moved into `TileFetchTransform.expand()` as `Redistribute.arbitrarily()`; `Enable-Native-Access: ALL-UNNAMED` in the JAR manifest (argv flag dropped); release script uses httpx, bootstraps `<project>_cloudbuild`, checks version vs pyproject, small functions.
- [BUILD] Final JAR: JUnit 82/82, class-file 65, manifest carries Enable-Native-Access, FanOutTiles present in TileFetchTransform.
- [RELEASE] Bumped cli/pyproject.toml to 0.1.0a3 — 0.1.0a2 on PyPI predates the fixes.
- [PASS] LXC, fresh `uv sync` at 0.1.0a3: ruff clean, 373 passed / 23 skipped. (On the dev Mac the same suite shows 3 `test_release_pins` failures purely because the venv's editable metadata still says 0.1.0a2 — no pip/uv there to refresh it.)
- [VERIFY] Post-review checks on the LXC: local demo 10 s, zero JDK native-access warnings, `_pipeline-config.json` sidecar written, tile md5 unchanged; `validate OUTPUT` with no `--config` PASS for both the local dir and the gs:// prefix (3.4 s); `status` renders Reason rows for the stocked-out job.
- [RELEASE] `scripts/release_template_cloudbuild.py 0.1.0a3` → Cloud Build a3c08ea3 SUCCESS; spec gs://datensee-templates/v0.1.0a3/datensee.json. Dataflow run #6 on it died instantly: launcher VM `ZONE_RESOURCE_POOL_EXHAUSTED` in us-central1-c (GCE capacity; the launcher VM type is Dataflow's, not ours). Run #7 relaunched.
- [PASS] DATAFLOW on the v0.1.0a3 template (final JAR, FanOutTiles inside TileFetchTransform): run #7 also lost the launcher VM to a us-central1 stockout; run #8 with `--region-gcp us-east1` (buckets/image stay in us-central1) → job 2026-09-01_09_27_03-1509872268027461962 DONE in 6.3 min, tiles_written=9, 400 vCPU-s, all 9 tiles byte-identical to the local run, `validate gs://…` (default config) PASS.
- [CI] ci.yml ran on PR #2: python 16 s, java 2m35s — both green.

## Final state
- Branch `refactor/pixel-vector-seam` @ 1087ef9 (+ this log), PR #2 open against master, CI green.
- Verified: 373 pytest, 82 JUnit, EE integration 22/22, local + Dataflow e2e on both runners with identical bytes, validate/status/retry surfaces exercised on a pip-only host.
- Release: `v0.1.0a3` template staged; tagging `v0.1.0a3` publishes the wheel that works on pip-only hosts. Repo is private → `jar download` needs a token until it's public.

## Phase 5 — public-but-unlisted distribution

- [DESIGN] Repo stays private for now; the well-lit path must not depend on repo visibility. `datensee jar download` keeps the GitHub Release as the primary host and falls back to the public template bucket: `gs://datensee-templates/v<version>/datensee-pipeline.jar` + `.sha256` sidecar, verified after download. Versioned prefix → a new release never touches an old path; existing installs never re-fetch (versioned cache filename); a mutated bucket object fails the checksum and is refused. Staged by the template-release step (which already writes to that bucket with ADC) — no new CI credentials.
- [STAGED] v0.1.0a3 JAR + sidecar uploaded (sha256 b8cf89f7dd66…).
- [PASS] Fallback e2e on the LXC (no GITHUB_TOKEN, empty cache): `datensee jar download` → "GitHub Release unavailable; trying the public bucket fallback." → sha256 verified → cached as datensee-pipeline-0.1.0a3.jar, byte-identical to the built JAR; `datensee demo` runs on it (9 tiles, 14 s). Sidecar + JAR confirmed anonymously fetchable over plain HTTPS.
