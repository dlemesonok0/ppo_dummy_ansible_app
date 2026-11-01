FROM ubuntu:24.04
ENV container=docker
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod +x /usr/sbin/policy-rc.d \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       systemd systemd-sysv openssh-server sudo docker.io python3 curl \
       ca-certificates iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash ansible \
    && passwd -d ansible \
    && printf 'ansible ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/ansible-lab \
    && chmod 440 /etc/sudoers.d/ansible-lab \
    && mkdir -p /etc/docker /etc/ssh/sshd_config.d \
    && printf '{"storage-driver":"vfs","features":{"containerd-snapshotter":false}}\n' > /etc/docker/daemon.json \
    && printf 'PasswordAuthentication no\nPermitEmptyPasswords no\nPermitRootLogin no\n' > /etc/ssh/sshd_config.d/lab.conf \
    && systemctl enable ssh.service docker.service \
    && truncate -s 0 /etc/machine-id
COPY docker/managed-entrypoint.sh /usr/local/sbin/lab-init
RUN sed -i 's/\r$//' /usr/local/sbin/lab-init && chmod 755 /usr/local/sbin/lab-init
STOPSIGNAL SIGRTMIN+3
ENTRYPOINT ["/usr/local/sbin/lab-init"]
