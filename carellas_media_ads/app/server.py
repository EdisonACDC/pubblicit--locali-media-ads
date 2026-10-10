#!/usr/bin/env python3
"""Carellas Media Ads - Home Assistant app."""

from __future__ import annotations

import copy
import base64
import hashlib
import hmac
import json
import math
import mimetypes
import os
import re
import shutil
import secrets
import subprocess
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
from datetime import datetime
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
SONOS_CATALOG_FILE = DATA_DIR / "carellas_sonos_catalog.json"
DEFAULT_FILE = APP_DIR / "default_config.json"
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
HA_API = os.environ.get("CARELLAS_HA_API", "http://supervisor/core/api")
HA_WS = os.environ.get("CARELLAS_HA_WS", "ws://supervisor/core/websocket")
PORT = 8099
PLAYER_PORT = 8101
SONOS_GROUP_SETTLE_SECONDS = float(os.environ.get("CARELLAS_SONOS_GROUP_SETTLE_SECONDS", "0.6"))
SONOS_RESTORE_SETTLE_SECONDS = float(os.environ.get("CARELLAS_SONOS_RESTORE_SETTLE_SECONDS", "0.6"))
SONOS_RESTORE_RETRIES = max(1, int(os.environ.get("CARELLAS_SONOS_RESTORE_RETRIES", "3")))
SONOS_FADE_STEPS = max(2, int(os.environ.get("CARELLAS_SONOS_FADE_STEPS", "6")))
SONOS_GROUP_CHECK_SECONDS = max(5.0, float(os.environ.get("CARELLAS_SONOS_GROUP_CHECK_SECONDS", "15")))
SONOS_SPOT_TAIL_GUARD_SECONDS = max(
    0.0, min(float(os.environ.get("CARELLAS_SONOS_SPOT_TAIL_GUARD_SECONDS", "0.4")), 2.0)
)
SONOS_QUIET_VOLUME = 0.01
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
        if self.config.get("language") not in {"it", "de"}:
            self.config["language"] = "it"
        player = self.config.setdefault("browser_player", {})
        player.setdefault("enabled", True)
        player.setdefault("public_url", "")
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
        return self.request("POST", f"/services/{domain}/{service}", payload) or []

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


store = Store()
ha = HomeAssistant()
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
        result.append({
            "id": media_id,
            "name": str(item.get("name") or media_id),
            "type": media_type,
        })
    return result


