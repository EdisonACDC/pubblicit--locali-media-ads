import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.request
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

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.temp.cleanup()

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

    def test_editor_preserves_unsaved_settings_and_shows_sequence_order(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn("if(dirty&&!force)return", html)
        self.assertIn("Ordine di riproduzione, da sinistra a destra", html)
        self.assertIn("images:[]", html)
        self.assertIn("foto selezionate su 6", html)
        self.assertNotIn('id="audioDriver"', html)

    def test_config_is_merged_and_saved_atomically(self):
        original_tv = self.app.store.config["tv"]["mode"]
        self.app.store.update({"audio": {"interval_minutes": 47}})
        saved = json.loads(self.app.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved["audio"]["interval_minutes"], 47)
        self.assertEqual(saved["tv"]["mode"], original_tv)

    def test_config_upload_range_and_delete(self):
        self.request("/api/config", "POST", {"audio": {"players": ["media_player.sala"], "repeat_count": 1}})
        self.request("/api/upload?filename=test.mp3&kind=audio", "POST", b"ID3test-audio", "audio/mpeg")
        with urllib.request.urlopen(urllib.request.Request(self.base + "/media/test.mp3", headers={"Range": "bytes=0-2"})) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"ID3")
        status, payload = self.request("/api/media/test.mp3", "DELETE")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

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

    def test_sonos_announce(self):
        ad = "Carellas_Ristorante_Spot_DE_Maschile.mp3"
        self.app.store.update({"audio": {"players": ["media_player.sala"], "ads": [ad], "repeat_count": 1, "volume": 35}})
        self.app.play_audio(ad, manual=True)
        _, service, payload = self.calls[-1]
        self.assertEqual(service, "play_media")
        self.assertTrue(payload["announce"])
        self.assertIn(ad, payload["media_content_id"])

    def test_multiple_sonos_are_grouped_then_played_once_on_coordinator(self):
        ad = "Carellas_Ristorante_Spot_DE_Maschile.mp3"
        players = ["media_player.sala", "media_player.terrazza", "media_player.bar"]
        states = [{
            "entity_id": player,
            "state": "playing",
            "attributes": {"friendly_name": player, "group_members": [player]},
        } for player in players]
        self.app.store.update({"audio": {"players": players, "ads": [ad], "repeat_count": 1}})
        self.calls.clear()
        with mock.patch.object(self.app.ha, "states", return_value=states), mock.patch.object(
            self.app, "SONOS_GROUP_SETTLE_SECONDS", 0
        ):
            self.app.play_audio(ad, manual=True)
        self.assertEqual(self.calls[0][0:2], ("media_player", "join"))
        self.assertEqual(self.calls[0][2]["entity_id"], players[0])
        self.assertEqual(self.calls[0][2]["group_members"], players[1:])
        play_calls = [call for call in self.calls if call[1] == "play_media"]
        self.assertEqual(len(play_calls), 1)
        self.assertEqual(play_calls[0][2]["entity_id"], players[0])

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
