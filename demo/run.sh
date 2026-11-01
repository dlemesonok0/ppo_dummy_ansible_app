#!/usr/bin/env bash
set -euo pipefail
root=/opt/ansiballz-demo
release=$(readlink -f "$root/current")
if [[ ${release%/*} != "$root/releases" || ! -d $release ]]; then
    echo "Invalid current release: $release" >&2
    exit 1
fi
IFS= read -r image < "$release/image"
exec /usr/bin/docker run --rm --pull=never --name ansiballz-demo \
    --network host --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m \
    --cap-drop ALL --security-opt no-new-privileges \
    --memory 128m --cpus 0.5 \
    --env-file "$release/podinfo.env" "$image"
