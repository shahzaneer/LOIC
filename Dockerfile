# Build stage: compile deps
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml requirements.txt ./
COPY loic/ loic/

RUN pip install --no-cache-dir --user .  \
    && pip install --no-cache-dir --user irc \
    && pip install --no-cache-dir --user scapy

# Runtime stage
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# Kernel tuning prefs (actual tuning must happen on the HOST)
ENV TCP_TW_REUSE=1 \
    TCP_FIN_TIMEOUT=15 \
    LOCAL_PORT_RANGE="1024 65535"

STOPSIGNAL SIGTERM

ENTRYPOINT ["loic"]
CMD ["--help"]
