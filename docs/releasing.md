# Releasing DatensEE

A release is three artifacts that must agree on one version:

| Artifact | Consumed by | Produced by |
|---|---|---|
| Flex Template spec `gs://datensee-templates/v<version>/datensee.json` + launcher image in Artifact Registry | `runner="dataflow"` — the wheel pins itself to this URI (`datensee/template.py`) | `scripts/release-template.sh <version>` (manual, needs `gcloud` + Docker) |
| `datensee-pipeline.jar` on the GitHub Release `v<version>` | `runner="local"` — `datensee jar download` / auto-download on first local export | `.github/workflows/publish.yml`, job `jar` |
| Wheel + sdist on PyPI | `pip install datensee` | `.github/workflows/publish.yml`, job `pypi` |

The version is declared **once**, in `cli/pyproject.toml`. `datensee.__version__`,
the template URI, the JAR download URL, and the Gradle `version` all derive from
it (`cli/tests/test_release_pins.py` enforces this).

## One-time setup

1. **Template hosting** (already done for `datensee-testing`; re-runnable):
   `scripts/bootstrap-template-hosting.sh` creates the Artifact Registry repo and
   the `datensee-templates` bucket, both readable by `allUsers`. Public read is
   required: Dataflow reads the spec with the *caller's* credentials and pulls
   the launcher image with the *caller's project's* worker service account.
2. **PyPI Trusted Publisher**: on pypi.org → *Your projects* → *Publishing*, add a
   pending publisher for project `datensee`, owner `michaelfdewitt`, repository
   `datensee`, workflow `publish.yml`, environment `pypi`. No API token is ever
   created; the workflow authenticates with a GitHub OIDC token.
3. **GitHub environment** `pypi` (Settings → Environments). Optional: require a
   reviewer so a tag push doesn't publish unattended.

## Cutting a release

```bash
# 1. bump — pyproject.toml is the only file that changes
cd cli && uv version 0.1.0a2 && uv lock && cd ..
git commit -am "chore: release 0.1.0a2"

# 2. stage the Flex Template BEFORE the wheel exists on PyPI
scripts/release-template.sh 0.1.0a2
#    (no Docker/gcloud at hand? scripts/release_template_cloudbuild.py 0.1.0a2
#     does the same via Cloud Build with just ADC — ~60 s)
#    (Python-only release, Java unchanged? copying the previous spec is enough:
#     gcloud storage cp gs://datensee-templates/v0.1.0a1/datensee.json \
#                       gs://datensee-templates/v0.1.0a2/datensee.json)

# 3. tag → CI builds, tests, attaches the JAR, publishes to PyPI
git tag v0.1.0a2 && git push origin master v0.1.0a2
```

PyPI versions are immutable — every test cut needs a new pre-release number
(`a2`, `a3`, …). Pre-releases are invisible to a plain `pip install datensee`;
testers pin the exact version: `pip install datensee==0.1.0a2` (an exact
pre-release pin needs no `--pre`). **Avoid `pip install --pre datensee`** —
`--pre` applies to every dependency in the resolve, not just `datensee`, and
pulls in things like `httpx 1.0.devN`. (`uv pip install` is stricter than
pip and needs `--prerelease=allow` even for an exact pre-release pin; the
`httpx<1` cap in `pyproject.toml` keeps that from dragging in httpx 1.0.)

## Smoke test

```bash
python -m venv /tmp/dt && /tmp/dt/bin/pip install datensee==0.1.0a2
/tmp/dt/bin/datensee --version
/tmp/dt/bin/datensee jar download            # exercises the release asset
/tmp/dt/bin/datensee demo --project <gcp-project> --output /tmp/dt-demo
```

## Private repository caveat

While the GitHub repo is private, the unauthenticated release-asset URL returns
404. Local mode then needs `GITHUB_TOKEN` (or `DATENSEE_GITHUB_TOKEN`) with
read access to releases; `datensee.jar` resolves a pre-signed URL through the
API and never forwards the token to the download host. Cloud mode is
unaffected — it only needs the public template bucket.