def save_sonos_catalog(items):
    """Salva atomicamente il catalogo perché i refresh della UI non lo cancellino."""
    normalized = [
        {
            "id": str(item["id"]),
            "name": str(item.get("name") or item["id"]),
            "type": str(item.get("type") or "music"),
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
            overlap = any(
                lday == rday and max(lstart, rstart) < min(lend, rend)
                for lday, lstart, lend in left_intervals
                for rday, rstart, rend in _slot_intervals(right)
            )
            if overlap:
                left_name = left.get("name") or f"Fascia {left_index + 1}"
                right_name = right.get("name") or "Fascia successiva"
                raise RuntimeError(
                    f"Le fasce ‘{left_name}’ e ‘{right_name}’ si sovrappongono sugli stessi Sonos"
                )


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
    return valid


def prepare_sonos_group(players, force=False):
    """Raggruppa gli altoparlanti e restituisce il coordinatore Sonos."""
    players = available_sonos_players(players)
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
        ha.service("media_player", "join", {
            "entity_id": coordinator,
            "group_members": players[1:],
        })
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
        if player in states and states[player].get("state") != "unavailable"
    ]
    unavailable = [player for player in requested if player not in reachable]
    if not reachable:
        return {"coordinator": None, "missing": [], "unavailable": unavailable}

    coordinator = preferred_coordinator if preferred_coordinator in reachable else reachable[0]
    members = states.get(coordinator, {}).get("attributes", {}).get("group_members") or [coordinator]
    missing = [player for player in reachable if player not in members]
    if missing:
        ha.service("media_player", "join", {
            "entity_id": coordinator,
            "group_members": missing,
        })
        if SONOS_GROUP_SETTLE_SECONDS > 0:
            time.sleep(SONOS_GROUP_SETTLE_SECONDS)
        if volume_level is not None:
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


def sonos_volume_levels(players, fallback=0.25):
    """Legge i volumi correnti; il fallback evita salti se HA non espone ancora lo stato."""
    fallback = max(0.0, min(float(fallback), 1.0))
    try:
        states = {item.get("entity_id"): item for item in ha.states()}
    except Exception as error:
        store.log("warning", f"Volume Sonos non leggibile: {error}")
        states = {}
    levels = {}
    for player in dict.fromkeys(players):
        raw = states.get(player, {}).get("attributes", {}).get("volume_level", fallback)
        try:
            levels[player] = max(0.0, min(float(raw), 1.0))
        except (TypeError, ValueError):
            levels[player] = fallback
    return levels


def set_sonos_volume_levels(levels):
    """Imposta anche volumi diversi senza perdere il bilanciamento tra le sale."""
    grouped = {}
    for player, level in levels.items():
        grouped.setdefault(round(max(0.0, min(float(level), 1.0)), 3), []).append(player)
    for level, players in grouped.items():
        ha.service("media_player", "volume_set", {
            "entity_id": players,
            "volume_level": level,
        })


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


def restore_sonos_playback(snapshot_targets, resume_targets, selected_players=None, volume_levels=None,
                           transition_seconds=0):
    """Ripristina gruppi/coda e riavvia la musica che proveniva direttamente da Sonos."""
    last_error = None
    for attempt in range(SONOS_RESTORE_RETRIES):
        try:
            ha.service("sonos", "restore", {
                "entity_id": snapshot_targets,
                "with_group": True,
            })
            last_error = None
            break
        except Exception as error:
            last_error = error
            store.log("warning", f"Ripristino Sonos, tentativo {attempt + 1}/{SONOS_RESTORE_RETRIES}: {error}")
            if attempt + 1 < SONOS_RESTORE_RETRIES:
                time.sleep(0.75)
    if last_error:
        raise last_error
    if SONOS_RESTORE_SETTLE_SECONDS > 0:
        time.sleep(SONOS_RESTORE_SETTLE_SECONDS)
    if resume_targets:
        selected_players = list(dict.fromkeys(selected_players or snapshot_targets))
        if volume_levels and transition_seconds > 0:
            set_sonos_volume_levels({player: SONOS_QUIET_VOLUME for player in selected_players})
        ha.service("media_player", "media_play", {"entity_id": resume_targets})
        if volume_levels and transition_seconds > 0:
            fade_sonos(
                selected_players,
                volume_levels,
                transition_seconds,
                {player: SONOS_QUIET_VOLUME for player in selected_players},
            )
        store.log("info", f"Ripresa forzata della musica Sonos su {len(resume_targets)} gruppo/i")


def play_audio(filename=None, manual=False):
    cfg = store.config["audio"]
    players = cfg.get("players", [])
    ads = cfg.get("ads", [])
    if not players:
        raise RuntimeError("Nessun altoparlante selezionato")
    automatically_selected = not filename
    if automatically_selected:
        filename = scheduler.pick_audio(ads, cfg.get("mode", "rotate"))
    if not filename:
        raise RuntimeError("Nessuno spot audio configurato")

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
    try:
        # La funzione Sonos announce avvia un AudioClip indipendente su ogni
        # diffusore e non garantisce la sincronizzazione. Salviamo quindi lo
        # stato dei soli Sonos scelti, li isoliamo e riproduciamo un unico
        # normale flusso sul coordinatore del gruppo temporaneo.
        snapshot_targets, resume_targets = sonos_playback_context(players)
        original_groups = sonos_group_topology(players)
        ha.service("sonos", "snapshot", {
            "entity_id": snapshot_targets,
            "with_group": True,
        })
        snapshot_created = True
        fade_sonos(players, SONOS_QUIET_VOLUME, transition, previous_volumes)
        ha.service("media_player", "unjoin", {"entity_id": players})
        if SONOS_GROUP_SETTLE_SECONDS > 0:
            time.sleep(SONOS_GROUP_SETTLE_SECONDS)
        coordinator = prepare_sonos_group(players, force=True)
        quiet_levels = {player: SONOS_QUIET_VOLUME for player in players}
        spot_levels = {player: volume / 100 for player in players}
        set_sonos_volume_levels(quiet_levels)

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
            store.log(
                "success",
                f"Spot sincronizzato su {len(players)} Sonos selezionati: "
                f"{filename} ({index + 1}/{repetitions})",
            )
            # Il piccolo margine compensa il buffering iniziale dei Sonos. In
            # precedenza la dissolvenza iniziava prima della fine e tagliava la
            # traccia; ora il ripristino parte solo dopo la durata completa.
            if audio_stop_event.wait(duration + SONOS_SPOT_TAIL_GUARD_SECONDS):
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


def play_sonos_media(payload, retries=3):
    """Avvia una sorgente Sonos e gestisce la transizione UPnP 701 ancora occupata."""
    last_error = None
    for attempt in range(max(1, int(retries))):
        try:
            ha.service("media_player", "play_media", payload)
            return
        except Exception as error:
            last_error = error
            if not sonos_transition_busy(error) or attempt + 1 >= retries:
                raise
            store.log("warning", "Sonos ancora occupato nel cambio sorgente: nuovo tentativo controllato")
            ha.service("media_player", "media_stop", {"entity_id": payload["entity_id"]})
            time.sleep(1.2 * (attempt + 1))
    raise last_error


def select_sonos_source(entity_id, source, retries=3):
    """Seleziona una sorgente nativa Sonos con recupero dall'errore UPnP 701."""
    last_error = None
    for attempt in range(max(1, int(retries))):
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
            time.sleep(1.2 * (attempt + 1))
    raise last_error


def start_music(selection=None, fade_in=False):
    cfg = store.config["music"]
    selection = selection or cfg
    players = available_sonos_players(selection.get("players") or cfg.get("players") or [])
    content_id = selection.get("content_id")
    if not players or not content_id:
        raise RuntimeError("Configurazione musica incompleta")
    coordinator = prepare_sonos_group(players)
    target_volume = max(0, min(int(selection.get("volume", cfg.get("volume", 25))), 100)) / 100
    transition = max(0.0, min(float(cfg.get("transition_seconds", 3)), 10.0)) if fade_in else 0
    previous_levels = sonos_volume_levels(players, target_volume)
    start_level = SONOS_QUIET_VOLUME if transition > 0 else target_volume
    set_sonos_volume_levels({player: start_level for player in players})
    content_type = selection.get("content_type", "music")
    payload = {
        "entity_id": coordinator,
        "media_content_type": content_type,
        "media_content_id": content_id,
    }
    try:
        if content_type == "sonos_radio_source":
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
                    {player: start_level for player in players},
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
            {player: start_level for player in players},
        )
    title = selection.get("name") or selection.get("title") or content_id
    store.log("success", f"Musica del locale avviata sul gruppo Sonos sincronizzato: {title}")
    return players


