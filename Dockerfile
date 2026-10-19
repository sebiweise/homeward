# Homeward outside DSM: the daemon, optionally with the web page (docs/docker.md).
#   docker build -t homeward .
# The version comes from the git tag, so build from a checkout with tags, or pass
# --build-arg HOMEWARD_VERSION=x.y.z when .git is not available.

# The full image brings git, which hatch-vcs needs to read the version from the tag.
FROM python:3.12 AS build
WORKDIR /src
COPY . .
ARG HOMEWARD_VERSION=
RUN if [ -n "${HOMEWARD_VERSION}" ]; then export SETUPTOOLS_SCM_PRETEND_VERSION="${HOMEWARD_VERSION}"; fi \
    && pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:3.12-slim
# The same pinned dependencies (with hashes) the .spk bundles.
COPY requirements/icloud.txt /tmp/icloud.txt
RUN pip install --no-cache-dir --only-binary :all: --require-hashes -r /tmp/icloud.txt \
    && rm /tmp/icloud.txt
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-deps --no-index /wheels/homeward-*.whl && rm -rf /wheels
COPY package/ui /app/ui

# HOME points into /config so libraries that write to ~ (cookies, caches) persist.
ENV HOMEWARD_VAR=/config \
    HOMEWARD_CONFIG=/config/config.toml \
    HOME=/config \
    HOMEWARD_UI_DIR=/app/ui \
    HOMEWARD_VOLUMES=/data \
    PYTHONUNBUFFERED=1

# Runs unprivileged; override with `user:` to match the owner of the mounted folders.
RUN groupadd --gid 1000 homeward \
    && useradd --uid 1000 --gid 1000 --home-dir /config --no-create-home homeward \
    && mkdir -p /config /data \
    && chown homeward:homeward /config /data
USER 1000:1000

VOLUME ["/config"]
EXPOSE 8080
ENTRYPOINT ["homeward-container"]
