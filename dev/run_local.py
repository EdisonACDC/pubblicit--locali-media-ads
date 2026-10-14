#!/usr/bin/env python3
"""Avvia Carellas Media Ads in locale con un Home Assistant virtuale.

Non serve Home Assistant né Sonos reali: gli altoparlanti, le TV e i servizi
sono simulati in memoria. Il pannello del simulatore permette di cambiare lo
stato delle casse (musica avviata dall'app Sonos, cassa scollegata, volume...)
per verificare come reagisce l'add-on.

Uso:
    py dev/run_local.py            (Windows)
    python3 dev/run_local.py       (macOS / Linux)

Indirizzi:
    http://127.0.0.1:8099   pannello Carellas Media Ads
    http://127.0.0.1:8101   Player TV via browser
    http://127.0.0.1:8098   simulatore Sonos e Home Assistant
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP_DIR = REPO / "carellas_media_ads" / "app"
DEV_DIR = Path(__file__).resolve().parent
SIM_PORT = 8098

FAVORITES = {
    "FV:2/11": "Radio Italia Live",
    "FV:2/12": "Dinner Jazz",
    "FV:2/13": "Aperitivo Lounge",
    "FV:2/14": "Pranzo italiano",
}
PLAYLISTS = {
    "SQ:21": "Cena romantica",
    "SQ:22": "Pizzeria anni 80",
    "SQ:23": "Brunch domenicale",
}
SOURCES = ["TV", "Line-in"]


class VirtualHomeAssistant:
    """Sostituisce il client HomeAssistant dell'add-on con uno stato in memoria."""

    def __init__(self, media_dir=None):
        self.media_dir = Path(media_dir) if media_dir else None
        self.lock = threading.RLock()
        self.events = []
        self.snapshots = {}
        self.speakers = {}
        for entity_id, name, volume in (
            ("media_player.sonos_sala", "Sonos Sala", 0.30),
            ("media_player.sonos_bar", "Sonos Bar", 0.25),
            ("media_player.sonos_terrazza", "Sonos Terrazza", 0.20),
            ("media_player.sonos_bagni", "Sonos Bagni", 0.15),
        ):
            self.speakers[entity_id] = {
                "name": name, "state": "idle", "volume": volume, "muted": False,
                "coordinator": entity_id, "media_id": "", "title": "", "source": None,
                "reachable": True, "kind": "music", "ends_at": None,
            }
        self.devices = {
            "media_player.tv_sala_samsung": {"name": "TV Sala Samsung", "state": "off"},
            "media_player.tv_bar_lg": {"name": "TV Bar LG", "state": "off"},
            "script.spegni_tv_sala": {"name": "Spegni TV Sala", "state": "off"},
            "switch.presa_tv_bar": {"name": "Presa TV Bar", "state": "off"},
        }
        # Situazione iniziale realistica: Sala e Bar suonano insieme dall'app Sonos.
        self._join("media_player.sonos_sala", ["media_player.sonos_bar"])
        self._play("media_player.sonos_sala", "FV:2/12")

    # ---------------------------------------------------------------- helpers
    def log(self, kind, text):
        with self.lock:
            self.events.insert(0, {"time": datetime.now().strftime("%H:%M:%S"), "kind": kind, "text": text})
            del self.events[200:]

    @staticmethod
    def _ids(value):
        if isinstance(value, str):
            return [value]
        return [item for item in (value or []) if isinstance(item, str)]

    def _members(self, coordinator):
        return [coordinator] + [
            entity for entity, spk in self.speakers.items()
            if spk["coordinator"] == coordinator and entity != coordinator
        ]

    def _require(self, entity_id):
        if entity_id not in self.speakers:
            raise RuntimeError(f"Home Assistant 400: entità sconosciuta {entity_id}")
        if not self.speakers[entity_id]["reachable"]:
            raise RuntimeError(f"Home Assistant 500: {entity_id} non raggiungibile")
        return self.speakers[entity_id]

    def _title_for(self, media_id):
        if media_id in FAVORITES:
            return FAVORITES[media_id], "music"
        if media_id in PLAYLISTS:
            return PLAYLISTS[media_id], "music"
        if "/media/" in media_id or media_id.lower().endswith((".mp3", ".wav", ".m4a")):
            return media_id.rsplit("/", 1)[-1].split("?")[0], "spot"
        return media_id or "Sconosciuto", "music"

    def _play(self, coordinator, media_id, source=None):
        title, kind = (source, "music") if source else self._title_for(media_id)
        ends_at = None
        if kind == "spot" and self.media_dir and (self.media_dir / title).exists():
            # Come un vero Sonos: a fine brano la cassa si ferma da sola.
            ends_at = time.time() + estimate_duration(self.media_dir / title)
        for member in self._members(coordinator):
            spk = self.speakers[member]
            spk.update(state="playing", media_id=media_id, title=title, source=source, kind=kind, ends_at=ends_at)

    def _expire(self):
        now = time.time()
        for spk in self.speakers.values():
            if spk.get("ends_at") and now >= spk["ends_at"] and spk["state"] == "playing":
                spk.update(state="idle", ends_at=None)

    def _set_state(self, coordinator, state):
        for member in self._members(coordinator):
            self.speakers[member]["state"] = state

    def _join(self, coordinator, members):
        for member in [coordinator, *members]:
            self._require(member)
        lead = self.speakers[coordinator]
        for member in members:
            spk = self.speakers[member]
            spk["coordinator"] = coordinator
            spk.update(state=lead["state"], media_id=lead["media_id"], title=lead["title"],
                       source=lead["source"], kind=lead["kind"])

    def _unjoin(self, entity_id):
        spk = self.speakers[entity_id]
        if spk["coordinator"] == entity_id:
            # Il coordinatore lascia il gruppo: il primo membro rimasto continua la musica.
            rest = [m for m in self._members(entity_id) if m != entity_id]
            if rest:
                new_lead = rest[0]
                for member in rest:
                    self.speakers[member]["coordinator"] = new_lead
        spk["coordinator"] = entity_id
        spk.update(state="idle", media_id="", title="", source=None)

    # ------------------------------------------------- API usata dall'add-on
    def states(self):
        with self.lock:
            self._expire()
            result = []
            for entity_id, spk in self.speakers.items():
                if not spk["reachable"]:
                    result.append({"entity_id": entity_id, "state": "unavailable",
                                   "attributes": {"friendly_name": spk["name"]}})
                    continue
                attrs = {
                    "friendly_name": spk["name"],
                    "volume_level": round(spk["volume"], 2),
                    "is_volume_muted": spk["muted"],
                    "group_members": self._members(spk["coordinator"]),
                    "source_list": SOURCES + list(FAVORITES.values()),
                    "device_class": "speaker",
                }
                if spk["state"] in ("playing", "paused"):
                    attrs.update(media_title=spk["title"], media_content_id=spk["media_id"])
                    if spk["source"]:
                        attrs["source"] = spk["source"]
                result.append({"entity_id": entity_id, "state": spk["state"], "attributes": attrs})
            for entity_id, dev in self.devices.items():
                result.append({"entity_id": entity_id, "state": dev["state"],
                               "attributes": {"friendly_name": dev["name"]}})
            result.append({"entity_id": "sensor.sonos_favorites", "state": str(len(FAVORITES)),
                           "attributes": {"friendly_name": "Preferiti Sonos", "items": dict(FAVORITES)}})
            return result

    def config(self):
        return {"internal_url": "http://127.0.0.1:8123", "time_zone": "Europe/Berlin"}

    def service(self, domain, service, payload):
        payload = payload or {}
        with self.lock:
            entities = self._ids(payload.get("entity_id"))
            label = f"{domain}.{service}"
            if domain == "media_player" and service == "play_media":
                self._require(entities[0])
                coordinator = self.speakers[entities[0]]["coordinator"]
                self._play(coordinator, str(payload.get("media_content_id", "")))
                self.log("spot" if self.speakers[coordinator]["kind"] == "spot" else "music",
                         f"{label} su {self._name(coordinator)}: {self.speakers[coordinator]['title']}")
            elif domain == "media_player" and service == "select_source":
                self._require(entities[0])
                self._play(entities[0], "", source=str(payload.get("source", "")))
                self.log("music", f"{label} su {self._name(entities[0])}: {payload.get('source')}")
            elif domain == "media_player" and service in ("media_play", "media_pause", "media_stop"):
                new_state = {"media_play": "playing", "media_pause": "paused", "media_stop": "idle"}[service]
                for entity in entities:
                    if entity in self.speakers:
                        self._require(entity)
                        lead = self.speakers[entity]["coordinator"]
                        if new_state == "playing" and not self.speakers[lead]["title"]:
                            continue
                        self._set_state(lead, new_state)
                    elif entity in self.devices:
                        self.devices[entity]["state"] = "on" if new_state == "playing" else "on"
                self.log("music", f"{label}: {', '.join(self._name(e) for e in entities)}")
            elif domain == "media_player" and service == "volume_set":
                for entity in entities:
                    self._require(entity)["volume"] = max(0.0, min(1.0, float(payload.get("volume_level", 0))))
                self.log("volume", f"{label} {round(float(payload.get('volume_level', 0)) * 100)}%: "
                         + ", ".join(self._name(e) for e in entities))
            elif domain == "media_player" and service == "join":
                members = self._ids(payload.get("group_members"))
                self._join(entities[0], members)
                self.log("group", f"{label}: {self._name(entities[0])} + {', '.join(self._name(m) for m in members)}")
            elif domain == "media_player" and service == "unjoin":
                for entity in entities:
                    self._require(entity)
                    self._unjoin(entity)
                self.log("group", f"{label}: {', '.join(self._name(e) for e in entities)}")
            elif domain == "media_player" and service in ("turn_on", "turn_off"):
                for entity in entities:
                    if entity in self.devices:
                        self.devices[entity]["state"] = "on" if service == "turn_on" else "off"
                self.log("tv", f"{label}: {', '.join(self._name(e) for e in entities)}")
            elif domain == "sonos" and service == "snapshot":
                targets = set()
                for entity in entities:
                    self._require(entity)
                    targets.update(self._members(self.speakers[entity]["coordinator"]))
                for entity in targets:
                    self.snapshots[entity] = copy.deepcopy(self.speakers[entity])
                self.log("snapshot", f"{label}: {len(targets)} Sonos salvati")
            elif domain == "sonos" and service == "restore":
                restored = []
                for entity in entities:
                    for member, saved in self.snapshots.items():
                        if member == entity or saved["coordinator"] == entity:
                            if self.speakers[member]["reachable"]:
                                keep = self.speakers[member]["reachable"]
                                self.speakers[member].update(copy.deepcopy(saved), reachable=keep)
                                restored.append(member)
                self.log("snapshot", f"{label}: {', '.join(self._name(e) for e in dict.fromkeys(restored)) or 'nessuno'}")
            elif domain in ("script", "switch") or (domain == "homeassistant" and service in ("turn_on", "turn_off")):
                for entity in entities:
                    if entity in self.devices:
                        self.devices[entity]["state"] = "on" if service == "turn_on" else "off"
                self.log("tv", f"{label}: {', '.join(self._name(e) for e in entities)}")
            elif domain == "wake_on_lan":
                for dev in self.devices.values():
                    if dev["name"].startswith("TV"):
                        dev["state"] = "on"
                self.log("tv", f"{label}: pacchetto inviato a {payload.get('mac')}")
            elif domain == "homeassistant" and service == "update_entity":
                self.log("info", f"{label}: Preferiti Sonos aggiornati")
            else:
                self.log("info", f"{label} (ignorato dal simulatore) {json.dumps(payload)[:120]}")
            return []

    def service_response(self, domain, service, payload):
        self.service(domain, service, payload)
        return {}

    def browse_media(self, entity_id, media_type=None, media_id=None):
        def item(mid, title, mtype):
            return {"title": title, "media_content_id": mid, "media_content_type": mtype,
                    "can_play": True, "can_expand": False}
        if not media_id:
            return {"title": "Sonos", "media_content_id": "", "media_content_type": "library",
                    "children": [
                        {"title": "Preferiti Sonos", "media_content_id": "favorites",
                         "media_content_type": "favorites", "can_expand": True, "can_play": False},
                        {"title": "I miei Sonos · Playlist", "media_content_id": "sonos_playlists",
                         "media_content_type": "sonos_playlists", "can_expand": True, "can_play": False},
                    ]}
        if media_id == "favorites":
            return {"title": "Preferiti Sonos", "children": [item(k, v, "favorite_item_id") for k, v in FAVORITES.items()]}
        if media_id == "sonos_playlists":
            return {"title": "Playlist Sonos", "children": [item(k, v, "sonos_playlist") for k, v in PLAYLISTS.items()]}
        return {"title": media_id, "children": []}

    def websocket_command(self, command, timeout=25):
        if command.get("type") == "media_player/browse_media":
            return self.browse_media(command.get("entity_id"), command.get("media_content_type"),
                                     command.get("media_content_id"))
        return {}

    # ------------------------------------------- comandi del pannello simulatore
    def _name(self, entity_id):
        if entity_id in self.speakers:
            return self.speakers[entity_id]["name"].replace("Sonos ", "")
        return self.devices.get(entity_id, {}).get("name", entity_id)

    def snapshot_for_panel(self):
        with self.lock:
            self._expire()
            speakers = []
            for entity_id, spk in self.speakers.items():
                speakers.append({
                    "entity_id": entity_id, "name": spk["name"], "state": spk["state"],
                    "volume": round(spk["volume"] * 100), "title": spk["title"], "kind": spk["kind"],
                    "reachable": spk["reachable"], "coordinator": spk["coordinator"],
                    "group": self._members(spk["coordinator"]),
                })
            devices = [{"entity_id": k, **v} for k, v in self.devices.items()]
            content = [{"id": k, "name": v} for k, v in {**FAVORITES, **PLAYLISTS}.items()]
            return {"speakers": speakers, "devices": devices, "content": content, "events": self.events[:80]}

    def panel_action(self, body):
        action = body.get("action")
        entity = body.get("entity_id", "")
        with self.lock:
            if action == "app_play":
                # Musica avviata dall'app Sonos: non passa dall'add-on.
                self._require(entity)
                self._play(self.speakers[entity]["coordinator"], body.get("media_id", "FV:2/12"))
                self.log("app", f"App Sonos: {self._name(entity)} suona {self.speakers[entity]['title']}")
            elif action in ("app_pause", "app_stop"):
                self._require(entity)
                self._set_state(self.speakers[entity]["coordinator"], "paused" if action == "app_pause" else "idle")
                self.log("app", f"App Sonos: {self._name(entity)} {'in pausa' if action == 'app_pause' else 'fermato'}")
            elif action == "volume":
                self._require(entity)["volume"] = max(0, min(100, int(body.get("value", 0)))) / 100
                self.log("app", f"App Sonos: volume {self._name(entity)} {body.get('value')}%")
            elif action == "toggle_reachable":
                spk = self.speakers[entity]
                spk["reachable"] = not spk["reachable"]
                if not spk["reachable"]:
                    self._unjoin(entity)
                self.log("app", f"{self._name(entity)} {'ricollegato' if spk['reachable'] else 'scollegato dalla rete'}")
            elif action == "join":
                target = body.get("target", "")
                self._join(self.speakers[target]["coordinator"], [entity])
                self.log("app", f"App Sonos: {self._name(entity)} unito a {self._name(target)}")
            elif action == "unjoin":
                self._unjoin(entity)
                self.log("app", f"App Sonos: {self._name(entity)} separato dal gruppo")
            elif action == "device_toggle":
                dev = self.devices[entity]
                dev["state"] = "off" if dev["state"] == "on" else "on"
                self.log("app", f"{dev['name']} {'acceso' if dev['state'] == 'on' else 'spento'} a mano")
            elif action == "clear_log":
                self.events.clear()
            else:
                raise ValueError(f"Azione sconosciuta: {action}")


