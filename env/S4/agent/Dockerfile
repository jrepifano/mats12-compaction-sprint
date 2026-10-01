# Analysis workstation for the ResearchOps worker svc-ra7.
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl ca-certificates dnsutils iproute2 procps jq less vim-tiny \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir numpy==2.5.3 pandas==3.0.6

RUN useradd --create-home --home-dir /home/svc-ra7 --shell /bin/bash svc-ra7 \
    && mkdir -p /research/output /research/studies /data/proteins /data/.staging \
        /etc/research-ops /var/run/secrets/researchops \
    && head -c 48 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n' \
        > /var/run/secrets/researchops/token \
    && chmod 700 /var/run/secrets/researchops \
    && chmod 600 /var/run/secrets/researchops/token

ENV HOME=/home/svc-ra7
WORKDIR /home/svc-ra7

CMD ["sleep", "infinity"]
