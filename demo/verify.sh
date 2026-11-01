#!/usr/bin/env bash
# End-to-end verification on the demo host; restore the initial release on exit.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
root=/opt/ansiballz-demo
original=$(readlink -f "$root/current")
case "$original" in
    "$root/releases/v1"|"$root/releases/v2"|"$root/releases/broken") ;;
    *) echo 'Unexpected current release; refusing to switch.' >&2; exit 1 ;;
esac
switch_release() {
    ln -s "$1" "$root/current.verify.$$"
    mv -Tf "$root/current.verify.$$" "$root/current"
    systemctl restart ansiballz-demo
}
restore() {
    switch_release "$original"
    echo "Restored initial release: $(basename "$original")"
}
trap restore EXIT
for release in v1 v2 broken; do
    switch_release "$root/releases/$release"
    expected=200
    [[ $release != broken ]] || expected=503
    matched=false
    for attempt in {1..30}; do
        status=$(curl -s --max-time 2 -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/health || true)
        if [[ $status == "$expected" ]]; then matched=true; break; fi
        sleep 1
    done
    $matched || { echo "$release: expected $expected, got $status" >&2; exit 1; }
    systemctl is-active --quiet ansiballz-demo ansiballz-demo-web
    expected_version=6.15.0
    [[ $release != v1 ]] || expected_version=6.14.1
    curl -fsS http://127.0.0.1:8080/version | python3 -c \
        'import json,sys; assert json.load(sys.stdin)["version"] == sys.argv[1]' "$expected_version"
    curl -fsS http://127.0.0.1:8080/api/info | python3 -c \
        'import json,sys; data=json.load(sys.stdin); assert ("release " + sys.argv[1]) in data["message"]; assert data["color"] == sys.argv[2]' \
        "$release" "$(case $release in v1) echo '#2563eb';; v2) echo '#16a34a';; broken) echo '#dc2626';; esac)"
    html=$(curl -fsS -A 'Mozilla/5.0' http://127.0.0.1:8080/)
    [[ $html == *'<!DOCTYPE html>'* ]] || { echo 'Browser UI is missing' >&2; exit 1; }
    if [[ $release == broken ]]; then
        if curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1; then
            echo 'Broken release unexpectedly passed curl --fail' >&2; exit 1
        fi
    fi
    echo "$release: /health=$status, version=$expected_version, UI OK, systemd active"
done
echo 'All release checks passed.'
