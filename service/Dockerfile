# Cloud Run image for datensee-service.
#
# Contains Python 3.12 + OpenJDK 25 + the datensee fat-jar baked at a
# stable path. The service itself is a thin FastAPI app wrapping
# `datensee.api.export()`; the JAR is needed because datensee spawns
# the Beam driver via subprocess to submit Dataflow jobs.
#
# Build from the datensee repo root so the build context can see both
# `service/` and the sibling `cli/` path-dep:
#     docker build -f service/Dockerfile -t datensee-service .

FROM eclipse-temurin:25-jdk AS jdk

FROM python:3.12-slim AS runtime

# Pull the JDK in from the first stage — keeps the base small but avoids
# apt installing a second openjdk.
COPY --from=jdk /opt/java/openjdk /opt/java/openjdk
ENV JAVA_HOME=/opt/java/openjdk
ENV PATH="$JAVA_HOME/bin:$PATH"

# uv for fast installs; we use it at build time only.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy the datensee CLI (path dep) first so Docker caches its install layer.
COPY cli /app/cli
COPY service/pyproject.toml /app/service/pyproject.toml
COPY service/src /app/service/src

# Install service + datensee into a venv at /app/.venv. `--no-editable` so
# the installed copies live inside the image and don't require the source
# trees to stay mounted at runtime.
RUN cd /app/service && uv sync --no-editable

# Bake the pipeline JAR at a stable path and tell datensee where to find it.
# The JAR is built outside Docker and passed in via build arg.
ARG PIPELINE_JAR=pipelines/build/libs/datensee-pipeline.jar
COPY ${PIPELINE_JAR} /opt/datensee/datensee-pipeline.jar
ENV DATENSEE_JAR=/opt/datensee/datensee-pipeline.jar

ENV PATH="/app/service/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# Cloud Run injects PORT; default to 8080 for local runs.
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn datensee_service.app:app --host 0.0.0.0 --port ${PORT}"]
