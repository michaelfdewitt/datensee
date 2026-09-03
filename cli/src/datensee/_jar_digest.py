"""SHA-256 of the pipeline JAR this release ships with, or ``None``.

Stamped by ``.github/workflows/publish.yml`` at release time: the workflow
builds the JAR from the tagged source, attaches it to the GitHub Release,
and writes its digest here *before* building the wheel, so an installed
``datensee`` can verify the exact bytes ``datensee jar download`` fetches
for its own version. In a source checkout this is ``None`` and downloads
are not verified (a checkout uses its own ``pipelines/build/libs`` JAR).

The assignment below is rewritten by regex; keep its exact one-line form.
"""

JAR_SHA256: str | None = None
