FROM debian:trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    build-essential ca-certificates debhelper dpkg-dev file lintian python3 \
    systemd binutils xz-utils \
    && rm -rf /var/lib/apt/lists/*

ENV UV_NO_PROGRESS=1 UV_PYTHON_DOWNLOADS=never PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
