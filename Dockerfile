FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI client only (no daemon/containerd, no buildx) for jobs that
# drive a remote Engine API via DOCKER_HOST (e.g. run-sync.sh's
# create/network-connect/start against docker-lxc's socket-proxy). Pinned
# to 28.5.2 because `docker network connect --gw-priority` requires a
# Docker 28.x-era CLI; assert it's actually present so a bad pin fails the
# build instead of failing a job at 2am.
RUN curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-28.5.2.tgz \
        -o /tmp/docker-cli.tgz \
    && tar -xzf /tmp/docker-cli.tgz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker-cli.tgz /tmp/docker \
    && docker network connect --help | grep -q gw-priority

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV DATA_DIR=/data
VOLUME /data

CMD ["python", "-m", "app.main"]