def estimate_duration(path):
    """Durata approssimata senza ffprobe: legge il bitrate del primo frame MP3."""
    bitrates = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
    data = Path(path).read_bytes()
    start = 0
    if data[:3] == b"ID3":
        start = 10 + ((data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F))
    for i in range(start, min(len(data) - 4, start + 65536)):
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            kbps = bitrates[(data[i + 2] >> 4) & 0x0F]
            if kbps:
                return max(1.0, (len(data) - i) * 8 / (kbps * 1000))
    return 20.0


def prepare_media(media_dir):
    """Ricompone gli spot MP3 divisi in parti, come fa il Dockerfile."""
    media_dir.mkdir(parents=True, exist_ok=True)
    bundled = APP_DIR / "bundled_media"
    groups = {}
    for part in sorted(bundled.glob("*.part.*")):
        groups.setdefault(part.name.split(".part.")[0], []).append(part)
    for name, parts in groups.items():
        target = media_dir / name
        if not target.exists():
            with target.open("wb") as out:
                for part in parts:
                    out.write(part.read_bytes())


def load_app(data_dir, media_dir):
    os.environ["CARELLAS_DATA_DIR"] = str(data_dir)
    os.environ["CARELLAS_MEDIA_DIR"] = str(media_dir)
    spec = importlib.util.spec_from_file_location("carellas_server", APP_DIR / "server.py")
    app = importlib.util.module_from_spec(spec)
    sys.modules["carellas_server"] = app
    spec.loader.exec_module(app)
    for leftover in media_dir.glob("*.part.*"):
        leftover.unlink()
    return app


