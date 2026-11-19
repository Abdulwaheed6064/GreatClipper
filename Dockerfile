FROM python:3.12.8-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    CLIPPER_DATA_DIR=/data CLIPPER_CONFIG=/app/config.yaml HF_HOME=/data/hf-cache

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY clipper ./clipper
RUN useradd --system --uid 10001 --home /app clipper && mkdir -p /data && chown clipper /data
USER clipper

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "clipper"]
CMD ["web"]
