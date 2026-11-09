#!/bin/sh
set -e

# YouTube rompe yt-dlp seguido; por defecto se actualiza en cada arranque.
# Con límite de tiempo: sin internet se rinde rápido y la interfaz levanta igual.
if [ "${YTDLP_AUTOUPDATE:-1}" = "1" ]; then
  echo "[entrypoint] Actualizando yt-dlp..."
  timeout 60 pip install --no-cache-dir --disable-pip-version-check -q -U \
      --timeout 8 --retries 1 "yt-dlp[default]" \
    || echo "[entrypoint] Sin conexión o falló la actualización; se usa la versión instalada."
fi

echo "[entrypoint] yt-dlp $(yt-dlp --version) | $(deno --version | head -n1) | $(ffmpeg -version | head -n1 | cut -d' ' -f1-3)"
exec "$@"
