#!/usr/bin/env bash
set -euo pipefail
/usr/bin/docker exec ansiballz-bg-web nginx -t -c /state/nginx.conf
exec /usr/bin/docker exec ansiballz-bg-web nginx -s reload -c /state/nginx.conf
