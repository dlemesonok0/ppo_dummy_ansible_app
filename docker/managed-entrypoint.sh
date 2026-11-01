#!/bin/bash
set -euo pipefail
install -d -m 0700 -o ansible -g ansible /home/ansible/.ssh
install -m 0600 -o ansible -g ansible /lab-keys/id_ed25519.pub /home/ansible/.ssh/authorized_keys
ssh-keygen -A
exec /sbin/init
