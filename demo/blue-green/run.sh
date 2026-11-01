#!/usr/bin/env bash
set -euo pipefail
root=/opt/ansiballz-bg
slot=${1:?Specify blue or green}
case "$slot" in blue) port=19901;; green) port=19902;; *) exit 2;; esac
release=$(readlink -f "$root/slots/$slot/release")
[[ ${release%/*} == "$root/releases" && -d $release ]] || exit 2
IFS= read -r image < "$release/image"
exec /usr/bin/docker run --rm --pull=never --name "ansiballz-bg-$slot" \
    --network host --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m \
    --cap-drop ALL --security-opt no-new-privileges --memory 128m --cpus 0.5 \
    --env-file "$release/podinfo.env" --env "PODINFO_PORT=$port" \
    --env "PODINFO_UI_MESSAGE=Release ${release##*/} | slot $slot" "$image"
