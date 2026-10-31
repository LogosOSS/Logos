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

# The package has no runtime dependencies, so installing it is a single layer.
COPY pyproject.toml README.md ./
COPY logos_node_manager ./logos_node_manager
RUN pip install --no-cache-dir .

# Marks every image of the manager. The deploy workflows remove the unused ones by this label after an update: once a
# newer image is pulled, an older one has no name left, so the label is the only thing that still identifies it.
LABEL io.logos.component="node-manager"

# Runs as root on purpose: the Docker socket of the host belongs to root, and the manager's only job is to use it.
ENTRYPOINT ["node-manager"]
CMD ["run"]
