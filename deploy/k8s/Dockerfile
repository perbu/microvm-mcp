# syntax=docker/dockerfile:1
# Container image for running microvm-mcp in Kubernetes. Built from the Debian
# packages produced by scripts/build-debs; see deploy/k8s/build-container.
FROM debian:trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a

# The packages bring iproute2, nftables and iptables. tini forwards SIGTERM to
# the controller only, matching the systemd unit's KillMode=mixed.
RUN --mount=type=bind,from=debs,target=/debs \
    cd /debs && sha256sum --check --strict --ignore-missing SHA256SUMS \
    && apt-get update -qq \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
        tini \
        /debs/microvm-mcp_*_amd64.deb \
        /debs/microvm-mcp-python_*_amd64.deb \
        /debs/microvm-mcp-firecracker-1.17_*_amd64.deb \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=0755 entrypoint.sh /usr/local/sbin/microvm-mcp-entrypoint

ENV PYTHONDONTWRITEBYTECODE=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    MICROVM_CONFIG=/etc/microvm-mcp/service.toml

LABEL org.opencontainers.image.title="microvm-mcp" \
      org.opencontainers.image.licenses="Apache-2.0"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/sbin/microvm-mcp-entrypoint"]
