#!/usr/bin/env bash
# Checks the node manager against a real Docker engine, without a GPU.
#
# It starts a local registry, builds a dummy worker in four flavors (v1, v2, v3 and a broken one), runs the real manager
# image next to a dummy worker in a throw-away compose project, and asks the manager for deployments. After each one it
# counts the dummy worker images on the host, to see that none is left that no longer has a purpose. Two more checks
# use a second registry that wants a password: the manager must log in with the login of the .env file, and must take
# the compose file out of the image of a tag. The last three checks are about the manager itself: it replaces itself
# with the image of another tag, it goes back to its own image if the new one does not come up, and it does the
# replacement on its own after a deployment that went well. Everything it creates is named nm-smoke... and is removed
# at the end, also the data volumes of the two registries. The base images that it downloads (registry:2, httpd:2) stay
# in the image cache of Docker. Set KEEP=1 to keep it for a look. SMOKE_COLLECT=<directory> copies the records of the
# manager there before everything is deleted.
#
# Needs a running Docker engine with the Compose plugin, and network access for the base images.
#   logos-node-manager/tests/smoke/run-smoke.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPONENT="$(cd "$HERE/../.." && pwd)"
# The image of the manager is built from the root of the repository, because it also carries the compose files.
REPO_ROOT="$(cd "$COMPONENT/.." && pwd)"

PORT="${SMOKE_REGISTRY_PORT:-5001}"
REGISTRY="localhost:${PORT}"
# A second registry that wants a login, for the check of the login from the .env file.
AUTH_PORT="${SMOKE_AUTH_REGISTRY_PORT:-5002}"
AUTH_REGISTRY="localhost:${AUTH_PORT}"
WORKER=nm-smoke-worker
MANAGER=nm-smoke-manager
# The container that does the swap of a self-update. The manager gives it this name.
HELPER=nm-self-update
REGISTRY_CONTAINER=nm-smoke-registry
AUTH_REGISTRY_CONTAINER=nm-smoke-registry-auth
MANAGER_IMAGE=nm-smoke-manager:local

# The project directory is mounted into the manager at the same path, so the Docker engine must be able to share it, and
# it should not contain spaces. The path of this repository does.
WORK="${SMOKE_DIR:-/tmp/nm-smoke}"
case "$(basename "$WORK")" in
  nm-smoke*) ;;
  *) echo "error: SMOKE_DIR must name a directory that starts with nm-smoke, because it is deleted at the end" >&2; exit 2 ;;
esac

failures=0
log() { printf '\n== %s\n' "$*"; }
pass() { printf '   ok: %s\n' "$*"; }
fail() { printf '   FAILED: %s\n' "$*"; failures=$((failures + 1)); }
die() { printf 'error: %s\n' "$*" >&2; exit 2; }

expect_equal() { # description, expected, actual
  if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (expected '$2', got '$3')"; fi
}

# True if the text on stdin matches. It reads all of the input on purpose: grep -q stops at the first match, the program
# that still writes into the pipe then dies of a broken pipe, and pipefail turns that into a failed check.
has() { grep "$@" >/dev/null; }

nm() { docker exec "$MANAGER" node-manager "$@"; }
worker_image() { docker inspect --format '{{.Config.Image}}' "$WORKER" 2>/dev/null; }
worker_healthy() { [ "$(docker inspect --format '{{.State.Health.Status}}' "$WORKER" 2>/dev/null)" = healthy ]; }
phase_is() { nm status "$1" --json 2>/dev/null | has "\"phase\": \"$2\""; }
image_exists() { docker image inspect "$1" >/dev/null 2>&1; }

# The dummy worker images carry the label nm-smoke, so they can be counted whether they have a name or not. An image
# with several names is listed once per name.
smoke_images() { docker images -q --filter label=nm-smoke | sort -u | wc -l | tr -d ' '; }
# The manager removes old images right after it has saved the result, so the result can arrive a moment before.
clean_up_finished() { docker logs "$MANAGER" 2>&1 | has "Deployment $1: clean-up finished"; }
expect_images() { # deployment id, number of dummy worker images on the host, description
  if wait_for 60 clean_up_finished "$1"; then
    expect_equal "$3" "$2" "$(smoke_images)"
  else
    fail "$3 (the manager did not finish the clean-up of $1)"
  fi
}

