# X3 pilot sandbox (unit U3 of docs/experiments/X3/implementation-plan.md).
#
# The privilege design (a non-root `dev` user who owns /agent, and a root-only /opt) is ported
# from agent-interp-envs, environments/coding/Dockerfile at commit
# 310c03fbafdbb16df9ee043a7d9dd18a6d8cd3ec (https://github.com/gkroiz/agent-interp-envs).
# MIT License, Copyright (c) 2026 Aditya Singh, Gerson Kroiz. The full license text is in that
# repository's LICENSE file.
#
# Unlike upstream, no harness runs inside the container: Inspect drives it from outside, as
# root by default, and X3 passes user="dev" on every agent command (plan D5). The container
# only needs to stay alive.
#
# Base image: python:3.11-slim (3.11.17-slim-trixie), pinned by its multi-arch index digest
# (linux/amd64 + linux/arm64), resolved 2026-10-04 with
# `docker buildx imagetools inspect python:3.11-slim`.
FROM python:3.11-slim@sha256:bab1b7ef4b450c81002278d035eff85ebe394ae94df904f7a3ba14f7e16e487b

ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir pytest==9.1.1

RUN useradd --create-home --home-dir /home/dev --uid 1000 --shell /bin/bash dev \
    && mkdir -p /agent \
    && chown -R dev:dev /agent \
    && chmod 700 /opt && chmod -R go-rwx /opt

WORKDIR /agent

CMD ["sleep", "infinity"]
