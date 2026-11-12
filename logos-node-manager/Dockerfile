# The Docker command line and the Compose plugin come from the official CLI image. The tag is pinned like the other
# build tools in this repo, so an engine release cannot change what lands in the image without a bump here. The command
# line talks to the engine of the host through the mounted socket and is compatible with older engines.
FROM docker:28.5.2-cli AS docker-cli

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

WORKDIR /app

# The build context is the root of the repository, because the image also carries the compose files of the commit (see
# below). The .dockerignore of the root lets through exactly what is copied here.
# The package has no runtime dependencies, so installing it is a single layer.
COPY logos-node-manager/pyproject.toml logos-node-manager/README.md ./
COPY logos-node-manager/logos_node_manager ./logos_node_manager
RUN pip install --no-cache-dir .

# The compose files of this commit, for the hosts to take out of the image of a tag: the manager of a host reads
# /compose/core.yaml or /compose/worker.yml from it (see compose_source.py), so that the settings of a deployment always
# match its images. The label says that the image has them. Its value is the version of this layout.
COPY docker-compose.yaml /compose/core.yaml
COPY logos-workernode/docker-compose.yml /compose/worker.yml
LABEL io.logos.compose-bundle="1"

# Marks every image of the manager. The deploy workflows remove the unused ones by this label after an update: once a
# newer image is pulled, an older one has no name left, so the label is the only thing that still identifies it.
LABEL io.logos.component="node-manager"

# Runs as root on purpose: the Docker socket of the host belongs to root, and the manager's only job is to use it.
ENTRYPOINT ["node-manager"]
CMD ["run"]