wait_for() { # seconds, command...
  local limit=$1
  shift
  for _ in $(seq 1 "$limit"); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

remove_leftovers() {
  # -v also removes the anonymous volume of a registry container, which holds the images that were pushed to it.
  docker rm -fv "$HELPER" "$WORKER" "$MANAGER" "$REGISTRY_CONTAINER" "$AUTH_REGISTRY_CONTAINER" >/dev/null 2>&1
  docker rmi -f "$MANAGER_IMAGE" "$REGISTRY/dummy-worker:v1" "$REGISTRY/dummy-worker:v2" "$REGISTRY/dummy-worker:v3" \
    "$REGISTRY/dummy-worker:broken" "$REGISTRY/dummy-worker:previous" "$REGISTRY/logos-node-manager:v1" \
    "$REGISTRY/logos-node-manager:v2" "$REGISTRY/logos-node-manager:v3" "$REGISTRY/logos-node-manager:broken" \
    "$REGISTRY/logos-node-manager:previous" \
    "$AUTH_REGISTRY/dummy-worker:v4" "$AUTH_REGISTRY/dummy-worker:previous" >/dev/null 2>&1
  docker logout "$AUTH_REGISTRY" >/dev/null 2>&1
  # Images that lost their name, which a run that found a leak leaves behind.
  docker images -q --filter label=nm-smoke | sort -u | xargs docker rmi -f >/dev/null 2>&1
  docker network rm nm-smoke_default >/dev/null 2>&1
}

cleanup() {
  if [ "${KEEP:-0}" = "1" ]; then
    echo "KEEP=1: leaving $WORK and the nm-smoke containers in place"
    return
  fi
  remove_leftovers
  rm -rf "$WORK"
}
trap cleanup EXIT

docker info >/dev/null 2>&1 || die "no Docker engine is reachable"
docker compose version >/dev/null 2>&1 || die "the Docker Compose plugin is missing"

remove_leftovers
rm -rf "$WORK"
mkdir -p "$WORK/node-manager"
# Resolve symbolic links (/tmp is one on macOS), so that the path is the same on the host and in the manager.
WORK="$(cd "$WORK" && pwd -P)"

log "Starting a local registry on $REGISTRY"
docker run -d --name "$REGISTRY_CONTAINER" -p "127.0.0.1:${PORT}:5000" registry:2 >/dev/null ||
  die "could not start the registry (is port $PORT free?)"
wait_for 30 curl -fsS "http://$REGISTRY/v2/" || die "the registry does not answer"

log "Building and pushing the dummy worker (v1, v2, v3, broken)"
build_worker() { # tag, mode
  docker build -q --build-arg "VERSION=$1" --build-arg "MODE=$2" -t "$REGISTRY/dummy-worker:$1" \
    "$HERE/dummy-worker" >/dev/null || die "the build of $1 failed"
  docker push -q "$REGISTRY/dummy-worker:$1" >/dev/null || die "the push of $1 failed"
}
build_worker v1 good
build_worker v2 good
build_worker v3 good
build_worker broken broken
# Remove the local copies, so that the deployments really download these images.
docker rmi "$REGISTRY/dummy-worker:v2" "$REGISTRY/dummy-worker:v3" "$REGISTRY/dummy-worker:broken" >/dev/null

log "Building the node manager image"
docker build -q -f "$COMPONENT/Dockerfile" -t "$MANAGER_IMAGE" "$REPO_ROOT" >/dev/null ||
  die "the build of the manager image failed"
expect_equal "the manager image carries the label of the deploy workflows" "node-manager" \
  "$(docker image inspect --format '{{ index .Config.Labels "io.logos.component" }}' "$MANAGER_IMAGE")"
expect_equal "the manager image says that it carries the compose files" "1" \
  "$(docker image inspect --format '{{ index .Config.Labels "io.logos.compose-bundle" }}' "$MANAGER_IMAGE")"
expect_equal "and it carries the files of the core, of the core deployer and of the worker" \
  "core.yaml deployer.yml worker.yml" \
  "$(docker run --rm --entrypoint ls "$MANAGER_IMAGE" /compose | tr '\n' ' ' | sed 's/ $//')"

# The service of the manager names its image by tag, as the compose file of a host does.
docker tag "$MANAGER_IMAGE" "$REGISTRY/logos-node-manager:v1"
cp "$HERE/compose.yml" "$WORK/docker-compose.yml"
# The login directory that the manager reads, with one made-up registry login. The local registry needs none.
mkdir -p "$WORK/.docker"
echo '{"auths":{"registry.invalid":{"auth":"c21va2U6c21va2U="}}}' >"$WORK/.docker/config.json"
cat >"$WORK/.env" <<EOF
SMOKE_DIR=$WORK
SMOKE_REGISTRY=$REGISTRY
IMAGE_TAG=v1
COMPOSE_PROFILES=node-manager
EOF
compose() { docker compose -p nm-smoke -f "$WORK/docker-compose.yml" --env-file "$WORK/.env" "$@"; }

log "Starting the dummy worker (v1) and the manager"
compose up -d worker node-manager || die "docker compose up failed"
wait_for 60 worker_healthy || die "the dummy worker did not become healthy"
wait_for 60 nm ping || die "the manager did not start"
expect_equal "the worker starts on v1" "$REGISTRY/dummy-worker:v1" "$(worker_image)"
if docker logs "$MANAGER" 2>&1 | has "Docker login file: 1 registry login(s)"; then
  pass "the manager reads the registry login from the worker directory"
else
  fail "the manager does not see the registry login in the worker directory"
fi

log "1. A healthy release replaces the container"
nm deploy --id t1 --tag v2 --wait --timeout 240
expect_equal "the deployment exits with 0" 0 "$?"
expect_equal "the worker runs v2" "$REGISTRY/dummy-worker:v2" "$(worker_image)"
if docker image inspect "$REGISTRY/dummy-worker:previous" >/dev/null 2>&1; then
  pass "the previous version is kept under its own name"
else
  fail "the previous version is not kept under its own name"
fi
expect_images t1 2 "the host keeps the running image and the previous one"

log "2. A broken release is rolled back"
nm deploy --id t2 --tag broken --wait --timeout 240
expect_equal "the deployment exits with 10 (rolled back)" 10 "$?"
expect_equal "the worker runs v2 again" "$REGISTRY/dummy-worker:v2" "$(worker_image)"
if nm status t2 --json | has "broken build"; then
  pass "the log of the broken container is saved in the record"
else
  fail "the log of the broken container is not in the record"
fi
expect_images t2 1 "only the running image is left on the host"
if image_exists "$REGISTRY/dummy-worker:broken"; then
  fail "the image of the failed release is still on the host"
else
  pass "the image of the failed release is removed"
fi
if image_exists "$REGISTRY/dummy-worker:v1"; then
  fail "the image from two versions back is still on the host"
else
  pass "the image from two versions back is removed"
fi

log "3. A tag that does not exist leaves the container alone"
nm deploy --id t3 --tag nope --wait --timeout 240
expect_equal "the deployment exits with 11 (failed)" 11 "$?"
expect_equal "the worker still runs v2" "$REGISTRY/dummy-worker:v2" "$(worker_image)"
if nm status t3 --json | has '"failure": "pull_failed"'; then
  pass "the failure is reported as pull_failed"
else
  fail "the failure is not reported as pull_failed"
fi
expect_images t3 1 "the host is left as it was"

log "4. A request for the image that runs already changes nothing"
nm deploy --id t4 --tag v2 --wait --timeout 240
expect_equal "the deployment exits with 0" 0 "$?"
if nm status t4 --json | has '"outcome": "unchanged"'; then
  pass "the outcome is unchanged"
else
  fail "the outcome is not unchanged"
fi
expect_images t4 1 "the host is left as it was"

log "5. The manager restarts in the middle of a deployment"
nm deploy --id t5 --tag v1 >/dev/null
if wait_for 90 phase_is t5 verifying; then
  docker restart "$MANAGER" >/dev/null
  wait_for 60 nm ping || die "the manager did not come back"
  nm wait t5 --timeout 240
  expect_equal "the deployment exits with 0 after the restart" 0 "$?"
  expect_equal "the worker runs v1" "$REGISTRY/dummy-worker:v1" "$(worker_image)"
  # The log of a container survives a restart, so it shows that the manager took the deployment up again where the
  # record said it was, and did not just happen to finish it.
  if docker logs "$MANAGER" 2>&1 | has "Processing deployment t5 (tag v1, phase verifying)"; then
    pass "the manager continued the deployment from the recorded phase"
  else
    fail "the manager did not continue the deployment from the recorded phase"
  fi
  expect_images t5 2 "the host keeps the running image and the previous one"
else
  fail "the deployment never reached the verification phase"
fi

log "6. A later success removes the image from two versions back"
# The name previous points to v2 now, and v1 runs. The new release moves the name to v1, and v2 is two versions old.
nm deploy --id t6 --tag v3 --wait --timeout 240
expect_equal "the deployment exits with 0" 0 "$?"
expect_equal "the worker runs v3" "$REGISTRY/dummy-worker:v3" "$(worker_image)"
expect_images t6 2 "the host keeps the running image and the previous one"
if image_exists "$REGISTRY/dummy-worker:v2"; then
  fail "the image from two versions back is still on the host"
else
  pass "the image from two versions back is removed"
fi

log "7. The compose file comes from the image of the tag"
# A stand-in for the image of a tag: the manager image plus the compose file of the dummy worker. The file has one more
# entry, a bind mount with a relative path. It only works if a relative path in the file taken out of the image still
# means the project directory of the host, and not the directory that the file was copied to.
echo "marker-from-the-project-directory" >"$WORK/marker"
carrier="$WORK/carrier"
mkdir -p "$carrier"
awk '{print} /^    container_name: nm-smoke-worker$/ {print "    volumes:"; print "      - ./marker:/marker:ro"}' \
  "$HERE/compose.yml" >"$carrier/worker.yml"
cat >"$carrier/Dockerfile" <<EOF
FROM $MANAGER_IMAGE
COPY worker.yml /compose/worker.yml
LABEL io.logos.compose-bundle="1"
EOF
docker build -q -t "$REGISTRY/logos-node-manager:v2" "$carrier" >/dev/null ||
  die "the build of the stand-in image failed"
docker push -q "$REGISTRY/logos-node-manager:v2" >/dev/null || die "the push of the stand-in image failed"
# Remove the local copy, so that the deployment really downloads the image.
docker rmi "$REGISTRY/logos-node-manager:v2" >/dev/null
cat >>"$WORK/.env" <<EOF
REGISTRY=$REGISTRY
NODE_MANAGER_COMPOSE_FROM_TAG=true
EOF
nm deploy --id t7 --tag v2 --wait --timeout 240
expect_equal "the deployment exits with 0" 0 "$?"
expect_equal "the worker runs v2" "$REGISTRY/dummy-worker:v2" "$(worker_image)"
if nm status t7 | has "^compose file: $WORK/node-manager/compose/t7/worker.yml"; then
  pass "the deployment used the compose file of the tag"
else
  fail "the status does not name the compose file of the tag"
fi
expect_equal "a relative path in that file still means the project directory" "marker-from-the-project-directory" \
  "$(docker exec "$WORKER" cat /marker 2>/dev/null)"
# The tag v1 has no such image. That is no error: the compose file of the host is used.
nm deploy --id t7b --tag v1 --wait --timeout 240
expect_equal "a tag without such an image still deploys (exit 0)" 0 "$?"
expect_equal "the worker runs v1" "$REGISTRY/dummy-worker:v1" "$(worker_image)"
if nm status t7b --json | has "carries no compose file"; then
  pass "the record says that the compose file of the host was used"
else
  fail "the record does not say that the compose file of the host was used"
fi
if docker exec "$WORKER" test -e /marker 2>/dev/null; then
  fail "the worker still has the entry of the compose file of the tag"
else
  pass "the compose file of the host ran: the entry of the other file is gone"
fi

log "8. The manager logs in with the login of the .env file"
mkdir -p "$WORK/auth"
docker run --rm --entrypoint htpasswd httpd:2 -Bbn smoke smoke-password >"$WORK/auth/htpasswd" ||
  die "could not make the password file"
docker run -d --name "$AUTH_REGISTRY_CONTAINER" -p "127.0.0.1:${AUTH_PORT}:5000" -v "$WORK/auth:/auth:ro" \
  -e REGISTRY_AUTH=htpasswd -e REGISTRY_AUTH_HTPASSWD_REALM=nm-smoke -e REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd \
  registry:2 >/dev/null || die "could not start the registry that wants a login (is port $AUTH_PORT free?)"
wait_for 30 curl -s -o /dev/null "http://$AUTH_REGISTRY/v2/" || die "the registry that wants a login does not answer"
# Push a release to it with the login of this machine, then take that login away again. The manager has to bring
# its own.
echo "smoke-password" | docker login "$AUTH_REGISTRY" -u smoke --password-stdin >/dev/null ||
  die "could not log in to the registry that wants a login"
docker build -q --build-arg "VERSION=v4" --build-arg "MODE=good" -t "$AUTH_REGISTRY/dummy-worker:v4" \
  "$HERE/dummy-worker" >/dev/null || die "the build of v4 failed"
docker push -q "$AUTH_REGISTRY/dummy-worker:v4" >/dev/null || die "the push of v4 failed"
docker rmi "$AUTH_REGISTRY/dummy-worker:v4" >/dev/null
docker logout "$AUTH_REGISTRY" >/dev/null
write_env() { # password
  cat >"$WORK/.env" <<EOF
SMOKE_DIR=$WORK
SMOKE_REGISTRY=$AUTH_REGISTRY
IMAGE_TAG=v1
COMPOSE_PROFILES=node-manager
REGISTRY=$AUTH_REGISTRY
REGISTRY_USER=smoke
REGISTRY_PASSWORD=$1
EOF
}
write_env "a-wrong-password"
before_login="$(worker_image)"
nm deploy --id t8 --tag v4 --wait --timeout 240
expect_equal "a wrong password fails the deployment (exit 11)" 11 "$?"
if nm status t8 --json | has "the login to the registry $AUTH_REGISTRY failed"; then
  pass "the record says that the login failed"
else
  fail "the record does not say that the login failed"
fi
expect_equal "the worker was not touched" "$before_login" "$(worker_image)"
write_env "smoke-password"
nm deploy --id t8b --tag v4 --wait --timeout 240
expect_equal "the right password lets the deployment pull and finish (exit 0)" 0 "$?"
expect_equal "the worker runs v4" "$AUTH_REGISTRY/dummy-worker:v4" "$(worker_image)"
if docker logs "$MANAGER" 2>&1 | has "smoke-password\|a-wrong-password"; then
  fail "a password is in the log of the manager"
else
  pass "no password is in the log of the manager"
fi

log "9. The manager replaces itself with the image of a tag"
# The .env file names neither the tag nor the profile of the manager. The update has to find its service without them.
write_plain_env() { # extra lines for the .env file...
  {
    echo "SMOKE_DIR=$WORK"
    echo "SMOKE_REGISTRY=$REGISTRY"
    echo "IMAGE_TAG=v1"
    echo "REGISTRY=$REGISTRY"
    for line in "$@"; do echo "$line"; done
  } >"$WORK/.env"
}
manager_image() { docker inspect --format '{{.Config.Image}}' "$MANAGER" 2>/dev/null; }
manager_image_id() { docker inspect --format '{{.Image}}' "$MANAGER" 2>/dev/null; }
manager_healthy() { [ "$(docker inspect --format '{{.State.Health.Status}}' "$MANAGER" 2>/dev/null)" = healthy ]; }
worker_container_id() { docker inspect --format '{{.Id}}' "$WORKER" 2>/dev/null; }
# The manager restarts during an update, so a command in its container fails for a moment. wait_for tries again.
update_done() { nm self-update-status "$1" --json 2>/dev/null | has '"phase": "done"'; }
deployment_done() { nm status "$1" --json 2>/dev/null | has '"phase": "done"'; }
helper_gone() { ! docker inspect "$HELPER" >/dev/null 2>&1; }
previous_gone() { ! image_exists "$REGISTRY/logos-node-manager:previous"; }

write_plain_env
old_manager_id="$(manager_image_id)"
worker_before="$(worker_container_id)"
# The stand-in image of tag v2 (scenario 7) is a different image from the one that runs.
nm self-update --tag v2 --id s1 >/dev/null
expect_equal "the request is queued (exit 0)" 0 "$?"
if wait_for 240 update_done s1; then
  if nm self-update-status s1 --json | has '"outcome": "succeeded"'; then
    pass "the update ended as succeeded"
  else
    fail "the update did not end as succeeded"
  fi
  expect_equal "the manager runs the image of the tag" "$REGISTRY/logos-node-manager:v2" "$(manager_image)"
  if [ "$(manager_image_id)" != "$old_manager_id" ]; then
    pass "the manager runs another image than before"
  else
    fail "the manager still runs the same image"
  fi
  if wait_for 60 manager_healthy; then pass "the new manager is healthy"; else fail "the new manager is not healthy"; fi
  expect_equal "the worker was not touched" "$worker_before" "$(worker_container_id)"
  if wait_for 60 helper_gone; then pass "the helper is removed"; else fail "the helper is still there"; fi
  if wait_for 60 previous_gone; then
    pass "the name for the old image is removed"
  else
    fail "the name for the old image is still on the host"
  fi
else
  fail "the update did not finish"
fi

log "10. A manager that does not come up is replaced by the one that ran before"
broken="$WORK/broken-manager"
mkdir -p "$broken"
cat >"$broken/Dockerfile" <<EOF
FROM $MANAGER_IMAGE
ENTRYPOINT ["sh", "-c", "echo broken manager; exit 1"]
EOF
docker build -q -t "$REGISTRY/logos-node-manager:broken" "$broken" >/dev/null ||
  die "the build of the broken manager failed"
docker push -q "$REGISTRY/logos-node-manager:broken" >/dev/null || die "the push of the broken manager failed"
docker rmi "$REGISTRY/logos-node-manager:broken" >/dev/null
kept_manager_id="$(manager_image_id)"
worker_before="$(worker_container_id)"
nm self-update --tag broken --id s2 >/dev/null
expect_equal "the request is queued (exit 0)" 0 "$?"
if wait_for 300 update_done s2; then
  if nm self-update-status s2 --json | has '"outcome": "rolled_back"'; then
    pass "the update ended as rolled back"
  else
    fail "the update did not end as rolled back"
  fi
  if nm self-update-status s2 --json | has '"failure": "verify_failed"'; then
    pass "the failure is reported as verify_failed"
  else
    fail "the failure is not reported as verify_failed"
  fi
  expect_equal "the manager runs the image of the tag that ran before" "$REGISTRY/logos-node-manager:v2" \
    "$(manager_image)"
  expect_equal "and it is the same image as before" "$kept_manager_id" "$(manager_image_id)"
  if wait_for 60 manager_healthy; then pass "the manager is healthy again"; else fail "the manager is not healthy"; fi
  expect_equal "the worker was not touched" "$worker_before" "$(worker_container_id)"
  if wait_for 60 helper_gone; then pass "the helper is removed"; else fail "the helper is still there"; fi
else
  fail "the update did not finish"
fi

log "11. A deployment that went well is followed by an update of the manager"
# A second stand-in image, so that tag v3 names another manager than the one that runs. It carries the compose file of
# the dummy worker, which holds the service of the manager too. The compose file comes from the tag, for the deployment
# and for the update that follows it.
standin="$WORK/standin-v3"
mkdir -p "$standin"
cp "$HERE/compose.yml" "$standin/worker.yml"
cat >"$standin/Dockerfile" <<EOF
FROM $MANAGER_IMAGE
COPY worker.yml /compose/worker.yml
LABEL io.logos.compose-bundle="1"
LABEL nm-smoke-stand-in="v3"
EOF
docker build -q -t "$REGISTRY/logos-node-manager:v3" "$standin" >/dev/null ||
  die "the build of the second stand-in image failed"
docker push -q "$REGISTRY/logos-node-manager:v3" >/dev/null || die "the push of the second stand-in image failed"
docker rmi "$REGISTRY/logos-node-manager:v3" >/dev/null
write_plain_env "NODE_MANAGER_COMPOSE_FROM_TAG=true" "NODE_MANAGER_SELF_UPDATE=true"
# No --wait: the manager restarts soon after the deployment, and a command in its container would end with it.
nm deploy --id t9 --tag v3 >/dev/null
expect_equal "the deployment is queued (exit 0)" 0 "$?"
if wait_for 300 deployment_done t9 && wait_for 300 update_done t9; then
  if nm status t9 --json | has '"outcome": "succeeded"'; then
    pass "the deployment ended as succeeded"
  else
    fail "the deployment did not end as succeeded"
  fi
  expect_equal "the worker runs v3" "$REGISTRY/dummy-worker:v3" "$(worker_image)"
  if nm self-update-status t9 --json | has '"outcome": "succeeded"'; then
    pass "the update that followed it ended as succeeded"
  else
    fail "the update that followed it did not end as succeeded"
  fi
  expect_equal "the manager runs the image of the tag" "$REGISTRY/logos-node-manager:v3" "$(manager_image)"
  if wait_for 60 manager_healthy; then pass "the new manager is healthy"; else fail "the new manager is not healthy"; fi
  if nm status t9 | has "^compose file: $WORK/node-manager/compose/t9/worker.yml"; then
    pass "the deployment used the compose file of the tag"
  else
    fail "the deployment did not use the compose file of the tag"
  fi
  if nm self-update-status t9 | has "^compose file: $WORK/node-manager/compose/self-t9/worker.yml"; then
    pass "the update used the compose file of the tag"
  else
    fail "the update did not use the compose file of the tag"
  fi
  # Compose notes the file that it made a container from. The new manager was made from the file of the tag.
  expect_equal "the new manager was made from that file" "$WORK/node-manager/compose/self-t9/worker.yml" \
    "$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$MANAGER")"
else
  fail "the deployment or the update did not finish"
fi

# The records of the manager are in the project directory, which is deleted at the end. SMOKE_COLLECT=<directory> keeps
# a copy of them, for a look at what a deployment or an update wrote.
if [ -n "${SMOKE_COLLECT:-}" ]; then
  mkdir -p "$SMOKE_COLLECT"
  cp -R "$WORK/node-manager/deployments" "$WORK/node-manager/self-update" "$SMOKE_COLLECT/" 2>/dev/null
  echo "The records are copied to $SMOKE_COLLECT"
fi

if [ "$failures" -eq 0 ]; then
  log "All checks passed"
else
  log "$failures check(s) failed"
  echo "--- end of the manager log ---"
  docker logs --tail 80 "$MANAGER" 2>&1
  echo "--- the latest self-update ---"
  nm self-update-status 2>&1
fi
[ "$failures" -eq 0 ]
