# Binario de Deno: yt-dlp lo usa para resolver los retos JS de YouTube
FROM denoland/deno:bin AS deno

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    DOWNLOAD_DIR=/downloads \
    FONDO_DIR=/data \
    DENO_DIR=/home/app/.cache/deno

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

COPY --from=deno /deno /usr/local/bin/deno

RUN useradd -m -u 1000 app \
 && mkdir -p /opt/venv /downloads /config /data \
 && chown -R app:app /opt/venv /downloads /config /data

USER app
RUN python -m venv /opt/venv

WORKDIR /app
COPY --chown=app:app requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

COPY --chown=app:app app/ /app/
COPY --chown=app:app entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

EXPOSE 8080
VOLUME ["/downloads", "/data"]

HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
# Un solo worker: el estado de las descargas vive en memoria del proceso
CMD ["gunicorn", "--workers", "1", "--threads", "16", "--bind", "0.0.0.0:8080", \
     "--timeout", "120", "--access-logfile", "-", "app:app"]
