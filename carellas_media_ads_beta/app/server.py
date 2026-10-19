#!/usr/bin/env python3
"""Carellas Media Ads - Home Assistant app."""

from __future__ import annotations

import copy
import csv
import difflib
import io
import base64
import hashlib
import hmac
import html
import json
import math
import mimetypes
import os
import random
import re
import shutil
import secrets
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
try:
    import websocket
except ModuleNotFoundError:  # Il pacchetto viene installato nell'immagine dell'add-on.
    websocket = None
from datetime import datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CARELLAS_DATA_DIR", "/data"))
MEDIA_DIR = Path(os.environ.get("CARELLAS_MEDIA_DIR", "/media/carellas_media_ads"))
IPTV_DIR = DATA_DIR / "iptv"
UPLOAD_DIR = DATA_DIR / "uploads"
CONFIG_FILE = DATA_DIR / "carellas_media_ads.json"
RUNTIME_FILE = DATA_DIR / "carellas_media_ads_runtime.json"
# Fascia che il motore sta seguendo: dopo un riavvio (es. aggiornamento) i Sonos continuano a suonarla
# e non va fatta ripartire dal primo brano.
MUSIC_STATE_FILE = DATA_DIR / "carellas_music_state.json"
SPOT_HISTORY_FILE = DATA_DIR / "carellas_spot_history.json"
SONOS_CATALOG_FILE = DATA_DIR / "carellas_sonos_catalog.json"
DEFAULT_FILE = APP_DIR / "default_config.json"
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
HA_API = os.environ.get("CARELLAS_HA_API", "http://supervisor/core/api")
HA_WS = os.environ.get("CARELLAS_HA_WS", "ws://supervisor/core/websocket")
PORT = 8100
PLAYER_PORT = 8102
REMOTE_PORT = 8104
# Accesso da Internet: uscita automatica dopo un periodo senza attività e comunque dopo 12 ore.
REMOTE_SESSION_IDLE_SECONDS = max(300, int(os.environ.get("CARELLAS_REMOTE_IDLE_MINUTES", "120")) * 60)
REMOTE_SESSION_MAX_SECONDS = 12 * 60 * 60
REMOTE_LOCK_SECONDS = 15 * 60
REMOTE_MAX_FAILURES_IP = 5
REMOTE_MAX_FAILURES_USER = 10
REMOTE_FAIL_DELAY_SECONDS = 1.0
REMOTE_MIN_PASSWORD = 10
SONOS_GROUP_SETTLE_SECONDS = float(os.environ.get("CARELLAS_SONOS_GROUP_SETTLE_SECONDS", "0.6"))
SONOS_RESTORE_SETTLE_SECONDS = float(os.environ.get("CARELLAS_SONOS_RESTORE_SETTLE_SECONDS", "0.6"))
SONOS_RESTORE_RETRIES = max(1, int(os.environ.get("CARELLAS_SONOS_RESTORE_RETRIES", "3")))
SONOS_FADE_STEPS = max(2, int(os.environ.get("CARELLAS_SONOS_FADE_STEPS", "6")))
SONOS_GROUP_CHECK_SECONDS = max(5.0, float(os.environ.get("CARELLAS_SONOS_GROUP_CHECK_SECONDS", "8")))
SONOS_SPOT_TAIL_GUARD_SECONDS = max(
    0.0, min(float(os.environ.get("CARELLAS_SONOS_SPOT_TAIL_GUARD_SECONDS", "0.4")), 2.0)
)
SONOS_QUIET_VOLUME = 0.01
HA_SERVICE_TIMEOUT = max(5, int(os.environ.get("CARELLAS_HA_SERVICE_TIMEOUT", "20")))
SONOS_RESTORE_WAIT_SECONDS = max(0, int(os.environ.get("CARELLAS_SONOS_RESTORE_WAIT_SECONDS", "25")))
# Cambio di sorgente (es. dalla radio a una playlist): Sonos risponde 701 finché chiude la sorgente
# precedente. Si riprova più a lungo e poi si controlla che la musica suoni davvero.
SONOS_SOURCE_RETRIES = max(1, int(os.environ.get("CARELLAS_SONOS_SOURCE_RETRIES", "5")))
# Secondi dopo il comando in cui lo stato letto può essere ancora quello della musica di prima.
SONOS_START_SETTLE_SECONDS = max(0, int(os.environ.get("CARELLAS_SONOS_START_SETTLE_SECONDS", "2")))
# Una playlist lunga può metterci più di 10 secondi a caricarsi sui Sonos prima di suonare.
SONOS_START_CHECK_SECONDS = max(0, int(os.environ.get("CARELLAS_SONOS_START_CHECK_SECONDS", "25")))
MUSIC_RETRY_SECONDS = max(15, int(os.environ.get("CARELLAS_MUSIC_RETRY_SECONDS", "45")))
# Sorgente rifiutata da Sonos (es. UPnP 402 Invalid Args): riprovare spesso non serve e a ogni
# tentativo la musica in corso si abbassa e si rialza. Si riprova molto più di rado.
MUSIC_RETRY_REJECTED_SECONDS = max(60, int(os.environ.get("CARELLAS_MUSIC_RETRY_REJECTED_SECONDS", "900")))
# Durante una fascia i Sonos si fermano senza che nessuno l'abbia chiesto dal pannello: dopo questi secondi
# la musica riparte da sola (fine della playlist, ripristino dopo uno spot non riuscito, intoppo di Sonos).
MUSIC_RESUME_SECONDS = max(10, int(os.environ.get("CARELLAS_MUSIC_RESUME_SECONDS", "20")))
# Ultimo volume scelto per ogni Sonos (cursore del pannello o volume letto prima di uno spot).
# Serve quando Home Assistant non riporta il volume e quando un Sonos rientra nel gruppo:
# ogni cassa torna al suo volume invece di un valore fisso uguale per tutte.
SONOS_VOLUME_MEMORY = {}
SONOS_VOLUME_MEMORY_LOCK = threading.Lock()
MAX_UPLOAD = 1024 * 1024 * 1024 * 4
UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024
MIN_FREE_AFTER_UPLOAD = 1024 * 1024 * 512
ALLOWED = {
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"},
    "image": {".jpg", ".jpeg", ".png", ".webp", ".gif"},
    "video": {".mp4", ".m4v", ".mov", ".webm", ".mkv"},
}
MEDIA_DURATION_CACHE = {}


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe_name(value):
    value = Path(urllib.parse.unquote(value)).name
    value = re.sub(r"[^A-Za-z0-9À-ÿ._ -]+", "_", value).strip(" .")
    return value[:180] or f"media-{uuid.uuid4().hex[:8]}"


def safe_channel_id(value):
    value = re.sub(r"[^a-z0-9-]+", "-", str(value).lower()).strip("-")
    return value[:48] or f"canale-{uuid.uuid4().hex[:6]}"


def probe_media_duration(source):
    """Legge la durata reale del file audio/video e la memorizza finché il file non cambia."""
    source = Path(source)
    try:
        stat = source.stat()
    except FileNotFoundError as error:
        raise RuntimeError(f"File multimediale non trovato: {source.name}") from error
    cache_key = (str(source), stat.st_mtime_ns, stat.st_size)
    if cache_key in MEDIA_DURATION_CACHE:
        return MEDIA_DURATION_CACHE[cache_key]
    try:
        probe = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(source),
        ], check=True, capture_output=True, text=True, timeout=30)
        duration = float(probe.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise RuntimeError(f"Durata non leggibile: {source.name}") from error
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Durata non valida: {source.name}")
    duration = min(duration, 6 * 60 * 60)
    for key in [key for key in MEDIA_DURATION_CACHE if key[0] == str(source)]:
        MEDIA_DURATION_CACHE.pop(key, None)
    MEDIA_DURATION_CACHE[cache_key] = duration
    return duration


def deep_merge(base, override):
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def password_hash(password, iterations=210000):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    )


def password_matches(password, encoded):
    try:
        algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(digest_text + "=" * (-len(digest_text) % 4))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (AttributeError, TypeError, ValueError):
        return False


def with_scheme(url, scheme):
    """Completa un indirizzo scritto senza protocollo: 'tv.esempio.com' -> 'https://tv.esempio.com'."""
    value = str(url or "").strip().rstrip("/")
    if value and "://" not in value:
        value = f"{scheme}://{value}"
    return value


