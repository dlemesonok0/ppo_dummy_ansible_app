#!/usr/bin/env bash
# Windows OpenSSH may wait on inherited WSL stdin for commands without input.
# Ansible's piped transfer must retain stdin for the dd payload.
set -eu
for arg in "$@"; do
    case "$arg" in
        *"dd of="*) exec /mnt/c/Windows/System32/OpenSSH/ssh.exe "$@" ;;
    esac
done
exec /mnt/c/Windows/System32/OpenSSH/ssh.exe -n "$@"
