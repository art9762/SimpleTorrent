FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd -g 10001 simpletorrent && useradd -u 10001 -g simpletorrent -M simpletorrent \
    && mkdir -p /data/downloads /data/config && chown -R simpletorrent:simpletorrent /data
WORKDIR /srv

FROM base AS transmission
RUN apt-get update && apt-get install -y --no-install-recommends transmission-daemon \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/transmission.py /srv/transmission.py
USER simpletorrent
EXPOSE 51413/tcp 51413/udp
CMD ["python", "/srv/transmission.py"]

FROM base AS app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
USER simpletorrent
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=8)"
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers", "--no-access-log"]