class Store:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        IPTV_DIR.mkdir(parents=True, exist_ok=True)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 24 * 60 * 60
        for item in UPLOAD_DIR.iterdir():
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink(missing_ok=True)
        self.lock = threading.RLock()
        self.logs = []
        defaults = json.loads(DEFAULT_FILE.read_text(encoding="utf-8"))
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            saved = {}
        self.config = deep_merge(defaults, saved)
        self.normalize()
        music = self.config.setdefault("music", {})
        music.setdefault("slots", [])
        self.save()
        self.install_bundled_media()

    def normalize(self):
        """Mantiene la configurazione stabile priva di opzioni beta/obsolete."""
        audio = self.config.setdefault("audio", {})
        audio.pop("driver", None)
        audio.pop("resume_music_after_ad", None)
        audio.pop("spot_duration_seconds", None)
        # Nella stabile ogni intervallo trasmette esattamente un solo spot.
        audio["repeat_count"] = 1
        audio["repeat_gap_seconds"] = 0
        try:
            audio["transition_seconds"] = max(0.0, min(float(audio.get("transition_seconds", 3)), 10.0))
        except (TypeError, ValueError):
            audio["transition_seconds"] = 3
        music = self.config.setdefault("music", {})
        try:
            music["transition_seconds"] = max(0.0, min(float(music.get("transition_seconds", 3)), 10.0))
        except (TypeError, ValueError):
            music["transition_seconds"] = 3
        music.setdefault("mix", {"enabled": False, "block_minutes": 60, "sources": [], "players": [], "volume": 25})
        if self.config.get("language") not in {"it", "de"}:
            self.config["language"] = "it"
        social = self.config.setdefault("social", {})
        social.setdefault("instagram", {"enabled": False, "channel_id": "", "keep": 3, "interval_minutes": 15})
        social.setdefault("website_events", {"enabled": False, "url": "https://carellas.de", "category": "events",
                                             "channel_id": "", "seconds": 15, "keep": 5, "interval_minutes": 60})
        player = self.config.setdefault("browser_player", {})
        player.setdefault("enabled", True)
        # Un dominio scritto senza https:// verrebbe aperto come pagina interna del pannello.
        player["public_url"] = with_scheme(player.get("public_url", ""), "https")
        self.config["media_base_url"] = with_scheme(self.config.get("media_base_url", ""), "http")
        if not player.get("session_secret"):
            player["session_secret"] = secrets.token_urlsafe(48)
        normalized_users = []
        seen = set()
        for raw in player.get("users") or []:
            username = str(raw.get("username", "")).strip().lower()
            if not re.fullmatch(r"[a-z0-9_.-]{3,40}", username):
                raise RuntimeError("Nome utente Player TV non valido: usa 3–40 lettere, numeri, punto, trattino o underscore")
            if username in seen:
                raise RuntimeError(f"Nome utente Player TV duplicato: {username}")
            seen.add(username)
            raw_channel_id = str(raw.get("channel_id", "")).strip()
            if not raw_channel_id:
                raise RuntimeError(f"Seleziona un canale IPTV per l'utente Player TV {username}")
            item = {
                "username": username,
                "channel_id": safe_channel_id(raw_channel_id),
                "fit": raw.get("fit") if raw.get("fit") in {"contain", "cover"} else "contain",
                "enabled": raw.get("enabled", True) is not False,
            }
            password = str(raw.get("password", ""))
            encoded = str(raw.get("password_hash", ""))
            if password:
                if len(password) < 6:
                    raise RuntimeError(f"La password di {username} deve contenere almeno 6 caratteri")
                encoded = password_hash(password)
            if not encoded.startswith("pbkdf2_sha256$"):
                raise RuntimeError(f"Imposta una password per l'utente Player TV {username}")
            item["password_hash"] = encoded
            normalized_users.append(item)
        player["users"] = normalized_users
        remote = self.config.setdefault("remote_admin", {})
        remote["enabled"] = remote.get("enabled") is True
        remote["public_url"] = str(remote.get("public_url", "")).strip().rstrip("/")[:200]
        remote_users, seen_remote = [], set()
        for raw in remote.get("users") or []:
            username = str(raw.get("username", "")).strip().lower()
            if not re.fullmatch(r"[a-z0-9_.-]{3,40}", username):
                raise RuntimeError("Nome utente per l'accesso da Internet non valido: usa 3–40 lettere, numeri, punto, trattino o underscore")
            if username in seen_remote:
                raise RuntimeError(f"Nome utente per l'accesso da Internet duplicato: {username}")
            seen_remote.add(username)
            password = str(raw.get("password", ""))
            encoded = str(raw.get("password_hash", ""))
            if password:
                if len(password) < REMOTE_MIN_PASSWORD:
                    raise RuntimeError(f"La password di {username} per l'accesso da Internet deve contenere almeno {REMOTE_MIN_PASSWORD} caratteri")
                if username in password.lower():
                    raise RuntimeError(f"La password di {username} non deve contenere il nome utente")
                encoded = password_hash(password)
            if not encoded.startswith("pbkdf2_sha256$"):
                raise RuntimeError(f"Imposta una password per l'accesso da Internet di {username}")
            remote_users.append({"username": username, "enabled": raw.get("enabled", True) is not False,
                                 # Operatore: dal tablet gestisce solo musica, casse e pubblicità.
                                 "role": "staff" if raw.get("role") == "staff" else "admin",
                                 "password_hash": encoded})
        remote["users"] = remote_users
        # Zone: informazione dell'interfaccia (luoghi del ristorante). Lo Scheduler non le usa.
        zones = []
        seen_zones = set()
        for raw in self.config.get("zones") or []:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "")).strip()[:60]
            if not name:
                raise RuntimeError("Ogni zona deve avere un nome")
            zone_id = safe_channel_id(str(raw.get("id") or name))
            if zone_id in seen_zones:
                raise RuntimeError(f"Zona duplicata: {name}")
            seen_zones.add(zone_id)
            zones.append({
                "id": zone_id,
                "name": name,
                "players": [p for p in raw.get("players") or [] if isinstance(p, str) and p.startswith("media_player.")],
                # True quando lo schermo TV LAN/DLNA principale si trova in questa zona.
                "main_tv": raw.get("main_tv") is True,
                "iptv_channels": [safe_channel_id(str(c)) for c in raw.get("iptv_channels") or [] if str(c).strip()],
            })
        self.config["zones"] = zones

    def install_bundled_media(self):
        source = APP_DIR / "bundled_media"
        if not source.exists():
            return
        for item in source.iterdir():
            target = MEDIA_DIR / item.name
            if item.is_file() and not target.exists():
                shutil.copy2(item, target)

    def save(self):
        with self.lock:
            temp = CONFIG_FILE.with_suffix(".tmp")
            temp.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, CONFIG_FILE)

    def update(self, payload):
        with self.lock:
            candidate = deep_merge(self.config, payload)
            validate_music_slots(candidate.get("music", {}))
            self.config = candidate
            self.normalize()
            self.save()
            return copy.deepcopy(self.config)

    def log(self, level, message):
        entry = {"time": now_iso(), "level": level, "message": message}
        with self.lock:
            self.logs.insert(0, entry)
            del self.logs[200:]
        print(f"[{entry['time']}] {level.upper()}: {message}", flush=True)

    def media(self):
        result = []
        for path in sorted(MEDIA_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            kind = next((k for k, extensions in ALLOWED.items() if suffix in extensions), "other")
            if kind == "other":
                continue
            stat = path.stat()
            item = {
                "name": path.name,
                "kind": kind,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "url": f"/media/{urllib.parse.quote(path.name)}",
            }
            if kind == "audio":
                try:
                    item["duration"] = round(probe_media_duration(path), 3)
                except RuntimeError:
                    item["duration"] = None
            result.append(item)
        return result


class HomeAssistant:
    def request(self, method, path, payload=None, timeout=12):
        headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(HA_API + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise RuntimeError(f"Home Assistant {error.code}: {detail[:300]}") from error

    def states(self):
        return self.request("GET", "/states") or []

    def config(self):
        return self.request("GET", "/config") or {}

    def service(self, domain, service, payload):
        # Alcuni comandi Sonos (ripristino di 10 diffusori con la radio) richiedono più di 12 secondi.
        return self.request("POST", f"/services/{domain}/{service}", payload, timeout=HA_SERVICE_TIMEOUT) or []

    def service_response(self, domain, service, payload):
        response = self.request("POST", f"/services/{domain}/{service}?return_response", payload) or {}
        return response.get("service_response", {})

    def websocket_command(self, command, timeout=25):
        """Esegue un comando WebSocket autenticato sull'istanza Home Assistant."""
        if websocket is None:
            raise RuntimeError("Supporto WebSocket non installato nell'add-on")
        connection = websocket.create_connection(HA_WS, timeout=timeout)
        try:
            greeting = json.loads(connection.recv())
            if greeting.get("type") != "auth_required":
                raise RuntimeError("Home Assistant WebSocket non richiede l'autenticazione attesa")
            connection.send(json.dumps({"type": "auth", "access_token": TOKEN}))
            authenticated = json.loads(connection.recv())
            if authenticated.get("type") != "auth_ok":
                raise RuntimeError("Autenticazione WebSocket Home Assistant non riuscita")
            request_id = int(time.time_ns() % 2_000_000_000) or 1
            connection.send(json.dumps({"id": request_id, **command}))
            while True:
                response = json.loads(connection.recv())
                if response.get("id") != request_id:
                    continue
                if not response.get("success"):
                    error = response.get("error") or {}
                    raise RuntimeError(
                        error.get("message") or "Comando WebSocket Home Assistant non riuscito"
                    )
                return response.get("result")
        finally:
            connection.close()

    def browse_media(self, entity_id, media_type=None, media_id=None):
        payload = {"type": "media_player/browse_media", "entity_id": entity_id}
        if media_type:
            payload["media_content_type"] = str(media_type)
        if media_id:
            payload["media_content_id"] = str(media_id)
        return self.websocket_command(payload)


class SpotHistory:
    """Storico degli spot andati in onda, salvato su disco per i resoconti agli inserzionisti."""

    MAX_DAYS = 400
    MAX_ENTRIES = 50000

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.entries = [item for item in data.get("entries", []) if isinstance(item, dict)]
        except (FileNotFoundError, json.JSONDecodeError, AttributeError):
            self.entries = []

    def _save(self):
        cutoff = (datetime.now().astimezone() - timedelta(days=self.MAX_DAYS)).isoformat(timespec="seconds")
        self.entries = [item for item in self.entries if item.get("time", "") >= cutoff][-self.MAX_ENTRIES:]
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps({"entries": self.entries}, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            store.log("warning", f"Storico spot non salvato: {error}")

    def add(self, spot, players, manual, status="played", duration=None, reason=""):
        entry = {
            "id": uuid.uuid4().hex[:12],
            "time": now_iso(),
            "spot": spot or "",
            "players": list(players or []),
            "trigger": "manual" if manual else "automatic",
            "status": status,
            "duration": round(float(duration), 1) if duration else None,
            "reason": str(reason)[:300],
        }
        with self.lock:
            self.entries.append(entry)
            self._save()
        return entry["id"]

    def update(self, entry_id, **fields):
        with self.lock:
            for item in reversed(self.entries):
                if item.get("id") == entry_id:
                    item.update(fields)
                    self._save()
                    return True
        return False

    def query(self, start=None, end=None):
        """Voci dal giorno ``start`` al giorno ``end`` compresi (AAAA-MM-GG), le più recenti per prime."""
        with self.lock:
            items = list(self.entries)
        if start:
            items = [item for item in items if item.get("time", "")[:10] >= start]
        if end:
            items = [item for item in items if item.get("time", "")[:10] <= end]
        return list(reversed(items))


store = Store()
ha = HomeAssistant()
spot_history = SpotHistory(SPOT_HISTORY_FILE)
upload_lock = threading.Lock()
audio_playback_lock = threading.Lock()
audio_stop_event = threading.Event()
sonos_catalog_lock = threading.RLock()


def load_sonos_catalog():
    """Restituisce il catalogo Sonos salvato dall'ultimo aggiornamento riuscito."""
    with sonos_catalog_lock:
        try:
            payload = json.loads(SONOS_CATALOG_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
    result = []
    for item in payload.get("items", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        media_id = str(item["id"])
        media_type = str(item.get("type") or "music")
        if media_id.startswith("media-source://"):
            continue
        if not (
            media_id.startswith(("FV:", "S:", "SQ:"))
            or media_type in {"favorite_item_id", "sonos_playlist", "sonos_source"}
        ):
            continue
        entry = {"id": media_id, "name": str(item.get("name") or media_id), "type": media_type}
        if item.get("kind") in ("radio", "playlist"):
            entry["kind"] = item["kind"]
        result.append(entry)
    return result


def save_sonos_catalog(items):
    """Salva atomicamente il catalogo perché i refresh della UI non lo cancellino."""
    normalized = [
        {
            "id": str(item["id"]),
            "name": str(item.get("name") or item["id"]),
            "type": str(item.get("type") or "music"),
            # Radio o playlist, se Sonos lo ha indicato: serve alle due cartelle del Palinsesto.
            **({"kind": item["kind"]} if item.get("kind") in ("radio", "playlist") else {}),
        }
        for item in items
        if isinstance(item, dict) and item.get("id")
    ]
    payload = {"updated_at": now_iso(), "items": normalized}
    temporary = SONOS_CATALOG_FILE.with_suffix(".tmp")
    with sonos_catalog_lock:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, SONOS_CATALOG_FILE)


def _sonos_browser(entity_id, media_type=None, media_id=None):
    browser = ha.browse_media(entity_id, media_type, media_id)
    if not isinstance(browser, dict):
        raise RuntimeError("Il player selezionato non ha restituito contenuti multimediali")
    return browser


def _is_sonos_browser_node(node):
    """Riconosce soltanto contenitori appartenenti a Sonos/My Sonos."""
    text = " ".join(str(node.get(key) or "") for key in (
        "title", "media_class", "media_content_type", "media_content_id",
    ))
    return bool(re.search(
        r"\bsonos\b|i miei sonos|mein sonos|my sonos|sonos[-_ ]?favorite|"
        r"preferiti sonos|favoriten sonos",
        text,
        re.IGNORECASE,
    ))


def sonos_item_kind(node, folder=None):
    """Radio o playlist, dalla classe del contenuto o dalla cartella dei Preferiti in cui si trova."""
    media_class = str(node.get("media_class") or "").lower()
    media_id = str(node.get("media_content_id") or "")
    folder_text = " ".join(str((folder or {}).get(key) or "") for key in (
        "title", "media_content_id", "media_class")).lower()
    if media_class == "playlist" or media_id.startswith(("SQ:", "S:")) or "playlist" in folder_text:
        return "playlist"
    if media_class in ("genre", "channel") or "audiobroadcast" in folder_text \
            or re.search(r"\bradio|sender|station", folder_text):
        return "radio"
    return ""


def _is_native_sonos_item(node):
    """ID riproducibili creati dal sistema Sonos, non da Media Browser HA."""
    media_id = str(node.get("media_content_id") or "")
    media_type = str(node.get("media_content_type") or "").lower()
    return (
        media_id.startswith(("FV:", "S:", "SQ:"))
        or media_type in {"favorite_item_id", "sonos_playlist"}
    )


def refresh_sonos_catalog(entity_id, max_depth=5, max_folders=120, max_items=3000):
    """Aggiorna i Preferiti e percorre ricorsivamente la sezione 'I miei Sonos'."""
    states = ha.states()
    favorite_entities = []
    items = {}
    for state in states:
        entity = str(state.get("entity_id", ""))
        attrs = state.get("attributes", {})
        if entity.startswith("media_player.") and isinstance(attrs.get("group_members"), list):
            for source in attrs.get("source_list") or []:
                source = str(source).strip()
                if source:
                    items[f"source:{source}"] = {
                        "id": source, "name": source, "type": "sonos_source",
                    }
        favorites = attrs.get("items")
        if entity == "sensor.sonos_favorites" or isinstance(favorites, dict):
            if entity.startswith("sensor."):
                favorite_entities.append(entity)
        if isinstance(favorites, dict):
            for media_id, name in favorites.items():
                if str(media_id).startswith("FV:"):
                    items[str(media_id)] = {
                        "id": str(media_id), "name": str(name), "type": "favorite_item_id",
                    }
    if favorite_entities:
        try:
            ha.service("homeassistant", "update_entity", {
                "entity_id": list(dict.fromkeys(favorite_entities)),
            })
            for state in ha.states():
                favorites = state.get("attributes", {}).get("items")
                if isinstance(favorites, dict):
                    for media_id, name in favorites.items():
                        if str(media_id).startswith("FV:"):
                            items[str(media_id)] = {
                                "id": str(media_id), "name": str(name), "type": "favorite_item_id",
                            }
        except Exception as error:
            store.log("warning", f"Aggiornamento Preferiti Sonos non riuscito: {error}")

    root = _sonos_browser(entity_id)

    def remember(browser, trusted=False):
        for child in browser.get("children") or []:
            media_id = str(child.get("media_content_id") or "")
            # Anche dentro una cartella chiamata "I miei Sonos" Home Assistant
            # può mescolare migliaia di elementi di Radio Browser. Importiamo
            # soltanto ID nativi Sonos (FV:/S:/SQ:) per evitare la lista infinita.
            if child.get("can_play") and media_id and _is_native_sonos_item(child):
                items[media_id] = {
                    "id": media_id,
                    "name": str(child.get("title") or media_id),
                    "type": str(child.get("media_content_type") or "music"),
                    "kind": sonos_item_kind(child, browser),
                }

    # ``media_player/browse_media`` può restituire la radice generale di Home
    # Assistant (Media locali, Radio Browser, ecc.). Non va mai importata come
    # se fosse la libreria Sonos. Una radice è attendibile solo se si identifica
    # esplicitamente come Sonos; gli ID FV:/S:/SQ: restano comunque sicuri.
    root_is_sonos = _is_sonos_browser_node(root)
    remember(root, trusted=root_is_sonos)
    root_folders = [child for child in (root.get("children") or []) if child.get("can_expand")]
    sonos_folders = [
        child for child in root_folders
        if root_is_sonos or _is_sonos_browser_node(child)
    ]
    queue = [(child, 1) for child in sonos_folders]
    visited = set()
    browsed = 0
    while queue and browsed < max_folders and len(items) < max_items:
        folder, depth = queue.pop(0)
        key = (str(folder.get("media_content_type") or ""), str(folder.get("media_content_id") or ""))
        if not key[1] or key in visited:
            continue
        visited.add(key)
        try:
            browser = _sonos_browser(entity_id, key[0], key[1])
        except Exception as error:
            store.log("warning", f"Cartella Sonos non leggibile ({folder.get('title', key[1])}): {error}")
            continue
        browsed += 1
        remember(browser, trusted=True)
        if depth < max_depth:
            queue.extend(
                (child, depth + 1)
                for child in (browser.get("children") or [])
                if child.get("can_expand")
            )
    unique_items = {(item["type"], item["id"]): item for item in items.values()}
    catalog_items = sorted(unique_items.values(), key=lambda item: item["name"].lower())
    save_sonos_catalog(catalog_items)
    store.log("success", f"Catalogo Sonos aggiornato e salvato: {len(catalog_items)} contenuti")
    return {
        "root": {
            "title": "I miei Sonos",
            "children": [{
                "title": item["name"],
                "media_content_id": item["id"],
                "media_content_type": item["type"],
                "can_expand": False,
                "can_play": True,
            } for item in catalog_items],
        },
        "items": catalog_items,
        "folders_scanned": browsed,
        "source": "sonos",
        "truncated": bool(queue or len(items) >= max_items),
    }


def local_base_url():
    configured = store.config.get("media_base_url", "").strip().rstrip("/")
    if configured:
        return configured
    try:
        url = ha.config().get("internal_url") or ha.config().get("external_url") or ""
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname:
            host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
            return f"{parsed.scheme or 'http'}://{host}:{PORT}"
    except Exception:
        pass
    return f"http://homeassistant.local:{PORT}"


def player_local_url():
    """Restituisce l'indirizzo LAN del player senza esporre la porta amministrativa."""
    parsed = urllib.parse.urlsplit(local_base_url())
    hostname = parsed.hostname or "homeassistant.local"
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{parsed.scheme or 'http'}://{host}:{PLAYER_PORT}"


def remote_local_url():
    """Indirizzo LAN della porta per l'accesso da Internet, da collegare a Cloudflare."""
    parsed = urllib.parse.urlsplit(local_base_url())
    hostname = parsed.hostname or "homeassistant.local"
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"http://{host}:{REMOTE_PORT}"


def player_public_url():
    configured = store.config.get("browser_player", {}).get("public_url", "").strip().rstrip("/")
    return configured or player_local_url()


def media_url(filename):
    return f"{local_base_url()}/media/{urllib.parse.quote(filename)}"


def is_schedule_active(schedule, moment=None):
    if not schedule:
        return True
    moment = moment or datetime.now().astimezone()
    weekday = moment.weekday()
    current = moment.hour * 60 + moment.minute
    for row in schedule:
        if row.get("enabled", True) is False:
            continue
        days = row.get("days", [])
        try:
            sh, sm = map(int, row.get("start", "00:00").split(":"))
            eh, em = map(int, row.get("end", "23:59").split(":"))
            start, end = sh * 60 + sm, eh * 60 + em
        except (TypeError, ValueError):
            continue
        if start <= end and weekday in days and start <= current < end:
            return True
        if start > end and ((weekday in days and current >= start) or ((weekday - 1) % 7 in days and current < end)):
            return True
    return False


def active_music_slot(music, moment=None):
    """Restituisce la prima sorgente attiva dentro un orario generale musica."""
    if not is_schedule_active(music.get("schedule", []), moment):
        return None
    slots = music.get("slots") or []
    for index, slot in enumerate(slots):
        if slot.get("enabled", True) is False:
            continue
        if is_music_slot_active(slot, moment):
            result = copy.deepcopy(slot)
            result["_index"] = index
            result["players"] = result.get("players") or music.get("players", [])
            return result
    return None


# Mix automatico: per tutto l'orario di apertura alterna a blocchi le radio e le playlist scelte.
# Due blocchi vicini non hanno mai la stessa sorgente, alla stessa ora non si ripete quella del giorno
# prima e ogni giorno l'ordine cambia. Il calcolo parte sempre dalla stessa data, così il mix di un
# giorno è sempre lo stesso (anteprima del Palinsesto = quello che suona davvero).
MIX_ANCHOR = datetime(2024, 1, 1).date()
MIX_CACHE = {"signature": None, "days": {}}
MIX_CACHE_LOCK = threading.Lock()
MIX_CONTENT_TYPES = {"favorite_item_id", "playlist", "sonos_playlist_source", "sonos_radio_source",
                     "sonos_source", "radio_url", "music"}


def mix_settings():
    music = store.config.get("music", {})
    raw = music.get("mix") or {}
    sources = []
    seen = set()
    for item in raw.get("sources") or []:
        if not isinstance(item, dict):
            continue
        content_id = str(item.get("content_id") or "").strip()
        content_type = str(item.get("content_type") or "favorite_item_id")
        if not content_id or content_type not in MIX_CONTENT_TYPES or content_id in seen:
            continue
        seen.add(content_id)
        sources.append({
            "content_id": content_id, "content_type": content_type,
            "name": str(item.get("name") or content_id)[:120],
            "kind": "radio" if item.get("kind") == "radio" or content_type in ("radio_url", "sonos_radio_source")
            else "playlist",
        })
    try:
        block = int(raw.get("block_minutes", 60))
    except (TypeError, ValueError):
        block = 60
    try:
        volume = int(raw.get("volume", music.get("volume", 25)))
    except (TypeError, ValueError):
        volume = 25
    return {
        "enabled": raw.get("enabled") is True and len(sources) >= 2,
        "sources": sources,
        "block_minutes": max(15, min(block, 240)),
        # Casse del mix; se non ne è scelta nessuna, quelle della musica o di tutte le fasce del Palinsesto.
        "players": [p for p in dict.fromkeys(
            raw.get("players") or music.get("players")
            or [player for slot in music.get("slots") or [] for player in slot.get("players") or []]
        ) if isinstance(p, str)],
        "volume": max(0, min(volume, 100)),
    }


def opening_windows(schedule, weekday):
    """Finestre di apertura di un giorno in minuti (come nel pannello); senza orari, tutto il giorno."""
    rows = [row for row in schedule or [] if row.get("enabled", True) is not False]
    if not rows:
        return [(0, 1440)]
    windows = []
    for row in rows:
        try:
            sh, sm = map(int, str(row.get("start", "00:00")).split(":"))
            eh, em = map(int, str(row.get("end", "23:59")).split(":"))
        except (TypeError, ValueError):
            continue
        start, end = sh * 60 + sm, eh * 60 + em
        days = row.get("days") or []
        if weekday in days:
            windows.append((start, end) if start < end else (start, 1440))
        if start >= end and (weekday - 1) % 7 in days:
            windows.append((0, end))
    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _mix_day(day, settings, schedule, previous, signature, recent=()):
    """Mix di un giorno; se la sequenza è già uscita in uno dei giorni precedenti si rifà."""
    plan = []
    for attempt in range(25):
        plan = _mix_day_attempt(day, settings, schedule, previous, f"{signature}|{day.isoformat()}|{attempt}")
        if tuple(item["content_id"] for item in plan) not in recent:
            break
    return plan


def _mix_day_attempt(day, settings, schedule, previous, seed):
    rng = random.Random(seed)
    block = settings["block_minutes"]
    blocks = []
    for start, end in opening_windows(schedule, day.weekday()):
        moment = start
        while moment < end:
            stop = min(end, moment + block)
            if end - stop < block / 2:
                stop = end
            blocks.append((moment, stop))
            moment = stop

    def yesterday_at(minute):
        return next((item["content_id"] for item in previous if item["start"] <= minute < item["end"]), None)

    sources = settings["sources"]
    kinds = {source["kind"] for source in sources}
    usage = {source["content_id"]: 0 for source in sources}
    plan, last = [], None
    for start, end in blocks:
        avoid = {yesterday_at(start), yesterday_at(end - 1), yesterday_at((start + end) // 2)}
        pool = [s for s in sources if s["content_id"] != (last or {}).get("content_id") and s["content_id"] not in avoid]
        if not pool:
            pool = [s for s in sources if s["content_id"] != (last or {}).get("content_id")] or sources
        # Radio e playlist si alternano quando ci sono tutte e due.
        if last and len(kinds) > 1:
            other = [s for s in pool if s["kind"] != last["kind"]]
            pool = other or pool
        fewest = min(usage[s["content_id"]] for s in pool)
        choice = rng.choice([s for s in pool if usage[s["content_id"]] == fewest])
        usage[choice["content_id"]] += 1
        plan.append({"start": start, "end": end, **choice})
        last = choice
    return plan


def mix_plan(day):
    """Blocchi del mix per una data, calcolati a catena dal giorno di partenza (in memoria)."""
    settings = mix_settings()
    schedule = store.config.get("music", {}).get("schedule", [])
    signature = hashlib.sha1(json.dumps([settings["sources"], settings["block_minutes"], schedule],
                                        sort_keys=True).encode()).hexdigest()[:12]
    with MIX_CACHE_LOCK:
        if MIX_CACHE["signature"] != signature:
            MIX_CACHE["signature"], MIX_CACHE["days"] = signature, {}
        days = MIX_CACHE["days"]
        if day in days:
            return days[day]
        known = [d for d in days if d < day]
        current = max(known) if known else MIX_ANCHOR - timedelta(days=1)
        # «Giorno prima» = l'ultimo giorno in cui il locale era aperto (dopo la domenica chiusa, il sabato).
        previous = next((days[d] for d in sorted(known, reverse=True) if days[d]), [])
        while current < day:
            current += timedelta(days=1)
            # Ultimi 60 giorni: nessun giorno ripete la stessa sequenza.
            recent = {tuple(item["content_id"] for item in days[current - timedelta(days=back)])
                      for back in range(1, 61) if current - timedelta(days=back) in days}
            plan = days[current] = _mix_day(current, settings, schedule, previous, signature, recent)
            if plan:
                previous = plan
        return days[day]


def active_mix_slot(moment=None):
    """La fascia del mix in onda adesso, nello stesso formato delle fasce del Palinsesto."""
    settings = mix_settings()
    if not settings["enabled"]:
        return None
    moment = moment or datetime.now().astimezone()
    minute = moment.hour * 60 + moment.minute
    for item in mix_plan(moment.date()):
        if item["start"] <= minute < item["end"]:
            return {
                "id": f"mix-{moment.date().isoformat()}-{item['start']}",
                "name": item["name"], "title": item["name"],
                "content_id": item["content_id"], "content_type": item["content_type"],
                "players": settings["players"], "volume": settings["volume"], "mix": True,
            }
    return None


def mix_week(today=None):
    """Mix della settimana in corso (da lunedì) per l'anteprima nel Palinsesto."""
    if not mix_settings()["enabled"]:
        return []
    today = today or datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    week = []
    for offset in range(7):
        day = monday + timedelta(days=offset)
        week.append({"date": day.isoformat(), "weekday": day.weekday(), "blocks": [
            {"start": item["start"], "end": item["end"], "name": item["name"], "kind": item["kind"],
             "content_id": item["content_id"]} for item in mix_plan(day)]})
    return week


def is_music_slot_active(slot, moment=None):
    """Verifica una sorgente con ora di partenza e durata, anche oltre mezzanotte."""
    moment = moment or datetime.now().astimezone()
    weekday = moment.weekday()
    current = moment.hour * 60 + moment.minute
    return any(
        day == weekday and start <= current < end
        for day, start, end in _slot_intervals(slot)
    )


def _slot_intervals(slot):
    """Espande una fascia nei sette giorni, dividendo quelle oltre mezzanotte."""
    try:
        sh, sm = map(int, str(slot.get("start", "")).split(":"))
        start = sh * 60 + sm
        duration = int(slot.get("duration_minutes", 0) or 0)
        if duration:
            if not 1 <= duration < 1440:
                return []
            end = (start + duration) % 1440
        else:
            eh, em = map(int, str(slot.get("end", "")).split(":"))
            end = eh * 60 + em
    except (TypeError, ValueError):
        return []
    if not (0 <= start < 1440 and 0 <= end < 1440) or start == end:
        return []
    intervals = []
    for day in slot.get("days", []):
        if not isinstance(day, int) or not 0 <= day <= 6:
            continue
        if start < end:
            intervals.append((day, start, end))
        else:
            intervals.append((day, start, 1440))
            intervals.append(((day + 1) % 7, 0, end))
    return intervals


def _schedule_intervals(schedule):
    """Espande le fasce generali nei sette giorni, comprese quelle notturne."""
    intervals = []
    for row in schedule or []:
        if row.get("enabled", True) is False:
            continue
        try:
            sh, sm = map(int, str(row.get("start", "")).split(":"))
            eh, em = map(int, str(row.get("end", "")).split(":"))
            start, end = sh * 60 + sm, eh * 60 + em
        except (TypeError, ValueError):
            continue
        if not (0 <= start < 1440 and 0 <= end < 1440) or start == end:
            continue
        for day in row.get("days", []):
            if not isinstance(day, int) or not 0 <= day <= 6:
                continue
            if start < end:
                intervals.append((day, start, end))
            else:
                intervals.append((day, start, 1440))
                intervals.append(((day + 1) % 7, 0, end))
    return intervals


DAY_NAMES_IT = ["il lunedì", "il martedì", "il mercoledì", "il giovedì", "il venerdì", "il sabato", "la domenica"]


def sonos_label(entity_id):
    """Nome breve di un Sonos per i messaggi: media_player.sonos_sala -> Sala."""
    name = str(entity_id).split(".", 1)[-1]
    if name.startswith("sonos_"):
        name = name[len("sonos_"):]
    return name.replace("_", " ").strip().capitalize() or str(entity_id)


def validate_music_slots(music):
    """Rifiuta fasce incomplete o sovrapposte sugli stessi Sonos."""
    slots = [slot for slot in (music.get("slots") or []) if slot.get("enabled", True)]
    global_players = set(music.get("players") or [])
    for index, slot in enumerate(slots):
        label = slot.get("name") or f"Fascia {index + 1}"
        if not slot.get("content_id"):
            raise RuntimeError(f"{label}: scegli una playlist o una radio")
        if not slot.get("days"):
            raise RuntimeError(f"{label}: seleziona almeno un giorno")
        if not _slot_intervals(slot):
            raise RuntimeError(f"{label}: orario non valido o inizio uguale alla fine")
        # Gli orari generali sono il cancello principale: una sorgente può
        # contenere giorni/orari più ampi e verrà semplicemente ignorata fuori
        # dalle fasce generali da active_music_slot().
        players = set(slot.get("players") or global_players)
        if not players:
            raise RuntimeError(f"{label}: seleziona almeno un altoparlante Sonos")
    for left_index, left in enumerate(slots):
        left_players = set(left.get("players") or global_players)
        left_intervals = _slot_intervals(left)
        for right in slots[left_index + 1:]:
            right_players = set(right.get("players") or global_players)
            if not left_players.intersection(right_players):
                continue
            overlap = next((
                (lday, max(lstart, rstart), min(lend, rend))
                for lday, lstart, lend in left_intervals
                for rday, rstart, rend in _slot_intervals(right)
                if lday == rday and max(lstart, rstart) < min(lend, rend)
            ), None)
            if overlap:
                left_name = left.get("name") or f"Fascia {left_index + 1}"
                right_name = right.get("name") or "Fascia successiva"
                day, start, end = overlap
                shared = ", ".join(sonos_label(player) for player in sorted(left_players & right_players))
                # Il messaggio dice dove intervenire: giorno, orario e casse in comune.
                raise RuntimeError(
                    f"Le fasce ‘{left_name}’ e ‘{right_name}’ si sovrappongono {DAY_NAMES_IT[day]} "
                    f"dalle {start // 60:02d}:{start % 60:02d} alle {end // 60 % 24:02d}:{end % 60:02d} su {shared}. "
                    "Un Sonos suona una sola musica alla volta: cambia l’orario, i giorni o le casse di una delle due."
                )


def configured_sonos_players():
    """Sonos citati in configurazione: restano visibili anche quando sono offline."""
    cfg = store.config
    players = list(cfg.get("audio", {}).get("players", []))
    players += cfg.get("music", {}).get("players", [])
    for slot in cfg.get("music", {}).get("slots", []):
        players += slot.get("players") or []
    for zone in cfg.get("zones", []):
        players += zone.get("players") or []
    return {player for player in players if isinstance(player, str)}


def sonos_state_detail(state, configured=()):
    """Stato leggibile di un Sonos per le schermate Oggi e Zona, oppure None se non è un Sonos."""
    entity_id = str(state.get("entity_id", ""))
    attrs = state.get("attributes", {}) or {}
    members = attrs.get("group_members")
    if not isinstance(members, list) and entity_id not in configured:
        return None
    volume = attrs.get("volume_level")
    return {
        "entity_id": entity_id,
        "name": attrs.get("friendly_name", entity_id),
        "state": state.get("state"),
        "available": state.get("state") != "unavailable",
        "volume": round(float(volume) * 100) if isinstance(volume, (int, float)) else None,
        "group_members": [m for m in (members or [entity_id]) if isinstance(m, str)],
        "media_title": attrs.get("media_title") or attrs.get("source") or "",
        # Artista del brano in onda (vuoto per molte radio).
        "media_artist": attrs.get("media_artist") or "",
    }


# Sonos spento o scollegato che Home Assistant non segna ancora come «non disponibile»: i comandi
# inviati a più Sonos insieme fallivano tutti per colpa sua. Viene saltato per qualche minuto.
SONOS_UNRESPONSIVE = {}
SONOS_UNRESPONSIVE_SECONDS = 180


def sonos_unresponsive(player):
    until = SONOS_UNRESPONSIVE.get(player)
    if until and time.monotonic() < until:
        return True
    SONOS_UNRESPONSIVE.pop(player, None)
    return False


def mark_sonos_unresponsive(player, error):
    if not sonos_unresponsive(player):
        store.log("warning", f"Il Sonos {player.removeprefix('media_player.')} non risponde ({error}): "
                             "lo salto per 3 minuti, gli altri continuano")
    SONOS_UNRESPONSIVE[player] = time.monotonic() + SONOS_UNRESPONSIVE_SECONDS


def sonos_service_each(domain, name, payload, call=None, split_on_timeout=True):
    """Comando a più Sonos insieme; se fallisce lo ripete uno alla volta e salta quelli che non rispondono."""
    call = call or ha.service
    entities = payload.get("entity_id")
    try:
        return call(domain, name, payload)
    except Exception as error:
        if not isinstance(entities, list) or len(entities) < 2 or sonos_transition_busy(error)                 or (not split_on_timeout and sonos_request_timed_out(error)):
            raise
        done = 0
        for entity in entities:
            try:
                call(domain, name, {**payload, "entity_id": [entity]})
                done += 1
            except Exception as single_error:
                mark_sonos_unresponsive(entity, single_error)
        if not done:
            raise
        return []


def available_sonos_players(players):
    """Scarta entità eliminate, non disponibili o non appartenenti all'integrazione Sonos."""
    requested = list(dict.fromkeys(player for player in players if player))
    try:
        states = {item.get("entity_id"): item for item in ha.states()}
    except Exception as error:
        store.log("warning", f"Verifica altoparlanti Sonos non riuscita: {error}")
        return requested
    sonos_ids = set()
    for entity_id, item in states.items():
        members = item.get("attributes", {}).get("group_members")
        if entity_id.startswith("media_player.") and isinstance(members, list):
            sonos_ids.add(entity_id)
            sonos_ids.update(member for member in members if isinstance(member, str))
    # Le versioni moderne dell'integrazione espongono group_members. Se nessuna
    # entità lo espone, manteniamo la compatibilità con installazioni più vecchie.
    if sonos_ids:
        valid = [
            player for player in requested
            if player in sonos_ids and states.get(player, {}).get("state") != "unavailable"
        ]
    else:
        valid = [player for player in requested if states.get(player, {}).get("state") != "unavailable"]
    removed = [player for player in requested if player not in valid]
    if removed:
        store.log("warning", "Altoparlanti ignorati perché non sono Sonos disponibili: " + ", ".join(removed))
    # Sonos che non hanno risposto da poco: si saltano senza aspettare un altro errore.
    valid = [player for player in valid if not sonos_unresponsive(player)]
    return valid


def leader_first(players):
    """La cassa principale (scelta nel Palinsesto) guida il gruppo: se si spegne il coordinatore, Sonos
    ferma la musica su tutte le casse. Conviene che guidi una cassa che non viene mai spenta."""
    leader = str(store.config.get("music", {}).get("leader") or "")
    players = list(players)
    return [leader] + [p for p in players if p != leader] if leader in players else players


def prepare_sonos_group(players, force=False):
    """Raggruppa gli altoparlanti e restituisce il coordinatore Sonos."""
    players = leader_first(available_sonos_players(players))
    if not players:
        raise RuntimeError("Nessun altoparlante Sonos disponibile tra quelli selezionati")
    coordinator = players[0]
    if len(players) == 1:
        return coordinator

    already_grouped = False
    try:
        states = {item.get("entity_id"): item for item in ha.states()}
        members = states.get(coordinator, {}).get("attributes", {}).get("group_members") or []
        already_grouped = members and members[0] == coordinator and set(players).issubset(set(members))
    except Exception as error:
        store.log("warning", f"Stato gruppo Sonos non leggibile: {error}")

    if force or not already_grouped:
        try:
            sonos_service("media_player", "join", {
                "entity_id": coordinator,
                "group_members": players[1:],
            })
        except Exception as error:
            if len(players) < 3 or sonos_transition_busy(error):
                raise
            # Un Sonos spento fa fallire tutto il raggruppamento: si aggiungono uno alla volta.
            for member in players[1:]:
                try:
                    sonos_service("media_player", "join", {"entity_id": coordinator, "group_members": [member]})
                except Exception as member_error:
                    mark_sonos_unresponsive(member, member_error)
        if SONOS_GROUP_SETTLE_SECONDS > 0:
            time.sleep(SONOS_GROUP_SETTLE_SECONDS)
        store.log("info", f"Gruppo Sonos sincronizzato: {len(players)} altoparlanti")
    return coordinator


def sonos_playback_context(players):
    """Trova una sola destinazione per gruppo e ricorda quali gruppi stavano suonando."""
    try:
        states = {item.get("entity_id"): item for item in ha.states()}
    except Exception as error:
        store.log("warning", f"Stato Sonos precedente non leggibile: {error}")
        return list(players), []

    snapshot_targets = []
    resume_targets = []
    for player in players:
        item = states.get(player, {})
        members = item.get("attributes", {}).get("group_members") or [player]
        coordinator = members[0] if members else player
        if coordinator not in snapshot_targets:
            snapshot_targets.append(coordinator)
        group_playing = any(
            states.get(member, {}).get("state") in ("playing", "buffering")
            for member in members
        )
        if (item.get("state") in ("playing", "buffering") or group_playing) and coordinator not in resume_targets:
            resume_targets.append(coordinator)
    return snapshot_targets, resume_targets


def sonos_group_ready(players, groups):
    """Coordinatore del gruppo se i Sonos scelti formano già da soli un unico gruppo (di solito quello
    della musica del locale). Allora lo spot parte subito lì: staccare e riunire i diffusori lasciava
    qualche secondo di silenzio tra la musica e lo spot."""
    players = list(dict.fromkeys(players))
    if len(groups) != 1:
        return None
    coordinator, members = next(iter(groups.items()))
    if coordinator in players and set(members) == set(players):
        return coordinator
    return None


def sonos_group_topology(players):
    """Memorizza i gruppi reali dei Sonos scelti prima di uno spot."""
    try:
        states = {item.get("entity_id"): item for item in ha.states()}
    except Exception as error:
        store.log("warning", f"Topologia Sonos precedente non leggibile: {error}")
        return {}
    groups = {}
    for player in dict.fromkeys(players):
        item = states.get(player, {})
        members = item.get("attributes", {}).get("group_members") or [player]
        coordinator = members[0] if members else player
        groups.setdefault(coordinator, [])
        for member in members:
            if member not in groups[coordinator]:
                groups[coordinator].append(member)
    return groups


def repair_sonos_group(players, preferred_coordinator=None, volume_level=None):
    """Riaggancia soltanto i membri usciti dal gruppo, senza fermare quelli attivi."""
    requested = list(dict.fromkeys(player for player in players if player))
    if not requested:
        return {"coordinator": None, "missing": [], "unavailable": []}
    try:
        states = {item.get("entity_id"): item for item in ha.states()}
    except Exception as error:
        raise RuntimeError(f"Controllo gruppo Sonos non riuscito: {error}") from error

    reachable = [
        player for player in requested
        if player in states and states[player].get("state") != "unavailable" and not sonos_unresponsive(player)
    ]
    unavailable = [player for player in requested if player not in reachable]
    if not reachable:
        return {"coordinator": None, "missing": [], "unavailable": unavailable}

    coordinator = preferred_coordinator if preferred_coordinator in reachable else leader_first(reachable)[0]
    members = states.get(coordinator, {}).get("attributes", {}).get("group_members") or [coordinator]
    missing = [player for player in reachable if player not in members]
    if missing:
        try:
            ha.service("media_player", "join", {
                "entity_id": coordinator,
                "group_members": missing,
            })
        except Exception as error:
            # Un Sonos appena spento risulta ancora acceso per qualche secondo: riprovare ogni
            # controllo bloccava tutto fino al timeout. Si salta per qualche minuto.
            if len(missing) == 1:
                mark_sonos_unresponsive(missing[0], error)
                return {"coordinator": coordinator, "missing": [], "unavailable": unavailable + missing}
            joined = []
            for member in missing:
                try:
                    ha.service("media_player", "join", {"entity_id": coordinator, "group_members": [member]})
                    joined.append(member)
                except Exception as member_error:
                    mark_sonos_unresponsive(member, member_error)
            unavailable = unavailable + [m for m in missing if m not in joined]
            missing = joined
            if not missing:
                return {"coordinator": coordinator, "missing": [], "unavailable": unavailable}
        if SONOS_GROUP_SETTLE_SECONDS > 0:
            time.sleep(SONOS_GROUP_SETTLE_SECONDS)
        if isinstance(volume_level, dict):
            # Ogni Sonos riagganciato riprende il proprio volume.
            set_sonos_volume_levels({player: volume_level[player] for player in missing if player in volume_level})
        elif volume_level is not None:
            level = max(0.0, min(float(volume_level), 1.0))
            set_sonos_volume_levels({player: level for player in missing})
        # Il coordinatore continua il flusso corrente; i membri appena entrati
        # lo seguono senza riavviare la playlist sugli altoparlanti già attivi.
        ha.service("media_player", "media_play", {"entity_id": coordinator})
    return {"coordinator": coordinator, "missing": missing, "unavailable": unavailable}


def repair_sonos_topology(groups, volume_levels=None):
    """Verifica dopo lo spot che ogni gruppo salvato sia stato davvero ripristinato."""
    repaired = []
    unavailable = []
    for coordinator, members in (groups or {}).items():
        result = repair_sonos_group(members, preferred_coordinator=coordinator)
        repaired.extend(result["missing"])
        unavailable.extend(result["unavailable"])
        if volume_levels and result["missing"]:
            set_sonos_volume_levels({
                player: volume_levels[player]
                for player in result["missing"] if player in volume_levels
            })
    return list(dict.fromkeys(repaired)), list(dict.fromkeys(unavailable))


def remember_sonos_volumes(levels):
    with SONOS_VOLUME_MEMORY_LOCK:
        for player, level in (levels or {}).items():
            try:
                SONOS_VOLUME_MEMORY[player] = max(0.0, min(float(level), 1.0))
            except (TypeError, ValueError):
                continue


# Volumi scelti a mano (cursori del pannello o app Sonos): valgono per tutta la giornata, anche quando
# cambia la fascia musicale. Il giorno dopo si riparte dal volume delle fasce. Restano salvati su file
# così sopravvivono a un riavvio dell'add-on.
SONOS_MANUAL_FILE = DATA_DIR / "carellas_sonos_volumes.json"
# Zone in cui gli operatori hanno tolto la musica (valgono per oggi): le casse non vengono raggruppate.
MUSIC_EXCLUDED = {}


def music_excluded_players():
    today = datetime.now().date().isoformat()
    return {player for player, day in MUSIC_EXCLUDED.items() if day == today}


# Zone in cui gli operatori hanno tolto gli spot: solo per oggi. Il cambio per sempre si fa dal
# pannello amministrativo (casse degli spot nella configurazione).
SPOTS_EXCLUDED = {}


def spots_excluded_players():
    today = datetime.now().date().isoformat()
    return {player for player, day in SPOTS_EXCLUDED.items() if day == today}


# Spot tolti dalla rotazione dagli operatori: solo per oggi (nome del file -> giorno).
ADS_EXCLUDED = {}


def ads_excluded_today():
    today = datetime.now().date().isoformat()
    return {name for name, day in ADS_EXCLUDED.items() if day == today}


def active_ads(cfg):
    """Spot in rotazione oggi: quelli scelti dall'amministratore meno quelli tolti per oggi."""
    excluded = ads_excluded_today()
    return [ad for ad in cfg.get("ads", []) if ad not in excluded]


# Righe del registro che riguardano musica e spot (le uniche che vedono gli operatori).
STAFF_LOG_WORDS = re.compile(r"spot|pubblicit|music|playlist|radio|fascia|sonos|brano|zona|volume|mix", re.I)
STAFF_LOG_HIDDEN = re.compile(r"\btv\b|iptv|canal[ei]|schermo|schermi|player tv|dlna|instagram|token|password|accesso|login", re.I)
SONOS_LAST_SET = {}
SONOS_LAST_SET_AT = {}
SONOS_MANUAL_CANDIDATES = {}


def _load_manual_volumes():
    try:
        data = json.loads(SONOS_MANUAL_FILE.read_text(encoding="utf-8"))
        return {str(player): (float(item[0]), str(item[1])) for player, item in data.items()}
    except (FileNotFoundError, OSError, ValueError, TypeError, IndexError, AttributeError):
        return {}


SONOS_MANUAL_VOLUMES = _load_manual_volumes()


# Punto in cui ogni playlist è stata lasciata (brano nella coda e secondi): quando il Mix o il Palinsesto
# ci ritornano riprende da lì invece di ricominciare dal primo brano. Le radio non hanno una coda.
PLAYLIST_POSITIONS_FILE = DATA_DIR / "carellas_playlist_positions.json"
PLAYLIST_POSITIONS_LOCK = threading.Lock()


def _load_playlist_positions():
    try:
        data = json.loads(PLAYLIST_POSITIONS_FILE.read_text(encoding="utf-8"))
        return {str(key): value for key, value in data.items() if isinstance(value, dict)}
    except (FileNotFoundError, OSError, ValueError, TypeError, AttributeError):
        return {}


PLAYLIST_POSITIONS = _load_playlist_positions()


def _save_playlist_positions():
    try:
        PLAYLIST_POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = PLAYLIST_POSITIONS_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(PLAYLIST_POSITIONS, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, PLAYLIST_POSITIONS_FILE)
    except OSError as error:
        store.log("warning", f"Punto delle playlist non salvato: {error}")


def playlist_key(selection):
    """Chiave della playlist della fascia, oppure None per le radio."""
    if not selection or selection.get("content_type") in ("radio_url", "sonos_radio_source"):
        return None
    if slot_looks_like_radio(selection):
        return None
    return str(selection.get("content_id") or "") or None


def remember_playlist_position(selection, coordinator):
    """Prima di cambiare sorgente: brano e secondo in cui si trova la playlist, per riprenderla da lì."""
    key = playlist_key(selection)
    if not key or not coordinator:
        return None
    try:
        item = next((state for state in ha.states() if state.get("entity_id") == coordinator), None)
    except Exception:
        return None
    attrs = (item or {}).get("attributes") or {}
    index, duration = attrs.get("queue_position"), attrs.get("media_duration")
    if not isinstance(index, int) or isinstance(index, bool) or index < 0 or not duration:
        # Niente coda (radio o sorgente esterna): non c'è un punto da ricordare.
        return None
    position = float(attrs.get("media_position") or 0)
    updated = attrs.get("media_position_updated_at")
    if item.get("state") == "playing" and updated:
        try:
            position += max(0.0, (datetime.now().astimezone() - datetime.fromisoformat(str(updated))).total_seconds())
        except (TypeError, ValueError):
            pass
    size = attrs.get("queue_size")
    if position > float(duration) - 15:
        # Brano quasi finito: la prossima volta si parte da quello dopo.
        index, position = index + 1, 0.0
    if isinstance(size, int) and size > 0 and index >= size:
        index, position = 0, 0.0
    saved = {"index": index, "position": round(position), "title": str(attrs.get("media_title") or ""),
             "saved_at": now_iso()}
    with PLAYLIST_POSITIONS_LOCK:
        PLAYLIST_POSITIONS[key] = saved
        _save_playlist_positions()
    return saved


def music_coordinator_now(states):
    """Sonos che guida la musica in onda: quello del motore oppure il primo che suona una coda."""
    coordinator = scheduler.music_coordinator
    if coordinator not in states:
        coordinator = next((entity for entity, item in states.items() if entity.startswith("media_player.")
                            and item.get("state") in ("playing", "paused")
                            and isinstance((item.get("attributes") or {}).get("queue_position"), int)), None)
    if not coordinator:
        return None
    members = (states.get(coordinator, {}).get("attributes") or {}).get("group_members") or [coordinator]
    return members[0] if members[0] in states else coordinator


def music_queue():
    """Brani nella coda del Sonos che guida la musica: la playlist in onda (una radio non ne ha)."""
    try:
        states = {item.get("entity_id"): item for item in ha.states()}
    except Exception as error:
        return {"tracks": [], "position": None, "error": f"Sonos non leggibili: {error}"}
    coordinator = music_coordinator_now(states)
    if not coordinator:
        return {"tracks": [], "position": None, "error": "Nessuna musica in onda"}
    attrs = states.get(coordinator, {}).get("attributes") or {}
    try:
        response = ha.service_response("sonos", "get_queue", {"entity_id": coordinator})
    except Exception as error:
        return {"coordinator": coordinator, "tracks": [], "position": None,
                "error": f"Home Assistant non dà l'elenco dei brani (serve Home Assistant 2024.12 o più recente): {error}"}
    items = response.get(coordinator) if isinstance(response, dict) else None
    if items is None and isinstance(response, dict) and len(response) == 1:
        items = next(iter(response.values()))
    # Home Assistant restituisce media_title, media_artist e media_album_name (prima si leggeva «title»
    # e tutti i brani risultavano senza titolo).
    tracks = [{"title": str(item.get("media_title") or item.get("title") or ""),
               "artist": str(item.get("media_artist") or item.get("artist") or ""),
               "album": str(item.get("media_album_name") or item.get("album") or "")}
              for item in (items or []) if isinstance(item, dict)][:1000]
    position = attrs.get("queue_position")
    if not isinstance(position, int) or isinstance(position, bool) or not 0 <= position < len(tracks):
        position = None
    return {"coordinator": coordinator, "tracks": tracks, "position": position}


def resume_playlist_position(coordinator, selection):
    """La playlist appena caricata riparte dal brano (e dal secondo) in cui era stata lasciata."""
    key = playlist_key(selection)
    with PLAYLIST_POSITIONS_LOCK:
        saved = dict(PLAYLIST_POSITIONS.get(key) or {}) if key else {}
    index, position = int(saved.get("index") or 0), int(saved.get("position") or 0)
    if index <= 0 and position < 5:
        return False
    name = selection.get("name") or selection.get("title") or key
    try:
        sonos_service("sonos", "play_queue", {"entity_id": coordinator, "queue_position": index})
        if position >= 5:
            ha.service("media_player", "media_seek", {"entity_id": coordinator, "seek_position": position})
    except Exception as error:
        store.log("warning", f"Playlist «{name}» ripartita dall'inizio: non riesco a riprenderla ({error})")
        return False
    # Usato: se più avanti la stessa playlist riparte (es. finita), non si torna di nuovo a questo punto.
    with PLAYLIST_POSITIONS_LOCK:
        PLAYLIST_POSITIONS.pop(key, None)
        _save_playlist_positions()
    track = f" («{saved['title']}»)" if saved.get("title") else ""
    store.log("info", f"Playlist «{name}» ripresa dal brano {index + 1}{track}")
    return True


def _save_manual_volumes():
    try:
        SONOS_MANUAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = SONOS_MANUAL_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps({p: list(v) for p, v in SONOS_MANUAL_VOLUMES.items()}), encoding="utf-8")
        os.replace(temporary, SONOS_MANUAL_FILE)
    except OSError as error:
        store.log("warning", f"Volumi scelti a mano non salvati: {error}")


def set_manual_sonos_volume(player, level):
    level = max(0.0, min(float(level), 1.0))
    with SONOS_VOLUME_MEMORY_LOCK:
        SONOS_MANUAL_VOLUMES[player] = (level, datetime.now().date().isoformat())
        SONOS_VOLUME_MEMORY[player] = level
        SONOS_LAST_SET[player] = level
        SONOS_MANUAL_CANDIDATES.pop(player, None)
        _save_manual_volumes()


def manual_sonos_volumes(players):
    """Volumi scelti a mano oggi per questi Sonos."""
    today = datetime.now().date().isoformat()
    with SONOS_VOLUME_MEMORY_LOCK:
        return {player: SONOS_MANUAL_VOLUMES[player][0] for player in dict.fromkeys(players)
                if player in SONOS_MANUAL_VOLUMES and SONOS_MANUAL_VOLUMES[player][1] == today}


def detect_manual_sonos_volumes(live_levels):
    """Volume cambiato fuori dall'add-on (es. app Sonos): se resta uguale per due controlli di fila
    e diverso da quello impostato dall'add-on, diventa il volume scelto a mano per oggi."""
    changed = []
    with SONOS_VOLUME_MEMORY_LOCK:
        for player, level in (live_levels or {}).items():
            last = SONOS_LAST_SET.get(player)
            # Subito dopo un cambio fatto dall'add-on (dissolvenze, nuova fascia) i volumi letti possono
            # essere a metà strada: non vanno scambiati per una scelta fatta a mano.
            recent = time.monotonic() - SONOS_LAST_SET_AT.get(player, -1e9) < 30
            if last is None or recent or abs(level - last) < 0.02:
                SONOS_MANUAL_CANDIDATES.pop(player, None)
                continue
            candidate = SONOS_MANUAL_CANDIDATES.get(player)
            if candidate is not None and abs(candidate - level) < 0.01:
                SONOS_MANUAL_CANDIDATES.pop(player, None)
                changed.append((player, level))
            else:
                SONOS_MANUAL_CANDIDATES[player] = level
    for player, level in changed:
        set_manual_sonos_volume(player, level)
        store.log("info", f"Volume di {player.removeprefix('media_player.')} cambiato a mano ({round(level * 100)}): "
                          "lo mantengo per oggi")
    return changed


def remembered_sonos_volumes(players, fallback):
    with SONOS_VOLUME_MEMORY_LOCK:
        return {player: SONOS_VOLUME_MEMORY.get(player, fallback) for player in dict.fromkeys(players)}


def sonos_volume_levels(players, fallback=0.25):
    """Legge i volumi correnti; se HA non li riporta usa l'ultimo volume noto di quel Sonos."""
    fallback = max(0.0, min(float(fallback), 1.0))
    try:
        states = {item.get("entity_id"): item for item in ha.states()}
    except Exception as error:
        store.log("warning", f"Volume Sonos non leggibile: {error}")
        states = {}
    levels = {}
    for player in dict.fromkeys(players):
        with SONOS_VOLUME_MEMORY_LOCK:
            known = SONOS_VOLUME_MEMORY.get(player, fallback)
        raw = states.get(player, {}).get("attributes", {}).get("volume_level", known)
        try:
            levels[player] = max(0.0, min(float(raw), 1.0))
        except (TypeError, ValueError):
            levels[player] = fallback
    return levels


def sonos_service(domain, name, payload, retries=None):
    """Comando Sonos che, se il diffusore sta ancora cambiando sorgente (UPnP 701), aspetta e riprova."""
    retries = max(1, int(retries or SONOS_SOURCE_RETRIES))
    for attempt in range(retries):
        try:
            return ha.service(domain, name, payload)
        except Exception as error:
            if not sonos_transition_busy(error) or attempt + 1 >= retries:
                raise
            store.log("warning", f"Sonos ancora occupato ({name}): attendo e riprovo")
            time.sleep(1.5 * (attempt + 1))


def set_sonos_volume_levels(levels):
    """Imposta anche volumi diversi senza perdere il bilanciamento tra le sale."""
    grouped = {}
    for player, level in levels.items():
        grouped.setdefault(round(max(0.0, min(float(level), 1.0)), 3), []).append(player)
    for level, players in grouped.items():
        sonos_service_each("media_player", "volume_set", {
            "entity_id": players,
            "volume_level": level,
        }, call=sonos_service)
        # Ultimo volume dato dall'add-on: serve a riconoscere quello cambiato a mano dall'app Sonos.
        with SONOS_VOLUME_MEMORY_LOCK:
            for player in players:
                SONOS_LAST_SET[player] = level
                SONOS_LAST_SET_AT[player] = time.monotonic()


def fade_sonos(players, target, seconds, start_levels=None):
    """Dissolvenza lineare corta e stabile, mantenendo i volumi relativi dei Sonos."""
    players = available_sonos_players(players)
    if not players:
        return
    seconds = max(0.0, min(float(seconds or 0), 15.0))
    if isinstance(target, dict):
        targets = {player: max(0.0, min(float(target.get(player, 0)), 1.0)) for player in players}
    else:
        targets = {player: max(0.0, min(float(target), 1.0)) for player in players}
    starts = start_levels or sonos_volume_levels(players, next(iter(targets.values()), 0.25))
    if seconds <= 0:
        set_sonos_volume_levels(targets)
        return
    for step in range(1, SONOS_FADE_STEPS + 1):
        progress = step / SONOS_FADE_STEPS
        set_sonos_volume_levels({
            player: starts.get(player, targets[player]) +
            (targets[player] - starts.get(player, targets[player])) * progress
            for player in players
        })
        if step < SONOS_FADE_STEPS:
            time.sleep(seconds / SONOS_FADE_STEPS)


def sonos_request_timed_out(error):
    return isinstance(error, TimeoutError) or "timed out" in str(error).lower()


def wait_sonos_groups(groups, seconds=None):
    """Aspetta che i gruppi Sonos tornino come prima (Home Assistant sta ancora eseguendo il ripristino)."""
    seconds = SONOS_RESTORE_WAIT_SECONDS if seconds is None else seconds
    for _second in range(seconds):
        try:
            states = {item.get("entity_id"): item for item in ha.states()}
        except Exception:
            states = {}
        ready = True
        for coordinator, members in (groups or {}).items():
            current = set(states.get(coordinator, {}).get("attributes", {}).get("group_members") or [coordinator])
            reachable = {m for m in members if states.get(m, {}).get("state") not in (None, "unavailable")}
            if not reachable.issubset(current):
                ready = False
        if ready:
            return True
        time.sleep(1)
    return False


def restore_sonos_playback(snapshot_targets, resume_targets, selected_players=None, volume_levels=None,
                           transition_seconds=0, groups=None):
    """Ripristina gruppi/coda e riavvia la musica che proveniva direttamente da Sonos."""
    last_error = None
    for attempt in range(SONOS_RESTORE_RETRIES):
        try:
            # Con un Sonos spento il ripristino di tutti fallirebbe: si ripristinano gli altri.
            # Se Home Assistant è solo lento (timeout) non si ripete: vedi sotto.
            sonos_service_each("sonos", "restore", {
                "entity_id": snapshot_targets,
                "with_group": True,
            }, split_on_timeout=False)
            last_error = None
            break
        except Exception as error:
            if sonos_request_timed_out(error):
                # Home Assistant continua il ripristino anche se non risponde in tempo: ripeterlo
                # raddoppiava l'attesa (circa 35 secondi di silenzio dopo lo spot). Si aspetta che finisca.
                store.log("info", "Ripristino Sonos lento: attendo che Home Assistant lo completi")
                wait_sonos_groups(groups or {target: [target] for target in snapshot_targets})
                last_error = None
                break
            last_error = error
            store.log("warning", f"Ripristino Sonos, tentativo {attempt + 1}/{SONOS_RESTORE_RETRIES}: {error}")
            if attempt + 1 < SONOS_RESTORE_RETRIES:
                time.sleep(0.75)
    if last_error:
        raise last_error
    if SONOS_RESTORE_SETTLE_SECONDS > 0:
        time.sleep(SONOS_RESTORE_SETTLE_SECONDS)
    if not resume_targets and volume_levels:
        # Nessuna musica da riprendere: i Sonos tornano comunque al volume di prima, non a quello dello spot.
        set_sonos_volume_levels(volume_levels)
    if resume_targets:
        selected_players = list(dict.fromkeys(selected_players or snapshot_targets))
        if volume_levels and transition_seconds > 0:
            set_sonos_volume_levels({player: SONOS_QUIET_VOLUME for player in selected_players})
        elif volume_levels:
            set_sonos_volume_levels(volume_levels)
        sonos_service_each("media_player", "media_play", {"entity_id": resume_targets})
        if volume_levels and transition_seconds > 0:
            fade_sonos(
                selected_players,
                volume_levels,
                transition_seconds,
                {player: SONOS_QUIET_VOLUME for player in selected_players},
            )
        store.log("info", f"Ripresa forzata della musica Sonos su {len(resume_targets)} gruppo/i")


def play_audio(filename=None, manual=False, players=None):
    cfg = store.config["audio"]
    # Una prova da una singola zona può indicare i suoi Sonos senza cambiare la configurazione.
    if players:
        players = list(players)
    else:
        # Zone in cui oggi gli operatori hanno tolto gli spot.
        excluded = spots_excluded_players()
        players = [player for player in cfg.get("players", []) if player not in excluded]
        if not players and cfg.get("players"):
            raise RuntimeError("Spot tolti per oggi in tutte le zone")
    ads = active_ads(cfg)
    if not players:
        raise RuntimeError("Nessun altoparlante selezionato")
    automatically_selected = not filename
    if automatically_selected:
        filename = scheduler.pick_audio(ads, cfg.get("mode", "rotate"))
    if not filename:
        raise RuntimeError("Nessuno spot audio configurato")
    # Lo storico deve indicare lo spot scelto anche se la trasmissione fallisce dopo.
    scheduler.last_spot_attempt = filename

    players = available_sonos_players(players)
    if not players:
        raise RuntimeError("Nessun altoparlante Sonos disponibile tra quelli selezionati")
    repetitions = max(1, min(int(cfg.get("repeat_count", 1)), 10))
    if not manual:
        # La programmazione automatica deve trasmettere un solo spot a ogni
        # intervallo. Con più file, pick_audio avanza al successivo e ricomincia
        # dal primo soltanto dopo aver completato l'intera rotazione.
        if automatically_selected:
            repetitions = 1
        remaining_today = max(0, int(cfg.get("daily_limit", 20)) - scheduler.audio_today())
        repetitions = min(repetitions, remaining_today)
        if repetitions < 1:
            raise RuntimeError("Limite giornaliero degli spot raggiunto")
    gap = max(0, min(int(cfg.get("repeat_gap_seconds", 5)), 600))
    duration = probe_media_duration(MEDIA_DIR / safe_name(filename))
    volume = max(1, min(int(cfg.get("volume", 35)), 100))
    transition = max(0.0, min(float(cfg.get("transition_seconds", 3)), 10.0))

    if not audio_playback_lock.acquire(blocking=False):
        raise RuntimeError("Uno spot Sonos è già in riproduzione")
    audio_stop_event.clear()
    snapshot_created = False
    snapshot_targets = list(players)
    original_groups = {}
    resume_targets = []
    stopped = False
    previous_volumes = sonos_volume_levels(players, 0.25)
    remember_sonos_volumes(previous_volumes)
    try:
        # La funzione Sonos announce avvia un AudioClip indipendente su ogni
        # diffusore e non garantisce la sincronizzazione. Salviamo quindi lo
        # stato dei soli Sonos scelti, li isoliamo e riproduciamo un unico
        # normale flusso sul coordinatore del gruppo temporaneo.
        snapshot_targets, resume_targets = sonos_playback_context(players)
        original_groups = sonos_group_topology(players)
        sonos_service_each("sonos", "snapshot", {
            "entity_id": snapshot_targets,
            "with_group": True,
        })
        snapshot_created = True
        fade_sonos(players, SONOS_QUIET_VOLUME, transition, previous_volumes)
        coordinator = sonos_group_ready(players, original_groups)
        if not coordinator:
            sonos_service_each("media_player", "unjoin", {"entity_id": players})
            if SONOS_GROUP_SETTLE_SECONDS > 0:
                time.sleep(SONOS_GROUP_SETTLE_SECONDS)
            coordinator = prepare_sonos_group(players, force=True)
        # I Sonos sono già a volume minimo dopo la dissolvenza: lo spot parte subito al suo volume.
        quiet_levels = {player: SONOS_QUIET_VOLUME for player in players}
        spot_levels = {player: volume / 100 for player in players}

        for index in range(repetitions):
            if audio_stop_event.is_set():
                stopped = True
                break
            # La dissolvenza riguarda la musica precedente, non lo spot: il
            # messaggio deve partire subito al volume impostato e deve restare
            # integro fino all'ultima sillaba.
            set_sonos_volume_levels(spot_levels)
            ha.service("media_player", "play_media", {
                "entity_id": coordinator,
                "media_content_type": "music",
                "media_content_id": media_url(filename),
            })
            scheduler.record_audio_play()
            history_id = spot_history.add(filename, players, manual, duration=duration)
            store.log(
                "success",
                f"Spot sincronizzato su {len(players)} Sonos selezionati: "
                f"{filename} ({index + 1}/{repetitions})",
            )
            # Il piccolo margine compensa il buffering iniziale dei Sonos. In
            # precedenza la dissolvenza iniziava prima della fine e tagliava la
            # traccia; ora il ripristino parte solo dopo la durata completa.
            if audio_stop_event.wait(duration + SONOS_SPOT_TAIL_GUARD_SECONDS):
                spot_history.update(history_id, status="stopped")
                stopped = True
                break
            set_sonos_volume_levels(quiet_levels)
            if index + 1 < repetitions and gap and audio_stop_event.wait(gap):
                stopped = True
                break
    finally:
        if snapshot_created:
            try:
                restore_sonos_playback(
                    snapshot_targets,
                    resume_targets,
                    players,
                    previous_volumes,
                    transition,
                    groups=original_groups,
                )
                repaired, unavailable = repair_sonos_topology(original_groups, previous_volumes)
                if repaired:
                    store.log("warning", "Sonos riagganciati dopo lo spot: " + ", ".join(repaired))
                if unavailable:
                    store.log("warning", "Sonos non raggiungibili dopo lo spot: " + ", ".join(unavailable))
                store.log("info", f"Musica e gruppi ripristinati su {len(players)} Sonos selezionati")
            except Exception as error:
                store.log("error", f"Ripristino Sonos non riuscito: {error}")
        audio_playback_lock.release()
        if stopped:
            store.log("info", "Spot fermato manualmente; riproduzione precedente ripristinata")

    return filename


def stop_audio():
    """Interrompe lo spot corrente; il thread di riproduzione ripristina lo snapshot Sonos."""
    if not audio_playback_lock.locked():
        store.log("info", "Nessuno spot Sonos attivo da fermare")
        return False
    audio_stop_event.set()
    store.log("info", "Richiesto arresto immediato dello spot Sonos")
    return True

def sonos_transition_busy(error):
    """Riconosce il rifiuto temporaneo di Sonos durante un cambio sorgente."""
    message = str(error).lower()
    return "701" in message and "transition not available" in message


def play_sonos_media(payload, retries=None):
    """Avvia una sorgente Sonos e gestisce la transizione UPnP 701 ancora occupata."""
    retries = max(1, int(retries or SONOS_SOURCE_RETRIES))
    last_error = None
    for attempt in range(retries):
        try:
            ha.service("media_player", "play_media", payload)
            return
        except Exception as error:
            last_error = error
            if not sonos_transition_busy(error) or attempt + 1 >= retries:
                raise
            store.log("warning", "Sonos ancora occupato nel cambio sorgente: nuovo tentativo controllato")
            try:
                ha.service("media_player", "media_stop", {"entity_id": payload["entity_id"]})
            except Exception as stop_error:
                # Anche l'arresto può rispondere 701 mentre Sonos cambia sorgente: si aspetta e si riprova.
                store.log("warning", f"Arresto transizione Sonos non riuscito: {stop_error}")
            time.sleep(1.5 * (attempt + 1))
    raise last_error


def sonos_source_rejected(error):
    """Errore che non passa riprovando: Sonos non accetta la sorgente indicata."""
    message = str(error).lower()
    return ("402" in message and "invalid args" in message) or "upnp error 714" in message         or "upnp error 711" in message or "upnp error 712" in message


def music_error_message(error, selection):
    title = (selection or {}).get("name") or (selection or {}).get("title") or (selection or {}).get("content_id", "")
    if sonos_source_rejected(error):
        return (f"Sonos non accetta la sorgente della fascia «{title}» (errore 402): nel Palinsesto "
                "scegli di nuovo la sorgente oppure cerca la radio in «Radio da Internet»")
    return str(error)


def matching_sonos_source(entity_id, selection):
    """Nome della sorgente Sonos (Preferiti) che corrisponde alla fascia, se esiste."""
    try:
        item = next((item for item in ha.states() if item.get("entity_id") == entity_id), {})
    except Exception:
        return None
    sources = {str(source).strip().lower(): str(source) for source in item.get("attributes", {}).get("source_list") or []}
    for wanted in (selection.get("name"), selection.get("title"), selection.get("content_id")):
        found = sources.get(str(wanted or "").strip().lower())
        if found:
            return found
    return None


# Radio da Internet: archivio pubblico e gratuito Radio Browser (radio-browser.info), senza chiavi.
# Serve quando Sonos rifiuta una radio dei Preferiti (es. TuneIn con errore 402): i Sonos
# suonano direttamente l'indirizzo della trasmissione.
RADIO_BROWSER_API = os.environ.get("CARELLAS_RADIO_BROWSER_API", "https://all.api.radio-browser.info").rstrip("/")
RADIO_STATION_CACHE = {}
RADIO_STATION_CACHE_SECONDS = 24 * 60 * 60


def radio_browser_search(query, limit=8):
    query = re.sub(r"\s+", " ", str(query or "")).strip()[:80]
    if len(query) < 2:
        raise RuntimeError("Scrivi almeno due lettere del nome della radio")
    params = urllib.parse.urlencode({
        "name": query, "limit": str(max(1, min(int(limit), 20))), "hidebroken": "true",
        "order": "clickcount", "reverse": "true",
    })
    request = urllib.request.Request(f"{RADIO_BROWSER_API}/json/stations/search?{params}",
                                     headers={"User-Agent": "CarellasMediaAds", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read() or b"[]")
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise RuntimeError(f"Archivio delle radio non raggiungibile: {getattr(error, 'reason', error)}") from None
    stations, seen = [], set()
    for item in data if isinstance(data, list) else []:
        url = str(item.get("url_resolved") or item.get("url") or "").strip()
        name = re.sub(r"\s+", " ", str(item.get("name") or "")).strip()[:120]
        if not name or not re.match(r"^https?://", url) or url in seen:
            continue
        seen.add(url)
        stations.append({
            "name": name, "url": url, "codec": str(item.get("codec") or "")[:12],
            "bitrate": int(item.get("bitrate") or 0), "country": str(item.get("countrycode") or "")[:4],
        })
    return stations


def sonos_radio_uris(url):
    """Indirizzi da provare: con «x-rincon-mp3radio» Sonos tratta il flusso come una radio."""
    plain = str(url)
    rincon = "x-rincon-mp3radio://" + plain.split("://", 1)[1]
    return [rincon, plain] if plain.startswith("http://") else [plain, rincon]


def play_radio_url(entity_id, url):
    last_error = None
    for uri in sonos_radio_uris(url):
        try:
            play_sonos_media({"entity_id": entity_id, "media_content_type": "music", "media_content_id": uri})
            return
        except Exception as error:
            last_error = error
    raise last_error


def slot_looks_like_radio(selection):
    content_id = str(selection.get("content_id") or "")
    if selection.get("content_type") in ("playlist", "sonos_playlist_source") or re.match(r"^(S:|SQ:)", content_id):
        return False
    title = str(selection.get("title") or selection.get("name") or "")
    # Un Preferito può essere anche una playlist: si cerca una radio solo se il nome lo dice.
    return selection.get("content_type") == "sonos_radio_source" \
        or bool(re.search(r"radio|\bfm\b|\d{2,3}[.,]\d", title, re.I))


def internet_radio_station(selection):
    """Stazione di Radio Browser con lo stesso nome della radio della fascia (controllata, in memoria per un giorno)."""
    title = re.sub(r"\s+", " ", str(selection.get("title") or selection.get("name") or "")).strip()
    if not title or not slot_looks_like_radio(selection):
        return None
    key = title.lower()
    cached = RADIO_STATION_CACHE.get(key)
    if cached and time.time() - cached[0] < RADIO_STATION_CACHE_SECONDS:
        return cached[1]
    best, best_score = None, 0.0
    shorter = re.sub(r"\s+(fm|radio|live|italia)$", "", re.sub(r"^radio\s+", "", title, flags=re.I), flags=re.I)
    queries = list(dict.fromkeys([title, re.sub(r"^radio\s+", "", title, flags=re.I), shorter]))
    targets = {query.lower() for query in queries}
    for query in queries:
        try:
            stations = radio_browser_search(query, limit=20)
        except RuntimeError as error:
            store.log("warning", str(error))
            continue
        for station in stations:
            # «RTL 102.5 [Mobile 48kbps]» è la stessa radio di «RTL 102.5 FM»; «RTL 102.5 Best» no.
            name = re.sub(r"\s+", " ", re.sub(r"[\[(].*?[\])]", "", station["name"])).strip().lower()
            score = difflib.SequenceMatcher(None, key, name).ratio()
            if name in targets:
                score = 1.0
            elif key in name or name in key:
                score = max(score, 0.9)
            # I flussi HLS (.m3u8) funzionano male sui Sonos: meglio un flusso MP3/AAC classico.
            if ".m3u8" in station["url"].lower() or "hls" in station["codec"].lower():
                score -= 0.3
            # A parità di nome, il flusso con qualità migliore.
            score += min(station["bitrate"], 320) / 100000
            if score > best_score:
                best, best_score = station, score
        # Nome identico trovato: inutile cercare ancora.
        if best_score >= 1.0:
            break
    station = best if best_score >= 0.75 else None
    RADIO_STATION_CACHE[key] = (time.time(), station)
    return station


def play_rejected_source_elsewhere(coordinator, selection, error):
    """Sonos rifiuta la sorgente della fascia: prima il Preferito con lo stesso nome, poi la radio da Internet."""
    title = selection.get("title") or selection.get("name") or selection.get("content_id", "")
    if selection.get("content_type") not in ("sonos_radio_source", "sonos_source", "sonos_playlist_source"):
        source = matching_sonos_source(coordinator, selection)
        if source:
            store.log("warning", f"Sonos ha rifiutato l'ID della fascia, provo il Preferito «{source}»")
            try:
                select_sonos_source(coordinator, source)
                return
            except Exception as source_error:
                if not sonos_source_rejected(source_error):
                    raise
    station = internet_radio_station(selection)
    if not station:
        raise error
    store.log("warning", f"Sonos non suona «{title}» dai Preferiti: la avvio come radio da Internet ({station['name']})")
    play_radio_url(coordinator, station["url"])


def sonos_state(entity_id):
    """Stato del Sonos in Home Assistant, oppure None se non è leggibile."""
    try:
        item = next((item for item in ha.states() if item.get("entity_id") == entity_id), None)
    except Exception:
        return None
    if not item or item.get("state") in (None, "unavailable", "unknown"):
        return None
    return item.get("state")


def sonos_playing(entity_id):
    state = sonos_state(entity_id)
    # Stato non leggibile: non si può verificare, si considera partito.
    if state is None:
        return None
    return state in ("playing", "buffering")


class SonosNotStarted(RuntimeError):
    """Il Sonos ha accettato il comando ma non risulta in riproduzione (es. playlist lunga ancora in caricamento)."""

    def __init__(self, message, players):
        super().__init__(message)
        self.players = list(players)


def ensure_sonos_playing(entity_id, checks=None):
    """Dopo il comando controlla che il Sonos suoni davvero: se è fermo prova a ripartire con play."""
    checks = SONOS_START_CHECK_SECONDS if checks is None else checks
    stable = 0
    for second in range(checks):
        # Subito dopo il comando Home Assistant può riportare ancora lo stato della musica di prima:
        # si guarda dopo qualche secondo e serve «playing» per due controlli di fila. Prima bastava
        # «playing» o «buffering» al primo istante e una fascia mai partita sembrava in onda.
        state = sonos_state(entity_id)
        if state is None:
            return True
        if second < SONOS_START_SETTLE_SECONDS:
            time.sleep(1)
            continue
        stable = stable + 1 if state == "playing" else 0
        if stable >= 2:
            return True
        if second in (5, 10, 16, 22) and state != "playing":
            try:
                ha.service("media_player", "media_play", {"entity_id": entity_id})
            except Exception as error:
                if not sonos_transition_busy(error):
                    store.log("warning", f"Ripartenza Sonos non riuscita: {error}")
        time.sleep(1)
    return checks == 0 or sonos_state(entity_id) in (None, "playing")


def select_sonos_source(entity_id, source, retries=None):
    """Seleziona una sorgente nativa Sonos con recupero dall'errore UPnP 701."""
    retries = max(1, int(retries or SONOS_SOURCE_RETRIES))
    last_error = None
    for attempt in range(retries):
        try:
            ha.service("media_player", "select_source", {
                "entity_id": entity_id,
                "source": source,
            })
            ha.service("media_player", "media_play", {"entity_id": entity_id})
            return
        except Exception as error:
            last_error = error
            if not sonos_transition_busy(error) or attempt + 1 >= retries:
                raise
            store.log("warning", "Sonos ancora occupato nella selezione della sorgente: attendo e riprovo")
            try:
                ha.service("media_player", "media_stop", {"entity_id": entity_id})
            except Exception as stop_error:
                store.log("warning", f"Arresto transizione Sonos non riuscito: {stop_error}")
            time.sleep(1.5 * (attempt + 1))
    raise last_error


def start_music(selection=None, fade_in=False):
    cfg = store.config["music"]
    selection = selection or cfg
    players = available_sonos_players(selection.get("players") or cfg.get("players") or [])
    # Le zone in cui gli operatori hanno tolto la musica restano fuori dal gruppo.
    excluded = music_excluded_players()
    if excluded and players:
        players = [player for player in players if player not in excluded]
        if not players:
            raise RuntimeError("Musica tolta dagli operatori in tutte le zone di questa fascia")
    content_id = selection.get("content_id")
    if not players or not content_id:
        raise RuntimeError("Configurazione musica incompleta")
    coordinator = prepare_sonos_group(players)
    # Il coordinatore (la cassa principale, se scelta) va per primo: lo usano i controlli del gruppo.
    players = [coordinator] + [player for player in players if player != coordinator]
    slot_volume = max(0, min(int(selection.get("volume", cfg.get("volume", 25))), 100)) / 100
    # Ogni Sonos con un volume scelto a mano oggi lo mantiene; gli altri prendono il volume della fascia.
    manual = manual_sonos_volumes(players)
    target_volume = {player: manual.get(player, slot_volume) for player in players}
    transition = max(0.0, min(float(cfg.get("transition_seconds", 3)), 10.0)) if fade_in else 0
    previous_levels = sonos_volume_levels(players, slot_volume)
    start_level = SONOS_QUIET_VOLUME if transition > 0 else None
    set_sonos_volume_levels({player: start_level if start_level is not None else target_volume[player]
                             for player in players})
    if manual:
        store.log("info", "Volumi scelti a mano mantenuti: " + ", ".join(
            f"{player.removeprefix('media_player.')} {round(level * 100)}" for player, level in manual.items()))
    content_type = selection.get("content_type", "music")
    payload = {
        "entity_id": coordinator,
        "media_content_type": content_type,
        "media_content_id": content_id,
    }
    try:
        try:
            if content_type == "radio_url":
                # Radio da Internet scelta nel Palinsesto: indirizzo diretto della trasmissione.
                play_radio_url(coordinator, content_id)
            elif content_type == "sonos_radio_source":
                select_sonos_source(coordinator, content_id)
            elif content_type == "sonos_playlist_source":
                # Le playlist Sonos vengono riprodotte in modo più affidabile con
                # play_media. Alcune versioni espongono invece la stessa voce come
                # source: in quel caso usiamo select_source come ripiego.
                try:
                    play_sonos_media({
                        "entity_id": coordinator,
                        "media_content_type": "playlist",
                        "media_content_id": content_id,
                    })
                except Exception as playlist_error:
                    store.log("warning", f"Playlist Sonos non avviata direttamente, provo come sorgente: {playlist_error}")
                    select_sonos_source(coordinator, content_id)
            elif content_type == "sonos_source":
                # Compatibilita con le configurazioni delle versioni precedenti.
                try:
                    select_sonos_source(coordinator, content_id)
                except Exception as source_error:
                    store.log("warning", f"Sorgente Sonos precedente non selezionabile, provo come playlist: {source_error}")
                    play_sonos_media({
                        "entity_id": coordinator,
                        "media_content_type": "playlist",
                        "media_content_id": content_id,
                    })
            else:
                play_sonos_media(payload)
        except Exception as play_error:
            # Sonos rifiuta la sorgente (es. 402 Invalid Args con le radio TuneIn dei Preferiti):
            # si prova il Preferito con lo stesso nome e poi la stessa radio da Internet.
            if content_type == "radio_url" or not sonos_source_rejected(play_error):
                raise
            play_rejected_source_elsewhere(coordinator, selection, play_error)
        resume_playlist_position(coordinator, selection)
        title = selection.get("name") or selection.get("title") or content_id
        if not ensure_sonos_playing(coordinator):
            raise SonosNotStarted(f"I Sonos non sono partiti con «{title}»: riprovo tra poco", players)
    except Exception:
        # Se il cambio non riesce, non lasciare i diffusori muti: prova a
        # riprendere il flusso che Sonos stava già eseguendo e il suo volume.
        try:
            ha.service("media_player", "media_play", {"entity_id": coordinator})
            if transition > 0:
                fade_sonos(
                    players,
                    previous_levels,
                    transition,
                    {player: SONOS_QUIET_VOLUME for player in players},
                )
            else:
                set_sonos_volume_levels(previous_levels)
        except Exception as restore_error:
            store.log("error", f"Ripresa musica dopo cambio fallito non riuscita: {restore_error}")
        raise
    if transition > 0:
        fade_sonos(
            players,
            target_volume,
            transition,
            {player: SONOS_QUIET_VOLUME for player in players},
        )
    title = selection.get("name") or selection.get("title") or content_id
    store.log("success", f"Musica del locale avviata sul gruppo Sonos sincronizzato: {title}")
    return players


def stop_music(players=None, fade_seconds=0):
    players = list(dict.fromkeys(players or store.config["music"].get("players", [])))
    if players:
        previous_levels = {}
        if fade_seconds:
            previous_levels = sonos_volume_levels(available_sonos_players(players))
            fade_sonos(players, SONOS_QUIET_VOLUME, fade_seconds, previous_levels)
        sonos_service_each("media_player", "media_stop", {"entity_id": players})
        if previous_levels:
            # Dopo la dissolvenza i Sonos resterebbero all'1%: a musica ferma
            # riportiamo il volume di prima, così l'app Sonos non parte muta.
            try:
                set_sonos_volume_levels(previous_levels)
            except Exception as error:
                store.log("warning", f"Volume Sonos non ripristinato dopo l'arresto: {error}")
        store.log("info", "Musica del locale arrestata dalla programmazione")


def play_tv_item(item):
    player = store.config["tv"].get("player")
    if not player:
        raise RuntimeError("Nessuna TV selezionata")
    filename = item.get("name")
    kind = item.get("kind") or "video"
    if not filename:
        raise RuntimeError("Contenuto TV non valido")
    ha.service("media_player", "play_media", {
        "entity_id": player,
        "media_content_type": kind,
        "media_content_id": media_url(filename),
    })
    store.log("success", f"Pubblicità TV avviata: {filename}")


# Menu sullo schermo diviso: sopra gira la pubblicità del canale, sotto restano
# fisse fino a tre pagine di menu (scritte nel pannello oppure prese da un PDF).
MENU_FONT = os.environ.get("CARELLAS_MENU_FONT", "")
MENU_FONT_BOLD = os.environ.get("CARELLAS_MENU_FONT_BOLD", "")
MENU_FONT_CANDIDATES = [
    "/usr/share/fonts/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/TTF/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "C:/Windows/Fonts/georgia.ttf",
]
MENU_FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "C:/Windows/Fonts/georgiab.ttf",
]
# Colori del logo Carellas: verde oliva e verde scuro su carta crema.
MENU_COLORS = {"page": "0xFBF7EE", "ink": "0x2F3A26", "accent": "0x6E7F1D", "background": "0x2F3A26"}
MENU_PRICE = re.compile(r"^(?P<name>.*?)[\s.·…_|:-]*\s(?P<price>(?:€\s?)?\d{1,4}(?:[.,]\d{1,2})?(?:\s?€)?)$")
MENU_MAX_PDF = 50 * 1024 * 1024
# Sullo schermo stanno tre pagine alla volta; oltre, i gruppi da tre si alternano a tempo.
MENU_MAX_PAGES = 12
# Logo Carellas Ristorante (PNG trasparente) usato quando non se ne sceglie un altro.
MENU_DEFAULT_LOGO = APP_DIR / "menu_logo.png"


def menu_settings(channel):
    """Impostazioni del menu di un canale, ripulite e con valori sicuri."""
    raw = channel.get("menu") or {}
    try:
        ad_percent = int(raw.get("ad_percent", 60))
    except (TypeError, ValueError):
        ad_percent = 60
    pages = []
    for page in (raw.get("pages") or [])[:MENU_MAX_PAGES]:
        if not isinstance(page, dict):
            continue
        title = str(page.get("title", "")).strip()[:40]
        text = str(page.get("text", ""))[:3000]
        if title or text.strip():
            pages.append({"title": title, "text": text})
    images = [safe_name(name) for name in (raw.get("page_images") or [])[:MENU_MAX_PAGES] if name]
    try:
        page_seconds = int(raw.get("page_seconds", 15))
    except (TypeError, ValueError):
        page_seconds = 15
    return {
        "enabled": raw.get("enabled") is True,
        "ad_percent": max(30, min(ad_percent, 80)),
        "mode": "images" if raw.get("mode") == "images" else "text",
        "pages": pages,
        "page_images": images,
        "page_seconds": max(5, min(page_seconds, 120)),
        "background": safe_name(raw["background"]) if raw.get("background") else "",
        # "" = logo Carellas predefinito, "none" = nessun logo, altrimenti un'immagine della libreria.
        "logo": "none" if raw.get("logo") == "none" else (safe_name(raw["logo"]) if raw.get("logo") else ""),
        # Il logo come sfondo sbiadito di ogni pagina, in alto, oppure entrambi.
        "logo_mode": raw.get("logo_mode") if raw.get("logo_mode") in {"background", "top", "both"} else "background",
        # La pubblicità riempie tutta la larghezza (taglia sopra e sotto), oppure resta intera.
        "ad_fit": raw.get("ad_fit") if raw.get("ad_fit") in {"cover", "smart", "contain"} else "cover",
    }


def parse_menu_line(line):
    """'Bruschetta 6,50' -> piatto e prezzo; una riga senza prezzo è un sottotitolo."""
    line = str(line).strip()
    if not line:
        return None
    match = MENU_PRICE.match(line)
    if match and match.group("name").strip():
        price = match.group("price").strip()
        if "€" not in price:
            price += " €"
        return {"name": match.group("name").strip(), "price": price}
    return {"name": line, "price": ""}


def menu_logo_path(menu):
    return None if menu["logo"] == "none" else (MEDIA_DIR / menu["logo"] if menu["logo"] else MENU_DEFAULT_LOGO)


def menu_title_size(title, width, height):
    """Il titolo si rimpicciolisce finché non sta nella larghezza della pagina."""
    size = max(12, min(30, int(height * 0.08)))
    while size > 10 and len(title) * size * MENU_BOLD_CHAR_WIDTH > width - 24:
        size -= 1
    return size


def menu_text_top(menu, title, width, height):
    """Spazio sopra i piatti: margine, logo in alto e titolo, come in render_menu_frame."""
    top = 10
    logo = menu_logo_path(menu)
    if logo and logo.is_file() and menu["logo_mode"] in {"top", "both"}:
        top += max(14, int(height * 0.16)) // 2 * 2 + 4
    if title:
        top += int(menu_title_size(title, width, height) * 1.35) + 8
    return top


def expand_menu_pages(menu):
    """Una pagina che non sta nel suo riquadro a una misura leggibile diventa più pagine con lo stesso titolo."""
    pages = menu["pages"] or [{"title": "Menu", "text": ""}]
    slots, result = min(3, len(pages)), pages
    # Le pagine nuove possono cambiare la larghezza dei riquadri: si ricalcola finché il numero resta stabile.
    for _ in range(4):
        _, cells = menu_layout(menu["ad_percent"], slots)
        width, height = cells[0][2], cells[0][3]
        result = []
        for page in pages:
            area = height - 12 - menu_text_top(menu, page["title"], width, height)
            chunks, current = [], []
            for line in (line for line in page["text"].splitlines() if line.strip()):
                candidate = current + [line]
                rows = menu_rows([parse_menu_line(item) for item in candidate], width - 24, MENU_MIN_READABLE)
                if current and not menu_rows_fit(rows, MENU_MIN_READABLE, area):
                    chunks.append(current)
                    current = [line]
                else:
                    current = candidate
            chunks.append(current)
            if len(chunks) == 1:
                result.append(page)
                continue
            # Le parti hanno lo stesso titolo, senza numeri: il menu continua nella pagina accanto.
            for chunk in chunks:
                result.append({"title": page["title"], "text": "\n".join(chunk)})
        if min(3, len(result)) == slots:
            break
        slots = min(3, len(result))
    return result


def menu_groups(menu):
    """Pagine divise in gruppi da tre: ogni gruppo resta sullo schermo page_seconds secondi."""
    if menu["mode"] == "images" and menu["page_images"]:
        items = menu["page_images"]
    else:
        items = expand_menu_pages(menu)
    return [items[index:index + 3] for index in range(0, len(items), 3)]


def menu_ad_filter(fit, width, height):
    """Filtro della pubblicità nella parte alta: a tutta larghezza (cover), intera con lati sfocati o intera."""
    if fit == "cover":
        return f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1[ad]"
    if fit == "smart":
        return (
            f"[0:v]split[adbg][adfg];[adbg]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=20:2[adblur];[adfg]scale={width}:{height}:force_original_aspect_ratio=decrease[adclear];"
            "[adblur][adclear]overlay=(W-w)/2:(H-h)/2,setsar=1[ad]"
        )
    return f"[0:v]scale=-2:{height},setsar=1[ad]"


def menu_cycle_command(frames, seconds, target):
    """Video corto in cui i gruppi di pagine si alternano ogni 'seconds': poi viene ripetuto sotto la pubblicità."""
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for frame in frames:
        command += ["-loop", "1", "-framerate", "25", "-t", str(seconds), "-i", str(frame)]
    joined = "".join(f"[{index}:v]" for index in range(len(frames)))
    return command + [
        "-filter_complex", f"{joined}concat=n={len(frames)}:v=1:a=0,format=yuv420p[v]", "-map", "[v]",
        "-c:v", "libx264", "-preset", "superfast", "-crf", "22", "-pix_fmt", "yuv420p", str(target),
    ]


def wrap_menu_text(text, max_chars):
    """Manda a capo tra le parole; una parola più lunga della riga viene spezzata."""
    lines, current = [], ""
    for word in str(text).split():
        while len(word) > max_chars:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:max_chars - 1] + "-")
            word = word[max_chars - 1:]
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


# Larghezza media di un carattere rispetto alla dimensione del font (stima prudente per DejaVu Serif).
MENU_CHAR_WIDTH = 0.6
MENU_BOLD_CHAR_WIDTH = 0.66
MENU_LINE_HEIGHT = 1.32


# Sotto questa misura (pixel su 720) il menu non si legge dalla sala: la pagina viene divisa.
MENU_MIN_READABLE = 12


def menu_rows(items, width, size):
    """Piatti con le righe già mandate a capo per una certa misura del testo."""
    rows = []
    for item in items:
        bold = not item["price"]
        price_room = (len(item["price"]) * size * MENU_BOLD_CHAR_WIDTH + size) if item["price"] else 0
        chars = max(4, int((width - price_room) / (size * (MENU_BOLD_CHAR_WIDTH if bold else MENU_CHAR_WIDTH))))
        rows.append((item, wrap_menu_text(item["name"], chars)))
    return rows


def menu_rows_fit(rows, size, height):
    return sum(len(lines) for _, lines in rows) * size * MENU_LINE_HEIGHT <= height


def fit_menu_items(items, width, height, max_size=22, min_size=8):
    """La dimensione più grande con cui tutti i piatti, andando a capo, stanno nella pagina."""
    rows = []
    for size in range(max_size, min_size - 1, -1):
        rows = menu_rows(items, width, size)
        if menu_rows_fit(rows, size, height):
            return size, rows
    # Anche alla misura più piccola non ci sta tutto: si mostrano le righe che entrano nella pagina.
    fitted, used = [], 0
    for item, lines in rows:
        room = int(height // (min_size * MENU_LINE_HEIGHT)) - used
        if room <= 0:
            break
        fitted.append((item, lines[:room]))
        used += len(lines[:room])
    return min_size, fitted


def menu_layout(ad_percent, page_count, width=1280, height=720, gap=12):
    """Altezza della pubblicità e riquadri delle pagine affiancate nella parte bassa."""
    ad_height = int(round(height * ad_percent / 100 / 2)) * 2
    top = ad_height + gap
    page_height = height - top - gap
    count = max(1, min(int(page_count), 3))
    page_width = (width - gap * (count + 1)) // count
    return ad_height, [(gap + index * (page_width + gap), top, page_width, page_height) for index in range(count)]


def menu_fonts():
    def first(preferred, candidates):
        for candidate in ([preferred] if preferred else []) + candidates:
            if candidate and Path(candidate).is_file():
                return Path(candidate)
        return None
    regular = first(MENU_FONT, MENU_FONT_CANDIDATES)
    bold = first(MENU_FONT_BOLD, MENU_FONT_BOLD_CANDIDATES) or regular
    if not regular:
        raise RuntimeError("Font per il menu non trovato: installa font-dejavu o imposta CARELLAS_MENU_FONT")
    return regular, bold


def render_menu_frame(menu, target, preview=False, group=0, width=1280, height=720):
    """Disegna lo sfondo 1280x720 con un gruppo di pagine del menu; la pubblicità viene sovrapposta in alto."""
    work = Path(target).parent / f".menu-{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        regular, bold = menu_fonts()
        # Font e testi stanno nella cartella di lavoro: niente percorsi da proteggere nei filtri FFmpeg.
        shutil.copyfile(regular, work / "regular.ttf")
        shutil.copyfile(bold, work / "bold.ttf")
        images_mode = menu["mode"] == "images" and menu["page_images"]
        groups = menu_groups(menu)
        group = max(0, min(int(group), len(groups) - 1))
        current = groups[group]
        # Stessa larghezza delle pagine in tutti i gruppi; un gruppo incompleto resta centrato.
        ad_height, cells = menu_layout(menu["ad_percent"], len(groups[0]), width, height)
        if len(current) < len(cells):
            step = cells[1][0] - cells[0][0]
            shift = (len(cells) - len(current)) * step // 2
            cells = [(x + shift, y, w, h) for x, y, w, h in cells[:len(current)]]
        inputs, filters = [], []
        background = MEDIA_DIR / menu["background"] if menu["background"] else None
        if background and background.is_file():
            inputs += ["-loop", "1", "-i", str(background)]
            filters.append(
                f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
                "boxblur=10:2,eq=brightness=-0.18,format=rgba[s0]"
            )
        else:
            inputs += ["-f", "lavfi", "-i", f"color=c={MENU_COLORS['background']}:s={width}x{height}"]
            filters.append("[0:v]format=rgba[s0]")
        state = {"label": "s0", "inputs": 1, "texts": 0}

        def chain(expression):
            new = f"s{len(filters)}"
            filters.append(f"[{state['label']}]{expression}[{new}]")
            state["label"] = new

        def overlay(source, x, y, scale):
            index = state["inputs"]
            state["inputs"] += 1
            inputs.extend(["-i", str(source)])
            filters.append(f"[{index}:v]{scale},format=rgba[i{index}]")
            new = f"s{len(filters)}"
            filters.append(f"[{state['label']}][i{index}]overlay=x={x}:y={y}:format=rgb[{new}]")
            state["label"] = new

        def text(value, font, size, color, x, y):
            state["texts"] += 1
            name = f"t{state['texts']}.txt"
            (work / name).write_text(value, encoding="utf-8")
            chain(f"drawtext=fontfile={font}:textfile={name}:expansion=none:fontsize={size}:fontcolor={color}:x={x}:y={y}")

        if preview:
            ad_width = width if menu["ad_fit"] in {"cover", "smart"} else ad_height * 16 // 9
            chain(f"drawbox=x={(width - ad_width) // 2}:y=0:w={ad_width}:h={ad_height}:color=0x15180F:t=fill")
            text("Pubblicità", "bold.ttf", max(18, ad_height // 8), "0xC9D3A5",
                 "(w-text_w)/2", f"{ad_height // 2}-text_h/2")
        if images_mode:
            for name, (x, y, w, h) in zip(current, cells):
                source = MEDIA_DIR / name
                if not source.is_file():
                    raise RuntimeError(f"Pagina del menu non trovata: {name}")
                overlay(source, f"{x}+({w}-overlay_w)/2", f"{y}+({h}-overlay_h)/2",
                        f"scale={w}:{h}:force_original_aspect_ratio=decrease")
        else:
            logo = menu_logo_path(menu)
            pages = current
            for index, (x, y, w, h) in enumerate(cells):
                page = pages[index] if index < len(pages) else {"title": "", "text": ""}
                chain(f"drawbox=x={x}:y={y}:w={w}:h={h}:color={MENU_COLORS['page']}:t=fill")
                chain(f"drawbox=x={x + 4}:y={y + 4}:w={w - 8}:h={h - 8}:color={MENU_COLORS['accent']}:t=2")
                cursor = y + 10
                if logo and logo.is_file() and menu["logo_mode"] in {"background", "both"}:
                    overlay(logo, f"{x}+({w}-overlay_w)/2", f"{y}+({h}-overlay_h)/2",
                            f"scale=w={int(w * 0.82)}:h={int(h * 0.82)}:force_original_aspect_ratio=decrease,"
                            "format=rgba,colorchannelmixer=aa=0.13")
                if logo and logo.is_file() and menu["logo_mode"] in {"top", "both"}:
                    logo_height = max(14, int(h * 0.16)) // 2 * 2
                    overlay(logo, f"{x}+({w}-overlay_w)/2", str(cursor),
                            f"scale=w={w - 24}:h={logo_height}:force_original_aspect_ratio=decrease")
                    cursor += logo_height + 4
                if page["title"]:
                    # Il titolo si rimpicciolisce finché non sta nella larghezza della pagina.
                    title_size = menu_title_size(page["title"], w, h)
                    text(page["title"], "bold.ttf", title_size, MENU_COLORS["accent"], f"{x}+({w}-text_w)/2", str(cursor))
                    cursor += int(title_size * 1.35)
                    chain(f"drawbox=x={x + w // 4}:y={cursor}:w={w // 2}:h=2:color={MENU_COLORS['accent']}:t=fill")
                    cursor += 8
                items = [item for item in (parse_menu_line(line) for line in page["text"].splitlines()) if item]
                if not items:
                    continue
                # Righe lunghe (descrizioni, allergeni) vanno a capo e la misura si adatta alla pagina.
                size, rows = fit_menu_items(items, w - 24, y + h - 12 - cursor)
                step = int(size * MENU_LINE_HEIGHT)
                for item, lines in rows:
                    font, color = ("regular.ttf", MENU_COLORS["ink"]) if item["price"] else ("bold.ttf", MENU_COLORS["accent"])
                    for number, line in enumerate(lines):
                        text(line, font, size, color, str(x + 12 + (6 if number else 0)), str(cursor))
                        if number == 0 and item["price"]:
                            text(item["price"], "bold.ttf", size, MENU_COLORS["ink"], f"{x + w - 12}-text_w", str(cursor))
                        cursor += step
        if len(groups) > 1:
            # Quadratini sotto le pagine, uno per schermata: il menu continua nelle schermate successive.
            dot, space = 8, 8
            start = (width - (len(groups) * dot + (len(groups) - 1) * space)) // 2
            for number in range(len(groups)):
                color = MENU_COLORS["accent"] if number == group else MENU_COLORS["page"]
                chain(f"drawbox=x={start + number * (dot + space)}:y={height - 10}:w={dot}:h={dot}:color={color}:t=fill")
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
                   "-filter_complex", ";".join(filters), "-map", f"[{state['label']}]",
                   "-frames:v", "1", str(Path(target).resolve())]
        subprocess.run(command, check=True, cwd=work, timeout=120)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def convert_menu_pdf(pdf_path, stem):
    """Trasforma le pagine di un PDF (al massimo MENU_MAX_PAGES) in immagini PNG nella libreria."""
    if not shutil.which("pdftoppm"):
        raise RuntimeError("Conversione PDF non disponibile: manca pdftoppm (poppler-utils)")
    output = Path(pdf_path).parent / "pagina"
    subprocess.run(["pdftoppm", "-png", "-r", "110", "-f", "1", "-l", str(MENU_MAX_PAGES), str(pdf_path), str(output)],
                   check=True, timeout=120)
    pages = sorted(Path(pdf_path).parent.glob("pagina-*.png"), key=lambda item: int(item.stem.rsplit("-", 1)[1]))
    if not pages:
        raise RuntimeError("Il PDF non contiene pagine leggibili")
    names = []
    for number, page in enumerate(pages[:MENU_MAX_PAGES], start=1):
        target = MEDIA_DIR / safe_name(f"{stem}_pagina_{number}.png")
        if target.exists():
            target = MEDIA_DIR / f"{target.stem}-{uuid.uuid4().hex[:6]}.png"
        shutil.move(str(page), target)
        names.append(target.name)
    return names


class IPTVEngine:
    BUILD_FORMAT_VERSION = 5

    def __init__(self):
        self.lock = threading.RLock()
        self.status = {}
        self.building = set()
        self.rebuild_pending = set()

    def channels(self):
        return store.config.get("iptv", {}).get("channels", [])

    def channel(self, channel_id):
        channel_id = safe_channel_id(channel_id)
        return next((item for item in self.channels() if safe_channel_id(item.get("id", "")) == channel_id), None)

    def output(self, channel_id):
        return IPTV_DIR / safe_channel_id(channel_id) / "channel.mp4"

    def hls_dir(self, channel_id):
        return IPTV_DIR / safe_channel_id(channel_id) / "hls"

    def hls_manifest(self, channel_id):
        return self.hls_dir(channel_id) / "channel.m3u8"

    def build_metadata(self, channel_id):
        return IPTV_DIR / safe_channel_id(channel_id) / "build.json"

    def build_info(self, channel_id):
        try:
            data = json.loads(self.build_metadata(channel_id).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def channel_signature(self, channel):
        payload = {
            "format_version": self.BUILD_FORMAT_VERSION,
            "playlist": channel.get("playlist") or [],
        }
        if (channel.get("menu") or {}).get("enabled"):
            payload["menu"] = menu_settings(channel)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def built_signature(self, channel_id):
        return str(self.build_info(channel_id).get("signature", ""))

    def is_current(self, channel):
        channel_id = safe_channel_id(channel.get("id", ""))
        return bool(
            self.output(channel_id).is_file()
            and self.hls_manifest(channel_id).is_file()
            and hmac.compare_digest(self.built_signature(channel_id), self.channel_signature(channel))
        )

    def runtime_status(self):
        with self.lock:
            result = copy.deepcopy(self.status)
        for channel in self.channels():
            channel_id = safe_channel_id(channel.get("id", ""))
            complete = self.output(channel_id).is_file() and self.hls_manifest(channel_id).is_file()
            current = complete and self.is_current(channel)
            previous = result.get(channel_id, {})
            build_info = self.build_info(channel_id)
            generated = {
                "built_at": build_info.get("built_at", ""),
                "sequence": build_info.get("sequence", []),
            }
            if channel_id in self.building:
                result[channel_id] = {**previous, **generated}
                continue
            # A failed rebuild must remain visible even when an older output is
            # still present. Otherwise the browser player reports "ready" and
            # keeps showing the previous sequence indefinitely.
            if previous.get("state") == "error":
                result[channel_id] = {**previous, **generated}
            elif current:
                result[channel_id] = {
                    "state": "ready", "message": "Canale HLS aggiornato", "time": "", **generated,
                }
            elif complete:
                result[channel_id] = {
                    "state": "outdated",
                    "message": "Sequenza modificata: aggiornamento automatico necessario",
                    "time": "",
                    **generated,
                }
            elif previous.get("state") != "error":
                result[channel_id] = {"state": "not_built", "message": "Canale da preparare", "time": ""}
        return result

    def set_status(self, channel_id, state, message=""):
        with self.lock:
            self.status[safe_channel_id(channel_id)] = {
                "state": state,
                "message": message,
                "time": now_iso(),
            }

    @staticmethod
    def _fit_filter(width=1280, height=720, fit="smart"):
        if fit == "contain":
            return (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
            )
        if fit == "smart":
            return (
                "split=2[background][foreground];"
                f"[background]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},boxblur=20:2[blurred];"
                f"[foreground]scale={width}:{height}:force_original_aspect_ratio=decrease[clear];"
                "[blurred][clear]overlay=(W-w)/2:(H-h)/2"
            )
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )

    @staticmethod
    def _effect_filter(effect, duration):
        frames = max(1, math.ceil(duration * 25))
        last_frame = max(1, frames - 1)
        # zoompan on a 1280x720 source rounds the crop position to whole
        # pixels and can visibly alternate around the centre.  Work at 3x
        # resolution and use cosine easing, then let zoompan downsample to the
        # final 1280x720 frame.  This removes the centre jitter without
        # distorting the image.
        smooth = f"(1-cos(PI*min(on/{last_frame},1)))/2"
        high_resolution = "scale=3840:2160:flags=lanczos,"
        if effect == "zoom_in":
            return (
                high_resolution +
                f"zoompan=z='1.0+0.30*{smooth}':"
                "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                "d=1:s=1280x720:fps=25"
            )
        if effect == "zoom_out":
            return (
                high_resolution +
                f"zoompan=z='1.30-0.30*{smooth}':"
                "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                "d=1:s=1280x720:fps=25"
            )
        if effect in {"pan", "pan_right"}:
            return (
                high_resolution + "zoompan=z=1.25:"
                f"x='(iw-iw/zoom)*{smooth}':"
                "y='ih/2-(ih/zoom/2)':d=1:s=1280x720:fps=25"
            )
        if effect == "pan_left":
            return (
                high_resolution + "zoompan=z=1.25:"
                f"x='(iw-iw/zoom)*(1-{smooth})':"
                "y='ih/2-(ih/zoom/2)':d=1:s=1280x720:fps=25"
            )
        if effect == "pan_down":
            return (
                high_resolution + "zoompan=z=1.25:"
                "x='iw/2-(iw/zoom/2)':"
                f"y='(ih-ih/zoom)*{smooth}':d=1:s=1280x720:fps=25"
            )
        if effect == "pan_up":
            return (
                high_resolution + "zoompan=z=1.25:"
                "x='iw/2-(iw/zoom/2)':"
                f"y='(ih-ih/zoom)*(1-{smooth})':d=1:s=1280x720:fps=25"
            )
        if effect == "ken_burns":
            return (
                high_resolution +
                f"zoompan=z='1.08+0.22*{smooth}':"
                f"x='(iw-iw/zoom)*{smooth}':"
                f"y='(ih-ih/zoom)*(1-{smooth})':d=1:s=1280x720:fps=25"
            )
        if effect == "ken_burns_reverse":
            return (
                high_resolution +
                f"zoompan=z='1.30-0.22*{smooth}':"
                f"x='(iw-iw/zoom)*(1-{smooth})':"
                f"y='(ih-ih/zoom)*{smooth}':d=1:s=1280x720:fps=25"
            )
        if effect == "black_white":
            return "hue=s=0,fps=25"
        return "fps=25"

    @staticmethod
    def _fade_filter(duration):
        fade_duration = min(1.2, max(0.6, duration * 0.12))
        fade_out = max(0.0, duration - fade_duration)
        return (
            f"fade=t=in:st=0:d={fade_duration:.3f},"
            f"fade=t=out:st={fade_out:.3f}:d={fade_duration:.3f}"
        )

    @staticmethod
    def _probe_duration(source):
        return probe_media_duration(source)

    @staticmethod
    def _has_audio(source):
        try:
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_type", "-of", "default=nw=1:nk=1",
                str(source),
            ], check=True, capture_output=True, text=True, timeout=30)
            return probe.stdout.strip() == "audio"
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def _collage_cells(count, layout):
        count = max(1, min(count, 6))
        if layout == "hero" and count >= 2:
            cells = [(0, 0, 768, 720)]
            heights = [720 // (count - 1)] * (count - 1)
            heights[-1] += 720 - sum(heights)
            y = 0
            for height in heights:
                cells.append((768, y, 512, height))
                y += height
            return cells
        if layout == "strip":
            widths = [1280 // count] * count
            widths[-1] += 1280 - sum(widths)
            cells, x = [], 0
            for width in widths:
                cells.append((x, 0, width, 720))
                x += width
            return cells
        if count == 1:
            return [(0, 0, 1280, 720)]
        if count == 2:
            return [(0, 0, 640, 720), (640, 0, 640, 720)]
        if count == 3:
            return [(0, 0, 640, 720), (640, 0, 640, 360), (640, 360, 640, 360)]
        columns = 2 if count == 4 else 3
        rows = math.ceil(count / columns)
        widths = [1280 // columns] * columns
        widths[-1] += 1280 - sum(widths)
        heights = [720 // rows] * rows
        heights[-1] += 720 - sum(heights)
        return [
            (sum(widths[:column]), sum(heights[:row]), widths[column], heights[row])
            for row in range(rows)
            for column in range(columns)
        ][:count]

    def _collage_command(self, item, part, duration):
        names = [safe_name(name) for name in item.get("images", [])][:6]
        sources = [MEDIA_DIR / name for name in names if name]
        if len(sources) < 2:
            raise RuntimeError("Un collage deve contenere almeno due foto")
        for source in sources:
            if not source.is_file() or source.suffix.lower() not in ALLOWED["image"]:
                raise RuntimeError(f"Foto del collage non trovata: {source.name}")
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        for source in sources:
            command += ["-loop", "1", "-i", str(source)]
        command += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        cells = self._collage_cells(len(sources), item.get("layout", "grid"))
        filters = []
        inputs = []
        layout = []
        for index, (x, y, width, height) in enumerate(cells):
            filters.append(
                f"[{index}:v]{self._fit_filter(width, height, 'cover')},setsar=1[cell{index}]"
            )
            inputs.append(f"[cell{index}]")
            layout.append(f"{x}_{y}")
        effect = self._effect_filter(item.get("effect", "fade"), duration)
        filters.append(
            f"{''.join(inputs)}xstack=inputs={len(inputs)}:layout={'|'.join(layout)}:fill=black,"
            f"{effect},{self._fade_filter(duration)},format=yuv420p[vout]"
        )
        command += [
            "-filter_complex", ";".join(filters),
            "-map", "[vout]", "-map", f"{len(sources)}:a:0",
        ]
        return command

    def part_key(self, item, kind):
        """Impronta di un elemento del canale: impostazioni, file sorgente (dimensione e data) e formato.
        Finché non cambia, la parte già convertita si riusa senza passare di nuovo da FFmpeg."""
        if kind == "collage":
            names = [safe_name(name) for name in item.get("images", [])][:6]
        else:
            names = [safe_name(item.get("name", ""))]
        files = []
        for name in names:
            try:
                info = (MEDIA_DIR / name).stat()
                files.append([name, info.st_size, info.st_mtime_ns])
            except OSError:
                files.append([name, None, None])
        payload = {"format_version": self.BUILD_FORMAT_VERSION, "kind": kind, "item": item, "files": files}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _cached_duration(part, info_file):
        """Durata di una parte già convertita, oppure None se va (ri)creata."""
        try:
            if part.stat().st_size < 1024:
                return None
            duration = float(json.loads(info_file.read_text(encoding="utf-8"))["duration"])
            return duration if duration > 0 else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def build(self, channel_id):
        channel = self.channel(channel_id)
        if not channel:
            raise RuntimeError("Canale IPTV non trovato")
        channel_id = safe_channel_id(channel.get("id"))
        self.set_status(channel_id, "building", "Conversione in corso")
        work = IPTV_DIR / channel_id
        parts = work / "parts"
        shutil.rmtree(parts, ignore_errors=True)
        parts.mkdir(parents=True, exist_ok=True)
        playlist = channel.get("playlist", [])
        if not playlist:
            raise RuntimeError("La playlist del canale è vuota")
        build_signature = self.channel_signature(channel)
        # Parti già convertite nelle volte precedenti: una modifica riconverte solo ciò che è cambiato.
        cache = work / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        concat_lines = []
        generated_sequence = []
        part_keys = []
        try:
            total_items = len(playlist)
            for index, item in enumerate(playlist):
                kind = item.get("kind", "video")
                name = safe_name(item.get("name", ""))
                source = MEDIA_DIR / name if name else None
                if kind != "collage" and (not source or not source.is_file()):
                    raise RuntimeError(f"File non trovato: {name}")
                if kind not in {"image", "video", "collage"}:
                    kind = next(
                        (key for key, values in ALLOWED.items() if source.suffix.lower() in values),
                        "video",
                    )
                key = self.part_key(item, kind)
                part = cache / f"{key}.mp4"
                info_file = cache / f"{key}.json"
                part_keys.append(key)
                cached = self._cached_duration(part, info_file)
                if cached is not None:
                    self.set_status(
                        channel_id,
                        "building",
                        f"Elemento {index + 1}/{total_items} già pronto: {name or 'Collage'}",
                    )
                    duration = cached
                    concat_lines.append(f"file '{part.as_posix()}'")
                    generated_sequence.append(self._sequence_entry(index, kind, name, duration, item))
                    continue
                self.set_status(
                    channel_id,
                    "building",
                    f"Conversione elemento {index + 1}/{total_items}: {name or 'Collage'}",
                )
                encoding = cache / f"{key}.tmp.mp4"
                if kind == "video":
                    duration = self._probe_duration(source)
                    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
                    if self._has_audio(source):
                        command += ["-map", "0:v:0", "-map", "0:a:0"]
                    else:
                        command += [
                            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                            "-map", "0:v:0", "-map", "1:a:0",
                        ]
                    command += [
                        "-vf", f"{self._fit_filter(fit=item.get('fit', 'smart'))},setsar=1,fps=25,format=yuv420p",
                    ]
                elif kind == "collage":
                    duration = max(2, min(float(item.get("duration", 12)), 3600))
                    command = self._collage_command(item, encoding, duration)
                else:
                    duration = max(2, min(float(item.get("duration", 12)), 3600))
                    effect = self._effect_filter(item.get("effect", "fade"), duration)
                    video_filter = (
                        f"{self._fit_filter(fit=item.get('fit', 'smart'))},setsar=1,"
                        f"{effect},{self._fade_filter(duration)},format=yuv420p"
                    )
                    command = [
                        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-loop", "1", "-i", str(source),
                        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                        "-map", "0:v:0", "-map", "1:a:0", "-vf", video_filter,
                    ]
                # A silent AAC track keeps video-only advertising compatible
                # with IPTV players that require both video and audio streams.
                command += [
                    "-c:v", "libx264", "-preset", "superfast",
                    "-crf", "22", "-profile:v", "high", "-level", "4.0",
                    "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
                    "-ar", "48000", "-ac", "2", "-t", f"{duration:.3f}", "-shortest",
                    "-movflags", "+faststart", str(encoding),
                ]
                subprocess.run(command, check=True, timeout=max(120, duration * 4 + 60))
                # La parte entra nella memoria solo quando FFmpeg ha finito davvero.
                os.replace(encoding, part)
                info_file.write_text(json.dumps({"duration": duration}), encoding="utf-8")
                concat_lines.append(f"file '{part.as_posix()}'")
                generated_sequence.append(self._sequence_entry(index, kind, name, duration, item))
            concat_file = parts / "concat.txt"
            concat_file.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
            temporary = work / "channel.tmp.mp4"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-fflags", "+genpts", "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(temporary),
            ], check=True, timeout=600)
            menu = menu_settings(channel)
            if menu["enabled"]:
                self.set_status(channel_id, "building", "Composizione dello schermo con il menu")
                frames = []
                for group in range(len(menu_groups(menu))):
                    frame = work / f"menu-frame-{group + 1}.png"
                    render_menu_frame(menu, frame, group=group)
                    frames.append(frame)
                ad_height = menu_layout(menu["ad_percent"], 1)[0]
                composed = work / "channel.menu.tmp.mp4"
                if len(frames) == 1:
                    loops, menu_input = 1, ["-loop", "1", "-i", str(frames[0])]
                    total = None
                else:
                    # La pubblicità si ripete finché tutti i gruppi di pagine sono passati almeno una volta.
                    ads_duration = max(1.0, self._probe_duration(temporary))
                    loops = max(1, math.ceil(len(frames) * menu["page_seconds"] / ads_duration))
                    total = ads_duration * loops
                    cycle = work / "menu-cycle.mp4"
                    subprocess.run(menu_cycle_command(frames, menu["page_seconds"], cycle), check=True, timeout=600)
                    menu_input = ["-stream_loop", "-1", "-i", str(cycle)]
                subprocess.run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-stream_loop", str(loops - 1), "-i", str(temporary), *menu_input,
                    "-filter_complex",
                    f"[1:v]fps=25,format=yuv420p[menu];{menu_ad_filter(menu['ad_fit'], 1280, ad_height)};"
                    "[menu][ad]overlay=(W-w)/2:0:shortest=1,fps=25,format=yuv420p[v]",
                    *(["-t", f"{total:.3f}"] if total else []),
                    "-map", "[v]", "-map", "0:a:0?",
                    "-c:v", "libx264", "-preset", "superfast", "-crf", "22",
                    "-profile:v", "high", "-level", "4.0", "-g", "50", "-keyint_min", "50",
                    "-sc_threshold", "0", "-pix_fmt", "yuv420p", "-c:a", "copy",
                    "-movflags", "+faststart", str(composed),
                ], check=True, timeout=1800)
                os.replace(composed, temporary)
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1",
                str(temporary),
            ], check=True, capture_output=True, text=True, timeout=30)
            if probe.stdout.strip() != "h264" or temporary.stat().st_size < 1024:
                raise RuntimeError("Il file IPTV generato non è un video H.264 valido")
            self.set_status(channel_id, "building", "Preparazione flusso HLS compatibile con Smart TV")
            hls_temporary = work / "hls.tmp"
            shutil.rmtree(hls_temporary, ignore_errors=True)
            hls_temporary.mkdir(parents=True, exist_ok=True)
            loop_segment = hls_temporary / "loop.ts"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(temporary), "-map", "0:v:0", "-map", "0:a:0?",
                "-c", "copy", "-bsf:v", "h264_mp4toannexb",
                "-mpegts_flags", "+resend_headers", "-f", "mpegts", str(loop_segment),
            ], check=True, timeout=600)
            duration_probe = subprocess.run([
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(temporary),
            ], check=True, capture_output=True, text=True, timeout=30)
            cycle_duration = max(1.0, float(duration_probe.stdout.strip()))
            repetitions = min(50000, max(1, math.ceil((24 * 60 * 60) / cycle_duration)))
            manifest_lines = [
                "#EXTM3U",
                "#EXT-X-VERSION:3",
                f"#EXT-X-TARGETDURATION:{math.ceil(cycle_duration)}",
                "#EXT-X-MEDIA-SEQUENCE:0",
                "#EXT-X-PLAYLIST-TYPE:VOD",
            ]
            for repetition in range(repetitions):
                if repetition:
                    manifest_lines.append("#EXT-X-DISCONTINUITY")
                manifest_lines += [f"#EXTINF:{cycle_duration:.3f},", f"loop.ts?v={repetition}"]
            manifest_lines.append("#EXT-X-ENDLIST")
            (hls_temporary / "channel.m3u8").write_text(
                "\n".join(manifest_lines) + "\n", encoding="utf-8"
            )
            final_hls = self.hls_dir(channel_id)
            shutil.rmtree(final_hls, ignore_errors=True)
            os.replace(hls_temporary, final_hls)
            os.replace(temporary, self.output(channel_id))
            metadata_temporary = self.build_metadata(channel_id).with_suffix(".tmp")
            metadata_temporary.write_text(json.dumps({
                "signature": build_signature,
                "built_at": now_iso(),
                "sequence": generated_sequence,
                "cycle_duration": round(cycle_duration, 3),
                "parts": part_keys,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(metadata_temporary, self.build_metadata(channel_id))
            # Restano in memoria solo le parti del canale attuale.
            used = set(part_keys)
            for leftover in cache.iterdir():
                if leftover.name.split(".", 1)[0] not in used:
                    leftover.unlink(missing_ok=True)
            self.set_status(channel_id, "ready", "Canale HLS pronto (foto e video)")
            store.log("success", f"Canale IPTV creato: {channel.get('name', channel_id)}")
        except Exception as error:
            self.set_status(channel_id, "error", str(error))
            store.log("error", f"Creazione canale IPTV non riuscita ({channel_id}): {error}")
            raise

    @staticmethod
    def _sequence_entry(index, kind, name, duration, item):
        return {
            "position": index + 1,
            "kind": kind,
            "name": name if kind != "collage" else "Collage",
            "duration": round(duration, 3),
            "effect": item.get("effect", "none") if kind in {"image", "collage"} else "none",
            "fit": item.get("fit", "smart") if kind != "collage" else "cover",
        }

    def build_async(self, channel_id):
        channel_id = safe_channel_id(channel_id)
        with self.lock:
            if channel_id in self.building:
                # Se l'utente salva di nuovo mentre FFmpeg sta lavorando,
                # ricrea ancora il canale al termine usando l'ultimo ordine.
                self.rebuild_pending.add(channel_id)
                return False
            self.building.add(channel_id)
        self.set_status(channel_id, "building", "Preparazione automatica del canale")

        def worker():
            try:
                self.build(channel_id)
            except Exception:
                pass
            finally:
                rebuild_again = False
                with self.lock:
                    self.building.discard(channel_id)
                    rebuild_again = channel_id in self.rebuild_pending
                    self.rebuild_pending.discard(channel_id)
                if rebuild_again:
                    self.build_async(channel_id)
        threading.Thread(target=worker, name=f"iptv-build-{safe_channel_id(channel_id)}", daemon=True).start()
        return True


iptv = IPTVEngine()


def _power_entity_action(entity_id, turn_on):
    if not entity_id or "." not in entity_id:
        raise RuntimeError("Entità di alimentazione TV non valida")
    domain = entity_id.split(".", 1)[0]
    if domain == "button":
        service = "press"
    elif domain == "script":
        service = "turn_on"
    elif domain in ("media_player", "switch"):
        service = "turn_on" if turn_on else "turn_off"
    else:
        raise RuntimeError(f"Tipo entità non supportato per alimentazione TV: {domain}")
    ha.service(domain, service, {"entity_id": entity_id})


def tv_power_on(cfg=None):
    cfg = cfg or store.config["tv"]
    mac = re.sub(r"[^0-9A-Fa-f]", "", cfg.get("wol_mac", ""))
    if mac:
        if len(mac) != 12:
            raise RuntimeError("Indirizzo MAC Wake-on-LAN non valido")
        formatted = ":".join(mac[index:index + 2] for index in range(0, 12, 2))
        ha.service("wake_on_lan", "send_magic_packet", {"mac": formatted})
        store.log("success", f"Comando Wake-on-LAN inviato alla TV: {formatted}")
        return
    entity = cfg.get("power_on_entity") or cfg.get("player")
    _power_entity_action(entity, True)
    store.log("success", f"Comando accensione TV inviato: {entity}")


def tv_power_off(cfg=None):
    cfg = cfg or store.config["tv"]
    entity = cfg.get("power_off_entity") or cfg.get("player")
    _power_entity_action(entity, False)
    store.log("success", f"Comando spegnimento TV inviato: {entity}")


class Scheduler(threading.Thread):
    daemon = True

    def __init__(self):
        super().__init__(name="carellas-scheduler")
        self.last_audio = time.monotonic()
        self.audio_paused_until = 0.0
        self.last_spot_attempt = None
        self.audio_index = 0
        self.tv_index = 0
        self.tv_next = 0.0
        self.music_active = False
        self.music_slot_key = None
        self.music_slot_id = None
        self.music_players = []
        self.music_volume = 0.25
        self.music_selection = None
        self.music_failed_slot_key = None
        self.music_retry_at = 0.0
        self.music_last_error = None
        # Fascia il cui avvio è stato accettato dai Sonos ma che non suonava ancora: (chiave, casse, attese).
        self.music_late_start = None
        # Controllo della musica ferma durante la fascia (vedi music_watchdog).
        self.music_watch_at = 0.0
        self.music_stopped_since = None
        # Fascia seguita prima dell'ultimo riavvio dell'add-on (vedi adopt_running_music).
        self.music_resume = self._load_music_state()
        # Musica accesa o spenta a mano dal pannello: {"mode": "on"/"off", "slot_key": ..., "name": ...}.
        self.music_override = None
        self.music_expected_players = []
        self.music_coordinator = None
        self.music_group_check_at = 0.0
        self.music_group_issue = None
        self.tv_active = False
        self.tv_ready_at = 0.0
        self.iptv_active = {}
        self.daily_audio_count = 0
        self.audio_count_lock = threading.Lock()
        self.day = datetime.now().date()
        self._load_runtime()
        self.busy = threading.Lock()

    def _load_runtime(self):
        try:
            saved = json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
            if saved.get("day") == self.day.isoformat():
                self.daily_audio_count = max(0, int(saved.get("audio_today", 0)))
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            self.daily_audio_count = 0

    def _save_runtime(self):
        temporary = RUNTIME_FILE.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps({
                "day": self.day.isoformat(),
                "audio_today": self.daily_audio_count,
            }), encoding="utf-8")
            os.replace(temporary, RUNTIME_FILE)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            store.log("warning", f"Conteggio giornaliero non salvato: {error}")

    def audio_today(self):
        with self.audio_count_lock:
            return self.daily_audio_count

    def record_audio_play(self):
        """Conta ogni spot che Home Assistant ha effettivamente avviato, anche nelle prove manuali."""
        today = datetime.now().date()
        with self.audio_count_lock:
            if today != self.day:
                self.day = today
                self.daily_audio_count = 0
            self.daily_audio_count += 1
            self._save_runtime()
            return self.daily_audio_count

    def pick_audio(self, ads, mode):
        if not ads:
            return None
        if mode == "female":
            return next((x for x in ads if "femmin" in x.lower() or "female" in x.lower()), ads[0])
        if mode == "male":
            return next((x for x in ads if "maschil" in x.lower() or "male" in x.lower()), ads[0])
        selected = ads[self.audio_index % len(ads)]
        self.audio_index += 1
        return selected

    def selected_playing(self):
        selected = set(available_sonos_players(store.config["audio"].get("players", [])))
        if not selected:
            return False
        try:
            states = {x.get("entity_id"): x.get("state") for x in ha.states()}
            return any(states.get(entity) == "playing" for entity in selected)
        except Exception as error:
            store.log("error", f"Lettura Sonos non riuscita: {error}")
            return False

    def peek_audio(self, ads, mode):
        """Spot che verrà trasmesso al prossimo intervallo, senza avanzare la rotazione."""
        if not ads:
            return None
        if mode in ("female", "male"):
            return self.pick_audio(ads, mode)
        return ads[self.audio_index % len(ads)]

    def audio_paused(self):
        return self.audio_paused_until > time.time()

    def pause_audio(self, minutes):
        """Sospende gli spot automatici; 0 riprende subito."""
        minutes = max(0, min(int(minutes), 24 * 60))
        self.audio_paused_until = time.time() + minutes * 60 if minutes else 0.0
        self.last_audio = time.monotonic()
        if minutes:
            store.log("info", f"Pubblicità automatica in pausa per {minutes} minuti")
        else:
            store.log("info", "Pubblicità automatica ripresa")
        return self.audio_paused_until

    def take_next_audio(self):
        """«Riproduci ora»: lo spot in arrivo parte subito e conta come quello di questo intervallo,
        così il programma non lo ripete appena dopo e la rotazione passa al successivo."""
        cfg = store.config["audio"]
        selected = self.pick_audio(active_ads(cfg), cfg.get("mode", "rotate"))
        self.last_audio = time.monotonic()
        return selected

    def skip_audio(self):
        """Salta lo spot in arrivo: riparte l'intervallo e la rotazione passa al successivo."""
        cfg = store.config["audio"]
        skipped = self.peek_audio(active_ads(cfg), cfg.get("mode", "rotate"))
        if cfg.get("mode", "rotate") == "rotate" and active_ads(cfg):
            self.audio_index += 1
        self.last_audio = time.monotonic()
        store.log("info", f"Spot saltato: {skipped or 'nessuno'}")
        return skipped

    def audio_status(self):
        """Stato degli spot per le schermate: prossimo spot, pausa, motivo dell'attesa."""
        cfg = store.config["audio"]
        interval = max(1, int(cfg.get("interval_minutes", 30))) * 60
        status = {
            "spot_playing": audio_playback_lock.locked(),
            "audio_paused_until": (
                datetime.fromtimestamp(self.audio_paused_until).astimezone().isoformat(timespec="seconds")
                if self.audio_paused() else None
            ),
            "next_spot": self.peek_audio(active_ads(cfg), cfg.get("mode", "rotate")),
            "next_spot_in_seconds": None,
            "audio_waiting": None,
        }
        if not cfg.get("enabled"):
            status["audio_waiting"] = "disabled"
        elif not active_ads(cfg):
            status["audio_waiting"] = "no_ads"
        elif self.audio_paused():
            status["audio_waiting"] = "paused"
        elif not is_schedule_active(cfg.get("schedule", [])):
            status["audio_waiting"] = "outside_schedule"
        elif self.audio_today() >= max(1, int(cfg.get("daily_limit", 20))):
            status["audio_waiting"] = "daily_limit"
        else:
            remaining = max(0, round(interval - (time.monotonic() - self.last_audio)))
            status["next_spot_in_seconds"] = remaining
            if remaining == 0 and cfg.get("only_when_playing", True) and not self.selected_playing():
                status["audio_waiting"] = "no_music"
        return status

    def zone_music(self, players, on):
        """Musica sì/no in una zona (dagli operatori): le altre zone continuano. Vale per oggi."""
        players = [p for p in dict.fromkeys(players) if isinstance(p, str) and p]
        if not players:
            raise RuntimeError("La zona non ha casse Sonos")
        today = datetime.now().date().isoformat()
        if not on:
            for player in players:
                MUSIC_EXCLUDED[player] = today
            lost_coordinator = self.music_coordinator in players
            try:
                sonos_service_each("media_player", "unjoin", {"entity_id": players})
                sonos_service_each("media_player", "media_stop", {"entity_id": players})
            except Exception as error:
                store.log("warning", f"Musica non fermata in tutte le casse della zona: {error}")
            self.music_expected_players = [p for p in self.music_expected_players if p not in players]
            self.music_players = [p for p in self.music_players if p not in players]
            if lost_coordinator and self.music_selection and self.music_active and not self.music_override:
                # Si è tolta la zona che guidava il gruppo: la musica riparte sulle altre.
                self.music_players = start_music(self.music_selection, fade_in=True)
                self.music_coordinator = self.music_players[0] if self.music_players else None
                self.music_expected_players = list(self.music_players)
            store.log("info", "Musica tolta dalla zona: " + ", ".join(p.removeprefix("media_player.") for p in players))
            return
        for player in players:
            MUSIC_EXCLUDED.pop(player, None)
        coordinator = self.music_coordinator
        if self.music_active and coordinator and not self.music_override:
            others = [p for p in available_sonos_players(players) if p != coordinator]
            if others:
                sonos_service("media_player", "join", {"entity_id": coordinator, "group_members": others})
                levels = remembered_sonos_volumes(others, self.music_volume)
                levels.update(manual_sonos_volumes(others))
                set_sonos_volume_levels(levels)
                ha.service("media_player", "media_play", {"entity_id": coordinator})
                self.music_players = list(dict.fromkeys(self.music_players + others))
                self.music_expected_players = list(dict.fromkeys(self.music_expected_players + others))
        store.log("info", "Musica rimessa nella zona: " + ", ".join(p.removeprefix("media_player.") for p in players))

    def music_control(self, action):
        """Pausa, riprendi, brano successivo o precedente sulla musica in corso."""
        services = {"pause": "media_pause", "play": "media_play", "next": "media_next_track",
                    "previous": "media_previous_track"}
        if action not in services:
            raise RuntimeError("Comando non valido")
        coordinator = self.music_coordinator
        if not coordinator:
            playing = [s for s in ha.states() if s.get("entity_id") in set(self.music_players or mix_settings()["players"])
                       and s.get("state") in ("playing", "paused")]
            coordinator = playing[0]["entity_id"] if playing else None
        if not coordinator:
            raise RuntimeError("Nessuna musica in corso")
        try:
            sonos_service("media_player", services[action], {"entity_id": coordinator})
        except Exception as error:
            if action in ("next", "previous"):
                raise RuntimeError("Questa sorgente non ha brani da saltare (per esempio una radio)") from None
            raise
        if action == "pause":
            # In pausa: il motore non la fa ripartire finché non premi Play (anche se cambia la fascia).
            # Si ricorda la scelta di prima (es. musica accesa a mano fuori programma) per Play.
            previous = self.music_override if (self.music_override or {}).get("mode") != "paused" else                 self.music_override.get("resume")
            self.music_override = {"mode": "paused", "slot_key": self.current_slot_key(),
                                   "name": (self.music_selection or {}).get("name", ""), "resume": previous,
                                   "day": datetime.now().date().isoformat()}
        elif action == "play" and self.music_override and self.music_override.get("mode") == "paused":
            self.music_override = self.music_override.get("resume")
        store.log("info", {"pause": "Musica in pausa", "play": "Musica ripresa", "next": "Brano successivo",
                           "previous": "Brano precedente"}[action])

    def music_started(self, slot, slot_key, players, target_players, cfg, late=False):
        """La fascia suona: da qui in poi il motore la segue (gruppo, volumi, prossimo cambio)."""
        self.music_players = list(players)
        self.music_expected_players = target_players
        self.music_coordinator = players[0] if players else None
        self.music_group_check_at = 0.0
        self.music_group_issue = None
        self.music_volume = max(0, min(int(slot.get("volume", cfg.get("volume", 25))), 100)) / 100
        self.music_slot_key = slot_key
        self.music_slot_id = slot.get("id")
        self.music_selection = copy.deepcopy(slot)
        self.music_failed_slot_key = None
        self.music_retry_at = 0.0
        self.music_last_error = None
        self.music_late_start = None
        self.music_stopped_since = None
        self._save_music_state()
        if late:
            # Il ripiego dopo il primo controllo ha rimesso i volumi di prima: si danno quelli della fascia.
            manual = manual_sonos_volumes(players)
            set_sonos_volume_levels({player: manual.get(player, self.music_volume) for player in players})
            title = slot.get("name") or slot.get("title") or slot.get("content_id", "")
            store.log("info", f"Musica partita con un po' di ritardo: «{title}»")

    @staticmethod
    def _load_music_state():
        try:
            saved = json.loads(MUSIC_STATE_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return None
        if not isinstance(saved, dict) or saved.get("day") != datetime.now().date().isoformat():
            return None
        return saved

    def _save_music_state(self):
        temporary = MUSIC_STATE_FILE.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps({
                "day": datetime.now().date().isoformat(), "slot_key": self.music_slot_key,
                "players": list(self.music_players), "expected": list(self.music_expected_players),
            }, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, MUSIC_STATE_FILE)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            store.log("warning", f"Stato della musica non salvato: {error}")

    def adopt_running_music(self, slot, slot_key, cfg):
        """Dopo un riavvio dell'add-on (per esempio un aggiornamento) i Sonos continuano a suonare la
        fascia in corso: si riprende a seguirla invece di rimandarla, che la faceva ripartire dal primo brano."""
        saved, self.music_resume = self.music_resume, None
        if not saved or saved.get("slot_key") != slot_key:
            return False
        players = [player for player in saved.get("players") or [] if isinstance(player, str)]
        if not players or sonos_playing(players[0]) is not True:
            return False
        expected = [player for player in saved.get("expected") or players if isinstance(player, str)]
        self.music_started(slot, slot_key, players, expected, cfg)
        name = slot.get("name") or slot.get("title") or slot.get("content_id", "")
        store.log("info", f"Musica già in onda dopo il riavvio: «{name}» continua senza ripartire")
        return True

    def music_watchdog(self, slot):
        """La fascia è in corso ma i Sonos sono fermi e nessuno l'ha chiesto dal pannello o dal tablet
        (fine della playlist, ripristino dopo uno spot non riuscito, intoppo di Sonos): la musica riparte."""
        now = time.monotonic()
        if now < self.music_watch_at:
            return
        self.music_watch_at = now + 10
        coordinator = self.music_coordinator
        if not coordinator or audio_playback_lock.locked() or sonos_playing(coordinator) is not False:
            self.music_stopped_since = None
            return
        if self.music_stopped_since is None:
            self.music_stopped_since = now
            return
        if now - self.music_stopped_since < MUSIC_RESUME_SECONDS:
            return
        self.music_stopped_since = None
        # Se non riparte, si riprova solo dopo un po': ogni tentativo può abbassare e rialzare il volume.
        self.music_watch_at = now + MUSIC_RETRY_SECONDS
        selection = self.music_selection or slot
        title = selection.get("name") or selection.get("title") or selection.get("content_id", "")
        store.log("warning", f"La musica «{title}» si era fermata da sola: la faccio ripartire")
        try:
            ha.service("media_player", "media_play", {"entity_id": coordinator})
            if ensure_sonos_playing(coordinator, checks=8) and sonos_playing(coordinator) is True:
                store.log("info", f"Musica ripartita: «{title}»")
                return
        except Exception as error:
            store.log("warning", f"Ripartenza dei Sonos non riuscita: {error}")
        # Play non basta (es. playlist finita): si riavvia la fascia dall'inizio.
        try:
            self.music_players = start_music(selection, fade_in=True)
            self.music_coordinator = self.music_players[0] if self.music_players else None
            store.log("info", f"Musica riavviata: «{title}»")
        except Exception as error:
            store.log("error", f"Musica non ripartita: {music_error_message(error, selection)}")

    @staticmethod
    def current_music_slot():
        """Fascia che il programma suona adesso: il blocco del Mix automatico se è acceso, altrimenti
        la fascia del Palinsesto (None fuori programma)."""
        cfg = store.config["music"]
        if not cfg.get("enabled"):
            return None
        return active_mix_slot() if mix_settings()["enabled"] else (
            active_music_slot(cfg) if cfg.get("slots") else (cfg if is_schedule_active(cfg.get("schedule", [])) else None))

    def restart_scheduled(self):
        """«Riavvia la musica»: il motore riparte da capo con la fascia in corso, su tutte le sue casse,
        e continua a seguirla (gruppo, volumi, prossimo cambio). Restituisce la fascia o None."""
        slot = self.current_music_slot()
        if not slot:
            return None
        self.music_override = None
        self._forget_music()
        self.music_failed_slot_key = None
        self.music_retry_at = 0.0
        self.music_last_error = None
        self.music_late_start = None
        return slot

    def current_slot_key(self):
        """Fascia del palinsesto in corso adesso (None fuori programma)."""
        cfg = store.config["music"]
        slot = self.current_music_slot()
        if not slot:
            return None
        return json.dumps([
            slot.get("id") or "legacy", slot.get("content_type", "music"), slot.get("content_id", ""),
            slot.get("players", []), slot.get("volume", cfg.get("volume", 25)),
        ], ensure_ascii=False, sort_keys=True)

    def _forget_music(self):
        self.music_players = []
        self.music_slot_key = None
        self.music_slot_id = None
        self.music_selection = None
        self.music_active = False
        self.music_coordinator = None
        self.music_expected_players = []
        self.music_failed_slot_key = None
        self.music_retry_at = 0.0

    def music_off(self, extra_players=None):
        """Spegne la musica a mano: resta spenta finché non la riaccendi, anche quando cambia la fascia
        o il blocco del Mix automatico. Il giorno dopo il programma riparte da solo."""
        players = list(dict.fromkeys(list(self.music_players) + list(extra_players or []))) \
            or mix_settings()["players"]
        transition = store.config["music"].get("transition_seconds", 3)
        remember_playlist_position(self.music_selection, self.music_coordinator)
        self.music_override = {"mode": "off", "slot_key": self.current_slot_key(), "name": "",
                               "day": datetime.now().date().isoformat()}
        try:
            stop_music(players, transition)
        finally:
            self._forget_music()
        store.log("info", "Musica spenta a mano: resta spenta finché non la riaccendi (domani riparte da sola)")

    def music_on(self, selection=None):
        """Riaccende la musica: senza scelta riprende il palinsesto, altrimenti suona la sorgente scelta
        (anche fuori dal programma) finché non la spegni o inizia un'altra fascia."""
        if not selection:
            if not self.current_slot_key():
                raise RuntimeError("Fuori programma: scegli cosa suonare")
            self.music_override = None
            self._forget_music()
            store.log("info", "Musica riaccesa: riprende il palinsesto")
            return None
        remember_playlist_position(self.music_selection, self.music_coordinator)
        # Prima si segna la scelta, così il motore non interviene mentre la musica parte.
        name = selection.get("name") or selection.get("content_id", "")
        self.music_override = {"mode": "on", "slot_key": self.current_slot_key(), "name": name}
        try:
            players = start_music(selection, fade_in=True)
        except Exception:
            self.music_override = None
            raise
        self.music_players = players
        self.music_coordinator = players[0] if players else None
        self.music_selection = copy.deepcopy(selection)
        self.music_active = True
        store.log("info", f"Musica accesa a mano: {name}")
        return players

    def audio_tick(self):
        cfg = store.config["audio"]
        if not cfg.get("enabled") or not is_schedule_active(cfg.get("schedule", [])) or self.audio_paused():
            self.last_audio = time.monotonic()
            return
        interval = max(1, int(cfg.get("interval_minutes", 30))) * 60
        if time.monotonic() - self.last_audio < interval:
            return
        if self.audio_today() >= max(1, int(cfg.get("daily_limit", 20))):
            return
        if cfg.get("only_when_playing", True) and not self.selected_playing():
            return
        # Uno spot avviato a mano è ancora in onda: si aspetta che finisca invece di segnalare un errore.
        if audio_playback_lock.locked():
            return
        if not self.busy.acquire(blocking=False):
            return
        self.last_audio = time.monotonic()
        self.last_spot_attempt = None
        try:
            play_audio()
        except Exception as error:
            store.log("error", f"Spot audio non riuscito: {error}")
            spot_history.add(self.last_spot_attempt, store.config["audio"].get("players", []), False,
                             status="failed", reason=error)
        finally:
            self.busy.release()

    def music_tick(self):
        cfg = store.config["music"]
        if audio_playback_lock.locked():
            return
        slot = active_music_slot(cfg) if cfg.get("enabled") and cfg.get("slots") else None
        if cfg.get("enabled") and not cfg.get("slots") and is_schedule_active(cfg.get("schedule", [])):
            slot = cfg
        if cfg.get("enabled") and mix_settings()["enabled"]:
            # Mix automatico acceso: prende il posto delle fasce del Palinsesto.
            slot = active_mix_slot()
        active = bool(slot)
        slot_key = None
        if slot:
            slot_key = json.dumps([
                slot.get("id") or "legacy",
                slot.get("content_type", "music"),
                slot.get("content_id", ""),
                slot.get("players", []),
                slot.get("volume", cfg.get("volume", 25)),
            ], ensure_ascii=False, sort_keys=True)
        override = self.music_override
        if override:
            held = override.get("mode") in ("off", "paused")
            if held and override.get("day") not in (None, datetime.now().date().isoformat()):
                # Spenta o in pausa ieri: oggi il programma riparte normalmente.
                self.music_override = override = None
                store.log("info", "Nuovo giorno: la musica riprende dal palinsesto")
            elif active and slot_key != override.get("slot_key") and not held:
                # Inizia un'altra fascia: la musica accesa a mano finisce e riprende il palinsesto.
                # Spenta o in pausa a mano invece resta così finché non la riaccendi (anche con il Mix,
                # che cambia blocco ogni mezz'ora: prima la musica ripartiva da sola).
                self.music_override = override = None
                store.log("info", "Nuova fascia: la musica riprende dal palinsesto")
            if override:
                # Musica spenta o accesa a mano: niente avvii, arresti o controlli automatici del gruppo.
                return
        late = self.music_late_start
        if active and late and late[0] == slot_key and slot_key == self.music_failed_slot_key \
                and sonos_playing(late[1][0]) is True:
            # La fascia era partita in ritardo (playlist lunga): i Sonos ora suonano. Riavviarla
            # la farebbe ricominciare da capo e interromperebbe la musica.
            self.music_started(slot, slot_key, late[1], late[2], cfg, late=True)
            self.music_active = True
            return
        if active and slot_key == self.music_failed_slot_key and time.monotonic() < self.music_retry_at:
            # Un Sonos in transizione può restituire UPnP 701 per alcuni
            # secondi. Evitiamo una raffica di tentativi a ogni ciclo.
            return
        changed = active and slot_key != self.music_slot_key
        if changed and self.music_slot_key is None and self.music_resume is not None:
            # Primo giro dopo un riavvio: se i Sonos suonano già questa fascia la si lascia continuare.
            changed = not self.adopt_running_music(slot, slot_key, cfg)
        if changed:
            remember_playlist_position(self.music_selection, self.music_coordinator)
            previous_selection = copy.deepcopy(self.music_selection)
            previous = list(self.music_players)
            previous_levels = {}
            group_kept = True
            transition = max(0.0, min(float(cfg.get("transition_seconds", 3)), 10.0))
            try:
                target_players = list(dict.fromkeys(slot.get("players") or cfg.get("players") or []))
                if previous:
                    # Volumi di ogni Sonos prima del cambio: servono se la nuova fascia non parte.
                    previous_levels = sonos_volume_levels(previous, self.music_volume)
                    fade_sonos(previous, SONOS_QUIET_VOLUME, transition, previous_levels)
                if previous and set(previous) != set(target_players):
                    group_kept = False
                    # Scioglie il vecchio gruppo prima di crearne uno diverso:
                    # un Sonos rimosso dalla fascia non deve continuare a suonare.
                    sonos_service_each("media_player", "media_stop", {"entity_id": previous})
                    sonos_service_each("media_player", "unjoin", {
                        "entity_id": list(dict.fromkeys(previous + target_players)),
                    })
                    if SONOS_GROUP_SETTLE_SECONDS > 0:
                        time.sleep(SONOS_GROUP_SETTLE_SECONDS)
                self.music_late_start = None
                players = start_music(slot, fade_in=True)
                self.music_started(slot, slot_key, players, target_players, cfg)
            except Exception as error:
                if isinstance(error, SonosNotStarted) and error.players:
                    # Il comando è passato ma i Sonos non suonavano ancora: al prossimo controllo, se nel
                    # frattempo sono partiti, la fascia si considera avviata senza ricominciarla.
                    self.music_late_start = (slot_key, error.players, target_players)
                    if ensure_sonos_playing(error.players[0], checks=5) and sonos_playing(error.players[0]) is True:
                        self.music_started(slot, slot_key, error.players, target_players, cfg, late=True)
                        self.music_active = True
                        return
                rejected = sonos_source_rejected(error)
                message = f"Avvio musica non riuscito: {music_error_message(error, slot)}"
                # Lo stesso errore sulla stessa fascia si scrive una volta sola nel Registro.
                if (slot_key, message) != self.music_last_error:
                    store.log("error", message)
                self.music_last_error = (slot_key, message)
                self.music_failed_slot_key = slot_key
                # Errore passeggero (Sonos occupato): si riprova presto. Sorgente rifiutata: di rado,
                # perché ogni tentativo abbassa e rialza la musica in corso.
                self.music_retry_at = time.monotonic() + (MUSIC_RETRY_REJECTED_SECONDS if rejected else MUSIC_RETRY_SECONDS)
                if previous_selection and group_kept and self.music_coordinator                         and sonos_playing(self.music_coordinator) is not False:
                    # La musica di prima suona ancora: si rialza solo il volume, senza riavviarla
                    # (riavviarla la faceva abbassare e rialzare un'altra volta).
                    try:
                        fade_sonos(previous, previous_levels, transition,
                                   {player: SONOS_QUIET_VOLUME for player in previous})
                    except Exception as volume_error:
                        store.log("warning", f"Volume della musica precedente non ripristinato: {volume_error}")
                    active = True
                    store.log("warning", "Cambio sorgente annullato: continua la musica precedente")
                elif previous_selection:
                    try:
                        self.music_players = start_music(previous_selection, fade_in=True)
                        if previous_levels:
                            set_sonos_volume_levels({p: v for p, v in previous_levels.items() if p in self.music_players})
                        self.music_expected_players = list(dict.fromkeys(
                            previous_selection.get("players") or cfg.get("players") or []
                        ))
                        self.music_coordinator = self.music_players[0] if self.music_players else None
                        self.music_group_check_at = 0.0
                        active = True
                        store.log("warning", "Cambio sorgente annullato: ripristinata la musica precedente")
                    except Exception as restore_error:
                        store.log("error", f"Ripristino sorgente musicale non riuscito: {restore_error}")
                        active = False
                else:
                    active = False
        elif not active and self.music_active and cfg.get("stop_at_end", True):
            try:
                stop_music(self.music_players, cfg.get("transition_seconds", 3))
            except Exception as error:
                store.log("error", f"Arresto musica non riuscito: {error}")
            self.music_players = []
            self.music_slot_key = None
            self.music_slot_id = None
            self.music_selection = None
            self.music_failed_slot_key = None
            self.music_retry_at = 0.0
            self.music_expected_players = []
            self.music_coordinator = None
            self.music_group_check_at = 0.0
            self.music_group_issue = None
        elif not active:
            self.music_failed_slot_key = None
            self.music_retry_at = 0.0
            self.music_expected_players = []
            self.music_coordinator = None
            self.music_group_issue = None
        self.music_active = active
        if active and not changed and slot_key == self.music_slot_key:
            self.music_watchdog(slot)
        if active and not changed and time.monotonic() >= self.music_group_check_at:
            self.music_group_check_at = time.monotonic() + SONOS_GROUP_CHECK_SECONDS
            expected = self.music_expected_players or list(dict.fromkeys(
                slot.get("players") or cfg.get("players") or []
            ))
            expected = [player for player in expected if player not in music_excluded_players()]
            try:
                lost = self.music_coordinator
                # Un Sonos rientrato nel gruppo riprende il suo ultimo volume, non quello della fascia.
                result = repair_sonos_group(expected, self.music_coordinator,
                                            remembered_sonos_volumes(expected, self.music_volume))
                if lost and result["coordinator"] and result["coordinator"] != lost and self.music_selection \
                        and lost in result["unavailable"] and not audio_playback_lock.locked():
                    # Si è spento il Sonos che guidava il gruppo: il gruppo si scioglie e la musica si
                    # ferma ovunque. La si fa ripartire sugli altri.
                    store.log("warning", f"Il Sonos {lost.removeprefix('media_player.')} che guidava il gruppo "
                                         "non risponde: faccio ripartire la musica sugli altri")
                    self.music_players = start_music(self.music_selection, fade_in=True)
                    result["coordinator"] = self.music_players[0] if self.music_players else None
                if result["coordinator"]:
                    self.music_coordinator = result["coordinator"]
                if not result["missing"] and not audio_playback_lock.locked():
                    # Gruppo a posto: aggiorna i volumi noti e riconosce quelli cambiati dall'app Sonos.
                    # Durante uno spot i volumi sono quelli dello spot: non vanno letti.
                    live = sonos_volume_levels(
                        [player for player in expected if player not in result["unavailable"]], self.music_volume)
                    if not audio_playback_lock.locked():
                        detect_manual_sonos_volumes(live)
                        remember_sonos_volumes(live)
                issue = tuple(sorted(result["unavailable"]))
                if result["missing"]:
                    store.log("warning", "Sonos riagganciati automaticamente: " + ", ".join(result["missing"]))
                if issue and issue != self.music_group_issue:
                    store.log("warning", "Sonos momentaneamente non raggiungibili: " + ", ".join(issue))
                if not issue and self.music_group_issue:
                    store.log("success", "Tutti i Sonos programmati sono nuovamente collegati")
                self.music_group_issue = issue or None
            except Exception as error:
                store.log("warning", f"Controllo automatico gruppo Sonos non riuscito: {error}")

    def tv_tick(self):
        cfg = store.config["tv"]
        playlist = cfg.get("playlist", [])
        mode = cfg.get("mode", "media_player")
        target_ready = mode == "lan_screen" or bool(cfg.get("player"))
        scheduled = bool(cfg.get("enabled") and target_ready and playlist and is_schedule_active(cfg.get("schedule", [])))

        if not scheduled:
            if self.tv_active and mode == "dlna" and cfg.get("power_off_at_end", True):
                try:
                    tv_power_off()
                except Exception as error:
                    store.log("error", f"Spegnimento TV non riuscito: {error}")
            self.tv_active = False
            self.tv_next = 0
            self.tv_ready_at = 0
            return

        if not self.tv_active:
            self.tv_active = True
            self.tv_index = 0
            self.tv_next = 0
            if mode == "dlna" and cfg.get("power_on_at_start", True):
                try:
                    tv_power_on()
                except Exception as error:
                    store.log("error", f"Accensione TV non riuscita: {error}")
                delay = max(0, min(int(cfg.get("startup_delay_seconds", 20)), 300))
                self.tv_ready_at = time.monotonic() + delay

        if mode == "lan_screen":
            return
        if time.monotonic() < self.tv_ready_at or time.monotonic() < self.tv_next:
            return

        item = playlist[self.tv_index % len(playlist)]
        try:
            play_tv_item(item)
            duration = max(2, min(int(item.get("duration", 15)), 86400))
            self.tv_next = time.monotonic() + duration
            self.tv_index += 1
            if not cfg.get("loop", True) and self.tv_index >= len(playlist):
                self.tv_next = float("inf")
        except Exception as error:
            store.log("error", f"Riproduzione TV non riuscita: {error}")
            self.tv_next = time.monotonic() + 30

    def iptv_tick(self):
        current_ids = set()
        for channel in iptv.channels():
            channel_id = safe_channel_id(channel.get("id", ""))
            current_ids.add(channel_id)
            active = bool(channel.get("enabled") and is_schedule_active(channel.get("schedule", [])))
            previous = self.iptv_active.get(channel_id, False)
            if active and not previous and channel.get("power_on_at_start", False):
                try:
                    tv_power_on(channel)
                except Exception as error:
                    store.log("error", f"Accensione {channel.get('name', channel_id)} non riuscita: {error}")
            elif not active and previous and channel.get("power_off_at_end", False):
                try:
                    tv_power_off(channel)
                except Exception as error:
                    store.log("error", f"Spegnimento {channel.get('name', channel_id)} non riuscito: {error}")
            self.iptv_active[channel_id] = active
        for channel_id in set(self.iptv_active) - current_ids:
            self.iptv_active.pop(channel_id, None)

    def run(self):
        store.log("info", "Motore Carellas Media Ads avviato")
        if not instagram.is_alive():
            instagram.start()
        if not website_events.is_alive():
            website_events.start()
        while True:
            try:
                today = datetime.now().date()
                if today != self.day:
                    with self.audio_count_lock:
                        self.day = today
                        self.daily_audio_count = 0
                        self._save_runtime()
                self.music_tick()
                self.audio_tick()
                self.tv_tick()
                self.iptv_tick()
            except Exception:
                store.log("error", traceback.format_exc(limit=2))
            time.sleep(5)


scheduler = Scheduler()


# Video di Instagram sulla TV: collegamento ufficiale "Instagram API con login Instagram"
# (profili Business o Creator). La chiave resta in un file del server e non arriva mai al pannello.
INSTAGRAM_API = os.environ.get("CARELLAS_INSTAGRAM_API", "https://graph.instagram.com/v21.0").rstrip("/")
INSTAGRAM_REFRESH_URL = os.environ.get("CARELLAS_INSTAGRAM_REFRESH_URL", "https://graph.instagram.com/refresh_access_token")
INSTAGRAM_TOKEN_FILE = DATA_DIR / "instagram_token.json"
INSTAGRAM_PREFIX = "Instagram_"
INSTAGRAM_MAX_VIDEO = 500 * 1024 * 1024
INSTAGRAM_REFRESH_DAYS = 20


def instagram_settings():
    raw = store.config.get("social", {}).get("instagram", {})
    try:
        keep = int(raw.get("keep", 3))
    except (TypeError, ValueError):
        keep = 3
    try:
        interval = int(raw.get("interval_minutes", 15))
    except (TypeError, ValueError):
        interval = 15
    try:
        photo_seconds = int(raw.get("photo_seconds", 10))
    except (TypeError, ValueError):
        photo_seconds = 10
    return {
        "enabled": raw.get("enabled") is True,
        "channel_id": safe_channel_id(raw.get("channel_id", "")) if raw.get("channel_id") else "",
        "keep": max(1, min(keep, 10)),
        "interval_minutes": max(5, min(interval, 240)),
        # Anche i post con foto e i caroselli (foto e video del carosello, nell'ordine del post).
        "include_photos": raw.get("include_photos") is not False,
        "photo_seconds": max(2, min(photo_seconds, 60)),
    }


class InstagramImporter(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="instagram")
        self.lock = threading.Lock()
        self.next_check = 0.0
        self.state = {"last_check": "", "last_error": "", "imported": []}

    # --- chiave di accesso -------------------------------------------------------
    def token_info(self):
        try:
            data = json.loads(INSTAGRAM_TOKEN_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and data.get("token") else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _write_token(self, data):
        INSTAGRAM_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = INSTAGRAM_TOKEN_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temporary, INSTAGRAM_TOKEN_FILE)

    def save_token(self, token):
        token = str(token or "").strip()
        if not token:
            INSTAGRAM_TOKEN_FILE.unlink(missing_ok=True)
            store.log("info", "Collegamento Instagram rimosso")
            return ""
        profile = self._get(f"{INSTAGRAM_API}/me", {"fields": "user_id,username", "access_token": token})
        username = str(profile.get("username", ""))
        self._write_token({"token": token, "saved_at": time.time(), "username": username})
        store.log("success", f"Instagram collegato: @{username}")
        self.next_check = 0.0
        return username

    def _refresh_if_needed(self, info):
        # La chiave lunga dura 60 giorni: si rinnova da sola ogni 20.
        if time.time() - float(info.get("saved_at", 0)) < INSTAGRAM_REFRESH_DAYS * 86400:
            return info["token"]
        data = self._get(INSTAGRAM_REFRESH_URL, {"grant_type": "ig_refresh_token", "access_token": info["token"]})
        token = data.get("access_token") or info["token"]
        self._write_token({**info, "token": token, "saved_at": time.time()})
        store.log("info", "Chiave di Instagram rinnovata")
        return token

    # --- richieste ------------------------------------------------------------------
    @staticmethod
    def _get(url, params):
        request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers={"User-Agent": "CarellasMediaAds"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            try:
                message = json.loads(error.read() or b"{}").get("error", {}).get("message", "")
            except (ValueError, AttributeError):
                message = ""
            raise RuntimeError(f"Instagram ha risposto {error.code}: {message or error.reason}") from None
        except urllib.error.URLError as error:
            raise RuntimeError(f"Instagram non raggiungibile: {error.reason}") from None

    @staticmethod
    def _download(url, target):
        temporary = target.with_name(f".{target.name}.download")
        request = urllib.request.Request(url, headers={"User-Agent": "CarellasMediaAds"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as handle:
                size = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > INSTAGRAM_MAX_VIDEO:
                        raise RuntimeError("Video di Instagram troppo grande")
                    handle.write(chunk)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    # --- controllo -------------------------------------------------------------------
    def public_status(self):
        info = self.token_info()
        with self.lock:
            return {**copy.deepcopy(self.state), "token_set": bool(info), "username": info.get("username", "")}

    def check(self, force=False):
        settings = instagram_settings()
        if not force and (not settings["enabled"] or time.time() < self.next_check):
            return self.public_status()
        self.next_check = time.time() + settings["interval_minutes"] * 60
        imported = []
        try:
            info = self.token_info()
            if not info:
                raise RuntimeError("Manca la chiave di accesso di Instagram")
            channel = iptv.channel(settings["channel_id"]) if settings["channel_id"] else None
            if not channel:
                raise RuntimeError("Scegli il canale TV per i video di Instagram")
            token = self._refresh_if_needed(info)
            data = self._get(f"{INSTAGRAM_API}/me/media", {
                "fields": "id,media_type,media_url,timestamp,children{id,media_type,media_url}",
                "limit": "25", "access_token": token,
            })
            posts = sorted(data.get("data", []), key=lambda item: str(item.get("timestamp", "")), reverse=True)
            entries, selected = [], 0
            for post in posts:
                parts = (post.get("children") or {}).get("data", []) if post.get("media_type") == "CAROUSEL_ALBUM" else [post]
                media = [part for part in parts if part.get("media_url") and part.get("media_type") in {"VIDEO", "IMAGE"}]
                if not settings["include_photos"]:
                    media = [part for part in media if part.get("media_type") == "VIDEO"]
                if not media:
                    continue
                entries += media
                selected += 1
                if selected >= settings["keep"]:
                    break
            items = []
            for part in entries:
                kind = "video" if part["media_type"] == "VIDEO" else "image"
                name = safe_name(f"{INSTAGRAM_PREFIX}{part.get('id', '')}{'.mp4' if kind == 'video' else '.jpg'}")
                target = MEDIA_DIR / name
                if not target.exists():
                    self._download(part["media_url"], target)
                    imported.append(name)
                    store.log("success", f"Nuovo contenuto di Instagram scaricato: {name}")
                items.append((name, kind))
            self._update_channel(channel.get("id", ""), items, settings["photo_seconds"])
            message = ""
        except Exception as error:
            message = str(error)
            store.log("warning", f"Instagram: {message}")
        with self.lock:
            self.state = {
                "last_check": now_iso(), "last_error": message,
                "imported": (imported + self.state.get("imported", []))[:10],
            }
        return self.public_status()

    def _update_channel(self, channel_id, items, photo_seconds=10):
        """Nel canale restano solo gli ultimi post di Instagram, in fondo; gli altri contenuti non vengono toccati."""
        channels = copy.deepcopy(store.config.get("iptv", {}).get("channels", []))
        channel = next((item for item in channels if safe_channel_id(item.get("id", "")) == safe_channel_id(channel_id)), None)
        if not channel:
            return
        playlist = channel.get("playlist") or []
        others = [item for item in playlist if not str(item.get("name", "")).startswith(INSTAGRAM_PREFIX)]
        updated = others + [
            {"name": name, "kind": "video", "fit": "smart"} if kind == "video"
            else {"name": name, "kind": "image", "duration": photo_seconds, "fit": "smart", "effect": "fade"}
            for name, kind in items
        ]
        if updated == playlist:
            return
        channel["playlist"] = updated
        store.update({"iptv": {"channels": channels}})
        iptv.build_async(channel_id)
        store.log("info", f"Contenuti di Instagram aggiornati nel canale {channel.get('name', channel_id)}")

    def run(self):
        while True:
            try:
                self.check()
            except Exception:
                store.log("error", traceback.format_exc(limit=2))
            time.sleep(30)


instagram = InstagramImporter()


# Eventi dal sito del ristorante (WordPress): ogni evento futuro della categoria scelta diventa
# una pagina sulla TV, con le prime due pagine del volantino PDF o, se manca, l'immagine dell'evento.
EVENTS_PREFIX = "Evento_"
EVENTS_MAX_FILE = 40 * 1024 * 1024
EVENTS_DEFAULT_URL = "https://carellas.de"


def events_settings():
    raw = store.config.get("social", {}).get("website_events", {})

    def number(key, default, low, high):
        try:
            return max(low, min(int(raw.get(key, default)), high))
        except (TypeError, ValueError):
            return default

    url = str(raw.get("url") or EVENTS_DEFAULT_URL).strip().rstrip("/")
    if not re.match(r"^https?://[^/\s]+", url):
        url = EVENTS_DEFAULT_URL
    return {
        "enabled": raw.get("enabled") is True,
        "url": url,
        "category": re.sub(r"[^a-z0-9_-]", "", str(raw.get("category") or "events").lower()) or "events",
        "channel_id": safe_channel_id(raw.get("channel_id", "")) if raw.get("channel_id") else "",
        "seconds": number("seconds", 15, 5, 60),
        "keep": number("keep", 5, 1, 10),
        "interval_minutes": number("interval_minutes", 60, 15, 1440),
    }


def event_dates(text, reference):
    """Date trovate nel testo dell'evento: 30.10.2026, 30.10.26 oppure 25.12. (anno dedotto)."""
    text = re.sub(r"\[/?[a-z_]+[^\]]*\]", " ", text)
    found = []
    for match in re.finditer(r"(?<![\d.,])(\d{1,2})\.\s?(\d{1,2})\.(?:\s?(\d{4}|\d{2})(?!\d))?", text):
        day, month, year = int(match.group(1)), int(match.group(2)), match.group(3)
        try:
            if year:
                moment = datetime(int(year) + (2000 if len(year) == 2 else 0), month, day).date()
            else:
                moment = datetime(reference.year, month, day).date()
                if moment < reference - timedelta(days=7):
                    moment = datetime(reference.year + 1, month, day).date()
        except ValueError:
            continue
        found.append(moment)
    return sorted(set(found))


def plain_text(markup):
    # I tag di blocco separano le parole, quelli in linea no (es. <strong>2</strong>4.12. resta 24.12.).
    text = re.sub(r"</?(?:p|br|div|li|ul|ol|h[1-6]|tr|td|th|table|section|article)[^>]*>", " ", str(markup or ""), flags=re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text))).strip()


def image_aspect(path):
    """Rapporto larghezza/altezza di un'immagine (None se non si riesce a leggere)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "csv=p=0:s=x", str(path)], capture_output=True, text=True, timeout=20)
        width, height = (int(value) for value in result.stdout.strip().split("x")[:2])
        return width / height if height else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def compose_event_slide(pages, target):
    """Pagina TV 1920x1080: le immagini intere e affiancate, sopra la prima sfocata come sfondo.
    Un'immagine orizzontale simile allo schermo (come quelle del sito) lo riempie tutto, senza bande."""
    pages = list(pages)[:2]
    aspect = image_aspect(pages[0]) if len(pages) == 1 else None
    if aspect and 1.45 <= aspect <= 2.2:
        # Si tagliano solo i bordi in più: niente bande sfocate ai lati o sopra e sotto.
        command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(pages[0]),
                   "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,format=yuvj420p",
                   "-frames:v", "1", "-q:v", "2"]
        temporary = target.with_name(f".{target.stem}.tmp.jpg")
        try:
            subprocess.run(command + [str(temporary)], check=True, timeout=120,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            os.replace(temporary, target)
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f"Pagina dell'evento non creata: {error.stderr.decode('utf-8', 'replace')[-200:]}") from None
        finally:
            temporary.unlink(missing_ok=True)
        return
    box_width, box_height = (1920, 1080) if len(pages) == 1 else (900, 1000)
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for page in pages:
        command += ["-i", str(page)]
    parts = ["[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
             "boxblur=24:2,eq=brightness=-0.22,format=rgb24[bg]"]
    for index in range(len(pages)):
        gap = 36 if index < len(pages) - 1 else 0
        parts.append(f"[{index}:v]scale={box_width}:{box_height}:force_original_aspect_ratio=decrease,format=rgba,"
                     f"pad=iw+{gap}:{box_height}:0:(oh-ih)/2:color=0x00000000[p{index}]")
    if len(pages) > 1:
        parts.append("".join(f"[p{index}]" for index in range(len(pages))) + f"hstack=inputs={len(pages)}[row]")
        row = "[row]"
    else:
        row = "[p0]"
    parts.append(f"[bg]{row}overlay=(W-w)/2:(H-h)/2:format=rgb[out]")
    temporary = target.with_name(f".{target.stem}.tmp.jpg")
    command += ["-filter_complex", ";".join(parts), "-map", "[out]", "-frames:v", "1", "-q:v", "3", str(temporary)]
    try:
        subprocess.run(command, check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        os.replace(temporary, target)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"Pagina dell'evento non creata: {error.stderr.decode('utf-8', 'replace')[-200:]}") from None
    finally:
        temporary.unlink(missing_ok=True)


class WebsiteEventsImporter(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="website-events")
        self.lock = threading.Lock()
        self.next_check = 0.0
        self.state = {"last_check": "", "last_error": "", "events": []}

    @staticmethod
    def _get(url, params):
        request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}",
                                         headers={"User-Agent": "CarellasMediaAds", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read() or b"[]")
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"Il sito ha risposto {error.code}: {error.reason}") from None
        except urllib.error.URLError as error:
            raise RuntimeError(f"Sito non raggiungibile: {error.reason}") from None
        except ValueError:
            raise RuntimeError("Il sito non ha restituito l'elenco degli eventi (serve WordPress)") from None

    @staticmethod
    def _download(url, target):
        if not re.match(r"^https?://", str(url)):
            raise RuntimeError("Indirizzo del file non valido")
        request = urllib.request.Request(url, headers={"User-Agent": "CarellasMediaAds"})
        with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as handle:
            size = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > EVENTS_MAX_FILE:
                    raise RuntimeError("File dell'evento troppo grande")
                handle.write(chunk)

    def public_status(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def fetch_events(self, settings, today=None):
        """Eventi futuri della categoria, dal più vicino; quelli senza data o già passati non vanno in TV."""
        today = today or datetime.now().date()
        api = f"{settings['url']}/wp-json/wp/v2"
        categories = self._get(f"{api}/categories", {"slug": settings["category"], "_fields": "id,slug"})
        if not isinstance(categories, list) or not categories:
            raise RuntimeError(f"Categoria «{settings['category']}» non trovata sul sito")
        posts = self._get(f"{api}/posts", {
            "categories": categories[0].get("id"), "per_page": "20", "_embed": "wp:featuredmedia",
            "_fields": "id,link,modified,title,content,featured_media,_links,_embedded",
        })
        events = []
        for post in posts if isinstance(posts, list) else []:
            content = str((post.get("content") or {}).get("rendered", ""))
            try:
                reference = datetime.fromisoformat(str(post.get("modified", ""))[:19]).date()
            except ValueError:
                reference = today
            upcoming = [day for day in event_dates(plain_text(content), reference) if day >= today]
            if not upcoming:
                continue
            flyer = next((link for link in re.findall(r'href="([^"]+?\.pdf)"', content, re.I)), "")
            media = ((post.get("_embedded") or {}).get("wp:featuredmedia") or [{}])[0] or {}
            events.append({
                "id": str(post.get("id", "")),
                "title": plain_text((post.get("title") or {}).get("rendered", ""))[:120],
                "link": str(post.get("link", "")),
                "date": upcoming[0].isoformat(),
                "modified": str(post.get("modified", "")),
                "flyer": html.unescape(flyer),
                "image": str(media.get("source_url", "")),
            })
        events.sort(key=lambda item: item["date"])
        return events[:settings["keep"]]

    def slides(self, event):
        """Pagine TV dell'evento: l'immagine a tutto schermo e poi il volantino (prime due pagine affiancate).
        Si creano una volta sola; se l'evento viene modificato sul sito cambiano nome e si rifanno."""
        # «v2»: le immagini del sito ora riempiono tutto lo schermo; le pagine già create si rifanno.
        signature = hashlib.sha1(f"v2|{event['modified']}|{event['flyer']}|{event['image']}".encode()).hexdigest()[:8]
        stem = f"{EVENTS_PREFIX}{safe_channel_id(event['id'])}_{signature}"
        wanted = []
        if event["image"]:
            wanted.append(("immagine", MEDIA_DIR / safe_name(f"{stem}_immagine.jpg")))
        if event["flyer"] and shutil.which("pdftoppm"):
            wanted.append(("volantino", MEDIA_DIR / safe_name(f"{stem}_volantino.jpg")))
        names, created = [], False
        for kind, target in wanted:
            if not target.exists():
                work = Path(tempfile.mkdtemp(prefix="evento-", dir=str(DATA_DIR)))
                try:
                    if kind == "immagine":
                        suffix = Path(urllib.parse.urlsplit(event["image"]).path).suffix.lower()
                        image = work / f"immagine{suffix if suffix in ALLOWED['image'] else '.jpg'}"
                        self._download(event["image"], image)
                        pages = [image]
                    else:
                        self._download(event["flyer"], work / "volantino.pdf")
                        subprocess.run([shutil.which("pdftoppm"), "-png", "-r", "110", "-f", "1", "-l", "2",
                                        str(work / "volantino.pdf"), str(work / "pagina")], check=True, timeout=120,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        pages = sorted(work.glob("pagina-*.png"), key=lambda item: int(item.stem.rsplit("-", 1)[1]))
                        if not pages:
                            raise RuntimeError("volantino senza pagine")
                    compose_event_slide(pages, target)
                    created = True
                except Exception as error:
                    store.log("warning", f"Evento «{event['title']}»: {kind} non usato ({error})")
                    continue
                finally:
                    shutil.rmtree(work, ignore_errors=True)
            names.append(target.name)
        if not names:
            raise RuntimeError(f"L'evento «{event['title']}» non ha né immagine né volantino PDF utilizzabili")
        return names, created

    def check(self, force=False):
        settings = events_settings()
        if not force and (not settings["enabled"] or time.time() < self.next_check):
            return self.public_status()
        self.next_check = time.time() + settings["interval_minutes"] * 60
        shown, message = [], ""
        try:
            channel = iptv.channel(settings["channel_id"]) if settings["channel_id"] else None
            if not channel:
                raise RuntimeError("Scegli il canale TV per gli eventi del sito")
            names = []
            for event in self.fetch_events(settings):
                event_names, created = self.slides(event)
                if created:
                    store.log("success", f"Nuovo evento dal sito sulla TV: {event['title']} ({event['date']})")
                names += event_names
                shown.append({key: event[key] for key in ("title", "date", "link")})
            self._update_channel(channel.get("id", ""), names, settings["seconds"])
        except Exception as error:
            message = str(error)
            store.log("warning", f"Eventi dal sito: {message}")
        with self.lock:
            self.state = {"last_check": now_iso(), "last_error": message,
                          "events": shown if not message else self.state.get("events", [])}
        return self.public_status()

    def _update_channel(self, channel_id, names, seconds):
        """Gli eventi restano nel punto del canale dove sono già; quelli passati spariscono da soli."""
        channels = copy.deepcopy(store.config.get("iptv", {}).get("channels", []))
        channel = next((item for item in channels if safe_channel_id(item.get("id", "")) == safe_channel_id(channel_id)), None)
        if not channel:
            return
        playlist = channel.get("playlist") or []
        is_event = [str(item.get("name", "")).startswith(EVENTS_PREFIX) for item in playlist]
        others = [item for item, flag in zip(playlist, is_event) if not flag]
        position = is_event.index(True) if True in is_event else len(playlist)
        position -= sum(is_event[:position])
        items = [{"name": name, "kind": "image", "duration": seconds, "fit": "smart", "effect": "fade"} for name in names]
        updated = others[:position] + items + others[position:]
        if updated != playlist:
            channel["playlist"] = updated
            store.update({"iptv": {"channels": channels}})
            iptv.build_async(channel_id)
            store.log("info", f"Eventi del sito aggiornati nel canale {channel.get('name', channel_id)}")
        # Le pagine di eventi passati o modificati non servono più a nessun canale.
        used = {item.get("name") for ch in store.config.get("iptv", {}).get("channels", []) for item in ch.get("playlist") or []}
        for path in MEDIA_DIR.glob(f"{EVENTS_PREFIX}*.jpg"):
            if path.name not in used:
                path.unlink(missing_ok=True)

    def run(self):
        while True:
            try:
                self.check()
            except Exception:
                store.log("error", traceback.format_exc(limit=2))
            time.sleep(30)


website_events = WebsiteEventsImporter()


class Handler(BaseHTTPRequestHandler):
    server_version = "CarellasMediaAds/0.4.47"
    # Utente entrato dalla porta per l'accesso da Internet (None sulla porta locale).
    remote_user = None
    # Richiesta di un operatore (pagina per tablet): i cambi valgono solo per oggi.
    staff_request = False

    def log_message(self, fmt, *args):
        return

    def route_path(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/hassio_ingress/"):
            positions = [path.rfind(marker) for marker in ("/media/", "/assets/", "/iptv/")]
            media_position = max(positions)
            if media_position >= 0:
                return path[media_position:]
            # Dentro Home Assistant le pagine arrivano come /api/hassio_ingress/<codice>/operatori:
            # senza togliere il prefisso, /operatori mostrava il pannello amministrativo.
            parts = path.split("/", 4)
            path = "/" + (parts[4] if len(parts) > 4 else "")
        api_position = path.rfind("/api/")
        if api_position >= 0:
            return path[api_position:]
        positions = [path.rfind(marker) for marker in ("/media/", "/assets/", "/iptv/")]
        media_position = max(positions)
        if media_position >= 0:
            return path[media_position:]
        return path

    def json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2 * 1024 * 1024:
            raise ValueError("Richiesta troppo grande")
        return json.loads(self.rfile.read(length) or b"{}")

    def send_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def send_text(self, payload, content_type="text/plain; charset=utf-8", status=200):
        data = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def send_spot_csv(self, entries, start, end):
        """Storico spot per Excel: separatore ';' e BOM UTF-8, come si aspetta Excel in IT/DE."""
        zone_of = {}
        for zone in store.config.get("zones", []):
            for player in zone.get("players", []):
                zone_of.setdefault(player, zone.get("name", player))

        def place(player):
            return zone_of.get(player) or player.removeprefix("media_player.").replace("_", " ")

        labels = {"played": "Trasmesso", "stopped": "Interrotto", "failed": "Non riuscito"}
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
        writer.writerow(["Data", "Ora", "Spot", "Zone", "Tipo", "Durata (s)", "Esito", "Note"])
        for item in reversed(entries):
            moment = item.get("time", "")
            duration = item.get("duration")
            writer.writerow([
                moment[:10], moment[11:19], item.get("spot", ""),
                ", ".join(dict.fromkeys(place(p) for p in item.get("players", []))),
                "Prova manuale" if item.get("trigger") == "manual" else "Automatico",
                str(duration).replace(".", ",") if duration is not None else "",
                labels.get(item.get("status"), item.get("status", "")), item.get("reason", ""),
            ])
        data = ("﻿" + buffer.getvalue()).encode("utf-8")
        name = f"storico-spot_{start or 'inizio'}_{end or datetime.now().date().isoformat()}.csv"
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_iptv_stream(self, channel_id):
        output = iptv.output(channel_id)
        if not output.is_file():
            self.send_text("Canale non ancora creato", status=404)
            return
        # IPTV clients commonly probe a channel with HEAD first. Starting the
        # endless FFmpeg stream for a HEAD request leaves those clients waiting
        # forever and appears as an infinite loading spinner.
        if self.command == "HEAD":
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-stream_loop", "-1",
            "-i", str(output), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
            "-mpegts_flags", "+resend_headers", "-muxdelay", "0", "-muxpreload", "0",
            "-f", "mpegts", "pipe:1",
        ]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            while True:
                chunk = process.stdout.read(128 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()

    def send_file(self, path, cache=False):
        if not path.is_file():
            self.send_error(404)
            return
        size = path.stat().st_size
        start, end, status = 0, size - 1, HTTPStatus.OK
        range_header = self.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if match and (match.group(1) or match.group(2)):
                if match.group(1):
                    start = int(match.group(1))
                    end = min(int(match.group(2) or size - 1), size - 1)
                else:
                    suffix_length = int(match.group(2))
                    start = max(0, size - suffix_length)
                    end = size - 1
                if size <= 0 or start >= size or start > end:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT
        length = max(0, end - start + 1)
        try:
            self.send_response(status)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "public, max-age=3600" if cache else "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining:
                    chunk = handle.read(min(1024 * 128, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # Sonos chiude normalmente alcune richieste di sondaggio appena
            # ha letto intestazioni o metadati del file.
            return

    def do_HEAD(self):
        self.do_GET()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def staff_state(self):
        """Stato per la pagina Operatori: casse, musica e pubblicità, senza impostazioni né password."""
        captured = {}
        original_send, original_path = self.send_json, self.path
        self.send_json = lambda payload, status=200: captured.update(payload=payload)
        self.path = "/api/state"
        try:
            Handler.do_GET(self)
        finally:
            self.send_json, self.path = original_send, original_path
        data = captured.get("payload") or {}
        config = data.get("config") or {}
        music = config.get("music") or {}
        audio = config.get("audio") or {}
        mix = music.get("mix") or {}
        runtime = data.get("runtime") or {}
        slot_keys = ("id", "name", "title", "start", "end", "duration_minutes", "days", "players", "volume",
                     "content_id", "content_type", "enabled")
        return {
            # Utente entrato con la password (None dentro Home Assistant o sulla rete locale).
            "remote_user": self.remote_user,
            "config": {
                "language": config.get("language", "it"),
                "zones": [{"id": z.get("id"), "name": z.get("name"), "players": z.get("players") or []}
                          for z in config.get("zones") or []],
                "music": {
                    "enabled": music.get("enabled"), "schedule": music.get("schedule") or [],
                    "slots": [{k: s.get(k) for k in slot_keys} for s in music.get("slots") or []],
                    "mix": {"enabled": mix.get("enabled"), "players": mix.get("players") or [],
                            "volume": mix.get("volume"),
                            "sources": [{k: s.get(k) for k in ("content_id", "content_type", "name", "kind")}
                                        for s in mix.get("sources") or []]},
                },
                "audio": {"enabled": audio.get("enabled"), "players": audio.get("players") or [],
                          "daily_limit": audio.get("daily_limit"), "schedule": audio.get("schedule") or [],
                          "volume": audio.get("volume"), "interval_minutes": audio.get("interval_minutes"),
                          "ads": audio.get("ads") or [], "mode": audio.get("mode", "rotate"),
                          "repeat_count": audio.get("repeat_count")},
            },
            # Solo i file degli spot in rotazione (niente foto né video).
            "media": [m for m in data.get("media") or [] if m.get("name") in (audio.get("ads") or [])],
            # Registro: solo musica e spot, senza TV né accessi.
            "logs": [entry for entry in data.get("logs") or []
                     if STAFF_LOG_WORDS.search(str(entry.get("message", "")))
                     and not STAFF_LOG_HIDDEN.search(str(entry.get("message", "")))],
            "sonos": data.get("sonos") or [],
            "sonos_favorites": data.get("sonos_favorites") or [],
            "runtime": {key: runtime.get(key) for key in (
                "music_active", "music_slot_id", "music_override", "music_excluded", "spots_excluded", "ads_excluded",
                "manual_volumes",
                "music_coordinator",
                "mix_week", "next_spot", "next_spot_in_seconds",
                "spot_playing", "audio_paused_until", "audio_waiting", "audio_today")},
            "ha_error": data.get("ha_error"),
        }

    def do_GET(self):
        path = self.route_path()
        try:
            if path == "/api/staff/state":
                self.send_json(self.staff_state())
                return
            if path in ("/operatori", "/operatori/"):
                # Pagina semplice per i tablet degli operatori (vedi anche l'accesso con password).
                self.send_file(APP_DIR / "operatori.html")
                return
            if path == "/api/iptv/status":
                self.send_json({"channels": iptv.runtime_status()})
                return
            if path == "/api/music/queue":
                # Brani della playlist in onda, con quello in corso.
                self.send_json(music_queue())
                return
            if path in ("/api/spots/history", "/api/spots/history.csv"):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                day = r"\d{4}-\d{2}-\d{2}"
                start = next((v for v in query.get("from", []) if re.fullmatch(day, v)), None)
                end = next((v for v in query.get("to", []) if re.fullmatch(day, v)), None)
                entries = spot_history.query(start, end)
                if path.endswith(".csv"):
                    self.send_spot_csv(entries, start, end)
                else:
                    self.send_json({"entries": entries, "from": start, "to": end})
                return
            if path == "/iptv/channels.m3u":
                lines = ["#EXTM3U"]
                for channel in iptv.channels():
                    channel_id = safe_channel_id(channel.get("id", ""))
                    name = channel.get("name") or channel_id
                    lines += [f'#EXTINF:-1 tvg-id="{channel_id}" group-title="Carellas",{name}', f"{local_base_url()}/iptv/{channel_id}/channel.m3u8"]
                self.send_text("\r\n".join(lines) + "\r\n", "audio/x-mpegurl; charset=utf-8")
                return
            match = re.fullmatch(r"/iptv/([a-z0-9-]+)\.m3u", path)
            if match:
                channel_id = safe_channel_id(match.group(1))
                channel = iptv.channel(channel_id)
                if not channel:
                    self.send_text("Canale non trovato", status=404)
                    return
                name = channel.get("name") or channel_id
                payload = f'#EXTM3U\r\n#EXTINF:-1 tvg-id="{channel_id}" group-title="Carellas",{name}\r\n{local_base_url()}/iptv/{channel_id}/channel.m3u8\r\n'
                self.send_text(payload, "audio/x-mpegurl; charset=utf-8")
                return
            match = re.fullmatch(r"/iptv/([a-z0-9-]+)/(channel\.m3u8|loop\.ts)", path)
            if match:
                channel_id = safe_channel_id(match.group(1))
                filename = match.group(2)
                target = iptv.hls_dir(channel_id) / filename
                self.send_file(target, cache=filename == "loop.ts")
                return
            match = re.fullmatch(r"/iptv/([a-z0-9-]+)\.ts", path)
            if match:
                self.send_iptv_stream(match.group(1))
                return
            if path == "/api/screen":
                cfg = store.config["tv"]
                playlist = []
                for item in cfg.get("playlist", []):
                    filename = safe_name(item.get("name", ""))
                    if filename:
                        playlist.append({
                            "name": filename,
                            "kind": item.get("kind", "video"),
                            "duration": max(2, min(int(item.get("duration", 15)), 86400)),
                            "url": media_url(filename),
                        })
                active = bool(cfg.get("enabled") and playlist and is_schedule_active(cfg.get("schedule", [])))
                self.send_json({
                    "active": active,
                    "playlist": playlist,
                    "loop": cfg.get("loop", True),
                    "fit": cfg.get("fit", "contain"),
                    "muted": cfg.get("muted", True),
                })
                return
            if path == "/api/state":
                entities = []
                media_entities = []
                sonos_detected = False
                power_entities = []
                favorites_by_id = {}
                sonos = []
                configured = configured_sonos_players()
                error = None
                try:
                    for state in ha.states():
                        entity_id = state.get("entity_id", "")
                        attrs = state.get("attributes", {})
                        item = {
                            "entity_id": entity_id,
                            "name": attrs.get("friendly_name", entity_id),
                            "state": state.get("state"),
                            "device_class": attrs.get("device_class"),
                        }
                        if entity_id.startswith("media_player."):
                            media_entities.append(item)
                            sonos_detail = sonos_state_detail(state, configured)
                            if sonos_detail:
                                sonos.append(sonos_detail)
                            if isinstance(attrs.get("group_members"), list):
                                sonos_detected = True
                                if state.get("state") != "unavailable":
                                    entities.append(item)
                        if entity_id.startswith(("media_player.", "switch.", "button.", "script.")):
                            power_entities.append(item)
                        favorites = attrs.get("items")
                        if isinstance(favorites, dict):
                            for media_id, name in favorites.items():
                                if str(media_id).startswith("FV:"):
                                    favorites_by_id[str(media_id)] = str(name)
                        if entity_id.startswith("media_player.") and isinstance(attrs.get("group_members"), list):
                            for source in attrs.get("source_list") or []:
                                source = str(source).strip()
                                if source:
                                    favorites_by_id[f"source:{source}"] = {
                                        "id": source,
                                        "name": source,
                                        "type": "sonos_source",
                                    }
                except Exception as exc:
                    error = str(exc)
                # Compatibilità con installazioni Sonos meno recenti, nelle
                # quali group_members potrebbe non essere ancora esposto.
                if not sonos_detected:
                    entities = media_entities
                for favorite in load_sonos_catalog():
                    favorites_by_id[favorite["id"]] = favorite
                self.send_json({
                    "config": store.config,
                    "media": store.media(),
                    "entities": sorted(entities, key=lambda x: x["name"].lower()),
                    "power_entities": sorted(power_entities, key=lambda x: x["name"].lower()),
                    "sonos_favorites": sorted((
                        value if isinstance(value, dict)
                        else {"id": media_id, "name": value, "type": "favorite_item_id"}
                        for media_id, value in favorites_by_id.items()
                    ), key=lambda x: x["name"].lower()),
                    "logs": store.logs,
                    "sonos": sorted(sonos, key=lambda x: x["name"].lower()),
                    "runtime": {
                        "audio_today": scheduler.audio_today(),
                        **scheduler.audio_status(),
                        "music_active": scheduler.music_active,
                        "music_slot_id": scheduler.music_slot_id,
                        "mix_week": mix_week(),
                        "music_excluded": sorted(music_excluded_players()),
                        "spots_excluded": sorted(spots_excluded_players()),
                        # Volumi scelti a mano oggi (0-100): restano anche quando la fascia ne prevede un altro.
                        "manual_volumes": {player: round(level * 100) for player, level in
                                           manual_sonos_volumes(list(SONOS_MANUAL_VOLUMES)).items()},
                        "ads_excluded": sorted(ads_excluded_today()),
                        "music_coordinator": scheduler.music_coordinator,
                        "music_override": (
                            {"mode": scheduler.music_override["mode"], "name": scheduler.music_override.get("name", "")}
                            if scheduler.music_override else None
                        ),
                        "tv_active": scheduler.tv_active,
                        "media_base_url": local_base_url(),
                        "player_url": player_public_url(),
                        "player_local_url": player_local_url(),
                        "remote_local_url": remote_local_url(),
                        "remote_port": REMOTE_PORT,
                        "remote_user": self.remote_user,
                        "instagram": instagram.public_status(),
                        "website_events": website_events.public_status(),
                        "iptv_status": iptv.runtime_status(),
                    },
                    "ha_error": error,
                })
                return
            if path.startswith("/media/"):
                name = safe_name(path.removeprefix("/media/"))
                self.send_file(MEDIA_DIR / name, cache=True)
                return
            if path.startswith("/assets/"):
                name = safe_name(path.removeprefix("/assets/"))
                self.send_file(APP_DIR / "assets" / name, cache=True)
                return
            if path in ("/screen", "/screen/"):
                self.send_file(APP_DIR / "screen.html")
                return
            if path == "/favicon.ico":
                # I browser la chiedono sempre: prima finiva nel Registro come errore.
                self.send_response(204)
                self.end_headers()
                return
            if path in ("/classic", "/classic/"):
                # Interfaccia precedente, disponibile fino al collaudo della nuova.
                self.send_file(APP_DIR / "index_classic.html")
                return
            self.send_file(APP_DIR / "index.html")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            store.log("error", f"GET {path}: {error}")
            self.send_json({"error": str(error)}, 500)

    def receive_upload_chunk(self):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        upload_id = query.get("upload_id", [""])[0].lower()
        filename = safe_name(query.get("filename", [""])[0])
        kind = query.get("kind", [""])[0]
        try:
            index = int(query.get("index", ["-1"])[0])
            total = int(query.get("total", ["0"])[0])
            file_size = int(query.get("size", ["0"])[0])
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json({"error": "Parametri del caricamento non validi"}, 400)
            return
        extension = Path(filename).suffix.lower()
        expected_total = math.ceil(file_size / UPLOAD_CHUNK_SIZE) if file_size else 0
        expected_length = min(UPLOAD_CHUNK_SIZE, file_size - index * UPLOAD_CHUNK_SIZE)
        if not re.fullmatch(r"[a-f0-9-]{8,64}", upload_id):
            self.send_json({"error": "Identificativo caricamento non valido"}, 400)
            return
        if kind not in ALLOWED or extension not in ALLOWED[kind]:
            self.send_json({"error": "Formato file non supportato"}, 400)
            return
        if file_size <= 0 or file_size > MAX_UPLOAD or total != expected_total:
            self.send_json({"error": "Dimensione file non valida (massimo 4 GB)"}, 400)
            return
        if index < 0 or index >= total or length != expected_length or length > UPLOAD_CHUNK_SIZE:
            self.send_json({"error": "Blocco del caricamento non valido"}, 400)
            return

        metadata_path = UPLOAD_DIR / f"{upload_id}.json"
        partial_path = UPLOAD_DIR / f"{upload_id}.part"
        with upload_lock:
            if index == 0:
                if metadata_path.exists() or partial_path.exists():
                    self.send_json({"error": "Caricamento già iniziato: seleziona nuovamente il file"}, 409)
                    return
                free_space = shutil.disk_usage(MEDIA_DIR).free
                if free_space - file_size < MIN_FREE_AFTER_UPLOAD:
                    available_mb = max(0, (free_space - MIN_FREE_AFTER_UPLOAD) // (1024 * 1024))
                    self.send_json({
                        "error": f"Spazio insufficiente. Disponibili circa {available_mb} MB mantenendo 512 MB liberi"
                    }, 507)
                    return
                target = MEDIA_DIR / filename
                if target.exists():
                    target = MEDIA_DIR / f"{target.stem}-{uuid.uuid4().hex[:6]}{target.suffix}"
                metadata = {
                    "filename": filename,
                    "target_name": target.name,
                    "kind": kind,
                    "size": file_size,
                    "total": total,
                    "next_index": 0,
                    "written": 0,
                }
                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            else:
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError):
                    self.send_json({"error": "Sessione di caricamento scaduta: riprova"}, 409)
                    return

            if any((
                metadata.get("filename") != filename,
                metadata.get("kind") != kind,
                metadata.get("size") != file_size,
                metadata.get("total") != total,
                metadata.get("next_index") != index,
            )):
                self.send_json({"error": "Ordine dei blocchi non valido: riprova il caricamento"}, 409)
                return

            remaining = length
            try:
                with partial_path.open("ab") as handle:
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise IOError("Caricamento interrotto")
                        handle.write(chunk)
                        remaining -= len(chunk)
            except Exception:
                metadata_path.unlink(missing_ok=True)
                partial_path.unlink(missing_ok=True)
                raise

            metadata["written"] += length
            metadata["next_index"] += 1
            complete = metadata["next_index"] == total
            if complete:
                if metadata["written"] != file_size or partial_path.stat().st_size != file_size:
                    metadata_path.unlink(missing_ok=True)
                    partial_path.unlink(missing_ok=True)
                    self.send_json({"error": "File ricevuto incompleto: riprova"}, 400)
                    return
                target = MEDIA_DIR / metadata["target_name"]
                destination_temporary = MEDIA_DIR / f".{target.name}.{upload_id}.upload"
                try:
                    # /data and /media are separate mounts in Home Assistant OS.
                    # Copy into the destination filesystem first, then rename
                    # atomically so incomplete files never appear in the library.
                    shutil.copy2(partial_path, destination_temporary)
                    os.replace(destination_temporary, target)
                    partial_path.unlink(missing_ok=True)
                    metadata_path.unlink(missing_ok=True)
                except Exception:
                    destination_temporary.unlink(missing_ok=True)
                    partial_path.unlink(missing_ok=True)
                    metadata_path.unlink(missing_ok=True)
                    raise
                store.log("success", f"File caricato da remoto: {target.name}")
                self.send_json({"ok": True, "complete": True, "name": target.name, "kind": kind})
                return

            temporary_metadata = metadata_path.with_suffix(".tmp")
            temporary_metadata.write_text(json.dumps(metadata), encoding="utf-8")
            os.replace(temporary_metadata, metadata_path)
            self.send_json({"ok": True, "complete": False, "next_index": metadata["next_index"]})

    def do_POST(self):
        path = self.route_path()
        try:
            if path == "/api/config":
                old_channels = {
                    safe_channel_id(channel.get("id", "")): json.dumps(
                        channel.get("playlist") or [], ensure_ascii=False, sort_keys=True
                    )
                    for channel in store.config.get("iptv", {}).get("channels", [])
                }
                config = store.update(self.json_body())
                rebuilt = []
                new_channels = {
                    safe_channel_id(channel.get("id", "")): channel
                    for channel in config.get("iptv", {}).get("channels", [])
                }
                for channel_id, channel in new_channels.items():
                    signature = json.dumps(channel.get("playlist") or [], ensure_ascii=False, sort_keys=True)
                    if old_channels.get(channel_id) == signature:
                        continue
                    if channel.get("playlist"):
                        iptv.build_async(channel_id)
                        rebuilt.append(channel_id)
                        store.log("info", f"Ordine aggiornato: ricostruzione automatica del canale {channel.get('name', channel_id)}")
                    else:
                        shutil.rmtree(iptv.output(channel_id).parent, ignore_errors=True)
                        iptv.set_status(channel_id, "not_built", "Canale vuoto")
                self.send_json({"ok": True, "config": config, "rebuilt_channels": rebuilt})
                return
            if path == "/api/social/instagram/token":
                username = instagram.save_token(self.json_body().get("token", ""))
                self.send_json({"ok": True, "username": username})
                return
            if path == "/api/social/instagram/check":
                self.send_json({"ok": True, **instagram.check(force=True)})
                return
            if path == "/api/social/events/check":
                self.send_json({"ok": True, **website_events.check(force=True)})
                return
            if path == "/api/upload/chunk":
                self.receive_upload_chunk()
                return
            if path == "/api/menu/preview":
                menu = menu_settings({"menu": self.json_body().get("menu") or {}})
                preview_dir = IPTV_DIR / "menu-preview"
                preview_dir.mkdir(parents=True, exist_ok=True)
                images = []
                for group in range(len(menu_groups(menu))):
                    target = preview_dir / f"{uuid.uuid4().hex}.png"
                    try:
                        render_menu_frame(menu, target, preview=True, group=group)
                        images.append("data:image/png;base64," + base64.b64encode(target.read_bytes()).decode("ascii"))
                    finally:
                        target.unlink(missing_ok=True)
                self.send_json({"ok": True, "image": images[0], "images": images})
                return
            if path == "/api/menu/pdf":
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                filename = safe_name(query.get("filename", ["menu.pdf"])[0])
                if Path(filename).suffix.lower() != ".pdf":
                    self.send_json({"error": "Carica un file PDF"}, 400)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MENU_MAX_PDF:
                    self.send_json({"error": "PDF non valido (massimo 50 MB)"}, 400)
                    return
                work = IPTV_DIR / "menu-pdf" / uuid.uuid4().hex
                work.mkdir(parents=True, exist_ok=True)
                try:
                    pdf = work / "menu.pdf"
                    pdf.write_bytes(self.rfile.read(length))
                    pages = convert_menu_pdf(pdf, Path(filename).stem)
                finally:
                    shutil.rmtree(work, ignore_errors=True)
                store.log("success", f"Menu PDF convertito in {len(pages)} pagine: {filename}")
                self.send_json({"ok": True, "pages": pages})
                return
            if path == "/api/upload":
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                filename = safe_name(query.get("filename", [""])[0])
                kind = query.get("kind", [""])[0]
                extension = Path(filename).suffix.lower()
                if kind not in ALLOWED or extension not in ALLOWED[kind]:
                    self.send_json({"error": "Formato file non supportato"}, 400)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_UPLOAD:
                    self.send_json({"error": "Dimensione file non valida (massimo 4 GB)"}, 400)
                    return
                free_space = shutil.disk_usage(MEDIA_DIR).free
                if free_space - length < MIN_FREE_AFTER_UPLOAD:
                    available_mb = max(0, (free_space - MIN_FREE_AFTER_UPLOAD) // (1024 * 1024))
                    self.send_json({
                        "error": f"Spazio insufficiente. Disponibili circa {available_mb} MB mantenendo 512 MB liberi"
                    }, 507)
                    return
                target = MEDIA_DIR / filename
                if target.exists():
                    target = MEDIA_DIR / f"{target.stem}-{uuid.uuid4().hex[:6]}{target.suffix}"
                remaining = length
                try:
                    with target.open("wb") as handle:
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise IOError("Caricamento interrotto")
                            handle.write(chunk)
                            remaining -= len(chunk)
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
                store.log("success", f"File caricato: {target.name}")
                self.send_json({"ok": True, "name": target.name, "kind": kind})
                return
            if path == "/api/iptv/rebuild":
                body = self.json_body()
                channel_id = safe_channel_id(body.get("id", ""))
                if not iptv.channel(channel_id):
                    raise RuntimeError("Canale IPTV non trovato")
                iptv.build_async(channel_id)
                self.send_json({"ok": True, "id": channel_id})
                return
            if path == "/api/iptv/power/on":
                body = self.json_body()
                channel = iptv.channel(body.get("id", ""))
                if not channel:
                    raise RuntimeError("Canale IPTV non trovato")
                tv_power_on(channel)
                self.send_json({"ok": True})
                return
            if path == "/api/iptv/power/off":
                body = self.json_body()
                channel = iptv.channel(body.get("id", ""))
                if not channel:
                    raise RuntimeError("Canale IPTV non trovato")
                tv_power_off(channel)
                self.send_json({"ok": True})
                return
            if path == "/api/music/queue/play":
                # Un brano scelto dall'elenco della playlist parte subito.
                index = int(self.json_body().get("index", -1))
                states = {item.get("entity_id"): item for item in ha.states()}
                coordinator = music_coordinator_now(states)
                if not coordinator:
                    raise RuntimeError("Nessuna musica in onda")
                if not 0 <= index < 2000:
                    raise RuntimeError("Brano non valido")
                sonos_service("sonos", "play_queue", {"entity_id": coordinator, "queue_position": index})
                who = f" da {self.remote_user}" if self.remote_user else ""
                store.log("info", f"Brano scelto dall'elenco: n. {index + 1}{who}")
                self.send_json({"ok": True})
                return
            if path == "/api/spot/today":
                # Uno spot tolto o rimesso nella rotazione solo per oggi (dagli operatori o dal pannello).
                body = self.json_body()
                name = str(body.get("name", ""))
                if name not in store.config.get("audio", {}).get("ads", []):
                    raise RuntimeError("Spot non trovato tra quelli in rotazione")
                who = f" da {self.remote_user}" if self.remote_user else ""
                if body.get("on") is True:
                    ADS_EXCLUDED.pop(name, None)
                    store.log("info", f"Spot rimesso per oggi: {name}{who}")
                else:
                    ADS_EXCLUDED[name] = datetime.now().date().isoformat()
                    store.log("info", f"Spot tolto per oggi: {name}{who}")
                self.send_json({"ok": True})
                return
            if path == "/api/test/audio":
                body = self.json_body()
                players = [p for p in body.get("players") or [] if isinstance(p, str) and p.startswith("media_player.")]
                if audio_playback_lock.locked():
                    self.send_json({"error": "C’è già uno spot in onda: aspetta che finisca"}, 409)
                    return
                if body.get("next"):
                    body["name"] = scheduler.take_next_audio() or body.get("name")

                def manual_spot():
                    try:
                        play_audio(body.get("name"), True, players or None)
                    except Exception as error:
                        # Prima l'errore di una prova manuale andava perso nel thread.
                        spot_history.add(body.get("name"), players or store.config["audio"].get("players", []),
                                         True, status="failed", reason=error)
                        store.log("error", f"Prova spot non riuscita: {error}")

                threading.Thread(target=manual_spot, daemon=True).start()
                self.send_json({"ok": True})
                return
            if path == "/api/audio/stop":
                self.send_json({"ok": True, "active": stop_audio()})
                return
            if path == "/api/audio/skip":
                self.send_json({"ok": True, "skipped": scheduler.skip_audio()})
                return
            if path == "/api/audio/pause":
                body = self.json_body()
                scheduler.pause_audio(body.get("minutes", 60))
                self.send_json({"ok": True, **scheduler.audio_status()})
                return
            if path == "/api/test/tv":
                play_tv_item(self.json_body())
                self.send_json({"ok": True})
                return
            if path == "/api/tv/power/on":
                tv_power_on()
                self.send_json({"ok": True})
                return
            if path == "/api/tv/power/off":
                tv_power_off()
                self.send_json({"ok": True})
                return
            if path == "/api/sonos/volume":
                body = self.json_body()
                entity_id = str(body.get("entity_id", ""))
                if not entity_id.startswith("media_player."):
                    raise RuntimeError("Altoparlante Sonos non valido")
                volume = max(0, min(int(body.get("volume", 0)), 100))
                ha.service("media_player", "volume_set", {"entity_id": entity_id, "volume_level": volume / 100})
                # Volume scelto dal pannello: resta per oggi anche quando cambia la fascia musicale.
                set_manual_sonos_volume(entity_id, volume / 100)
                self.send_json({"ok": True, "volume": volume})
                return
            if path == "/api/sonos/refresh":
                body = self.json_body()
                entity_id = str(body.get("entity_id", ""))
                if not entity_id.startswith("media_player."):
                    raise RuntimeError("Altoparlante Sonos non valido")
                ha.service("homeassistant", "update_entity", {"entity_id": entity_id})
                self.send_json({"ok": True})
                return
            if path in ("/api/zone/music", "/api/zone/spots"):
                body = self.json_body()
                zone = next((z for z in store.config.get("zones", []) if str(z.get("id")) == str(body.get("zone_id"))), None)
                players = [p for p in (zone or {}).get("players") or [] if isinstance(p, str)]
                if not zone or not players:
                    raise RuntimeError("Zona non trovata o senza casse Sonos")
                on = body.get("on") is True
                if path == "/api/zone/music":
                    scheduler.zone_music(players, on)
                else:
                    if body.get("today") is True or self.staff_request:
                        # Operatori (o «solo per oggi»): la zona resta com'è nella configurazione e
                        # domani torna come l'ha impostata l'amministratore.
                        today = datetime.now().date().isoformat()
                        for player in players:
                            if on:
                                SPOTS_EXCLUDED.pop(player, None)
                            else:
                                SPOTS_EXCLUDED[player] = today
                        who = f" da {self.remote_user}" if self.remote_user else ""
                        store.log("info", f"Spot {'rimessi' if on else 'tolti'} per oggi nella zona {zone.get('name', '')}{who}")
                    else:
                        # Pannello amministrativo: cambia per sempre le casse che trasmettono la pubblicità.
                        current = list(store.config.get("audio", {}).get("players", []))
                        updated = list(dict.fromkeys(current + players)) if on else [p for p in current if p not in players]
                        store.update({"audio": {"players": updated}})
                        store.log("info", f"Spot {'attivati' if on else 'tolti'} nella zona {zone.get('name', '')}")
                self.send_json({"ok": True})
                return
            if path == "/api/music/control":
                scheduler.music_control(str(self.json_body().get("action", "")))
                self.send_json({"ok": True})
                return
            if path == "/api/music/manual":
                body = self.json_body()
                if body.get("action") == "off":
                    scheduler.music_off()
                    self.send_json({"ok": True})
                    return
                selection = None
                if body.get("content_id"):
                    music = store.config["music"]
                    mix = mix_settings()
                    selection = {
                        "name": str(body.get("name") or body["content_id"])[:120],
                        "title": str(body.get("name") or body["content_id"])[:120],
                        "content_id": str(body["content_id"]),
                        "content_type": str(body.get("content_type") or "favorite_item_id"),
                        "players": [p for p in body.get("players") or mix["players"] if isinstance(p, str)],
                        "volume": max(0, min(int(body.get("volume", mix["volume"] or music.get("volume", 25))), 100)),
                    }
                try:
                    scheduler.music_on(selection)
                except Exception as error:
                    if sonos_transition_busy(error):
                        raise RuntimeError("Il Sonos sta ancora cambiando sorgente e non risponde: "
                                           "riprova tra qualche secondo") from None
                    raise
                self.send_json({"ok": True})
                return
            if path == "/api/music/start":
                body = self.json_body()
                selection = None
                slot_id = str(body.get("slot_id") or "")
                if slot_id:
                    # Prova di una singola fascia dal Palinsesto.
                    selection = next((
                        slot for slot in store.config["music"].get("slots", [])
                        if str(slot.get("id")) == slot_id
                    ), None)
                    if not selection:
                        raise RuntimeError("Fascia musicale non trovata: salva prima le modifiche")
                elif body.get("scheduled"):
                    # «Riavvia la musica»: prima partiva la prima fascia del Palinsesto anche con il
                    # Mix automatico acceso, su casse diverse e senza che il motore lo sapesse.
                    current = scheduler.restart_scheduled()
                    if current:
                        name = current.get("name") or current.get("title") or current.get("content_id", "")
                        store.log("info", f"Riavvio della musica in corso: «{name}»")
                        self.send_json({"ok": True, "source": name})
                        return
                    music = store.config["music"]
                    selection = active_music_slot(music)
                    if not selection:
                        selection = next((
                            slot for slot in music.get("slots", [])
                            if slot.get("enabled", True)
                            and slot.get("content_id")
                            and (slot.get("players") or music.get("players"))
                        ), None)
                    selection = selection or music
                try:
                    start_music(selection)
                except Exception as error:
                    if sonos_transition_busy(error):
                        raise RuntimeError("Il Sonos sta ancora cambiando sorgente e non risponde: "
                                           "riprova tra qualche secondo") from None
                    if sonos_source_rejected(error):
                        raise RuntimeError(music_error_message(error, selection)) from None
                    raise
                selected = selection or store.config["music"]
                self.send_json({
                    "ok": True,
                    "source": selected.get("name") or selected.get("title")
                    or selected.get("content_id", ""),
                })
                return
            if path == "/api/radio/search":
                self.send_json({"ok": True, "stations": radio_browser_search(self.json_body().get("q", ""))})
                return
            if path == "/api/music/refresh":
                body = self.json_body()
                entity_id = str(body.get("entity_id", ""))
                if not entity_id.startswith("media_player."):
                    raise RuntimeError("Seleziona prima un altoparlante Sonos")
                catalog = refresh_sonos_catalog(entity_id)
                self.send_json({"ok": True, **catalog})
                return
            if path == "/api/music/browse":
                body = self.json_body()
                entity_id = str(body.get("entity_id", ""))
                if not entity_id.startswith("media_player."):
                    raise RuntimeError("Seleziona prima un altoparlante Sonos")
                browser = _sonos_browser(
                    entity_id, body.get("media_content_type"), body.get("media_content_id")
                )
                self.send_json({"ok": True, "browser": browser})
                return
            if path == "/api/music/stop":
                body = self.json_body()
                players = [p for p in body.get("players") or [] if isinstance(p, str) and p.startswith("media_player.")]
                if not players and body.get("all"):
                    # Con le fasce i Sonos stanno nelle singole fasce, non in music.players.
                    music = store.config["music"]
                    players = list(music.get("players", [])) + list(scheduler.music_players or [])
                    for slot in music.get("slots", []):
                        players += slot.get("players") or []
                    # «Ferma tutto» è una musica spenta a mano: il motore non la deve far ripartire.
                    scheduler.music_off(players)
                    self.send_json({"ok": True})
                    return
                stop_music(players or None)
                self.send_json({"ok": True})
                return
            self.send_json({"error": "Operazione sconosciuta"}, 404)
        except Exception as error:
            store.log("error", f"POST {path}: {error}")
            self.send_json({"error": str(error)}, 500)

    def do_DELETE(self):
        path = self.route_path()
        if not path.startswith("/api/media/"):
            self.send_json({"error": "Operazione sconosciuta"}, 404)
            return
        name = safe_name(path.removeprefix("/api/media/"))
        target = MEDIA_DIR / name
        try:
            target.unlink()
            for section in ("audio", "tv"):
                if section == "audio":
                    store.config[section]["ads"] = [x for x in store.config[section].get("ads", []) if x != name]
                else:
                    store.config[section]["playlist"] = [x for x in store.config[section].get("playlist", []) if x.get("name") != name]
            affected_channels = []
            for channel in store.config.get("iptv", {}).get("channels", []):
                before = channel.get("playlist", [])
                after = []
                changed = False
                for item in before:
                    if item.get("name") == name:
                        changed = True
                        continue
                    if item.get("kind") == "collage" and name in item.get("images", []):
                        item = copy.deepcopy(item)
                        item["images"] = [image for image in item.get("images", []) if image != name]
                        changed = True
                        if len(item["images"]) < 2:
                            continue
                    after.append(item)
                if changed:
                    channel["playlist"] = after
                    channel_id = safe_channel_id(channel.get("id", ""))
                    affected_channels.append(channel_id)
                    shutil.rmtree(iptv.output(channel_id).parent, ignore_errors=True)
                    iptv.set_status(channel_id, "not_built", "Contenuto eliminato: ricrea il canale")
            store.save()
            message = f"File eliminato: {name}"
            if affected_channels:
                message += f"; {len(affected_channels)} canale/i IPTV da ricreare"
            store.log("info", message)
            self.send_json({"ok": True})
        except FileNotFoundError:
            self.send_json({"error": "File non trovato"}, 404)


PLAYER_LOGIN_LOCK = threading.Lock()
PLAYER_LOGIN_ATTEMPTS = {}
DUMMY_PASSWORD_HASH = password_hash("carellas-invalid-login")


def player_account(username):
    username = str(username or "").strip().lower()
    return next((item for item in store.config.get("browser_player", {}).get("users", [])
                 if item.get("username") == username and item.get("enabled", True)), None)


def player_session_token(username, lifetime=30 * 24 * 60 * 60):
    account = player_account(username)
    if not account:
        raise ValueError("Account Player TV non disponibile")
    payload = json.dumps({
        "username": username,
        "expires": int(time.time()) + lifetime,
        "version": hashlib.sha256(account["password_hash"].encode()).hexdigest()[:16],
    }, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    secret = store.config["browser_player"]["session_secret"].encode()
    signature = hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def player_session_username(token):
    try:
        encoded, signature = token.split(".", 1)
        secret = store.config["browser_player"]["session_secret"].encode()
        expected = hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if int(payload.get("expires", 0)) < int(time.time()):
            return None
        username = str(payload.get("username", ""))
        account = player_account(username)
        expected_version = hashlib.sha256(account["password_hash"].encode()).hexdigest()[:16] if account else ""
        return username if account and hmac.compare_digest(str(payload.get("version", "")), expected_version) else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


class PlayerHandler(Handler):
    """Porta pubblica limitata al player TV: nessun accesso alla configurazione dell'add-on."""

    server_version = "CarellasTVPlayer/0.4.47"

    def player_username(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("carellas_player")
        return player_session_username(morsel.value) if morsel else None

    def player_user(self):
        return player_account(self.player_username())

    def send_player_json(self, payload, status=200, cookie=None):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            secure = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
            attributes = "; Path=/; HttpOnly; SameSite=Lax"
            if secure:
                attributes += "; Secure"
            self.send_header("Set-Cookie", f"carellas_player={cookie}{attributes}")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def client_key(self):
        forwarded = self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For", "")
        return forwarded.split(",", 1)[0].strip() or self.client_address[0]

    def login_allowed(self):
        key, now = self.client_key(), time.monotonic()
        with PLAYER_LOGIN_LOCK:
            attempts = [stamp for stamp in PLAYER_LOGIN_ATTEMPTS.get(key, []) if now - stamp < 300]
            PLAYER_LOGIN_ATTEMPTS[key] = attempts
            return len(attempts) < 10

    def login_failed(self):
        key = self.client_key()
        with PLAYER_LOGIN_LOCK:
            PLAYER_LOGIN_ATTEMPTS.setdefault(key, []).append(time.monotonic())

    def login_succeeded(self):
        with PLAYER_LOGIN_LOCK:
            PLAYER_LOGIN_ATTEMPTS.pop(self.client_key(), None)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        try:
            if path in ("/", "/player"):
                self.send_file(APP_DIR / "player.html")
                return
            if path == "/health":
                self.send_player_json({"ok": True})
                return
            user = self.player_user()
            if not user or not store.config.get("browser_player", {}).get("enabled", True):
                self.send_player_json({"error": "Accesso richiesto"}, 401)
                return
            channel_id = safe_channel_id(user.get("channel_id", ""))
            channel = iptv.channel(channel_id)
            output = iptv.output(channel_id)
            if path == "/api/state":
                status = iptv.runtime_status().get(channel_id, {})
                if channel and channel.get("playlist") and status.get("state") in {"not_built", "outdated"}:
                    started = iptv.build_async(channel_id)
                    status = iptv.runtime_status().get(channel_id, {})
                    # Also makes the state deterministic for mocked/slow build
                    # starters: an existing old file remains playable only while
                    # the replacement is genuinely being prepared.
                    if started and status.get("state") in {"not_built", "outdated"}:
                        status = {"state": "building", "message": "Aggiornamento del canale in corso"}
                if not channel:
                    player_state, message = "missing_channel", "Il canale assegnato non esiste più"
                elif not channel.get("playlist"):
                    player_state, message = "empty_channel", "Il canale non contiene ancora video o foto"
                elif output.is_file() and status.get("state") != "error":
                    player_state, message = "ready", "Canale pronto"
                else:
                    player_state = status.get("state", "not_built")
                    message = status.get("message") or "Il canale deve essere preparato"
                self.send_player_json({
                    "ok": True,
                    "username": user["username"],
                    "channel_id": channel_id,
                    "channel_name": channel.get("name", channel_id) if channel else channel_id,
                    "ready": bool(
                        channel and channel.get("playlist") and output.is_file()
                        and player_state in {"ready", "building"}
                    ),
                    "status": player_state,
                    "message": message,
                    "fit": user.get("fit", "contain"),
                    "video_url": (
                        f"/channel.mp4?v={iptv.built_signature(channel_id)[:16]}-{output.stat().st_mtime_ns}"
                        if output.is_file() else ""
                    ),
                })
                return
            if path == "/channel.mp4":
                if not channel or not output.is_file():
                    self.send_player_json({"error": "Canale non ancora creato"}, 404)
                    return
                self.send_file(output, cache=False)
                return
            self.send_player_json({"error": "Pagina non trovata"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            store.log("error", f"Player TV GET {path}: {error}")
            self.send_player_json({"error": "Errore del Player TV"}, 500)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        try:
            if path == "/api/logout":
                self.send_player_json({"ok": True}, cookie="; Max-Age=0")
                return
            if path != "/api/login":
                self.send_player_json({"error": "Operazione sconosciuta"}, 404)
                return
            if not store.config.get("browser_player", {}).get("enabled", True):
                self.send_player_json({"error": "Player TV disattivato"}, 403)
                return
            if not self.login_allowed():
                self.send_player_json({"error": "Troppi tentativi. Attendi cinque minuti."}, 429)
                return
            body = self.json_body()
            username = str(body.get("username", "")).strip().lower()
            password = str(body.get("password", ""))
            account = player_account(username)
            encoded = account.get("password_hash") if account else DUMMY_PASSWORD_HASH
            if not password_matches(password, encoded):
                self.login_failed()
                self.send_player_json({"error": "Nome utente o password errati"}, 401)
                return
            self.login_succeeded()
            token = player_session_token(username)
            self.send_player_json({"ok": True}, cookie=f"{token}; Max-Age={30 * 24 * 60 * 60}")
        except Exception as error:
            store.log("error", f"Player TV POST {path}: {error}")
            self.send_player_json({"error": "Errore del Player TV"}, 500)


# Accesso da Internet: lo stesso pannello su una porta separata, solo dopo il login.
REMOTE_LOCK = threading.Lock()
REMOTE_SESSIONS = {}
REMOTE_FAILURES = {}
REMOTE_COOKIE = "carellas_admin"
# Le sole operazioni permesse agli operatori (pagina per tablet).
STAFF_POST_PATHS = {"/api/sonos/volume", "/api/music/manual", "/api/audio/pause", "/api/audio/skip",
                    "/api/audio/stop", "/api/test/audio", "/api/zone/music", "/api/zone/spots", "/api/music/control",
                    "/api/spot/today", "/api/music/queue/play"}
# Letture permesse agli operatori oltre alla pagina: il suo stato e lo storico degli spot.
STAFF_GET_PATHS = {"/api/staff/state", "/api/spots/history", "/api/spots/history.csv", "/api/music/queue"}


def remote_account(username):
    username = str(username or "").strip().lower()
    return next((item for item in store.config.get("remote_admin", {}).get("users", [])
                 if item.get("username") == username and item.get("enabled", True)), None)


def remote_password_version(account):
    return hashlib.sha256(account["password_hash"].encode()).hexdigest()[:16]


def remote_session_create(username):
    """Sessione tenuta solo in memoria: il browser riceve un codice casuale, il server ne conserva l'impronta."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with REMOTE_LOCK:
        for key in [key for key, item in REMOTE_SESSIONS.items() if now - item["created"] > REMOTE_SESSION_MAX_SECONDS]:
            REMOTE_SESSIONS.pop(key, None)
        REMOTE_SESSIONS[hashlib.sha256(token.encode()).hexdigest()] = {
            "username": username, "created": now, "seen": now,
            "version": remote_password_version(remote_account(username)),
        }
    return token


def remote_session_user(token):
    if not token or not store.config.get("remote_admin", {}).get("enabled"):
        return None
    key, now = hashlib.sha256(str(token).encode()).hexdigest(), time.time()
    with REMOTE_LOCK:
        item = REMOTE_SESSIONS.get(key)
        if not item:
            return None
        account = remote_account(item["username"])
        expired = (now - item["seen"] > REMOTE_SESSION_IDLE_SECONDS
                   or now - item["created"] > REMOTE_SESSION_MAX_SECONDS)
        # Utente eliminato, disattivato o password cambiata: la sessione non vale più.
        if expired or not account or not hmac.compare_digest(item["version"], remote_password_version(account)):
            REMOTE_SESSIONS.pop(key, None)
            return None
        item["seen"] = now
        return item["username"]


def remote_session_drop(token):
    with REMOTE_LOCK:
        REMOTE_SESSIONS.pop(hashlib.sha256(str(token or "").encode()).hexdigest(), None)


def remote_blocked(keys):
    now = time.monotonic()
    with REMOTE_LOCK:
        blocked = False
        for key, limit in keys:
            recent = [stamp for stamp in REMOTE_FAILURES.get(key, []) if now - stamp < REMOTE_LOCK_SECONDS]
            REMOTE_FAILURES[key] = recent
            blocked = blocked or len(recent) >= limit
        return blocked


def remote_failed(keys):
    with REMOTE_LOCK:
        for key, _limit in keys:
            REMOTE_FAILURES.setdefault(key, []).append(time.monotonic())


def remote_login_page(error="", disabled=False, username=""):
    de = store.config.get("language") == "de"
    text = {
        "title": "Anmelden" if de else "Accedi",
        "lead": ("Zugang von außen zum Bedienfeld Carellas Media Ads." if de
                 else "Accesso da fuori al pannello Carellas Media Ads."),
        "user": "Benutzername" if de else "Nome utente",
        "password": "Passwort" if de else "Password",
        "button": "Anmelden" if de else "Entra",
        "disabled": ("Der Zugang über das Internet ist ausgeschaltet. Schalte ihn im Bedienfeld unter Einstellungen ein."
                     if de else "L'accesso da Internet è spento. Attivalo dal pannello in Impostazioni."),
    }
    if de:
        error = {
            "Nome utente o password errati": "Benutzername oder Passwort falsch",
            "Troppi tentativi sbagliati. Riprova tra 15 minuti.": "Zu viele Fehlversuche. Versuche es in 15 Minuten erneut.",
            "Richiesta non valida": "Ungültige Anfrage",
        }.get(error, error)
    message = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    if disabled:
        form = f'<p class="err">{html.escape(text["disabled"])}</p>'
    else:
        form = f"""{message}<form method="post" action="login">
<label>{text["user"]}<input name="username" value="{html.escape(username)}" autocomplete="username" autocapitalize="none" spellcheck="false" required maxlength="40"></label>
<label>{text["password"]}<input name="password" type="password" autocomplete="current-password" required maxlength="200"></label>
<button type="submit">{text["button"]}</button></form>"""
    return f"""<!doctype html><html lang="{'de' if de else 'it'}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>Carellas · {text["title"]}</title><style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4efe6;color:#2b2118;font:16px system-ui,sans-serif}}
main{{width:min(380px,calc(100% - 32px));background:#fffaf2;border:1px solid #e3d6c3;border-radius:16px;padding:28px;box-shadow:0 10px 30px #0000000f}}
h1{{margin:0 0 4px;font-size:26px}}p{{margin:0 0 18px;color:#6b5a48}}label{{display:grid;gap:6px;margin-bottom:14px;font-weight:600}}
input{{font:inherit;padding:12px;border:1px solid #cdbba3;border-radius:10px;background:#fff}}
button{{width:100%;font:inherit;font-weight:700;padding:13px;border:0;border-radius:10px;background:#8a3b1f;color:#fff;cursor:pointer}}
.err{{color:#a3261b;font-weight:600}}</style></head><body><main><h1>Carellas</h1><p>{html.escape(text["lead"])}</p>{form}</main></body></html>"""


class RemoteAdminHandler(Handler):
    """Porta per l'accesso da Internet: il pannello completo, ma solo dopo nome utente e password."""

    server_version = "CarellasRemote"
    sys_version = ""

    def send_header(self, keyword, value):
        name = keyword.lower()
        # Niente CORS aperto e niente cache condivise: il pannello si usa solo dal suo indirizzo.
        if name == "access-control-allow-origin":
            return
        if name == "cache-control" and "public" in str(value):
            value = str(value).replace("public", "private")
        super().send_header(keyword, value)

    def end_headers(self):
        super().send_header("X-Frame-Options", "DENY")
        super().send_header("X-Content-Type-Options", "nosniff")
        super().send_header("Referrer-Policy", "same-origin")
        super().end_headers()

    def client_ip(self):
        return PlayerHandler.client_key(self)

    def cookie_value(self):
        morsel = SimpleCookie(self.headers.get("Cookie", "")).get(REMOTE_COOKIE)
        return morsel.value if morsel else ""

    def cookie_header(self, value, max_age):
        attributes = f"; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            attributes += "; Secure"
        return f"{REMOTE_COOKIE}={value}{attributes}"

    def same_origin(self):
        """Blocca i moduli inviati da altri siti (CSRF): l'origine deve essere questo indirizzo."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        allowed = {value.strip().lower() for value in (
            self.headers.get("Host", ""), self.headers.get("X-Forwarded-Host", ""),
            urllib.parse.urlsplit(store.config.get("remote_admin", {}).get("public_url", "")).netloc,
        ) if value.strip()}
        return urllib.parse.urlsplit(origin).netloc.lower() in allowed

    def send_page(self, page, status=200, cookie=None):
        data = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def redirect(self, location, cookie=None):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def authorized(self):
        if not store.config.get("remote_admin", {}).get("enabled"):
            self.send_page(remote_login_page(disabled=True), 403)
            return False
        username = remote_session_user(self.cookie_value())
        if username:
            self.remote_user = username
            return True
        if self.command in {"GET", "HEAD"} and "/api/" not in urllib.parse.urlsplit(self.path).path:
            self.redirect("/login")
        else:
            self.send_json({"error": "Accesso richiesto: entra di nuovo con nome utente e password"}, 401)
        return False

    def login(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 4096:
            self.send_page(remote_login_page("Richiesta non valida"), 400)
            return
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        username = str((form.get("username") or [""])[0]).strip().lower()[:40]
        password = str((form.get("password") or [""])[0])
        ip = self.client_ip()
        shown = re.sub(r"[^a-z0-9_.-]", "", username) or "?"
        keys = [(f"ip:{ip}", REMOTE_MAX_FAILURES_IP), (f"user:{username}", REMOTE_MAX_FAILURES_USER)]
        if remote_blocked(keys):
            self.send_page(remote_login_page("Troppi tentativi sbagliati. Riprova tra 15 minuti."), 429)
            return
        account = remote_account(username)
        if not password_matches(password, account.get("password_hash") if account else DUMMY_PASSWORD_HASH):
            remote_failed(keys)
            store.log("warning", f"Accesso da Internet: nome utente o password errati per «{shown}» da {ip}")
            if remote_blocked(keys):
                store.log("warning", f"Accesso da Internet bloccato per 15 minuti: troppi tentativi da {ip} o per «{shown}»")
            time.sleep(REMOTE_FAIL_DELAY_SECONDS)
            self.send_page(remote_login_page("Nome utente o password errati", username=shown if shown != "?" else ""), 401)
            return
        with REMOTE_LOCK:
            REMOTE_FAILURES.pop(f"ip:{ip}", None)
            REMOTE_FAILURES.pop(f"user:{username}", None)
        token = remote_session_create(username)
        store.log("info", f"Accesso da Internet: {username} è entrato da {ip}")
        self.redirect("/", cookie=self.cookie_header(token, REMOTE_SESSION_MAX_SECONDS))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, POST, DELETE")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def staff(self):
        """Utente con ruolo Operatore: solo musica, casse e pubblicità."""
        account = remote_account(self.remote_user)
        return bool(account and account.get("role") == "staff")

    def staff_forbidden(self):
        self.send_json({"error": "Gli operatori possono gestire solo musica, casse e pubblicità"}, 403)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/login":
            if not store.config.get("remote_admin", {}).get("enabled"):
                self.send_page(remote_login_page(disabled=True), 403)
            elif remote_session_user(self.cookie_value()):
                self.redirect("/")
            else:
                self.send_page(remote_login_page())
            return
        if self.authorized():
            if self.staff():
                route = self.route_path()
                if path in ("/", "/operatori"):
                    self.send_file(APP_DIR / "operatori.html")
                elif route in STAFF_GET_PATHS:
                    super().do_GET()
                elif route.startswith("/media/") and urllib.parse.unquote(route.removeprefix("/media/")) in \
                        store.config.get("audio", {}).get("ads", []):
                    # Ascolto degli spot in rotazione; gli altri file (foto e video) restano chiusi.
                    super().do_GET()
                else:
                    self.staff_forbidden()
                return
            super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if not self.same_origin():
            store.log("warning", f"Accesso da Internet: richiesta da un altro sito bloccata ({self.client_ip()})")
            self.send_json({"error": "Richiesta da un altro sito bloccata"}, 403)
            return
        if path == "/logout":
            remote_session_drop(self.cookie_value())
            self.redirect("/login", cookie=self.cookie_header("", 0))
            return
        if path == "/login":
            if not store.config.get("remote_admin", {}).get("enabled"):
                self.send_page(remote_login_page(disabled=True), 403)
                return
            self.login()
            return
        if self.authorized():
            if self.staff():
                if self.route_path() not in STAFF_POST_PATHS:
                    self.staff_forbidden()
                    return
                self.staff_request = True
            super().do_POST()

    def do_DELETE(self):
        if not self.same_origin():
            self.send_json({"error": "Richiesta da un altro sito bloccata"}, 403)
            return
        if self.authorized():
            if self.staff():
                self.staff_forbidden()
                return
            super().do_DELETE()


if __name__ == "__main__":
    scheduler.start()
    player_server = ThreadingHTTPServer(("0.0.0.0", PLAYER_PORT), PlayerHandler)
    threading.Thread(target=player_server.serve_forever, name="carellas-tv-player", daemon=True).start()
    store.log("info", f"Player TV protetto pronto sulla porta {PLAYER_PORT}")
    remote_server = ThreadingHTTPServer(("0.0.0.0", REMOTE_PORT), RemoteAdminHandler)
    threading.Thread(target=remote_server.serve_forever, name="carellas-remote-admin", daemon=True).start()
    store.log("info", f"Accesso da Internet pronto sulla porta {REMOTE_PORT}")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    store.log("info", f"Interfaccia pronta sulla porta {PORT}")
    server.serve_forever()