def stop_music(players=None, fade_seconds=0):
    players = list(dict.fromkeys(players or store.config["music"].get("players", [])))
    if players:
        if fade_seconds:
            fade_sonos(players, SONOS_QUIET_VOLUME, fade_seconds)
        ha.service("media_player", "media_stop", {"entity_id": players})
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
        concat_lines = []
        generated_sequence = []
        try:
            total_items = len(playlist)
            for index, item in enumerate(playlist):
                kind = item.get("kind", "video")
                name = safe_name(item.get("name", ""))
                source = MEDIA_DIR / name if name else None
                if kind != "collage" and (not source or not source.is_file()):
                    raise RuntimeError(f"File non trovato: {name}")
                self.set_status(
                    channel_id,
                    "building",
                    f"Conversione elemento {index + 1}/{total_items}: {name or 'Collage'}",
                )
                part = parts / f"{index:04d}.mp4"
                if kind not in {"image", "video", "collage"}:
                    kind = next(
                        (key for key, values in ALLOWED.items() if source.suffix.lower() in values),
                        "video",
                    )
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
                    command = self._collage_command(item, part, duration)
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
                    "-movflags", "+faststart", str(part),
                ]
                subprocess.run(command, check=True, timeout=max(120, duration * 4 + 60))
                concat_lines.append(f"file '{part.as_posix()}'")
                generated_sequence.append({
                    "position": index + 1,
                    "kind": kind,
                    "name": name if kind != "collage" else "Collage",
                    "duration": round(duration, 3),
                    "effect": item.get("effect", "none") if kind in {"image", "collage"} else "none",
                    "fit": item.get("fit", "smart") if kind != "collage" else "cover",
                })
            concat_file = parts / "concat.txt"
            concat_file.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
            temporary = work / "channel.tmp.mp4"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-fflags", "+genpts", "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(temporary),
            ], check=True, timeout=600)
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
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(metadata_temporary, self.build_metadata(channel_id))
            self.set_status(channel_id, "ready", "Canale HLS pronto (foto e video)")
            store.log("success", f"Canale IPTV creato: {channel.get('name', channel_id)}")
        except Exception as error:
            self.set_status(channel_id, "error", str(error))
            store.log("error", f"Creazione canale IPTV non riuscita ({channel_id}): {error}")
            raise

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

    def audio_tick(self):
        cfg = store.config["audio"]
        if not cfg.get("enabled") or not is_schedule_active(cfg.get("schedule", [])):
            self.last_audio = time.monotonic()
            return
        interval = max(1, int(cfg.get("interval_minutes", 30))) * 60
        if time.monotonic() - self.last_audio < interval:
            return
        if self.audio_today() >= max(1, int(cfg.get("daily_limit", 20))):
            return
        if cfg.get("only_when_playing", True) and not self.selected_playing():
            return
        if not self.busy.acquire(blocking=False):
            return
        self.last_audio = time.monotonic()
        try:
            play_audio()
        except Exception as error:
            store.log("error", f"Spot audio non riuscito: {error}")
        finally:
            self.busy.release()

    def music_tick(self):
        cfg = store.config["music"]
        if audio_playback_lock.locked():
            return
        slot = active_music_slot(cfg) if cfg.get("enabled") and cfg.get("slots") else None
        if cfg.get("enabled") and not cfg.get("slots") and is_schedule_active(cfg.get("schedule", [])):
            slot = cfg
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
        if active and slot_key == self.music_failed_slot_key and time.monotonic() < self.music_retry_at:
            # Un Sonos in transizione può restituire UPnP 701 per alcuni
            # secondi. Evitiamo una raffica di tentativi a ogni ciclo.
            return
        changed = active and slot_key != self.music_slot_key
        if changed:
            previous_selection = copy.deepcopy(self.music_selection)
            try:
                previous = list(self.music_players)
                target_players = list(dict.fromkeys(slot.get("players") or cfg.get("players") or []))
                transition = max(0.0, min(float(cfg.get("transition_seconds", 3)), 10.0))
                if previous:
                    fade_sonos(previous, SONOS_QUIET_VOLUME, transition)
                if previous and set(previous) != set(target_players):
                    # Scioglie il vecchio gruppo prima di crearne uno diverso:
                    # un Sonos rimosso dalla fascia non deve continuare a suonare.
                    ha.service("media_player", "media_stop", {"entity_id": previous})
                    ha.service("media_player", "unjoin", {
                        "entity_id": list(dict.fromkeys(previous + target_players)),
                    })
                    if SONOS_GROUP_SETTLE_SECONDS > 0:
                        time.sleep(SONOS_GROUP_SETTLE_SECONDS)
                players = start_music(slot, fade_in=True)
                self.music_players = players
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
            except Exception as error:
                store.log("error", f"Avvio musica non riuscito: {error}")
                self.music_failed_slot_key = slot_key
                self.music_retry_at = time.monotonic() + 300
                if previous_selection:
                    try:
                        self.music_players = start_music(previous_selection, fade_in=True)
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
        if active and not changed and time.monotonic() >= self.music_group_check_at:
            self.music_group_check_at = time.monotonic() + SONOS_GROUP_CHECK_SECONDS
            expected = self.music_expected_players or list(dict.fromkeys(
                slot.get("players") or cfg.get("players") or []
            ))
            try:
                result = repair_sonos_group(expected, self.music_coordinator, self.music_volume)
                if result["coordinator"]:
                    self.music_coordinator = result["coordinator"]
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


