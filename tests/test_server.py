import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.request
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

    def test_config_upload_range_and_delete(self):
        self.request("/api/config", "POST", {"audio": {"players": ["media_player.sala"], "repeat_count": 1}})
        self.request("/api/upload?filename=test.mp3&kind=audio", "POST", b"ID3test-audio", "audio/mpeg")
        with urllib.request.urlopen(urllib.request.Request(self.base + "/media/test.mp3", headers={"Range": "bytes=0-2"})) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"ID3")
        status, payload = self.request("/api/media/test.mp3", "DELETE")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_sonos_announce(self):
        ad = "Carellas_Ristorante_Spot_DE_Maschile.mp3"
        self.app.store.update({"audio": {"driver": "sonos", "players": ["media_player.sala"], "ads": [ad], "repeat_count": 1, "volume": 35}})
        self.app.play_audio(ad, manual=True)
        _, service, payload = self.calls[-1]
        self.assertEqual(service, "play_media")
        self.assertTrue(payload["announce"])
        self.assertIn(ad, payload["media_content_id"])

    def test_alexa_beta_uses_direct_media_without_sonos_announce(self):
        ad = "Carellas_Ristorante_Spot_DE_Femminile.mp3"
        self.app.store.update({"audio": {
            "driver": "alexa",
            "players": ["media_player.sala"],
            "ads": [ad],
            "repeat_count": 1,
            "volume": 40,
            "resume_music_after_ad": False,
        }})
        self.calls.clear()
        self.app.play_audio(ad, manual=True)
        services = [call[1] for call in self.calls]
        self.assertEqual(services, ["volume_set", "play_media"])
        payload = self.calls[-1][2]
        self.assertNotIn("announce", payload)
        self.assertIn(ad, payload["media_content_id"])

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

    def test_overnight_schedule(self):
        schedule = [{"days": [6], "start": "22:00", "end": "02:00", "enabled": True}]
        self.assertTrue(self.app.is_schedule_active(schedule, datetime(2026, 9, 13, 23, 0)))
        self.assertTrue(self.app.is_schedule_active(schedule, datetime(2026, 9, 14, 1, 0)))
        self.assertFalse(self.app.is_schedule_active(schedule, datetime(2026, 9, 14, 3, 0)))


if __name__ == "__main__":
    unittest.main()
