import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from unittest import mock
from datetime import datetime
from pathlib import Path
from http.server import ThreadingHTTPServer


class CarellasServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        os.environ["CARELLAS_DATA_DIR"] = str(Path(cls.temp.name) / "data")
        os.environ["CARELLAS_MEDIA_DIR"] = str(Path(cls.temp.name) / "media")
        source = Path(__file__).parents[1] / "carellas_media_ads/app/server.py"
        spec = importlib.util.spec_from_file_location("carellas_server", source)
        cls.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.app)
        cls.calls = []
        cls.app.ha.states = lambda: [{
            "entity_id": "media_player.sala",
            "state": "playing",
            "attributes": {"friendly_name": "Sonos Sala"},
        }]
        cls.app.ha.config = lambda: {"internal_url": "http://192.168.1.10:8123"}
        cls.app.ha.service = lambda domain, service, payload: cls.calls.append((domain, service, payload)) or []
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls.app.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.player_httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls.app.PlayerHandler)
        cls.player_base = f"http://127.0.0.1:{cls.player_httpd.server_address[1]}"
        threading.Thread(target=cls.player_httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.player_httpd.shutdown()
        cls.player_httpd.server_close()
        cls.temp.cleanup()

    def setUp(self):
        self.app.SONOS_CATALOG_FILE.unlink(missing_ok=True)

    def request(self, path, method="GET", body=None, content_type="application/json"):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": content_type})
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read())

    def test_state_and_ingress_path(self):
        status, payload = self.request("/api/hassio_ingress/example/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(payload["entities"][0]["entity_id"], "media_player.sala")
        self.assertEqual(payload["runtime"]["media_base_url"], "http://192.168.1.10:8099")
        self.assertEqual(payload["runtime"]["player_local_url"], "http://192.168.1.10:8101")

    def test_browser_player_requires_login_and_only_serves_assigned_channel(self):
        self.app.store.update({
            "iptv": {"channels": [{
                "id": "sala-tv", "name": "Sala TV", "enabled": True,
                "playlist": [{"name": "spot.mp4", "kind": "video"}],
            }]},
            "browser_player": {"enabled": True, "users": [{
                "username": "sala", "password": "segreta1", "channel_id": "sala-tv", "fit": "cover",
            }]},
        })
        output = self.app.iptv.output("sala-tv")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"0123456789")
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(self.player_base + "/api/state")
        self.assertEqual(denied.exception.code, 401)
        request = urllib.request.Request(
            self.player_base + "/api/login",
            data=json.dumps({"username": "sala", "password": "segreta1"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        state_request = urllib.request.Request(self.player_base + "/api/state", headers={"Cookie": cookie})
        with urllib.request.urlopen(state_request) as response:
            state = json.loads(response.read())
        self.assertEqual(state["channel_id"], "sala-tv")
        self.assertEqual(state["fit"], "cover")
        self.assertTrue(state["ready"])
        media_request = urllib.request.Request(
            self.player_base + "/channel.mp4", headers={"Cookie": cookie, "Range": "bytes=2-5"}
        )
        with urllib.request.urlopen(media_request) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"2345")
        private_request = urllib.request.Request(self.player_base + "/api/config", headers={"Cookie": cookie})
        with self.assertRaises(urllib.error.HTTPError) as private_api:
            urllib.request.urlopen(private_request)
        self.assertEqual(private_api.exception.code, 404)

    def test_browser_player_hashes_password_and_has_fullscreen_audio_ui(self):
        self.app.store.update({"browser_player": {"enabled": True, "users": [{
            "username": "sala", "password": "segreta1", "channel_id": "carellas-demo", "fit": "contain",
        }]}})
        account = self.app.store.config["browser_player"]["users"][0]
        self.assertNotIn("password", account)
        self.assertTrue(account["password_hash"].startswith("pbkdf2_sha256$"))
        self.assertTrue(self.app.password_matches("segreta1", account["password_hash"]))
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/player.html").read_text(encoding="utf-8")
        self.assertIn('video.muted=false', html)
        self.assertIn("objectFit=state.fit", html)
        self.assertIn("100vw;height:100vh", html)
        self.assertIn("Avvia video e audio", html)
        self.assertIn("Video und Ton starten", html)
        self.assertIn('onclick="togglePassword(\'password\',this)"', html)
        self.assertIn("Visualizza password", html)
        self.assertIn("Passwort anzeigen", html)
        self.assertIn("$('language').style.display=name?'flex':'none'", html)
        dashboard = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn('id="playerUsers"', dashboard)
        self.assertIn('id="playerUserSelect"', dashboard)
        self.assertIn('onchange="selectPlayerUser(this.value)"', dashboard)
        self.assertIn("function selectPlayerUser(value)", dashboard)
        self.assertIn("c.browser_player=", dashboard)
        self.assertIn('onclick="togglePasswordField(this)"', dashboard)
        self.assertIn('onclick="savePlayerUser(${i},this)"', dashboard)
        self.assertIn('Reimposta password', dashboard)
        self.assertIn('playerPasswordConfirm', dashboard)
        self.assertIn('function showPasswordReset(button)', dashboard)
        self.assertIn('Le due password non coincidono', dashboard)

    def test_browser_player_prepares_missing_assigned_channel_automatically(self):
        self.app.store.update({
            "iptv": {"channels": [{
                "id": "automatico", "name": "Automatico", "enabled": True,
                "playlist": [{"name": "video.mp4", "kind": "video"}],
            }]},
            "browser_player": {"enabled": True, "users": [{
                "username": "tvauto", "password": "segreta2", "channel_id": "automatico",
            }]},
        })
        output = self.app.iptv.output("automatico")
        output.unlink(missing_ok=True)
        self.app.iptv.status.pop("automatico", None)
        login = urllib.request.Request(
            self.player_base + "/api/login",
            data=json.dumps({"username": "tvauto", "password": "segreta2"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(login) as response:
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        state_request = urllib.request.Request(self.player_base + "/api/state", headers={"Cookie": cookie})
        with mock.patch.object(self.app.iptv, "build_async", return_value=True) as build:
            with urllib.request.urlopen(state_request) as response:
                state = json.loads(response.read())
        build.assert_called_once_with("automatico")
        self.assertFalse(state["ready"])
        self.assertEqual(state["status"], "building")
        self.assertIn("message", state)

    def test_browser_player_reports_empty_channel_without_endless_build(self):
        self.app.store.update({
            "iptv": {"channels": [{"id": "vuoto", "name": "Vuoto", "enabled": True, "playlist": []}]},
            "browser_player": {"enabled": True, "users": [{
                "username": "tvvuoto", "password": "segreta3", "channel_id": "vuoto",
            }]},
        })
        login = urllib.request.Request(
            self.player_base + "/api/login",
            data=json.dumps({"username": "tvvuoto", "password": "segreta3"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(login) as response:
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        state_request = urllib.request.Request(self.player_base + "/api/state", headers={"Cookie": cookie})
        with mock.patch.object(self.app.iptv, "build_async") as build:
            with urllib.request.urlopen(state_request) as response:
                state = json.loads(response.read())
        build.assert_not_called()
        self.assertEqual(state["status"], "empty_channel")

    def test_admin_save_persists_player_credentials_for_login(self):
        status, payload = self.request("/api/config", "POST", {
            "iptv": {"channels": [{"id": "carellas", "name": "Carellas", "enabled": True, "playlist": []}]},
            "browser_player": {"enabled": True, "users": [{
                "username": "tv1", "password": "Marius1988", "channel_id": "carellas", "fit": "cover",
            }]},
        })
        self.assertEqual(status, 200)
        account = payload["config"]["browser_player"]["users"][0]
        self.assertEqual(account["username"], "tv1")
        self.assertNotIn("password", account)
        self.assertTrue(self.app.password_matches("Marius1988", account["password_hash"]))
        login = urllib.request.Request(
            self.player_base + "/api/login",
            data=json.dumps({"username": "tv1", "password": "Marius1988"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(login) as response:
            self.assertEqual(response.status, 200)

    def test_video_builder_preserves_original_audio_stream(self):
        source = self.app.MEDIA_DIR / "spot-con-audio.mp4"
        source.write_bytes(b"video")
        self.app.store.update({"iptv": {"channels": [{
            "id": "audio-tv", "name": "Audio TV", "enabled": True,
            "playlist": [{"name": source.name, "kind": "video", "fit": "contain"}], "schedule": [],
        }]}})
        commands = []
        def fake_run(command, **kwargs):
            commands.append(command)
            if command[0] == "ffprobe":
                return mock.Mock(stdout="h264\n" if "stream=codec_name" in command else "3.0\n")
            Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(command[-1]).write_bytes(b"built" * 300)
            return mock.Mock(stdout="")
        with mock.patch.object(self.app.IPTVEngine, "_probe_duration", return_value=3), mock.patch.object(
            self.app.IPTVEngine, "_has_audio", return_value=True
        ), mock.patch.object(self.app.subprocess, "run", side_effect=fake_run):
            self.app.iptv.build("audio-tv")
        first = commands[0]
        self.assertIn("0:a:0", first)
        self.assertNotIn("anullsrc=channel_layout=stereo:sample_rate=48000", first)

    def test_iptv_builder_records_exact_saved_order_and_visible_effects(self):
        names = ["01-prima.jpg", "02-video.mp4", "03-ultima.jpg"]
        for name in names:
            (self.app.MEDIA_DIR / name).write_bytes(b"source")
        channel = {
            "id": "ordine-effetti", "name": "Ordine ed effetti", "enabled": True,
            "playlist": [
                {"name": names[0], "kind": "image", "duration": 4, "effect": "zoom_in"},
                {"name": names[1], "kind": "video", "fit": "smart"},
                {"name": names[2], "kind": "image", "duration": 5, "effect": "pan"},
            ],
            "schedule": [],
        }
        self.app.store.update({"iptv": {"channels": [channel]}})

        def fake_run(command, **kwargs):
            if command[0] == "ffprobe":
                value = "h264\n" if "stream=codec_name" in command else "16.0\n"
                return mock.Mock(stdout=value)
            target = Path(command[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"built" * 300)
            return mock.Mock(stdout="")

        with mock.patch.object(self.app.IPTVEngine, "_probe_duration", return_value=7), mock.patch.object(
            self.app.IPTVEngine, "_has_audio", return_value=False
        ), mock.patch.object(self.app.subprocess, "run", side_effect=fake_run):
            self.app.iptv.build("ordine-effetti")

        metadata = json.loads(self.app.iptv.build_metadata("ordine-effetti").read_text(encoding="utf-8"))
        self.assertEqual([item["name"] for item in metadata["sequence"]], names)
        self.assertEqual([item["position"] for item in metadata["sequence"]], [1, 2, 3])
        self.assertEqual([item["duration"] for item in metadata["sequence"]], [4, 7, 5])
        self.assertEqual([item["effect"] for item in metadata["sequence"]], ["zoom_in", "none", "pan"])
        concat = (self.app.iptv.output("ordine-effetti").parent / "parts" / "concat.txt").read_text()
        self.assertLess(concat.index("0000.mp4"), concat.index("0001.mp4"))
        self.assertLess(concat.index("0001.mp4"), concat.index("0002.mp4"))

    def test_image_effects_are_deliberately_visible(self):
        zoom_in = self.app.IPTVEngine._effect_filter("zoom_in", 10)
        zoom_out = self.app.IPTVEngine._effect_filter("zoom_out", 10)
        pan = self.app.IPTVEngine._effect_filter("pan", 10)
        fade = self.app.IPTVEngine._fade_filter(10)
        self.assertIn("scale=3840:2160:flags=lanczos", zoom_in)
        self.assertIn("cos(PI", zoom_in)
        self.assertIn("0.30", zoom_in)
        self.assertIn("scale=3840:2160:flags=lanczos", zoom_out)
        self.assertIn("1.30", zoom_out)
        self.assertIn("scale=3840:2160:flags=lanczos", pan)
        self.assertIn("cos(PI", pan)
        self.assertIn("z=1.25", pan)
        self.assertIn("d=1.200", fade)

    def test_professional_effect_presets_are_real_ffmpeg_filters(self):
        effects = {
            "ken_burns": ["zoompan", "1.08+0.22", "x='(iw-iw/zoom)"],
            "ken_burns_reverse": ["zoompan", "1.30-0.22", "y='(ih-ih/zoom)"],
            "pan_right": ["zoompan=z=1.25", "x='(iw-iw/zoom)"],
            "pan_left": ["zoompan=z=1.25", "*(1-"],
            "pan_down": ["zoompan=z=1.25", "y='(ih-ih/zoom)"],
            "pan_up": ["zoompan=z=1.25", "y='(ih-ih/zoom)*(1-"],
        }
        for name, fragments in effects.items():
            graph = self.app.IPTVEngine._effect_filter(name, 8)
            self.assertIn("scale=3840:2160:flags=lanczos", graph, name)
            self.assertIn("cos(PI", graph, name)
            for fragment in fragments:
                self.assertIn(fragment, graph, name)

    def test_dashboard_lists_new_effects_in_both_languages(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        for label in (
            "Ken Burns diagonale", "Ken Burns inverso", "Panoramica verso destra",
            "Panoramica verso sinistra", "Panoramica verso il basso", "Panoramica verso l’alto",
            "Diagonaler Ken-Burns-Effekt", "Schwenk nach links", "Schwenk nach oben",
        ):
            self.assertIn(label, html)

    def test_state_exposes_sonos_favorites_for_music_picker(self):
        states = [{
            "entity_id": "media_player.sala",
            "state": "idle",
            "attributes": {"friendly_name": "Sonos Sala"},
        }, {
            "entity_id": "sensor.sonos_favorites",
            "state": "2",
            "attributes": {
                "friendly_name": "Sonos Favorites",
                "items": {"FV:2/31": "Radio Italia", "FV:2/4": "Cena Carellas"},
            },
        }]
        with mock.patch.object(self.app.ha, "states", return_value=states):
            status, payload = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(payload["sonos_favorites"], [
            {"id": "FV:2/4", "name": "Cena Carellas", "type": "favorite_item_id"},
            {"id": "FV:2/31", "name": "Radio Italia", "type": "favorite_item_id"},
        ])

    def test_sonos_refresh_forces_favorites_update_and_scans_nested_playlists(self):
        before = [{
            "entity_id": "sensor.sonos_favorites",
            "state": "1",
            "attributes": {"items": {"FV:2/31": "Radio Italia"}},
        }]
        after = [{
            "entity_id": "sensor.sonos_favorites",
            "state": "2",
            "attributes": {"items": {
                "FV:2/31": "Radio Italia", "FV:2/4": "Carellas Ristorante",
            }},
        }]
        root = {"title": "Sonos", "children": [{
            "title": "I miei Sonos", "media_content_type": "favorites",
            "media_content_id": "my-sonos", "can_expand": True, "can_play": False,
        }]}
        my_sonos = {"title": "I miei Sonos", "children": [{
            "title": "Playlist", "media_content_type": "playlist_folder",
            "media_content_id": "playlists", "can_expand": True, "can_play": False,
        }, {
            "title": "Radio Kiss Kiss", "media_content_type": "favorite_item_id",
            "media_content_id": "FV:2/88", "can_expand": False, "can_play": True,
        }]}
        playlists = {"title": "Playlist", "children": [{
            "title": "Gigi D'Alessio", "media_content_type": "playlist",
            "media_content_id": "S:/Gigi", "can_expand": False, "can_play": True,
        }, {
            "title": "Freitag", "media_content_type": "playlist",
            "media_content_id": "S:/Freitag", "can_expand": False, "can_play": True,
        }]}

        def browse(_entity_id, _media_type=None, media_id=None):
            browser = {None: root, "my-sonos": my_sonos, "playlists": playlists}[media_id]
            return browser

        with mock.patch.object(self.app.ha, "states", side_effect=[before, after]), \
                mock.patch.object(self.app.ha, "service") as update, \
                mock.patch.object(self.app.ha, "browse_media", side_effect=browse):
            catalog = self.app.refresh_sonos_catalog("media_player.sala")

        update.assert_called_once_with("homeassistant", "update_entity", {
            "entity_id": ["sensor.sonos_favorites"],
        })
        self.assertEqual({item["name"] for item in catalog["items"]}, {
            "Radio Italia", "Carellas Ristorante", "Radio Kiss Kiss",
            "Gigi D'Alessio", "Freitag",
        })
        self.assertEqual(catalog["folders_scanned"], 2)
        self.assertFalse(catalog["truncated"])

    def test_sonos_refresh_rejects_generic_home_assistant_media_browser(self):
        states = [{
            "entity_id": "sensor.sonos_favorites", "state": "1",
            "attributes": {"items": {"FV:2/31": "Radio Italia"}},
        }]
        generic_root = {"title": "Audio", "children": [{
            "title": "Radio Browser", "media_content_type": "library",
            "media_content_id": "media-source://radio_browser", "can_expand": True,
            "can_play": False,
        }, {
            "title": "Brano locale", "media_content_type": "audio/mpeg",
            "media_content_id": "media-source://media_source/local/song.mp3",
            "can_expand": False, "can_play": True,
        }]}

        with mock.patch.object(self.app.ha, "states", side_effect=[states, states]), \
                mock.patch.object(self.app.ha, "service"), \
                mock.patch.object(self.app.ha, "browse_media", return_value=generic_root) as browse:
            catalog = self.app.refresh_sonos_catalog("media_player.sala")

        self.assertEqual(catalog["items"], [{
            "id": "FV:2/31", "name": "Radio Italia", "type": "favorite_item_id",
        }])
        self.assertEqual(catalog["folders_scanned"], 0)
        self.assertEqual(catalog["source"], "sonos")
        browse.assert_called_once_with("media_player.sala", None, None)

    def test_saved_sonos_catalog_discards_old_radio_browser_entries(self):
        self.app.SONOS_CATALOG_FILE.write_text(json.dumps({"items": [{
            "id": "media-source://radio_browser/abc",
            "name": "Radio Browser inutile",
            "type": "audio/mpeg",
        }, {
            "id": "Carellas Ristorante",
            "name": "Carellas Ristorante",
            "type": "sonos_source",
        }]}), encoding="utf-8")

        self.assertEqual(self.app.load_sonos_catalog(), [{
            "id": "Carellas Ristorante",
            "name": "Carellas Ristorante",
            "type": "sonos_source",
        }])

    def test_sonos_refresh_reads_native_sources_from_sonos_player(self):
        states = [{
            "entity_id": "media_player.sala",
            "state": "playing",
            "attributes": {
                "friendly_name": "Sonos Sala",
                "group_members": ["media_player.sala"],
                "source_list": ["Carellas Ristorante", "Gigi D'Alessio"],
            },
        }]
        generic_root = {"title": "Audio", "children": [{
            "title": "Radio Browser",
            "media_content_type": "library",
            "media_content_id": "media-source://radio_browser",
            "can_expand": True,
            "can_play": False,
        }]}

        with mock.patch.object(self.app.ha, "states", return_value=states), \
                mock.patch.object(self.app.ha, "browse_media", return_value=generic_root):
            catalog = self.app.refresh_sonos_catalog("media_player.sala")

        self.assertEqual(catalog["items"], [{
            "id": "Carellas Ristorante",
            "name": "Carellas Ristorante",
            "type": "sonos_source",
        }, {
            "id": "Gigi D'Alessio",
            "name": "Gigi D'Alessio",
            "type": "sonos_source",
        }])

    def test_sonos_refresh_scans_only_explicit_sonos_folder_in_generic_root(self):
        states = []
        root = {"title": "Audio", "children": [{
            "title": "Radio Browser", "media_content_type": "library",
            "media_content_id": "radio-browser", "can_expand": True, "can_play": False,
        }, {
            "title": "I miei Sonos", "media_content_type": "favorites",
            "media_content_id": "my-sonos", "can_expand": True, "can_play": False,
        }]}
        sonos = {"title": "I miei Sonos", "children": [{
            "title": "Carellas Ristorante", "media_content_type": "playlist",
            "media_content_id": "S:/Carellas", "can_expand": False, "can_play": True,
        }]}

        def browse(_entity_id, _media_type=None, media_id=None):
            return {None: root, "my-sonos": sonos}[media_id]

        with mock.patch.object(self.app.ha, "states", return_value=states), \
                mock.patch.object(self.app.ha, "browse_media", side_effect=browse) as call:
            catalog = self.app.refresh_sonos_catalog("media_player.sala")

        self.assertEqual([item["id"] for item in catalog["items"]], ["S:/Carellas"])
        self.assertEqual(catalog["folders_scanned"], 1)
        self.assertEqual(call.call_count, 2)

    def test_refreshed_sonos_catalog_survives_next_state_reload(self):
        self.app.SONOS_CATALOG_FILE.unlink(missing_ok=True)
        before = [{
            "entity_id": "sensor.sonos_favorites", "state": "1",
            "attributes": {"items": {"FV:2/31": "Radio Italia"}},
        }]
        after = [{
            "entity_id": "sensor.sonos_favorites", "state": "1",
            "attributes": {"items": {"FV:2/31": "Radio Italia"}},
        }]
        state_reload = [{
            "entity_id": "media_player.sala", "state": "idle",
            "attributes": {
                "friendly_name": "Sonos Sala",
                "group_members": ["media_player.sala"],
            },
        }, *after]
        root = {"title": "Sonos", "children": [{
            "title": "Carellas Ristorante", "media_content_type": "playlist",
            "media_content_id": "S:/Carellas", "can_expand": False, "can_play": True,
        }]}
        with mock.patch.object(self.app.ha, "states", side_effect=[before, after, state_reload]), \
                mock.patch.object(self.app.ha, "service"), \
                mock.patch.object(self.app.ha, "browse_media", return_value=root):
            status, refreshed = self.request("/api/music/refresh", "POST", {
                "entity_id": "media_player.sala",
            })
            self.assertEqual(status, 200)
            self.assertIn("S:/Carellas", {item["id"] for item in refreshed["items"]})
            status, reloaded = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertIn("S:/Carellas", {item["id"] for item in reloaded["sonos_favorites"]})

    def test_state_sonos_picker_hides_unavailable_and_non_sonos_players(self):
        states = [{
            "entity_id": "media_player.sala",
            "state": "playing",
            "attributes": {"friendly_name": "Sonos Sala", "group_members": ["media_player.sala"]},
        }, {
            "entity_id": "media_player.vecchio",
            "state": "unavailable",
            "attributes": {"friendly_name": "Sonos Vecchio", "group_members": ["media_player.vecchio"]},
        }, {
            "entity_id": "media_player.televisore",
            "state": "idle",
            "attributes": {"friendly_name": "Televisore"},
        }]
        with mock.patch.object(self.app.ha, "states", return_value=states):
            status, payload = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual([item["entity_id"] for item in payload["entities"]], ["media_player.sala"])

    def test_editor_preserves_unsaved_settings_and_shows_sequence_order(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn("if(dirty&&!force)return", html)
        self.assertIn("Ordine di riproduzione, da sinistra a destra", html)
        self.assertIn("Sequenza realmente generata", html)
        self.assertIn("monitorIptvBuild", html)
        self.assertIn("Salva e genera sequenza", html)
        self.assertIn("images:[]", html)
        self.assertIn("foto selezionate su 6", html)
        self.assertNotIn('id="audioDriver"', html)

    def test_desktop_sonos_picker_and_ingress_music_controls(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn('id="audioPlayers" class="speaker-picker"', html)
        self.assertIn('id="musicPlayers" class="speaker-picker"', html)
        self.assertIn("speakerValues('musicPlayers')", html)
        self.assertIn("api('api/music/start')", html)
        self.assertIn("api('api/music/stop')", html)
        self.assertNotIn("api('/api/music/start')", html)
        self.assertIn('id="musicFavorite"', html)
        self.assertIn("favorite_item_id", html)
        self.assertIn("Sfoglia la musica del Sonos", html)
        self.assertIn("api/music/browse", html)
        self.assertIn("browseSonosBack", html)
        self.assertIn("Aggiorna playlist Sonos", html)
        self.assertIn("refreshSonosSources", html)
        self.assertIn("api/music/refresh", html)
        self.assertIn("Aggiornamento completo della libreria Sonos", html)

    def test_dashboard_is_responsive_on_phone(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn("@media(max-width:700px)", html)
        self.assertIn("overflow-x:hidden", html)
        self.assertIn(".speaker-option span{min-width:0;max-width:100%;flex:1;overflow:hidden}", html)
        self.assertIn("padding:8px 8px calc(88px + env(safe-area-inset-bottom))", html)
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", html)
        self.assertIn(".speaker-option strong,.speaker-option small{white-space:normal", html)
        self.assertIn(".savebar{position:static", html)

    def test_language_switch_is_always_visible_and_persists_immediately(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn('class="language-control"', html)
        self.assertIn('id="language" aria-label="Lingua / Sprache"', html)
        self.assertEqual(html.count('id="language"'), 1)
        self.assertIn("async function saveLanguage(lang)", html)
        self.assertIn("JSON.stringify({language:lang})", html)

    def test_audio_duration_is_automatic_in_the_interface(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn("Automatica: viene letta direttamente dal file audio", html)
        self.assertIn("formatDuration(m.duration)", html)
        self.assertNotIn('id="spotDuration" type="number"', html)

    def test_stop_spot_button_is_available_on_dashboard_and_audio_page(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count('onclick="stopAudio()"'), 2)
        self.assertIn("api/audio/stop", html)

    def test_audio_frequency_and_repeat_controls_are_unambiguous(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn("Riproduci uno spot ogni (minuti)", html)
        self.assertIn("A ogni intervallo viene riprodotto un solo spot", html)
        self.assertIn("Uno alla volta, nell’ordine della lista", html)
        self.assertNotIn("Quante volte consecutive", html)

    def test_complete_german_ui_and_audio_rotation_summary_are_available(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn("Sonos-Audiowerbung", html)
        self.assertIn("Geplante Playlists und Radios", html)
        self.assertIn("IPTV-Kanäle für mehrere Fernseher", html)
        self.assertIn("Aktivitätsprotokoll", html)
        self.assertIn('id="audioRotationSummary"', html)
        self.assertIn("updateAudioRotationSummary()", html)
        self.assertIn("e.target.id==='language'", html)
        self.assertIn(". Es können beliebig viele Zeiträume erstellt werden.", html)

    def test_audio_rotation_plays_one_different_spot_per_interval(self):
        ads = ["spot-1.mp3", "spot-2.mp3", "spot-3.mp3"]
        self.app.scheduler.audio_index = 0
        self.assertEqual(
            [self.app.scheduler.pick_audio(ads, "rotate") for _ in range(5)],
            ["spot-1.mp3", "spot-2.mp3", "spot-3.mp3", "spot-1.mp3", "spot-2.mp3"],
        )

    def test_automatic_rotation_ignores_legacy_consecutive_repetitions(self):
        ad = "single-per-interval.mp3"
        (self.app.MEDIA_DIR / ad).write_bytes(b"audio")
        self.app.store.update({"audio": {
            "players": ["media_player.sala"],
            "ads": [ad, "next-interval.mp3"],
            "mode": "rotate",
            "repeat_count": 5,
            "repeat_gap_seconds": 10,
            "daily_limit": 20,
        }})
        self.app.scheduler.audio_index = 0
        self.calls.clear()
        with mock.patch.object(self.app, "probe_media_duration", return_value=1), mock.patch.object(
            self.app.time, "sleep"
        ), mock.patch.object(self.app.audio_stop_event, "wait", return_value=False):
            self.app.play_audio()
        play_calls = [call for call in self.calls if call[1] == "play_media"]
        self.assertEqual(len(play_calls), 1)
        self.assertTrue(play_calls[0][2]["media_content_id"].endswith("/media/single-per-interval.mp3"))
        (self.app.MEDIA_DIR / ad).unlink()

    def test_manual_spot_waits_for_real_audio_duration_once(self):
        ad = "duration-test.mp3"
        (self.app.MEDIA_DIR / ad).write_bytes(b"audio")
        self.app.store.update({"audio": {
            "players": ["media_player.sala"],
            "ads": [ad],
            "repeat_count": 2,
            "repeat_gap_seconds": 7,
            "transition_seconds": 0,
        }})
        self.calls.clear()
        count_before = self.app.scheduler.audio_today()
        with mock.patch.object(self.app, "probe_media_duration", return_value=42.25) as probe, mock.patch.object(
            self.app.time, "sleep"
        ) as sleep, mock.patch.object(self.app.audio_stop_event, "wait", return_value=False) as wait:
            self.app.play_audio(ad, manual=True)
        probe.assert_called_once_with(self.app.MEDIA_DIR / ad)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.6, 0.6])
        self.assertAlmostEqual(wait.call_args_list[0].args[0], 42.25, places=2)
        self.assertEqual(len([call for call in self.calls if call[1] == "play_media"]), 1)
        self.assertTrue(any(call[1] == "media_play" for call in self.calls))
        self.assertEqual(self.app.scheduler.audio_today(), count_before + 1)
        (self.app.MEDIA_DIR / ad).unlink()

    def test_music_start_endpoint_plays_on_selected_sonos(self):
        self.app.store.update({"music": {
            "players": ["media_player.sala"],
            "content_id": "https://example.test/radio.mp3",
            "content_type": "music",
            "volume": 25,
        }})
        self.calls.clear()
        status, payload = self.request("/api/music/start", "POST", {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        play_calls = [call for call in self.calls if call[1] == "play_media"]
        self.assertEqual(len(play_calls), 1)
        self.assertEqual(play_calls[0][2]["entity_id"], "media_player.sala")
        self.assertEqual(play_calls[0][2]["media_content_id"], "https://example.test/radio.mp3")

    def test_music_start_uses_select_source_for_native_sonos_playlist(self):
        self.app.store.update({"music": {
            "players": ["media_player.sala"],
            "content_id": "Carellas Ristorante",
            "content_type": "sonos_source",
            "volume": 25,
        }})
        self.calls.clear()

        status, payload = self.request("/api/music/start", "POST", {})

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIn(("media_player", "select_source", {
            "entity_id": "media_player.sala",
            "source": "Carellas Ristorante",
        }), self.calls)
        self.assertTrue(any(call[1] == "media_play" for call in self.calls))
        self.assertFalse(any(call[1] == "play_media" for call in self.calls))

    def test_music_browser_uses_selected_sonos_media_library(self):
        browser = {
            "title": "Libreria Sonos",
            "media_content_type": "library",
            "media_content_id": "root",
            "can_expand": True,
            "can_play": False,
            "children": [{
                "title": "Playlist cena",
                "media_content_type": "playlist",
                "media_content_id": "S:/Playlist cena",
                "can_expand": False,
                "can_play": True,
            }],
        }
        with mock.patch.object(self.app.ha, "browse_media", return_value=browser) as service:
            status, payload = self.request("/api/music/browse", "POST", {
                "entity_id": "media_player.sala",
                "media_content_type": "library",
                "media_content_id": "root",
            })
        self.assertEqual(status, 200)
        self.assertEqual(payload["browser"]["children"][0]["title"], "Playlist cena")
        service.assert_called_once_with("media_player.sala", "library", "root")

    def test_home_assistant_browse_media_uses_authenticated_websocket(self):
        browser = {
            "title": "Sonos Playlists",
            "children": [{
                "title": "Carellas Ristorante",
                "media_content_type": "playlist",
                "media_content_id": "Carellas Ristorante",
                "can_play": True,
            }],
        }

        class Connection:
            def __init__(self):
                self.sent = []
                self.responses = iter([
                    json.dumps({"type": "auth_required"}),
                    json.dumps({"type": "auth_ok"}),
                ])
                self.closed = False

            def send(self, value):
                payload = json.loads(value)
                self.sent.append(payload)
                if payload.get("type") == "media_player/browse_media":
                    self.responses = iter([json.dumps({
                        "id": payload["id"], "type": "result", "success": True,
                        "result": browser,
                    })])

            def recv(self):
                return next(self.responses)

            def close(self):
                self.closed = True

        connection = Connection()
        fake_websocket = mock.Mock()
        fake_websocket.create_connection.return_value = connection
        with mock.patch.object(self.app, "websocket", fake_websocket):
            result = self.app.ha.browse_media("media_player.sala", "library", "root")

        self.assertEqual(result, browser)
        fake_websocket.create_connection.assert_called_once_with(self.app.HA_WS, timeout=25)
        self.assertEqual(connection.sent[0]["type"], "auth")
        self.assertEqual(connection.sent[1]["type"], "media_player/browse_media")
        self.assertEqual(connection.sent[1]["entity_id"], "media_player.sala")
        self.assertEqual(connection.sent[1]["media_content_type"], "library")
        self.assertEqual(connection.sent[1]["media_content_id"], "root")
        self.assertTrue(connection.closed)

    def test_music_slots_select_different_sources_at_adjacent_times(self):
        music = {
            "players": ["media_player.sala"],
            "slots": [{
                "id": "playlist-pranzo", "name": "Playlist pranzo",
                "days": [0], "start": "10:00", "end": "12:00",
                "content_id": "S:/Pranzo", "content_type": "playlist", "volume": 25,
            }, {
                "id": "radio-pomeriggio", "name": "Radio pomeriggio",
                "days": [0], "start": "12:00", "end": "14:00",
                "content_id": "FV:2/31", "content_type": "favorite_item_id", "volume": 30,
            }],
        }
        first = self.app.active_music_slot(music, datetime(2026, 9, 21, 11, 0))
        second = self.app.active_music_slot(music, datetime(2026, 9, 21, 13, 0))
        self.assertEqual(first["content_id"], "S:/Pranzo")
        self.assertEqual(second["content_id"], "FV:2/31")

    def test_music_source_duration_and_global_opening_hours(self):
        music = {
            "schedule": [{"days": [0], "start": "10:00", "end": "14:00"}],
            "players": ["media_player.sala"],
            "slots": [{
                "id": "playlist-pranzo", "name": "Playlist pranzo",
                "days": [0], "start": "10:00", "duration_minutes": 120,
                "content_id": "S:/Pranzo", "content_type": "playlist", "volume": 25,
            }, {
                "id": "radio-pranzo", "name": "Radio pranzo",
                "days": [0], "start": "12:00", "duration_minutes": 120,
                "content_id": "FV:2/31", "content_type": "favorite_item_id", "volume": 30,
            }],
        }
        self.assertEqual(
            self.app.active_music_slot(music, datetime(2026, 9, 21, 11, 0))["id"],
            "playlist-pranzo",
        )
        self.assertEqual(
            self.app.active_music_slot(music, datetime(2026, 9, 21, 13, 0))["id"],
            "radio-pranzo",
        )
        self.assertIsNone(self.app.active_music_slot(music, datetime(2026, 9, 21, 15, 0)))

    def test_music_slots_reject_overlap_only_on_shared_sonos(self):
        base = {
            "players": [],
            "slots": [{
                "name": "Playlist", "days": [0], "start": "10:00", "end": "12:00",
                "players": ["media_player.sala"], "content_id": "S:/Pranzo",
            }, {
                "name": "Radio", "days": [0], "start": "11:30", "end": "13:00",
                "players": ["media_player.sala"], "content_id": "FV:2/31",
            }],
        }
        with self.assertRaisesRegex(RuntimeError, "si sovrappongono"):
            self.app.validate_music_slots(base)
        base["slots"][1]["players"] = ["media_player.terrazza"]
        self.app.validate_music_slots(base)

    def test_music_slot_is_automatically_limited_by_general_opening_hours(self):
        music = {
            "schedule": [{"days": [0], "start": "10:00", "end": "12:00"}],
            "players": ["media_player.sala"],
            "slots": [{
                "name": "Playlist troppo lunga", "days": [0], "start": "11:00",
                "duration_minutes": 120, "content_id": "S:/Pranzo",
            }],
        }
        self.app.validate_music_slots(music)
        self.assertIsNotNone(self.app.active_music_slot(music, datetime(2026, 9, 21, 11, 30)))
        self.assertIsNone(self.app.active_music_slot(music, datetime(2026, 9, 21, 12, 30)))

    def test_music_slot_ignores_days_missing_from_general_schedule(self):
        music = {
            "schedule": [{"days": [0, 1, 2, 3, 4, 5], "start": "10:00", "end": "23:00"}],
            "players": ["media_player.sala"],
            "slots": [{
                "name": "Radio", "days": [0, 1, 2, 3, 4, 5, 6], "start": "12:00",
                "duration_minutes": 10, "content_id": "FV:2/31",
            }],
        }
        self.app.validate_music_slots(music)
        self.assertIsNotNone(self.app.active_music_slot(music, datetime(2026, 9, 21, 12, 5)))
        self.assertIsNone(self.app.active_music_slot(music, datetime(2026, 9, 27, 12, 5)))

    def test_music_slot_accepts_adjacent_general_schedule_windows(self):
        music = {
            "schedule": [
                {"days": [0], "start": "10:00", "end": "12:00"},
                {"days": [0], "start": "12:00", "end": "14:00"},
            ],
            "players": ["media_player.sala"],
            "slots": [{
                "name": "Playlist", "days": [0], "start": "11:00",
                "duration_minutes": 120, "content_id": "S:/Pranzo",
            }],
        }
        self.app.validate_music_slots(music)

    def test_music_slot_obeys_gap_between_general_schedule_windows(self):
        music = {
            "schedule": [
                {"days": [0], "start": "10:00", "end": "12:00"},
                {"days": [0], "start": "12:30", "end": "14:00"},
            ],
            "players": ["media_player.sala"],
            "slots": [{
                "name": "Playlist", "days": [0], "start": "11:00",
                "duration_minutes": 120, "content_id": "S:/Pranzo",
            }],
        }
        self.app.validate_music_slots(music)
        self.assertIsNotNone(self.app.active_music_slot(music, datetime(2026, 9, 21, 11, 30)))
        self.assertIsNone(self.app.active_music_slot(music, datetime(2026, 9, 21, 12, 15)))
        self.assertIsNotNone(self.app.active_music_slot(music, datetime(2026, 9, 21, 12, 45)))

    def test_music_slot_accepts_overnight_general_schedule(self):
        music = {
            "schedule": [{"days": [6], "start": "22:00", "end": "02:00"}],
            "players": ["media_player.sala"],
            "slots": [{
                "name": "Notte", "days": [6], "start": "23:00",
                "duration_minutes": 120, "content_id": "S:/Notte",
            }],
        }
        self.app.validate_music_slots(music)

    def test_sonos_fade_reaches_target_gradually(self):
        self.calls.clear()
        levels = {"media_player.sala": 0.4}
        with mock.patch.object(self.app.time, "sleep") as sleep:
            self.app.fade_sonos(["media_player.sala"], 0.1, 3, levels)
        volume_calls = [call for call in self.calls if call[1] == "volume_set"]
        self.assertEqual(len(volume_calls), self.app.SONOS_FADE_STEPS)
        self.assertGreater(volume_calls[0][2]["volume_level"], 0.1)
        self.assertEqual(volume_calls[-1][2]["volume_level"], 0.1)
        self.assertEqual(len(sleep.call_args_list), self.app.SONOS_FADE_STEPS - 1)

    def test_only_available_sonos_entities_are_used(self):
        states = [{
            "entity_id": "media_player.sala",
            "state": "playing",
            "attributes": {"group_members": ["media_player.sala", "media_player.bar"]},
        }, {
            "entity_id": "media_player.bar",
            "state": "idle",
            "attributes": {"group_members": ["media_player.sala", "media_player.bar"]},
        }, {
            "entity_id": "media_player.vecchio",
            "state": "unavailable",
            "attributes": {},
        }]
        with mock.patch.object(self.app.ha, "states", return_value=states):
            players = self.app.available_sonos_players([
                "media_player.sala", "media_player.bar", "media_player.vecchio",
            ])
        self.assertEqual(players, ["media_player.sala", "media_player.bar"])

    def test_sonos_group_repair_joins_only_missing_members(self):
        states = [{
            "entity_id": "media_player.sala",
            "state": "playing",
            "attributes": {"group_members": ["media_player.sala"]},
        }, {
            "entity_id": "media_player.bar",
            "state": "idle",
            "attributes": {"group_members": ["media_player.bar"]},
        }]
        self.calls.clear()
        with mock.patch.object(self.app.ha, "states", return_value=states), mock.patch.object(
            self.app, "SONOS_GROUP_SETTLE_SECONDS", 0
        ):
            result = self.app.repair_sonos_group(
                ["media_player.sala", "media_player.bar"],
                "media_player.sala",
                0.25,
            )
        self.assertEqual(result["missing"], ["media_player.bar"])
        self.assertIn(("media_player", "join", {
            "entity_id": "media_player.sala",
            "group_members": ["media_player.bar"],
        }), self.calls)
        self.assertIn(("media_player", "media_play", {
            "entity_id": "media_player.sala",
        }), self.calls)
        volume = next(call for call in self.calls if call[1] == "volume_set")
        self.assertEqual(volume[2]["entity_id"], ["media_player.bar"])
        self.assertEqual(volume[2]["volume_level"], 0.25)

    def test_music_scheduler_repairs_group_without_restarting_source(self):
        original = json.loads(json.dumps(self.app.store.config["music"]))
        slot = {
            "id": "one", "name": "Playlist", "players": ["media_player.sala", "media_player.bar"],
            "content_id": "S:/Pranzo", "content_type": "playlist", "volume": 20,
        }
        states = [{
            "entity_id": "media_player.sala", "state": "playing",
            "attributes": {"group_members": ["media_player.sala"]},
        }, {
            "entity_id": "media_player.bar", "state": "idle",
            "attributes": {"group_members": ["media_player.bar"]},
        }]
        engine = self.app.Scheduler()
        self.app.store.config["music"] = {
            "enabled": True, "players": slot["players"], "slots": [slot],
            "schedule": [], "stop_at_end": True, "volume": 25,
        }
        self.calls.clear()
        try:
            with mock.patch.object(self.app, "active_music_slot", return_value=slot), mock.patch.object(
                self.app, "start_music", return_value=slot["players"]
            ) as start, mock.patch.object(self.app.ha, "states", return_value=states), mock.patch.object(
                self.app, "SONOS_GROUP_SETTLE_SECONDS", 0
            ):
                engine.music_tick()
                engine.music_tick()
        finally:
            self.app.store.config["music"] = original
        self.assertEqual(start.call_count, 1)
        joins = [call for call in self.calls if call[1] == "join"]
        self.assertEqual(len(joins), 1)
        self.assertEqual(joins[0][2]["group_members"], ["media_player.bar"])

    def test_sonos_701_stops_briefly_and_retries_once(self):
        calls = []
        attempts = 0

        def service(domain, name, payload):
            nonlocal attempts
            calls.append((domain, name, payload))
            if name == "play_media":
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("UPnP Error 701 received: Transition not available")

        payload = {
            "entity_id": "media_player.sala",
            "media_content_type": "favorite_item_id",
            "media_content_id": "FV:2/31",
        }
        with mock.patch.object(self.app.ha, "service", side_effect=service), mock.patch.object(
            self.app.time, "sleep"
        ):
            self.app.play_sonos_media(payload)
        self.assertEqual(attempts, 2)
        self.assertIn(("media_player", "media_stop", {"entity_id": "media_player.sala"}), calls)

    def test_scheduler_switches_source_when_music_slot_changes(self):
        original = json.loads(json.dumps(self.app.store.config["music"]))
        first = {
            "id": "one", "name": "Playlist", "players": ["media_player.sala"],
            "content_id": "S:/Pranzo", "content_type": "playlist", "volume": 20,
        }
        second = {
            "id": "two", "name": "Radio", "players": ["media_player.terrazza"],
            "content_id": "FV:2/31", "content_type": "favorite_item_id", "volume": 30,
        }
        engine = self.app.Scheduler()
        self.app.store.config["music"] = {
            "enabled": True, "players": ["media_player.sala"], "slots": [first, second],
            "schedule": [], "stop_at_end": True, "volume": 25,
        }
        self.calls.clear()
        try:
            with mock.patch.object(self.app, "active_music_slot", side_effect=[first, second]), mock.patch.object(
                self.app.time, "sleep"
            ):
                engine.music_tick()
                engine.music_tick()
        finally:
            self.app.store.config["music"] = original
        played = [call[2]["media_content_id"] for call in self.calls if call[1] == "play_media"]
        self.assertEqual(played, ["S:/Pranzo", "FV:2/31"])
        unjoin = [call for call in self.calls if call[1] == "unjoin"]
        self.assertEqual(unjoin[-1][2]["entity_id"], ["media_player.sala", "media_player.terrazza"])

    def test_scheduler_restores_previous_source_when_switch_fails(self):
        original = json.loads(json.dumps(self.app.store.config["music"]))
        first = {
            "id": "one", "name": "Playlist", "players": ["media_player.sala"],
            "content_id": "S:/Pranzo", "content_type": "playlist", "volume": 20,
        }
        second = {
            "id": "two", "name": "Radio", "players": ["media_player.sala"],
            "content_id": "FV:2/31", "content_type": "favorite_item_id", "volume": 30,
        }
        engine = self.app.Scheduler()
        self.app.store.config["music"] = {
            "enabled": True, "players": ["media_player.sala"], "slots": [first, second],
            "schedule": [], "stop_at_end": True, "volume": 25, "transition_seconds": 0,
        }
        try:
            with mock.patch.object(self.app, "active_music_slot", side_effect=[first, second]), mock.patch.object(
                self.app, "fade_sonos"
            ), mock.patch.object(
                self.app, "start_music", side_effect=[["media_player.sala"], RuntimeError("Sonos occupato"), ["media_player.sala"]]
            ) as start:
                engine.music_tick()
                engine.music_tick()
        finally:
            self.app.store.config["music"] = original
        self.assertEqual(start.call_count, 3)
        self.assertEqual(start.call_args_list[-1].args[0]["id"], "one")
        self.assertEqual(engine.music_slot_id, "one")

    def test_scheduler_waits_before_retrying_failed_music_slot(self):
        original = json.loads(json.dumps(self.app.store.config["music"]))
        slot = {
            "id": "radio", "name": "Radio", "players": ["media_player.sala"],
            "content_id": "FV:2/31", "content_type": "favorite_item_id", "volume": 30,
        }
        engine = self.app.Scheduler()
        self.app.store.config["music"] = {
            "enabled": True, "players": ["media_player.sala"], "slots": [slot],
            "schedule": [], "stop_at_end": True, "volume": 25,
        }
        try:
            with mock.patch.object(self.app, "active_music_slot", return_value=slot), mock.patch.object(
                self.app, "start_music", side_effect=RuntimeError("UPnP 701")
            ) as start:
                engine.music_tick()
                engine.music_tick()
        finally:
            self.app.store.config["music"] = original
        self.assertEqual(start.call_count, 1)
        self.assertIsNotNone(engine.music_failed_slot_key)
        self.assertGreater(engine.music_retry_at, 0)
        self.assertFalse(engine.music_active)

    def test_music_slot_editor_is_available_on_phone(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn("Orari di accensione musica", html)
        self.assertIn("Aggiungi playlist o radio", html)
        self.assertIn("Durata (minuti)", html)
        self.assertIn("Scegli playlist o radio Sonos", html)
        self.assertIn('class="musicSlotSource"', html)
        self.assertIn('id="musicNewSlotSource"', html)
        self.assertIn("function renderMusicSlots()", html)
        self.assertIn("function replaceMusicSlotSource", html)
        self.assertIn("function duplicateMusicSlot", html)
        self.assertIn(".music-slot-grid{grid-template-columns:1fr}", html)
        self.assertIn('id="audioTransition"', html)
        self.assertIn('id="musicTransition"', html)
        self.assertIn("musicSlotWithinGeneralSchedule", html)
        self.assertIn("uncoveredMusicSlotParts", html)
        self.assertIn("defaultMusicDays", html)
        self.assertIn("verrà ignorata automaticamente", html)
        self.assertIn("Limitata automaticamente", html)

    def test_upload_refreshes_library_without_discarding_unsaved_settings(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn("async function refreshMediaLibrary()", html)
        self.assertIn("preserveMediaChoices()", html)
        self.assertIn("api/state?media_refresh=", html)
        self.assertIn("{cache:'no-store'}", html)
        self.assertIn("await refreshMediaLibrary()", html)
        self.assertIn("['uploadFile','uploadKind'].includes(e.target.id)", html)

    def test_config_is_merged_and_saved_atomically(self):
        original_tv = self.app.store.config["tv"]["mode"]
        self.app.store.update({"audio": {"interval_minutes": 47}})
        saved = json.loads(self.app.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved["audio"]["interval_minutes"], 47)
        self.assertEqual(saved["tv"]["mode"], original_tv)

    def test_saving_changed_iptv_order_rebuilds_channel_automatically(self):
        first = {"name": "uno.mp4", "kind": "video", "fit": "contain"}
        second = {"name": "due.mp4", "kind": "video", "fit": "contain"}
        self.app.store.update({"iptv": {"channels": [{
            "id": "ordine-auto", "name": "Ordine automatico", "enabled": True,
            "playlist": [first, second], "schedule": [],
        }]}})
        with mock.patch.object(self.app.iptv, "build_async", return_value=True) as rebuild:
            status, payload = self.request("/api/config", "POST", {"iptv": {"channels": [{
                "id": "ordine-auto", "name": "Ordine automatico", "enabled": True,
                "playlist": [second, first], "schedule": [],
            }]}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["rebuilt_channels"], ["ordine-auto"])
        rebuild.assert_called_once_with("ordine-auto")

    def test_saving_unchanged_iptv_order_does_not_rebuild_channel(self):
        playlist = [{"name": "stesso.mp4", "kind": "video", "fit": "contain"}]
        self.app.store.update({"iptv": {"channels": [{
            "id": "ordine-stesso", "name": "Ordine invariato", "enabled": True,
            "playlist": playlist, "schedule": [],
        }]}})
        with mock.patch.object(self.app.iptv, "build_async", return_value=True) as rebuild:
            status, payload = self.request("/api/config", "POST", {"iptv": {"channels": [{
                "id": "ordine-stesso", "name": "Ordine invariato", "enabled": True,
                "playlist": playlist, "schedule": [],
            }]}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["rebuilt_channels"], [])
        rebuild.assert_not_called()

    def test_stable_config_removes_legacy_audio_options_on_every_save(self):
        saved = self.app.store.update({
            "language": "unsupported",
            "audio": {
                "driver": "alexa",
                "resume_music_after_ad": False,
                "spot_duration_seconds": 999,
                "repeat_count": 7,
                "repeat_gap_seconds": 1800,
            },
        })
        self.assertEqual(saved["language"], "it")
        self.assertEqual(saved["audio"]["repeat_count"], 1)
        self.assertEqual(saved["audio"]["repeat_gap_seconds"], 0)
        self.assertNotIn("driver", saved["audio"])
        self.assertNotIn("resume_music_after_ad", saved["audio"])
        self.assertNotIn("spot_duration_seconds", saved["audio"])

    def test_daily_audio_counter_survives_scheduler_restart(self):
        self.app.RUNTIME_FILE.write_text(json.dumps({
            "day": datetime.now().date().isoformat(),
            "audio_today": 7,
        }), encoding="utf-8")
        restarted = self.app.Scheduler()
        self.assertEqual(restarted.audio_today(), 7)
        restarted.record_audio_play()
        persisted = json.loads(self.app.RUNTIME_FILE.read_text(encoding="utf-8"))
        self.assertEqual(persisted["audio_today"], 8)

    def test_daily_counter_disk_error_does_not_interrupt_playback(self):
        restarted = self.app.Scheduler()
        before = restarted.audio_today()
        with mock.patch.object(Path, "write_text", side_effect=OSError("disk busy")):
            count = restarted.record_audio_play()
        self.assertEqual(count, before + 1)

    def test_config_upload_range_and_delete(self):
        self.request("/api/config", "POST", {"audio": {"players": ["media_player.sala"], "repeat_count": 1}})
        self.request("/api/upload?filename=test.mp3&kind=audio", "POST", b"ID3test-audio", "audio/mpeg")
        with urllib.request.urlopen(urllib.request.Request(self.base + "/media/test.mp3", headers={"Range": "bytes=0-2"})) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"ID3")
            self.assertEqual(response.headers["Content-Range"], "bytes 0-2/13")
        with urllib.request.urlopen(urllib.request.Request(self.base + "/media/test.mp3", headers={"Range": "bytes=-5"})) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"audio")
            self.assertEqual(response.headers["Content-Range"], "bytes 8-12/13")
        with urllib.request.urlopen(urllib.request.Request(self.base + "/api/hassio_ingress/example/media/test.mp3", headers={"Range": "bytes=3-6"})) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"test")
            self.assertEqual(response.headers["Content-Range"], "bytes 3-6/13")
        status, payload = self.request("/api/media/test.mp3", "DELETE")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_library_audio_uses_ingress_relative_player(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn("function libraryMediaUrl(name)", html)
        self.assertIn('<audio class="library-audio" controls', html)
        self.assertNotIn("state.runtime.media_base_url+'/media/'", html)

    def test_remote_chunk_upload_through_ingress(self):
        content = b"remote-photo-content"
        first = content[:10]
        second = content[10:]
        common = "upload_id=abcdef12-3456&filename=remote.jpg&kind=image&total=2&size=20"
        real_replace = os.replace

        def reject_cross_device_replace(source, destination):
            if Path(source).parent != Path(destination).parent:
                raise OSError(18, "Cross-device link")
            return real_replace(source, destination)

        with mock.patch.object(self.app, "UPLOAD_CHUNK_SIZE", 10), mock.patch.object(
            self.app.os, "replace", side_effect=reject_cross_device_replace
        ):
            status, payload = self.request(
                "/api/hassio_ingress/example/api/upload/chunk?" + common + "&index=0",
                "POST",
                first,
                "application/octet-stream",
            )
            self.assertEqual(status, 200)
            self.assertFalse(payload["complete"])
            status, payload = self.request(
                "/api/hassio_ingress/example/api/upload/chunk?" + common + "&index=1",
                "POST",
                second,
                "application/octet-stream",
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["complete"])
        self.assertEqual((self.app.MEDIA_DIR / payload["name"]).read_bytes(), content)
        self.request("/api/media/" + payload["name"], "DELETE")

    def test_delete_media_cleans_iptv_references_and_invalidates_built_channel(self):
        deleted = self.app.MEDIA_DIR / "deleted.jpg"
        kept = self.app.MEDIA_DIR / "kept.jpg"
        deleted.write_bytes(b"photo")
        kept.write_bytes(b"photo")
        self.app.store.update({"iptv": {"channels": [{
            "id": "cleanup",
            "name": "Cleanup",
            "enabled": False,
            "playlist": [
                {"name": "deleted.jpg", "kind": "image", "duration": 10},
                {"kind": "collage", "images": ["deleted.jpg", "kept.jpg"], "duration": 10},
                {"name": "kept.jpg", "kind": "image", "duration": 10},
            ],
            "schedule": [],
        }]}})
        output = self.app.iptv.output("cleanup")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"built")
        status, payload = self.request("/api/media/deleted.jpg", "DELETE")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        playlist = self.app.store.config["iptv"]["channels"][0]["playlist"]
        self.assertEqual(playlist, [{"name": "kept.jpg", "kind": "image", "duration": 10}])
        self.assertFalse(output.parent.exists())
        self.assertEqual(self.app.iptv.runtime_status()["cleanup"]["state"], "not_built")

    def test_iptv_status_endpoint_includes_unbuilt_channels(self):
        self.app.store.update({"iptv": {"channels": [{
            "id": "not-built-yet", "name": "Not built", "enabled": False,
            "playlist": [], "schedule": [],
        }]}})
        status, payload = self.request("/api/iptv/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload["channels"]["not-built-yet"]["state"], "not_built")

    def test_iptv_detects_when_built_channel_no_longer_matches_saved_sequence(self):
        channel = {
            "id": "revision-check", "name": "Controllo revisione", "enabled": True,
            "playlist": [{"name": "prima.jpg", "kind": "image", "duration": 10}],
            "schedule": [],
        }
        self.app.store.update({"iptv": {"channels": [channel]}})
        output = self.app.iptv.output("revision-check")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"old channel")
        hls = self.app.iptv.hls_manifest("revision-check")
        hls.parent.mkdir(parents=True, exist_ok=True)
        hls.write_text("#EXTM3U\n", encoding="utf-8")
        self.assertEqual(self.app.iptv.runtime_status()["revision-check"]["state"], "outdated")

        metadata = self.app.iptv.build_metadata("revision-check")
        metadata.write_text(json.dumps({
            "signature": self.app.iptv.channel_signature(channel),
        }), encoding="utf-8")
        self.assertEqual(self.app.iptv.runtime_status()["revision-check"]["state"], "ready")

        changed = dict(channel)
        changed["playlist"] = [{"name": "seconda.jpg", "kind": "image", "duration": 15}]
        self.app.store.update({"iptv": {"channels": [changed]}})
        self.assertEqual(self.app.iptv.runtime_status()["revision-check"]["state"], "outdated")

    def test_failed_rebuild_never_reports_stale_output_as_ready(self):
        channel = {
            "id": "failed-rebuild", "name": "Errore rigenerazione", "enabled": True,
            "playlist": [{"name": "nuova-foto.jpg", "kind": "image", "duration": 8}],
            "schedule": [],
        }
        self.app.store.update({
            "iptv": {"channels": [channel]},
            "browser_player": {"enabled": True, "users": [{
                "username": "tverrore", "password": "segreta5", "channel_id": "failed-rebuild",
            }]},
        })
        output = self.app.iptv.output("failed-rebuild")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"old output")
        hls = self.app.iptv.hls_manifest("failed-rebuild")
        hls.parent.mkdir(parents=True, exist_ok=True)
        hls.write_text("#EXTM3U\n", encoding="utf-8")
        self.app.iptv.set_status("failed-rebuild", "error", "File sorgente non trovato")
        self.assertEqual(self.app.iptv.runtime_status()["failed-rebuild"]["state"], "error")

        login = urllib.request.Request(
            self.player_base + "/api/login",
            data=json.dumps({"username": "tverrore", "password": "segreta5"}).encode(),
            method="POST", headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(login) as response:
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        state_request = urllib.request.Request(self.player_base + "/api/state", headers={"Cookie": cookie})
        with urllib.request.urlopen(state_request) as response:
            state = json.loads(response.read())
        self.assertFalse(state["ready"])
        self.assertEqual(state["status"], "error")
        self.assertIn("File sorgente", state["message"])

    def test_player_requests_rebuild_for_outdated_channel_and_keeps_old_video_until_ready(self):
        channel = {
            "id": "player-revision", "name": "Player revisione", "enabled": True,
            "playlist": [{"name": "nuovo.mp4", "kind": "video"}], "schedule": [],
        }
        self.app.store.update({
            "iptv": {"channels": [channel]},
            "browser_player": {"enabled": True, "users": [{
                "username": "revisiontv", "password": "segreta4", "channel_id": "player-revision",
            }]},
        })
        output = self.app.iptv.output("player-revision")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"previous video")
        hls = self.app.iptv.hls_manifest("player-revision")
        hls.parent.mkdir(parents=True, exist_ok=True)
        hls.write_text("#EXTM3U\n", encoding="utf-8")
        login = urllib.request.Request(
            self.player_base + "/api/login",
            data=json.dumps({"username": "revisiontv", "password": "segreta4"}).encode(),
            method="POST", headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(login) as response:
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        state_request = urllib.request.Request(self.player_base + "/api/state", headers={"Cookie": cookie})
        with mock.patch.object(self.app.iptv, "build_async", return_value=True) as rebuild:
            with urllib.request.urlopen(state_request) as response:
                state = json.loads(response.read())
        rebuild.assert_called_once_with("player-revision")
        self.assertTrue(state["ready"])
        self.assertIn("/channel.mp4?v=", state["video_url"])

    def test_single_sonos_uses_synchronized_playback_and_restores_state(self):
        ad = "Carellas_Ristorante_Spot_DE_Maschile.mp3"
        self.app.store.update({"audio": {"players": ["media_player.sala"], "ads": [ad], "repeat_count": 1, "volume": 35, "transition_seconds": 0}})
        self.calls.clear()
        with mock.patch.object(self.app, "probe_media_duration", return_value=57), mock.patch.object(
            self.app.time, "sleep"
        ), mock.patch.object(self.app.audio_stop_event, "wait", return_value=False):
            self.app.play_audio(ad, manual=True)
        self.assertEqual(self.calls[0], ("sonos", "snapshot", {
            "entity_id": ["media_player.sala"], "with_group": True,
        }))
        play_call = next(call for call in self.calls if call[1] == "play_media")
        self.assertEqual(play_call[2]["entity_id"], "media_player.sala")
        self.assertNotIn("announce", play_call[2])
        self.assertIn(ad, play_call[2]["media_content_id"])
        self.assertIn(("sonos", "restore", {
            "entity_id": ["media_player.sala"], "with_group": True,
        }), self.calls)
        self.assertEqual(self.calls[-1], ("media_player", "media_play", {
            "entity_id": ["media_player.sala"],
        }))

    def test_only_selected_sonos_are_grouped_and_spot_uses_coordinator(self):
        ad = "Carellas_Ristorante_Spot_DE_Maschile.mp3"
        players = ["media_player.sala", "media_player.terrazza", "media_player.bar"]
        states = [{
            "entity_id": player,
            "state": "playing",
            "attributes": {"friendly_name": player, "group_members": [player]},
        } for player in players] + [{
            "entity_id": "media_player.non_selezionato",
            "state": "playing",
            "attributes": {"friendly_name": "Non selezionato", "group_members": ["media_player.non_selezionato"]},
        }]
        self.app.store.update({"audio": {"players": players, "ads": [ad], "repeat_count": 1, "transition_seconds": 0}})
        self.calls.clear()
        with mock.patch.object(self.app.ha, "states", return_value=states), mock.patch.object(
            self.app, "probe_media_duration", return_value=57
        ), mock.patch.object(self.app, "SONOS_GROUP_SETTLE_SECONDS", 0), mock.patch.object(
            self.app, "SONOS_RESTORE_SETTLE_SECONDS", 0
        ), mock.patch.object(self.app.time, "sleep"), mock.patch.object(
            self.app.audio_stop_event, "wait", return_value=False
        ):
            self.app.play_audio(ad, manual=True)
        snapshot = self.calls[0]
        self.assertEqual(snapshot, ("sonos", "snapshot", {
            "entity_id": players, "with_group": True,
        }))
        unjoin = next(call for call in self.calls if call[1] == "unjoin")
        self.assertEqual(unjoin[2]["entity_id"], players)
        join = next(call for call in self.calls if call[1] == "join")
        self.assertEqual(join[2]["entity_id"], players[0])
        self.assertEqual(join[2]["group_members"], players[1:])
        play_calls = [call for call in self.calls if call[1] == "play_media"]
        self.assertEqual(len(play_calls), 1)
        self.assertEqual(play_calls[0][2]["entity_id"], players[0])
        self.assertNotIn("announce", play_calls[0][2])
        self.assertIn(("sonos", "restore", {
            "entity_id": players, "with_group": True,
        }), self.calls)
        self.assertEqual(self.calls[-1], ("media_player", "media_play", {
            "entity_id": players,
        }))
        self.assertNotIn("media_player.non_selezionato", json.dumps(self.calls))

    def test_stop_audio_endpoint_interrupts_spot_and_restores_snapshot(self):
        ad = "Carellas_Ristorante_Spot_DE_Maschile.mp3"
        players = ["media_player.sala", "media_player.bar"]
        self.app.store.update({"audio": {"players": players, "ads": [ad], "repeat_count": 1, "transition_seconds": 0}})
        self.calls.clear()
        waiting = threading.Event()
        real_wait = self.app.audio_stop_event.wait

        def interruptible_wait(timeout):
            waiting.set()
            return real_wait(timeout)

        with mock.patch.object(self.app, "probe_media_duration", return_value=600), mock.patch.object(
            self.app, "SONOS_GROUP_SETTLE_SECONDS", 0
        ), mock.patch.object(self.app, "SONOS_RESTORE_SETTLE_SECONDS", 0), mock.patch.object(
            self.app.audio_stop_event, "wait", side_effect=interruptible_wait
        ):
            playback = threading.Thread(target=self.app.play_audio, args=(ad, True))
            playback.start()
            self.assertTrue(waiting.wait(1), "Lo spot non è entrato nella fase di riproduzione")
            status, payload = self.request("/api/audio/stop", "POST", {})
            playback.join(1)

        self.assertEqual(status, 200)
        self.assertTrue(payload["active"])
        self.assertFalse(playback.is_alive())
        self.assertIn(("sonos", "restore", {
            "entity_id": players, "with_group": True,
        }), self.calls)
        self.assertEqual(self.calls[-1], ("media_player", "media_play", {
            "entity_id": ["media_player.sala"],
        }))
        self.assertFalse(self.app.audio_playback_lock.locked())

    def test_sonos_state_is_restored_when_spot_playback_fails(self):
        ad = "Carellas_Ristorante_Spot_DE_Maschile.mp3"
        players = ["media_player.sala", "media_player.bar"]
        self.app.store.update({"audio": {"players": players, "ads": [ad], "repeat_count": 1, "transition_seconds": 0}})
        self.calls.clear()
        count_before = self.app.scheduler.audio_today()

        def service(domain, name, payload):
            self.calls.append((domain, name, payload))
            if name == "play_media":
                raise RuntimeError("riproduzione non riuscita")
            return []

        with mock.patch.object(self.app.ha, "service", side_effect=service), mock.patch.object(
            self.app, "probe_media_duration", return_value=57
        ), mock.patch.object(self.app, "SONOS_GROUP_SETTLE_SECONDS", 0), mock.patch.object(
            self.app, "SONOS_RESTORE_SETTLE_SECONDS", 0
        ), self.assertRaisesRegex(RuntimeError, "riproduzione non riuscita"):
            self.app.play_audio(ad, manual=True)
        self.assertIn(("sonos", "restore", {
            "entity_id": players, "with_group": True,
        }), self.calls)
        self.assertEqual(self.app.scheduler.audio_today(), count_before)
        self.assertFalse(self.app.audio_playback_lock.locked())

    def test_spot_restores_one_snapshot_per_original_sonos_group(self):
        ad = "Carellas_Ristorante_Spot_DE_Maschile.mp3"
        players = ["media_player.sala", "media_player.bar", "media_player.terrazza"]
        original_group = ["media_player.sala", "media_player.bar", "media_player.terrazza"]
        states = [{
            "entity_id": player,
            "state": "playing",
            "attributes": {"group_members": original_group},
        } for player in players]
        self.app.store.update({"audio": {"players": players, "ads": [ad], "repeat_count": 1, "transition_seconds": 0}})
        self.calls.clear()
        with mock.patch.object(self.app.ha, "states", return_value=states), mock.patch.object(
            self.app, "probe_media_duration", return_value=57
        ), mock.patch.object(self.app, "SONOS_GROUP_SETTLE_SECONDS", 0), mock.patch.object(
            self.app, "SONOS_RESTORE_SETTLE_SECONDS", 0
        ), mock.patch.object(self.app.audio_stop_event, "wait", return_value=False):
            self.app.play_audio(ad, manual=True)
        self.assertEqual(self.calls[0], ("sonos", "snapshot", {
            "entity_id": ["media_player.sala"], "with_group": True,
        }))
        self.assertIn(("sonos", "restore", {
            "entity_id": ["media_player.sala"], "with_group": True,
        }), self.calls)
        self.assertEqual(self.calls[-1], ("media_player", "media_play", {
            "entity_id": ["media_player.sala"],
        }))

    def test_sonos_restore_is_retried_after_temporary_failure(self):
        calls = []
        failures = {"remaining": 1}

        def service(domain, name, payload):
            calls.append((domain, name, payload))
            if domain == "sonos" and name == "restore" and failures["remaining"]:
                failures["remaining"] -= 1
                raise RuntimeError("Sonos occupato")
            return []

        with mock.patch.object(self.app.ha, "service", side_effect=service), mock.patch.object(
            self.app, "SONOS_RESTORE_SETTLE_SECONDS", 0
        ), mock.patch.object(self.app.time, "sleep"):
            self.app.restore_sonos_playback(["media_player.sala"], ["media_player.sala"])
        self.assertEqual(len([call for call in calls if call[1] == "restore"]), 2)
        self.assertEqual(calls[-1], ("media_player", "media_play", {
            "entity_id": ["media_player.sala"],
        }))

    def test_existing_sonos_group_is_not_regrouped(self):
        players = ["media_player.sala", "media_player.terrazza"]
        states = [{
            "entity_id": player,
            "state": "playing",
            "attributes": {"group_members": players},
        } for player in players]
        self.calls.clear()
        with mock.patch.object(self.app.ha, "states", return_value=states):
            coordinator = self.app.prepare_sonos_group(players)
        self.assertEqual(coordinator, players[0])
        self.assertFalse(any(call[1] == "join" for call in self.calls))

    def test_collage_layout_uses_selected_photos_and_16_9_output(self):
        names = ["one.jpg", "two.jpg", "three.jpg"]
        for name in names:
            (self.app.MEDIA_DIR / name).write_bytes(b"photo")
        command = self.app.iptv._collage_command({
            "kind": "collage",
            "images": names,
            "layout": "hero",
            "effect": "zoom_in",
        }, Path("part.mp4"), 12)
        graph = command[command.index("-filter_complex") + 1]
        self.assertEqual(command.count("-loop"), 3)
        self.assertIn("xstack=inputs=3", graph)
        self.assertIn("768_0", graph)
        self.assertIn("zoompan", graph)
        self.assertIn("s=1280x720", graph)

    def test_lan_screen_api(self):
        self.app.store.update({"tv": {
            "enabled": True,
            "mode": "lan_screen",
            "playlist": [{"name": "promo.jpg", "kind": "image", "duration": 12}],
            "schedule": [],
            "loop": True,
            "fit": "cover",
            "muted": True,
        }})
        status, payload = self.request("/api/screen")
        self.assertEqual(status, 200)
        self.assertTrue(payload["active"])
        self.assertEqual(payload["playlist"][0]["duration"], 12)
        self.assertEqual(payload["fit"], "cover")

    def test_dlna_power_on_with_wol_and_power_off_entity(self):
        self.app.store.update({"tv": {
            "mode": "dlna",
            "player": "media_player.sala",
            "wol_mac": "AA-BB-CC-DD-EE-FF",
            "power_off_entity": "switch.tv_power",
        }})
        self.calls.clear()
        self.app.tv_power_on()
        self.app.tv_power_off()
        self.assertEqual(self.calls[0][0:2], ("wake_on_lan", "send_magic_packet"))
        self.assertEqual(self.calls[0][2]["mac"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(self.calls[1][0:2], ("switch", "turn_off"))

    def test_multi_tv_m3u_lists_independent_channel(self):
        self.app.store.update({"iptv": {"channels": [{
            "id": "sala-napoli",
            "name": "Sala Napoli",
            "enabled": True,
            "playlist": [{"name": "Demo_Carellas_SmartTV_DLNA.mp4", "kind": "video", "duration": 18}],
            "schedule": [],
        }]}})
        with urllib.request.urlopen(self.base + "/iptv/channels.m3u") as response:
            body = response.read().decode()
            self.assertEqual(response.headers.get_content_type(), "audio/x-mpegurl")
        self.assertIn("Sala Napoli", body)
        self.assertIn("/iptv/sala-napoli/channel.m3u8", body)

    def test_multi_tv_m3u_also_lists_channels_with_automation_disabled(self):
        self.app.store.update({"iptv": {"channels": [{
            "id": "menu-serale",
            "name": "Menu serale",
            "enabled": False,
            "playlist": [{"name": "promo.jpg", "kind": "image", "duration": 10}],
            "schedule": [],
        }]}})
        with urllib.request.urlopen(self.base + "/iptv/channels.m3u") as response:
            body = response.read().decode()
        self.assertIn("Menu serale", body)
        self.assertIn("/iptv/menu-serale/channel.m3u8", body)

    def test_hls_manifest_and_segment_are_served(self):
        hls = self.app.iptv.hls_dir("menu-serale")
        hls.mkdir(parents=True, exist_ok=True)
        (hls / "channel.m3u8").write_text(
            "#EXTM3U\n#EXTINF:10.0,\nloop.ts?v=0\n#EXT-X-ENDLIST\n",
            encoding="utf-8",
        )
        (hls / "loop.ts").write_bytes(b"transport-stream")
        with urllib.request.urlopen(self.base + "/iptv/menu-serale/channel.m3u8") as response:
            self.assertIn(b"loop.ts?v=0", response.read())
            self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "*")
        with urllib.request.urlopen(self.base + "/iptv/menu-serale/loop.ts?v=0") as response:
            self.assertEqual(response.read(), b"transport-stream")

    def test_iptv_head_probe_returns_without_starting_ffmpeg(self):
        channel_id = "sala-napoli"
        output = self.app.iptv.output(channel_id)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fake-mp4")
        with mock.patch.object(self.app.subprocess, "Popen") as popen:
            request = urllib.request.Request(self.base + f"/iptv/{channel_id}.ts", method="HEAD")
            with urllib.request.urlopen(request, timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), "video/mp2t")
                self.assertEqual(response.read(), b"")
        popen.assert_not_called()

    def test_cors_preflight_allows_direct_large_uploads(self):
        request = urllib.request.Request(
            self.base + "/api/upload",
            method="OPTIONS",
            headers={"Origin": "http://homeassistant.local:8123", "Access-Control-Request-Method": "POST"},
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "*")
            self.assertIn("POST", response.headers.get("Access-Control-Allow-Methods"))

    def test_overnight_schedule(self):
        schedule = [{"days": [6], "start": "22:00", "end": "02:00", "enabled": True}]
        self.assertTrue(self.app.is_schedule_active(schedule, datetime(2026, 9, 13, 23, 0)))
        self.assertTrue(self.app.is_schedule_active(schedule, datetime(2026, 9, 14, 1, 0)))
        self.assertFalse(self.app.is_schedule_active(schedule, datetime(2026, 9, 14, 3, 0)))


if __name__ == "__main__":
    unittest.main()