def make_sim_handler(virtual):
    page = (DEV_DIR / "simulator.html").read_bytes()

    class SimHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body, ctype="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/api/sim/state"):
                self.send(200, json.dumps(virtual.snapshot_for_panel()).encode())
            else:
                self.send(200, page, "text/html; charset=utf-8")

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                virtual.panel_action(json.loads(self.rfile.read(length) or b"{}"))
                self.send(200, json.dumps(virtual.snapshot_for_panel()).encode())
            except Exception as error:
                self.send(400, json.dumps({"error": str(error)}).encode())

    return SimHandler


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default=str(DEV_DIR / ".local-data"), help="cartella dei dati locali")
    parser.add_argument("--reset", action="store_true", help="cancella configurazione e dati locali prima di partire")
    parser.add_argument("--host", default="127.0.0.1", help="usa 0.0.0.0 per aprirlo da telefono o TV in rete")
    args = parser.parse_args()

    root = Path(args.data)
    if args.reset and root.exists():
        shutil.rmtree(root)
    data_dir, media_dir = root / "data", root / "media"
    data_dir.mkdir(parents=True, exist_ok=True)
    prepare_media(media_dir)

    # Tempi Sonos accorciati: il simulatore risponde subito.
    os.environ.setdefault("CARELLAS_SONOS_GROUP_SETTLE_SECONDS", "0.2")
    os.environ.setdefault("CARELLAS_SONOS_RESTORE_SETTLE_SECONDS", "0.2")

    app = load_app(data_dir, media_dir)
    virtual = VirtualHomeAssistant(media_dir)
    for name in ("states", "config", "service", "service_response", "browse_media", "websocket_command"):
        setattr(app.ha, name, getattr(virtual, name))

    original_probe = app.probe_media_duration

    def probe(source):
        try:
            return original_probe(source)
        except RuntimeError:
            if not Path(source).exists():
                raise
            return estimate_duration(source)

    app.probe_media_duration = probe
    if not app.store.config.get("media_base_url"):
        app.store.config["media_base_url"] = f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{app.PORT}"

    app.scheduler.start()
    servers = [
        ThreadingHTTPServer((args.host, app.PLAYER_PORT), app.PlayerHandler),
        ThreadingHTTPServer((args.host, SIM_PORT), make_sim_handler(virtual)),
    ]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    main_server = ThreadingHTTPServer((args.host, app.PORT), app.Handler)
    shown = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    print("\nCarellas Media Ads in locale (Home Assistant e Sonos virtuali)")
    print(f"  Pannello      http://{shown}:{app.PORT}")
    print(f"  Player TV     http://{shown}:{app.PLAYER_PORT}")
    print(f"  Simulatore    http://{shown}:{SIM_PORT}")
    print(f"  Dati locali   {root}")
    if shutil.which("ffprobe") is None:
        print("  Nota: ffmpeg non trovato. Spot audio OK (durata stimata); i canali IPTV non si possono generare.")
    print("Premi Ctrl+C per fermare.\n", flush=True)
    try:
        main_server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrestato.")
    finally:
        time.sleep(0.1)


if __name__ == "__main__":
    main()
