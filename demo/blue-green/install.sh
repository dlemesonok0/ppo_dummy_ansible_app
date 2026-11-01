#!/usr/bin/env bash
# Initial provisioning only; application updates go through safe_release.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=/opt/ansiballz-bg
if [[ -e "$root" ]]; then
    echo "$root already exists; refusing to overwrite deployment state." >&2
    exit 1
fi
if ! command -v docker >/dev/null; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io
fi
systemctl start docker
for release in v1 v2 broken; do
    image=$(cat "$source_dir/../releases/$release/image")
    docker pull "$image"
done
docker pull nginx:1.28.0-alpine@sha256:30f1c0d78e0ad60901648be663a710bdadf19e4c10ac6782c235200619158284
for port in 8081 19901 19902; do
    if ss -H -ltn "sport = :$port" | grep -q .; then
        echo "Port $port is occupied; refusing to provision." >&2
        exit 1
    fi
done
install -d "$root/releases"
for release in v1 v2 broken; do
    install -d "$root/releases/$release"
    install -m 0644 "$source_dir/../releases/$release/image" "$root/releases/$release/image"
    install -m 0644 "$source_dir/../releases/$release/podinfo.env" "$root/releases/$release/podinfo.env"
done
for slot in blue green; do
    install -d "$root/slots/$slot"
    ln -s "$root/releases/v1" "$root/slots/$slot/release"
    port=19901
    [[ $slot != green ]] || port=19902
    printf 'upstream application { server 127.0.0.1:%s; }\nmap "" $deployment_slot { default %s; }\n' "$port" "$slot" > "$root/slots/$slot/upstream.conf"
done
ln -s slots/blue "$root/active"
install -m 0755 "$source_dir/run.sh" "$source_dir/reload.sh" "$root/"
install -m 0644 "$source_dir/nginx.conf" "$root/nginx.conf"
install -m 0644 "$source_dir/ansiballz-bg@.service" "$source_dir/ansiballz-bg-web.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ansiballz-bg@blue.service ansiballz-bg@green.service ansiballz-bg-web.service
curl --fail --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:8081/health
echo 'Blue-green ready: blue/v1, frontend 127.0.0.1:8081.'
