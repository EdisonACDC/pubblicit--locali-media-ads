#!/usr/bin/env python3
"""Carellas Media Ads - Home Assistant app, no external Python dependencies."""

from __future__ import annotations

import copy
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CARELLAS_DATA_DIR", "/data"))
MEDIA_DIR = Path(os.environ.get("CARELLAS_MEDIA_DIR", "/media/carellas_media_ads"))
IPTV_DIR = DATA_DIR / "iptv"
UPLOAD_DIR = DATA_DIR / "uploads"
CONFIG_FILE = DATA_DIR / "carellas_media_ads.json"
DEFAULT_FILE = APP_DIR / "default_config.json"
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
HA_API = os.environ.get("CARELLAS_HA_API", "http://supervisor/core/api")
PORT = 8099
SONOS_GROUP_SETTLE_SECONDS = float(os.environ.get("CARELLAS_SONOS_GROUP_SETTLE_SECONDS", "1.5"))
SONOS_RESTORE_SETTLE_SECONDS = float(os.environ.get("CARELLAS_SONOS_RESTORE_SETTLE_SECONDS", "1.5"))
SONOS_RESTORE_RETRIES = max(1, int(os.environ.get("CARELLAS_SONOS_RESTORE_RETRIES", "3")))
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
        # La versione stabile supporta solo Sonos. Elimina eventuali opzioni
        # Alexa rimaste da configurazioni beta precedenti.
        self.config.setdefault("audio", {}).pop("driver", None)
        self.config["audio"].pop("resume_music_after_ad", None)
        self.config["audio"].pop("spot_duration_seconds", None)
        music = self.config.setdefault("music", {})
        music.setdefault("slots", [])
        self.save()
        self.install_bundled_media()

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


store = Store()
ha = HomeAssistant()
upload_lock = threading.Lock()
audio_playback_lock = threading.Lock()
audio_stop_event = threading.Event()


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


def prepare_sonos_group(players, force=False):
    """Raggruppa gli altoparlanti e restituisce il coordinatore Sonos."""
    players = list(dict.fromkeys(player for player in players if player))
    if not players:
        raise RuntimeError("Nessun altoparlante selezionato")
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


def restore_sonos_playback(snapshot_targets, resume_targets):
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
        ha.service("media_player", "media_play", {"entity_id": resume_targets})
        store.log("info", f"Ripresa forzata della musica Sonos su {len(resume_targets)} gruppo/i")


def play_audio(filename=None, manual=False):
    cfg = store.config["audio"]
    players = cfg.get("players", [])
    ads = cfg.get("ads", [])
    if not players:
        raise RuntimeError("Nessun altoparlante selezionato")
    if not filename:
        filename = scheduler.pick_audio(ads, cfg.get("mode", "rotate"))
    if not filename:
        raise RuntimeError("Nessuno spot audio configurato")

    players = list(dict.fromkeys(player for player in players if player))
    repetitions = max(1, min(int(cfg.get("repeat_count", 1)), 10))
    if not manual:
        remaining_today = max(0, int(cfg.get("daily_limit", 20)) - scheduler.audio_today())
        repetitions = min(repetitions, remaining_today)
        if repetitions < 1:
            raise RuntimeError("Limite giornaliero degli spot raggiunto")
    gap = max(0, min(int(cfg.get("repeat_gap_seconds", 5)), 600))
    duration = probe_media_duration(MEDIA_DIR / safe_name(filename))
    volume = max(1, min(int(cfg.get("volume", 35)), 100))

    if not audio_playback_lock.acquire(blocking=False):
        raise RuntimeError("Uno spot Sonos è già in riproduzione")
    audio_stop_event.clear()
    snapshot_created = False
    snapshot_targets = list(players)
    resume_targets = []
    stopped = False
    try:
        # La funzione Sonos announce avvia un AudioClip indipendente su ogni
        # diffusore e non garantisce la sincronizzazione. Salviamo quindi lo
        # stato dei soli Sonos scelti, li isoliamo e riproduciamo un unico
        # normale flusso sul coordinatore del gruppo temporaneo.
        snapshot_targets, resume_targets = sonos_playback_context(players)
        ha.service("sonos", "snapshot", {
            "entity_id": snapshot_targets,
            "with_group": True,
        })
        snapshot_created = True
        ha.service("media_player", "unjoin", {"entity_id": players})
        if SONOS_GROUP_SETTLE_SECONDS > 0:
            time.sleep(SONOS_GROUP_SETTLE_SECONDS)
        coordinator = prepare_sonos_group(players, force=True)
        ha.service("media_player", "volume_set", {
            "entity_id": players,
            "volume_level": volume / 100,
        })

        for index in range(repetitions):
            if audio_stop_event.is_set():
                stopped = True
                break
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
            if audio_stop_event.wait(duration):
                stopped = True
                break
            if index + 1 < repetitions and gap and audio_stop_event.wait(gap):
                stopped = True
                break
    finally:
        if snapshot_created:
            try:
                restore_sonos_playback(snapshot_targets, resume_targets)
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

