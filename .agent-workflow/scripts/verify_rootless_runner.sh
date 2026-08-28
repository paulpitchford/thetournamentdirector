#!/usr/bin/env bash
set -Eeuo pipefail

uid=$(id -u)
socket_path="/run/user/${uid}/podman/podman.sock"
docker_host="unix://${socket_path}"
image="docker.io/library/alpine@sha256:7c8cb692ae09657cbc4a3f3cbd0e8d5a2690ba38386aaaf252dbb060bf5eb2e6"

test -S "$socket_path"
test "$(podman info --format '{{.Host.Security.Rootless}}')" = "true"
DOCKER_HOST="$docker_host" docker info >/dev/null

result=$(podman run --rm \
  --network none \
  --read-only \
  --cap-drop all \
  --security-opt no-new-privileges \
  --pids-limit 32 \
  --memory 64m \
  --cpus 0.25 \
  --user 65532:65532 \
  --env HOME=/tmp \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=8m \
  "$image" \
  /bin/sh -eu -c '
    test "$(id -u)" = 65532
    test ! -e /workspace
    test ! -e /home/paul
    test ! -e /var/run/docker.sock
    test ! -e /run/podman/podman.sock
    test ! -e /downloads
    printf "isolated-noop-ok\n"
  ')

test "$result" = "isolated-noop-ok"
printf 'Rootless runner verification passed.\n'
