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
CONFIG_FILE = DATA_DIR / "carellas_media_ads.json"
DEFAULT_FILE = APP_DIR / "default_config.json"
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
HA_API = os.environ.get("CARELLAS_HA_API", "http://supervisor/core/api")
PORT = 8099
MAX_UPLOAD = 1024 * 1024 * 1024 * 4
MIN_FREE_AFTER_UPLOAD = 1024 * 1024 * 512
ALLOWED = {
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"},
    "image": {".jpg", ".jpeg", ".png", ".webp", ".gif"},
    "video": {".mp4", ".m4v", ".mov", ".webm", ".mkv"},
}


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe_name(value):
    value = Path(urllib.parse.unquote(value)).name
    value = re.sub(r"[^A-Za-z0-9À-ÿ._ -]+", "_", value).strip(" .")
    return value[:180] or f"media-{uuid.uuid4().hex[:8]}"


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
        self.lock = threading.RLock()
        self.logs = []
        defaults = json.loads(DEFAULT_FILE.read_text(encoding="utf-8"))
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            saved = {}
        self.config = deep_merge(defaults, saved)
        # Stable releases support Sonos only. Remove obsolete audio-driver
        # settings from configurations created by earlier test versions.
        self.config.setdefault("audio", {}).pop("driver", None)
        self.config["audio"].pop("resume_music_after_ad", None)
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
            self.config = deep_merge(self.config, payload)
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
            result.append({
                "name": path.name,
                "kind": kind,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "url": f"/media/{urllib.parse.quote(path.name)}",
            })
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


store = Store()
ha = HomeAssistant()


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

    repetitions = max(1, min(int(cfg.get("repeat_count", 1)), 10))
    gap = max(0, min(int(cfg.get("repeat_gap_seconds", 5)), 600))
    duration = max(5, min(int(cfg.get("spot_duration_seconds", 65)), 600))
    volume = max(1, min(int(cfg.get("volume", 35)), 100))
    for index in range(repetitions):
        payload = {
            "entity_id": players,
            "announce": True,
            "media_content_type": "music",
            "media_content_id": media_url(filename),
            "extra": {"volume": volume},
        }
        ha.service("media_player", "play_media", payload)
        store.log("success", f"Spot audio Sonos avviato: {filename} ({index + 1}/{repetitions})")
        if index + 1 < repetitions:
            time.sleep(duration + gap)

    if not manual:
        scheduler.daily_audio_count += repetitions
    return filename

def start_music():
    cfg = store.config["music"]
    if not cfg.get("players") or not cfg.get("content_id"):
        raise RuntimeError("Configurazione musica incompleta")
    ha.service("media_player", "volume_set", {
        "entity_id": cfg["players"], "volume_level": max(0, min(int(cfg.get("volume", 25)), 100)) / 100
    })
    payload = {
        "entity_id": cfg["players"],
        "media_content_type": cfg.get("content_type", "music"),
        "media_content_id": cfg["content_id"],
    }
    ha.service("media_player", "play_media", payload)
    store.log("success", "Musica del locale avviata dalla programmazione")


def stop_music():
    players = store.config["music"].get("players", [])
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
        video_filter = "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=25,format=yuv420p"
        try:
            total_items = len(playlist)
            for index, item in enumerate(playlist):
                name = safe_name(item.get("name", ""))
                source = MEDIA_DIR / name
                if not source.is_file():
                    raise RuntimeError(f"File non trovato: {name}")
                self.set_status(
                    channel_id,
                    "building",
                    f"Conversione file {index + 1}/{total_items}: {name}",
                )
                duration = max(2, min(int(item.get("duration", 15)), 3600))
                part = parts / f"{index:04d}.mp4"
                kind = item.get("kind") or next(
                    (key for key, values in ALLOWED.items() if source.suffix.lower() in values),
                    "video",
                )
                command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
                if kind == "image":
                    command += ["-loop", "1", "-i", str(source)]
                else:
                    # Repeat short clips so every item lasts for the configured time.
                    command += ["-stream_loop", "-1", "-i", str(source)]
                # A silent AAC track makes the transport stream compatible with
                # IPTV players that reject video-only MPEG-TS channels.
                command += [
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                ]
                command += [
                    "-map", "0:v:0", "-map", "1:a:0", "-vf", video_filter,
                    "-c:v", "libx264", "-preset", "superfast",
                    "-crf", "22", "-profile:v", "high", "-level", "4.0",
                    "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
                    "-ar", "48000", "-ac", "2", "-t", str(duration), "-shortest",
                    "-movflags", "+faststart", str(part),
                ]
                subprocess.run(command, check=True, timeout=max(120, duration * 4))
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
        self.tv_active = False
        self.tv_ready_at = 0.0
        self.iptv_active = {}
        self.daily_audio_count = 0
        self.day = datetime.now().date()
        self.busy = threading.Lock()

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
        if self.daily_audio_count >= max(1, int(cfg.get("daily_limit", 20))):
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
        active = bool(cfg.get("enabled") and is_schedule_active(cfg.get("schedule", [])))
        if active and not self.music_active:
            try:
                start_music()
            except Exception as error:
                store.log("error", f"Avvio musica non riuscito: {error}")
        elif not active and self.music_active and cfg.get("stop_at_end", True):
            try:
                stop_music()
            except Exception as error:
                store.log("error", f"Arresto musica non riuscito: {error}")
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
    server_version = "CarellasMediaAds/0.4.0"

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
            match = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if match:
                start = int(match.group(1) or 0)
                end = min(int(match.group(2) or size - 1), size - 1)
                status = HTTPStatus.PARTIAL_CONTENT
        length = max(0, end - start + 1)
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
                except Exception as exc:
                    error = str(exc)
                self.send_json({
                    "config": store.config,
                    "media": store.media(),
                    "entities": sorted(entities, key=lambda x: x["name"].lower()),
                    "power_entities": sorted(power_entities, key=lambda x: x["name"].lower()),
                    "logs": store.logs,
                    "runtime": {
                        "audio_today": scheduler.daily_audio_count,
                        "music_active": scheduler.music_active,
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
        except Exception as error:
            store.log("error", f"GET {path}: {error}")
            self.send_json({"error": str(error)}, 500)

    def do_POST(self):
        path = self.route_path()
        try:
            if path == "/api/config":
                config = store.update(self.json_body())
                self.send_json({"ok": True, "config": config})
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