def start_music(selection=None):
    cfg = store.config["music"]
    selection = selection or cfg
    players = list(dict.fromkeys(selection.get("players") or cfg.get("players") or []))
    content_id = selection.get("content_id")
    if not players or not content_id:
        raise RuntimeError("Configurazione musica incompleta")
    coordinator = prepare_sonos_group(players)
    ha.service("media_player", "volume_set", {
        "entity_id": players,
        "volume_level": max(0, min(int(selection.get("volume", cfg.get("volume", 25))), 100)) / 100,
    })
    payload = {
        "entity_id": coordinator,
        "media_content_type": selection.get("content_type", "music"),
        "media_content_id": content_id,
    }
    ha.service("media_player", "play_media", payload)
    title = selection.get("name") or selection.get("title") or content_id
    store.log("success", f"Musica del locale avviata sul gruppo Sonos sincronizzato: {title}")
    return players


def stop_music(players=None):
    players = list(dict.fromkeys(players or store.config["music"].get("players", [])))
    if players:
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


def safe_channel_id(value):
    value = re.sub(r"[^a-z0-9-]+", "-", str(value).lower()).strip("-")
    return value[:48] or f"canale-{uuid.uuid4().hex[:6]}"


class IPTVEngine:
    def __init__(self):
        self.lock = threading.RLock()
        self.status = {}

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

    def runtime_status(self):
        with self.lock:
            result = copy.deepcopy(self.status)
        for channel in self.channels():
            channel_id = safe_channel_id(channel.get("id", ""))
            if channel_id not in result:
                result[channel_id] = {
                    "state": "ready" if self.hls_manifest(channel_id).is_file() else "not_built",
                    "message": "Canale HLS pronto" if self.hls_manifest(channel_id).is_file() else "Premi Crea/Aggiorna canale",
                    "time": "",
                }
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
        if effect == "zoom_in":
            return (
                "zoompan=z='min(zoom+0.0012,1.12)':"
                "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                "d=1:s=1280x720:fps=25"
            )
        if effect == "zoom_out":
            return (
                "zoompan=z='if(eq(on,1),1.12,max(1.0,zoom-0.0012))':"
                "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                "d=1:s=1280x720:fps=25"
            )
        if effect == "pan":
            return (
                "zoompan=z=1.12:"
                f"x='(iw-iw/zoom)*on/{frames}':"
                "y='ih/2-(ih/zoom/2)':d=1:s=1280x720:fps=25"
            )
        if effect == "black_white":
            return "hue=s=0,fps=25"
        return "fps=25"

    @staticmethod
    def _fade_filter(duration):
        fade_out = max(0.0, duration - min(0.6, duration / 3))
        return f"fade=t=in:st=0:d=0.5,fade=t=out:st={fade_out:.3f}:d=0.5"

    @staticmethod
    def _probe_duration(source):
        return probe_media_duration(source)

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
        concat_lines = []
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
                    command = [
                        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(source),
                        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                        "-map", "0:v:0", "-map", "1:a:0",
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
            concat_file = parts / "concat.txt"
            concat_file.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
            temporary = work / "channel.tmp.mp4"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-c", "copy", "-movflags", "+faststart", str(temporary),
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
            self.set_status(channel_id, "ready", "Canale HLS pronto (foto e video)")
            store.log("success", f"Canale IPTV creato: {channel.get('name', channel_id)}")
        except Exception as error:
            self.set_status(channel_id, "error", str(error))
            store.log("error", f"Creazione canale IPTV non riuscita ({channel_id}): {error}")
            raise

    def build_async(self, channel_id):
        def worker():
            try:
                self.build(channel_id)
            except Exception:
                pass
        threading.Thread(target=worker, name=f"iptv-build-{safe_channel_id(channel_id)}", daemon=True).start()


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
        self.tv_active = False
        self.tv_ready_at = 0.0
        self.iptv_active = {}
        self.daily_audio_count = 0
        self.audio_count_lock = threading.Lock()
        self.day = datetime.now().date()
        self.busy = threading.Lock()

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
        selected = set(store.config["audio"].get("players", []))
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
        changed = active and slot_key != self.music_slot_key
        if changed:
            try:
                previous = list(self.music_players)
                target_players = list(dict.fromkeys(slot.get("players") or cfg.get("players") or []))
                if previous and set(previous) != set(target_players):
                    # Scioglie il vecchio gruppo prima di crearne uno diverso:
                    # un Sonos rimosso dalla fascia non deve continuare a suonare.
                    ha.service("media_player", "media_stop", {"entity_id": previous})
                    ha.service("media_player", "unjoin", {
                        "entity_id": list(dict.fromkeys(previous + target_players)),
                    })
                    if SONOS_GROUP_SETTLE_SECONDS > 0:
                        time.sleep(SONOS_GROUP_SETTLE_SECONDS)
                players = start_music(slot)
                self.music_players = players
                self.music_slot_key = slot_key
                self.music_slot_id = slot.get("id")
            except Exception as error:
                store.log("error", f"Avvio musica non riuscito: {error}")
                active = False
        elif not active and self.music_active and cfg.get("stop_at_end", True):
            try:
                stop_music(self.music_players)
            except Exception as error:
                store.log("error", f"Arresto musica non riuscito: {error}")
            self.music_players = []
            self.music_slot_key = None
            self.music_slot_id = None
        self.music_active = active

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
                self.music_tick()
                self.audio_tick()
                self.tv_tick()
                self.iptv_tick()
            except Exception:
                store.log("error", traceback.format_exc(limit=2))
            time.sleep(5)


scheduler = Scheduler()


class Handler(BaseHTTPRequestHandler):
    server_version = "CarellasMediaAds/0.4.15"

    def log_message(self, fmt, *args):
        return

    def route_path(self):
        path = urllib.parse.urlsplit(self.path).path
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
                self.send_json({"channels": iptv.status})
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
                            entities.append(item)
                        if entity_id.startswith(("media_player.", "switch.", "button.", "script.")):
                            power_entities.append(item)
                        favorites = attrs.get("items")
                        if isinstance(favorites, dict):
                            for media_id, name in favorites.items():
                                if str(media_id).startswith("FV:"):
                                    favorites_by_id[str(media_id)] = str(name)
                except Exception as exc:
                    error = str(exc)
                self.send_json({
                    "config": store.config,
                    "media": store.media(),
                    "entities": sorted(entities, key=lambda x: x["name"].lower()),
                    "power_entities": sorted(power_entities, key=lambda x: x["name"].lower()),
                    "sonos_favorites": sorted(
                        ({"id": media_id, "name": name} for media_id, name in favorites_by_id.items()),
                        key=lambda x: x["name"].lower(),
                    ),
                    "logs": store.logs,
                    "runtime": {
                        "audio_today": scheduler.audio_today(),
                        "music_active": scheduler.music_active,
                        "music_slot_id": scheduler.music_slot_id,
                        "tv_active": scheduler.tv_active,
                        "media_base_url": local_base_url(),
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
                config = store.update(self.json_body())
                self.send_json({"ok": True, "config": config})
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
            if path == "/api/music/browse":
                body = self.json_body()
                entity_id = str(body.get("entity_id", ""))
                if not entity_id.startswith("media_player."):
                    raise RuntimeError("Seleziona prima un altoparlante Sonos")
                payload = {"entity_id": entity_id}
                if body.get("media_content_type"):
                    payload["media_content_type"] = str(body["media_content_type"])
                if body.get("media_content_id"):
                    payload["media_content_id"] = str(body["media_content_id"])
                response = ha.service_response("media_player", "browse_media", payload)
                browser = response.get(entity_id)
                if browser is None and response:
                    browser = next(iter(response.values()))
                if not isinstance(browser, dict):
                    raise RuntimeError("Il player selezionato non ha restituito contenuti multimediali")
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
            store.save()
            store.log("info", f"File eliminato: {name}")
            self.send_json({"ok": True})
        except FileNotFoundError:
            self.send_json({"error": "File non trovato"}, 404)


if __name__ == "__main__":
    scheduler.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    store.log("info", f"Interfaccia pronta sulla porta {PORT}")
    server.serve_forever()