class Handler(BaseHTTPRequestHandler):
    server_version = "CarellasMediaAds/0.4.44"

    def log_message(self, fmt, *args):
        return

    def route_path(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/hassio_ingress/"):
            positions = [path.rfind(marker) for marker in ("/media/", "/assets/", "/iptv/")]
            media_position = max(positions)
            if media_position >= 0:
                return path[media_position:]
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

    def do_GET(self):
        path = self.route_path()
        try:
            if path == "/api/iptv/status":
                self.send_json({"channels": iptv.runtime_status()})
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
                    "runtime": {
                        "audio_today": scheduler.audio_today(),
                        "music_active": scheduler.music_active,
                        "music_slot_id": scheduler.music_slot_id,
                        "tv_active": scheduler.tv_active,
                        "media_base_url": local_base_url(),
                        "player_url": player_public_url(),
                        "player_local_url": player_local_url(),
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
            if path == "/api/upload/chunk":
                self.receive_upload_chunk()
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
            if path == "/api/test/audio":
                body = self.json_body()
                threading.Thread(target=play_audio, args=(body.get("name"), True), daemon=True).start()
                self.send_json({"ok": True})
                return
            if path == "/api/audio/stop":
                self.send_json({"ok": True, "active": stop_audio()})
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
            if path == "/api/music/start":
                start_music()
                self.send_json({"ok": True})
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
                stop_music()
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

    server_version = "CarellasTVPlayer/0.4.44"

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


if __name__ == "__main__":
    scheduler.start()
    player_server = ThreadingHTTPServer(("0.0.0.0", PLAYER_PORT), PlayerHandler)
    threading.Thread(target=player_server.serve_forever, name="carellas-tv-player", daemon=True).start()
    store.log("info", f"Player TV protetto pronto sulla porta {PLAYER_PORT}")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    store.log("info", f"Interfaccia pronta sulla porta {PORT}")
    server.serve_forever()
