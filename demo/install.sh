#!/usr/bin/env bash
# Run as root on the dedicated Ubuntu/Debian demo host.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=/opt/ansiballz-demo
if ! command -v docker >/dev/null; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io
fi
systemctl start docker
install -d "$root/releases"
for release in v1 v2 broken; do
    install -d "$root/releases/$release"
    install -m 0644 "$source_dir/releases/$release/image" "$root/releases/$release/image"
    install -m 0644 "$source_dir/releases/$release/podinfo.env" "$root/releases/$release/podinfo.env"
done
install -m 0755 "$source_dir/run.sh" "$root/run.sh"
install -m 0644 "$source_dir/nginx.conf" "$root/nginx.conf"
install -m 0644 "$source_dir/systemd/ansiballz-demo.service" /etc/systemd/system/ansiballz-demo.service
install -m 0644 "$source_dir/systemd/ansiballz-demo-web.service" /etc/systemd/system/ansiballz-demo-web.service
images=("$(cat "$source_dir/releases/v1/image")" "$(cat "$source_dir/releases/v2/image")" 'nginx:1.28.0-alpine@sha256:30f1c0d78e0ad60901648be663a710bdadf19e4c10ac6782c235200619158284')
for image in "${images[@]}"; do
    docker pull "$image"
    docker image inspect "$image" --format '{{index .RepoDigests 0}}'
done
# Preserve the active release on a subsequent installation.
if [[ ! -e "$root/current" && ! -L "$root/current" ]]; then
    ln -s releases/v1 "$root/current"
fi
systemctl daemon-reload
systemctl enable ansiballz-demo.service ansiballz-demo-web.service
systemctl restart ansiballz-demo.service ansiballz-demo-web.service
echo 'Installed. Open http://HOST (public port 80), or tunnel to 127.0.0.1:8080; see docs/run.md.'
