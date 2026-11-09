"""
Descargador web de video y audio (yt-dlp + Flask).
Pensado para correr en contenedor con UN solo worker de gunicorn
(el estado de las descargas vive en memoria + job.json por descarga).
Los textos están en i18n.py (español e inglés); aquí solo viajan claves.
"""
import hmac
import json
import os
import re
import shutil
import threading
import time
import uuid
import warnings
import zipfile
from pathlib import Path
from urllib.parse import quote

import mimetypes

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_file
from PIL import Image, ImageOps, UnidentifiedImageError
import yt_dlp
from yt_dlp.utils import DownloadCancelled
from yt_dlp.version import __version__ as YTDLP_VERSION

from i18n import DEFAULT_LANG, LANGS, TEXTS, client_texts


# ---------------------------------------------------------------- config ---
def env_bool(name, default):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "si", "sí", "on")


DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))
COOKIES_FILE = Path(os.environ.get("COOKIES_FILE", "/config/cookies.txt"))
MAX_CONCURRENT = max(1, int(os.environ.get("MAX_CONCURRENT", "2")))
RETENTION_HOURS = float(os.environ.get("RETENTION_HOURS", "24"))
ALLOW_PLAYLISTS = env_bool("ALLOW_PLAYLISTS", True)
# 0 (default): nada se conserva — se borra al bajarlo y al reiniciar el contenedor.
# 1: se guardan las descargas y la lista sobrevive reinicios (hasta RETENTION_HOURS).
KEEP_FILES = env_bool("KEEP_FILES", False)
APP_USER = os.environ.get("APP_USER", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Fondo de la portada: la imagen que suba el usuario vive aquí (volumen aparte)
FONDO_DIR = Path(os.environ.get("FONDO_DIR", "/data"))
# Fondo predeterminado: static/bg.jpg si existe (tu imagen local, no se sube al repo);
# si no, el póster original static/bg-default.jpg que viene con el proyecto.
_STATIC = Path(__file__).resolve().parent / "static"
FONDO_DEFAULT = _STATIC / "bg.jpg" if (_STATIC / "bg.jpg").is_file() else _STATIC / "bg-default.jpg"
FONDO_FIELD_DEFAULT = "#ba3232"   # rojo de la imagen predeterminada
MAX_UPLOAD_MB = max(1, int(os.environ.get("MAX_UPLOAD_MB", "20")))
FONDO_DIR.mkdir(parents=True, exist_ok=True)

VIDEO_PROFILES = {          # etiquetas y explicaciones: i18n.py
    "mkv": {"merge": "mkv"},
    "mp4": {"merge": "mp4"},
    "edit": {"merge": "mp4"},
}

AUDIO_PROFILES = {
    "mp3_320": {"codec": "mp3", "quality": "320"},
    "mp3_v0": {"codec": "mp3", "quality": "0"},
    "original": {"codec": "best", "quality": None},
    "m4a": {"codec": "m4a", "quality": "256"},
}

RESOLUTIONS = ["max", "2160", "1440", "1080", "720", "480"]

SCOPES = ("single", "playlist", "channel")
CHANNEL_TABS = ("videos", "shorts", "streams", "all")
CHANNEL_RE = re.compile(
    r"^(https?://(?:www\.|m\.)?youtube\.com/(?:@[^/?#]+|channel/[\w-]+|c/[^/?#]+|user/[^/?#]+))"
    r"(?:/[^?#]*)?(?:[?#].*)?$", re.I)
MAX_LIMIT = 10000

IGNORED_SUFFIXES = {".part", ".ytdl", ".jpg", ".jpeg", ".png", ".webp", ".json", ".txt", ".tmp"}
# "stage", "error_key" y "error_hint" son claves; la página las traduce al idioma de cada quien
PUBLIC_FIELDS = ("id", "url", "mode", "profile", "resolution", "playlist",
                 "scope", "tab", "limit", "status", "stage", "percent", "speed", "eta",
                 "downloaded", "total", "item", "current", "title", "uploader", "thumbnail",
                 "failed", "files", "error", "error_key", "error_hint", "log", "created", "finished")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024 + 64 * 1024
jobs = {}                       # id -> dict
jobs_lock = threading.Lock()
slots = threading.Semaphore(MAX_CONCURRENT)


# --------------------------------------------------------------- helpers ---
def update(job, **fields):
    with jobs_lock:
        job.update(fields)


def public(job):
    with jobs_lock:
        return {k: job.get(k) for k in PUBLIC_FIELDS}


def job_dir(job_id):
    return DOWNLOAD_DIR / job_id


def save_meta(job):
    try:
        (job_dir(job["id"]) / "job.json").write_text(
            json.dumps(public(job), ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def clean_error(msg):
    """Mensaje de yt-dlp (en inglés) + una pista traducible para los errores más comunes."""
    msg = ANSI_RE.sub("", str(msg)).strip()
    msg = re.sub(r"^ERROR:\s*", "", msg)
    low = msg.lower()
    hint = None
    if "sign in to confirm" in low or "not a bot" in low:
        hint = "bot"
    elif "requested format is not available" in low:
        hint = "format"
    return msg[:800], hint


def get_lang():
    """Idioma de quien hace la petición: cookie "lang" o, si no hay, el del navegador."""
    lang = request.cookies.get("lang")
    if lang in LANGS:
        return lang
    return request.accept_languages.best_match(LANGS) or DEFAULT_LANG


def tr(key, **kw):
    return TEXTS[get_lang()]["api"][key].format(**kw)


def api_error(key, status=400, **kw):
    return jsonify(error=tr(key, **kw)), status


class JobLogger:
    """Recibe los mensajes de yt-dlp y guarda los últimos en el job."""

    def __init__(self, job):
        self.job = job

    def _add(self, msg):
        msg = ANSI_RE.sub("", str(msg)).strip()
        if not msg:
            return
        with jobs_lock:
            log = self.job.setdefault("log", [])
            log.append(msg)
            del log[:-40]

    def debug(self, msg):
        if not msg.startswith("[debug] "):
            self._add(msg)

    def info(self, msg):
        self._add(msg)

    def warning(self, msg):
        self._add("WARNING: " + msg)

    def error(self, msg):
        self._add(msg)
        with jobs_lock:
            self.job["failed"] = (self.job.get("failed") or 0) + 1


def video_format(profile, res):
    h = "" if res == "max" else f"[height<={int(res)}]"
    if profile == "edit":
        return (f"bv*[vcodec^=avc1]{h}+ba[acodec^=mp4a]/"
                f"b[vcodec^=avc1][acodec^=mp4a]{h}/"
                f"bv*{h}+ba/b{h}/bv*+ba/b")
    return f"bv*{h}+ba/b{h}/bv*+ba/b"


def build_opts(job, jdir):
    def progress_hook(d):
        if job.get("_cancel"):
            raise DownloadCancelled("Cancelled by the user")
        info = d.get("info_dict") or {}
        idx = info.get("playlist_index")
        total_items = info.get("n_entries") or info.get("playlist_count")
        vcodec, acodec = info.get("vcodec"), info.get("acodec")
        if vcodec == "none":
            kind = "audio"
        elif acodec == "none":
            kind = "video"
        else:
            kind = "av"
        fields = {"item": (f"{idx}/{total_items}" if total_items else str(idx)) if idx else None}
        if info.get("thumbnail"):
            fields["thumbnail"] = info["thumbnail"]
        if info.get("uploader") or info.get("channel"):
            fields["uploader"] = info.get("uploader") or info.get("channel")
        if job["playlist"]:
            fields["current"] = info.get("title")
            if not job.get("title") and (info.get("playlist_title") or info.get("playlist")):
                fields["title"] = info.get("playlist_title") or info.get("playlist")
        elif info.get("title"):
            fields["title"] = info["title"]
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            fields.update(
                stage=f"dl:{kind}",
                percent=round(done * 100 / total, 1) if total else None,
                speed=d.get("speed"), eta=d.get("eta"),
                downloaded=done, total=total,
            )
        elif d["status"] == "finished":
            fields.update(stage="downloaded", percent=100.0,
                          speed=None, eta=None)
        update(job, **fields)

    def pp_hook(d):
        if d.get("status") == "started":
            name = d.get("postprocessor", "")
            update(job, stage=f"pp:{name}", speed=None, eta=None)

    def post_hook(filename):
        with jobs_lock:
            job["_paths"].append(filename)

    opts = {
        "outtmpl": str(jdir / "%(title).180B.%(ext)s"),
        "windowsfilenames": True,          # nombres válidos al bajarlos a Windows
        "noplaylist": not job["playlist"],
        "ignoreerrors": "only_download" if job["playlist"] else False,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        "quiet": True,
        "noprogress": True,
        "logger": JobLogger(job),
        "playlistend": job.get("limit") or None,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
        "post_hooks": [post_hook],
    }

    pps = []
    if job["mode"] == "audio":
        prof = AUDIO_PROFILES[job["profile"]]
        opts["format"] = "ba[ext=m4a]/ba/b" if job["profile"] == "m4a" else "ba/b"
        extract = {"key": "FFmpegExtractAudio", "preferredcodec": prof["codec"]}
        if prof["quality"] is not None:
            extract["preferredquality"] = prof["quality"]
        opts["writethumbnail"] = True
        pps += [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
            extract,
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail", "already_have_thumbnail": False},
        ]
    else:
        prof = VIDEO_PROFILES[job["profile"]]
        opts["format"] = video_format(job["profile"], job["resolution"])
        opts["merge_output_format"] = prof["merge"]
        pps += [
            # si el sitio entrega un solo archivo ya combinado, igual lo deja en mkv/mp4
            {"key": "FFmpegVideoRemuxer", "preferedformat": prof["merge"]},
            {"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True},
        ]
    opts["postprocessors"] = pps

    # yt-dlp reescribe el cookiefile al terminar; usamos una copia por descarga
    if COOKIES_FILE.is_file():
        tmp = jdir / ".cookies.txt"
        shutil.copyfile(COOKIES_FILE, tmp)
        opts["cookiefile"] = str(tmp)
    return opts


def collect_files(job, jdir):
    paths = []
    for p in job["_paths"] + [str(x) for x in sorted(jdir.iterdir())]:
        p = Path(p)
        if (p.is_file() and p.parent == jdir and not p.name.startswith(".")
                and p.suffix.lower() not in IGNORED_SUFFIXES and p not in paths):
            paths.append(p)
    return [{"name": p.name, "size": p.stat().st_size} for p in paths]


def remove_partials(jdir):
    for p in jdir.iterdir():
        if p.suffix in (".part", ".ytdl") or ".part-Frag" in p.name:
            p.unlink(missing_ok=True)


# ------------------------------------------------------------------ jobs ---
def run_job(job):
    jdir = job_dir(job["id"])
    jdir.mkdir(parents=True, exist_ok=True)
    save_meta(job)
    with slots:
        if job.get("_cancel"):
            update(job, status="cancelled", stage="cancelled", finished=time.time())
            save_meta(job)
            return
        update(job, status="running", stage="fetching")
        try:
            with yt_dlp.YoutubeDL(build_opts(job, jdir)) as ydl:
                info = ydl.extract_info(job["url"], download=True)
            if info and info.get("title"):
                update(job, title=info["title"])
            if info and job["playlist"]:
                update(job, uploader=info.get("uploader") or info.get("channel") or job.get("uploader"),
                       current=None)
            files = collect_files(job, jdir)
            if not files:
                last = next((m for m in reversed(job.get("log") or []) if "ERROR" in m), None)
                if not last:
                    update(job, error_key="nofile")
                raise RuntimeError(last or "No file was produced.")
            update(job, status="done", stage="done", percent=100.0, speed=None, eta=None,
                   files=files, finished=time.time())
        except Exception as exc:  # noqa: BLE001
            if job.get("_cancel") or isinstance(exc, DownloadCancelled):
                remove_partials(jdir)
                update(job, status="cancelled", stage="cancelled", speed=None, eta=None,
                       files=collect_files(job, jdir), finished=time.time())
            else:
                msg, hint = clean_error(exc)
                update(job, status="error", stage="error", speed=None, eta=None,
                       error=msg, error_hint=hint, files=collect_files(job, jdir),
                       finished=time.time())
        finally:
            (jdir / ".cookies.txt").unlink(missing_ok=True)
            save_meta(job)


def delete_job(job_id):
    with jobs_lock:
        jobs.pop(job_id, None)
    shutil.rmtree(job_dir(job_id), ignore_errors=True)


def load_existing_jobs():
    """Recupera las descargas que ya estaban en el volumen tras un reinicio."""
    for d in DOWNLOAD_DIR.iterdir():
        meta = d / "job.json"
        if not (d.is_dir() and meta.is_file()):
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("status") in ("queued", "running"):
            data.update(status="error", stage="error", error=None, error_key="interrupted")
        data["_paths"] = []
        data["files"] = collect_files(data, d)
        jobs[data["id"]] = data


def janitor():
    while True:
        time.sleep(600)
        if RETENTION_HOURS <= 0:
            continue
        cutoff = time.time() - RETENTION_HOURS * 3600
        with jobs_lock:
            old = [j["id"] for j in jobs.values()
                   if j["status"] not in ("queued", "running")
                   and (j.get("finished") or j["created"]) < cutoff]
        for job_id in old:
            delete_job(job_id)


def wipe_downloads():
    """Borra lo que haya quedado de ejecuciones anteriores (solo carpetas de descargas)."""
    for d in DOWNLOAD_DIR.iterdir():
        if d.is_dir() and (d / "job.json").is_file():
            shutil.rmtree(d, ignore_errors=True)


if KEEP_FILES:
    load_existing_jobs()
else:
    wipe_downloads()
threading.Thread(target=janitor, daemon=True).start()


# ----------------------------------------------------------------- web ----
@app.before_request
def basic_auth():
    if not APP_PASSWORD or request.path == "/healthz":
        return None
    auth = request.authorization
    if (auth and hmac.compare_digest(auth.username or "", APP_USER)
            and hmac.compare_digest(auth.password or "", APP_PASSWORD)):
        return None
    return Response(tr("auth"), 401, {"WWW-Authenticate": 'Basic realm="ytdl-web"'})


@app.get("/")
def index():
    # ?lang=es|en cambia el idioma y lo recuerda en una cookie (funciona aunque no haya JS)
    pedido = request.args.get("lang")
    if pedido in LANGS:
        resp = redirect("./")
        resp.set_cookie("lang", pedido, max_age=365 * 24 * 3600, samesite="Lax")
        return resp
    lang = get_lang()
    return render_template(
        "index.html", lang=lang, langs=LANGS, t=TEXTS[lang], client=client_texts(lang),
        allow_playlists=ALLOW_PLAYLISTS, ytdlp_version=YTDLP_VERSION,
        channel_tabs=CHANNEL_TABS, limits=(5, 10, 25, 50, 100),
        fondo=fondo_info(), max_upload_mb=MAX_UPLOAD_MB,
    )


@app.get("/healthz")
def healthz():
    return {"ok": True, "yt_dlp": YTDLP_VERSION}


@app.post("/api/jobs")
def create_job():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    mode = data.get("mode", "video")
    profile = data.get("profile", "")
    resolution = str(data.get("resolution", "max"))
    scope = data.get("scope", "single")
    tab = data.get("tab", "videos")
    try:
        limit = max(0, min(MAX_LIMIT, int(data.get("limit") or 0)))
    except (TypeError, ValueError):
        limit = 0

    if not re.match(r"^https?://\S+$", url) or len(url) > 2000:
        return api_error("invalid_url")
    if scope not in SCOPES:
        return api_error("bad_scope")
    if scope != "single" and not ALLOW_PLAYLISTS:
        return api_error("playlists_off")
    if scope == "channel":
        m = CHANNEL_RE.match(url)
        if not m:
            return api_error("not_channel")
        if tab not in CHANNEL_TABS:
            return api_error("bad_tab")
        url = m.group(1) + ("" if tab == "all" else "/" + tab)
    else:
        tab = None
    if scope == "single":
        limit = 0
    playlist = scope != "single"
    if mode == "video":
        if profile not in VIDEO_PROFILES or resolution not in RESOLUTIONS:
            return api_error("bad_video")
    elif mode == "audio":
        if profile not in AUDIO_PROFILES:
            return api_error("bad_audio")
    else:
        return api_error("bad_mode")

    job = {
        "id": uuid.uuid4().hex[:12], "url": url, "mode": mode, "profile": profile,
        "resolution": resolution, "playlist": playlist,
        "scope": scope, "tab": tab, "limit": limit,
        "status": "queued", "stage": "queued", "percent": None, "speed": None,
        "eta": None, "downloaded": None, "total": None, "item": None, "current": None,
        "title": None, "uploader": None, "thumbnail": None, "failed": 0,
        "files": [], "error": None, "error_key": None, "error_hint": None,
        "log": [], "created": time.time(), "finished": None,
        "_paths": [], "_cancel": False,
    }
    with jobs_lock:
        jobs[job["id"]] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return jsonify(public(job)), 201


@app.get("/api/jobs")
def list_jobs():
    with jobs_lock:
        items = sorted(jobs.values(), key=lambda j: j["created"], reverse=True)
    return jsonify([public(j) for j in items])


def get_job_or_404(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        abort(404)
    return job


@app.get("/api/jobs/<job_id>")
def get_job(job_id):
    return jsonify(public(get_job_or_404(job_id)))


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id):
    job = get_job_or_404(job_id)
    update(job, _cancel=True, stage="cancelling")
    return jsonify(public(job))


@app.delete("/api/jobs/<job_id>")
def remove_job(job_id):
    job = get_job_or_404(job_id)
    if job["status"] in ("queued", "running"):
        return api_error("cancel_first", 409)
    delete_job(job_id)
    return "", 204


def job_file_path(job, name):
    jdir = job_dir(job["id"]).resolve()
    path = (jdir / name).resolve()
    if path.parent != jdir or not path.is_file():
        abort(404)
    return path


def attachment(name):
    fallback = re.sub(r'[^\x20-\x7e]|["\\]', "_", name) or "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"


def delivered(job):
    """El navegador recibió todo: se borra del servidor (salvo KEEP_FILES=1)."""
    if not KEEP_FILES and job.get("status") == "done":
        delete_job(job["id"])


@app.get("/api/jobs/<job_id>/files/<int:idx>")
def download_file(job_id, idx):
    job = get_job_or_404(job_id)
    files = job.get("files") or []
    if idx < 0 or idx >= len(files):
        abort(404)
    path = job_file_path(job, files[idx]["name"])
    size = path.stat().st_size
    src = open(path, "rb")  # se abre ya: si borran la carpeta a medias, el archivo sigue legible
    whole_job = len(files) == 1

    def generate():
        try:
            while chunk := src.read(1 << 20):
                yield chunk
        finally:
            src.close()
        # solo se llega aquí si se envió completo; si el cliente corta, no se borra nada
        if whole_job:
            delivered(job)

    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return Response(generate(), mimetype=mime, direct_passthrough=True,
                    headers={"Content-Disposition": attachment(path.name),
                             "Content-Length": str(size)})


class _ZipStream:
    """Archivo de solo escritura que va acumulando bytes para el streaming."""

    def __init__(self):
        self.buf = bytearray()

    def write(self, b):
        self.buf += b
        return len(b)

    def flush(self):
        pass

    def pop(self):
        data = bytes(self.buf)
        self.buf.clear()
        return data


@app.get("/api/jobs/<job_id>/zip")
def download_zip(job_id):
    job = get_job_or_404(job_id)
    paths = [job_file_path(job, f["name"]) for f in job.get("files") or []]
    if not paths:
        abort(404)

    entries = [(zipfile.ZipInfo.from_file(p, arcname=p.name), open(p, "rb")) for p in paths]

    def generate():
        stream = _ZipStream()
        try:
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                for zinfo, src in entries:
                    with zf.open(zinfo, "w", force_zip64=True) as dest:
                        while chunk := src.read(1 << 20):
                            dest.write(chunk)
                            if stream.buf:
                                yield stream.pop()
                    yield stream.pop()
            yield stream.pop()
        finally:
            for _, src in entries:
                src.close()
        delivered(job)

    base = re.sub(r'[\\/:*?"<>|]+', "_", job.get("title") or job["id"])[:120].strip() or job["id"]
    return Response(generate(), mimetype="application/zip", direct_passthrough=True,
                    headers={"Content-Disposition": attachment(base + ".zip")})


# ----------------------------------------------------------------- fondo ---
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
PAPER_RGB = (244, 237, 225)
fondo_lock = threading.Lock()
Image.MAX_IMAGE_PIXELS = 60_000_000   # más que eso se rechaza (evita imágenes "bomba")


def _luminancia(rgb):
    def canal(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (canal(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contraste(a, b):
    la, lb = sorted((_luminancia(a), _luminancia(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def color_de_orilla(im):
    """Color de la orilla izquierda de la imagen (donde se funde con el anuncio).
    Se oscurece lo necesario para que el texto crema se siga leyendo encima."""
    w, h = im.size
    tira = im.crop((0, 0, max(1, w // 16), h)).convert("RGBA")
    tira.thumbnail((64, 512))
    pix = [p for p in tira.getdata() if p[3] > 200]
    if len(pix) < 0.3 * tira.width * tira.height:   # orilla transparente (recorte PNG)
        return None
    rgb = [sorted(p[i] for p in pix)[len(pix) // 2] for i in range(3)]
    for _ in range(40):
        if _contraste(PAPER_RGB, rgb) >= 4.5:
            break
        rgb = [c * 0.92 for c in rgb]
    return "#%02x%02x%02x" % tuple(round(c) for c in rgb)


def _fondo_meta():
    try:
        return json.loads((FONDO_DIR / "fondo.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _guardar_meta(meta):
    tmp = FONDO_DIR / "fondo.json.tmp"
    tmp.write_text(json.dumps(meta), encoding="utf-8")
    tmp.replace(FONDO_DIR / "fondo.json")


def fondo_info():
    meta = _fondo_meta()
    archivo = meta.get("file")
    custom = bool(archivo) and (FONDO_DIR / archivo).is_file()
    if custom:
        auto, version = meta.get("auto_field"), meta.get("v", 0)
    else:
        auto = FONDO_FIELD_DEFAULT
        version = int(FONDO_DEFAULT.stat().st_mtime) if FONDO_DEFAULT.is_file() else 0
    override = meta.get("field") if HEX_RE.match(str(meta.get("field") or "")) else None
    field = override or auto or FONDO_FIELD_DEFAULT
    return {
        "custom": custom,
        "path": FONDO_DIR / archivo if custom else FONDO_DEFAULT,
        "url": f"fondo?v={version}",
        "field": field.lower(),
        "auto_field": (auto or FONDO_FIELD_DEFAULT).lower(),
        "override": bool(override),
    }


def fondo_publico():
    info = fondo_info()
    info.pop("path")
    return info


def procesar_imagen(stream):
    """Valida que sea una imagen de verdad y la re-codifica a WebP (sin metadatos)."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            src = Image.open(stream)
            src.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("too_many_px")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise ValueError("invalid_image")
    if src.format not in ("JPEG", "PNG", "WEBP", "GIF", "MPO", "BMP"):
        raise ValueError("unsupported")
    im = ImageOps.exif_transpose(src)
    alfa = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
    im = im.convert("RGBA" if alfa else "RGB")
    if min(im.size) < 200:
        raise ValueError("too_small")
    im.thumbnail((2600, 2600), Image.LANCZOS)
    return im, color_de_orilla(im)


@app.errorhandler(413)
def demasiado_grande(_e):
    return api_error("too_big", 413, mb=MAX_UPLOAD_MB)


@app.get("/fondo")
def fondo():
    info = fondo_info()
    if not info["path"].is_file():
        abort(404)
    resp = send_file(info["path"], conditional=True)
    # la URL lleva ?v=…, así que el navegador puede guardarla; cambia cuando cambia la imagen
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if request.args.get("v") else "no-cache"
    return resp


@app.get("/api/fondo")
def fondo_estado():
    return jsonify(fondo_publico())


@app.post("/api/fondo")
def fondo_subir():
    archivo = request.files.get("imagen")
    if not archivo or not archivo.filename:
        return api_error("no_image")
    try:
        im, auto = procesar_imagen(archivo.stream)
    except ValueError as exc:      # el mensaje es una clave de i18n.py
        return api_error(str(exc))
    version = int(time.time() * 1000)
    nombre = f"fondo-{version}.webp"
    with fondo_lock:
        im.save(FONDO_DIR / nombre, "WEBP", quality=88, method=4)
        meta = _fondo_meta()
        anterior = meta.get("file")
        meta.update(file=nombre, auto_field=auto or FONDO_FIELD_DEFAULT, v=version)
        meta.pop("field", None)          # imagen nueva: vuelve al color automático
        _guardar_meta(meta)
        if anterior and anterior != nombre:
            (FONDO_DIR / anterior).unlink(missing_ok=True)
    return jsonify(fondo_publico()), 201


@app.put("/api/fondo/color")
def fondo_color():
    data = request.get_json(silent=True) or {}
    color = data.get("field")
    if color is not None and not HEX_RE.match(str(color)):
        return api_error("bad_color")
    with fondo_lock:
        meta = _fondo_meta()
        if color:
            meta["field"] = color.lower()
        else:
            meta.pop("field", None)
        _guardar_meta(meta)
    return jsonify(fondo_publico())


@app.delete("/api/fondo")
def fondo_restablecer():
    with fondo_lock:
        meta = _fondo_meta()
        if meta.get("file"):
            (FONDO_DIR / meta["file"]).unlink(missing_ok=True)
        (FONDO_DIR / "fondo.json").unlink(missing_ok=True)
    return jsonify(fondo_publico())
