FROM debian:13-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv bash git ca-certificates curl iproute2 \
    build-essential nodejs npm procps util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 -s /bin/bash developer \
    && mkdir -p /workspace /opt/microvm \
    && chown developer:developer /workspace
COPY supervisor.py /opt/microvm/supervisor.py
COPY init.sh /sbin/microvm-init
RUN chmod 0755 /sbin/microvm-init
