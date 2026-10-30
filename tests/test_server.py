import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
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
        # Nei test lo stato finto dei Sonos è subito quello giusto: niente attesa iniziale.
        os.environ["CARELLAS_SONOS_START_SETTLE_SECONDS"] = "0"
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
        # Ogni test parte senza una fascia «già in onda» lasciata da un test precedente.
        self.app.MUSIC_STATE_FILE.unlink(missing_ok=True)

    def request(self, path, method="GET", body=None, content_type="application/json"):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": content_type})
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read())

    def test_channel_page_edits_channels_without_zone(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        # Ogni canale ha la sua pagina: nome, zona, sequenza e impostazioni anche se non è in una zona.
        self.assertIn("canale:pageCanale", html)
        self.assertIn('data-bind="iptv.channels.${i}.name"', html)
        self.assertIn('data-channel-zone=', html)
        self.assertIn("if(t.dataset.channelZone!==undefined)", html)
        # Impostazioni, configurazione guidata e Nuovo canale TV portano alla pagina del canale.
        self.assertEqual(html.count('href="#/canale/${encodeURIComponent(ch.id)}"'), 2)
        self.assertIn("if(el){location.hash='#/canale/'+encodeURIComponent(ch.id);render()}", html)
        self.assertIn("['Nome e zona','Name und Zone']", html)
        # Foto e video si caricano direttamente nella sequenza del canale e vengono aggiunti subito.
        self.assertIn('data-seq-upload="${esc(ch.id)}"', html)
        self.assertIn("if(t.dataset.seqUpload!==undefined)", html)
        self.assertIn("if(r&&r.complete)done.push({name:r.name,kind})", html)
        self.assertIn("  return done;", html)
        # Dal riquadro delle TV si apre il canale mostrato da ogni TV.
        self.assertIn('href="#/canale/${encodeURIComponent(u.channel_id)}"', html)

    def test_state_and_ingress_path(self):
        status, payload = self.request("/api/hassio_ingress/example/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(payload["entities"][0]["entity_id"], "media_player.sala")
        self.assertEqual(payload["runtime"]["media_base_url"], "http://192.168.1.10:8099")
        self.assertEqual(payload["runtime"]["player_local_url"], "http://192.168.1.10:8101")

    def test_operator_page_through_ingress(self):
        # Dentro Home Assistant /operatori arriva con il prefisso di Ingress: deve aprire la pagina degli operatori.
        with urllib.request.urlopen(self.base + "/api/hassio_ingress/example/operatori") as response:
            html = response.read().decode("utf-8")
        self.assertIn("api/staff/state", html)
        with urllib.request.urlopen(self.base + "/api/hassio_ingress/example/") as response:
            self.assertNotIn("api/staff/state", response.read().decode("utf-8"))
        status, payload = self.request("/api/hassio_ingress/example/api/staff/state")
        self.assertEqual(status, 200)
        # Senza accesso con password non c'è «Esci».
        self.assertIsNone(payload["remote_user"])

    def test_ingress_user_without_admin_rights_is_an_operator(self):
        # Dalla plancia di Home Assistant: nessuna password in più, ma chi non è amministratore
        # di Home Assistant vede solo la pagina degli operatori.
        app = self.app

        def call(path, method="GET", body=None, user="operaio"):
            data = None if body is None else json.dumps(body).encode()
            headers = {"Content-Type": "application/json", "X-Remote-User-Id": user}
            request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status, response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as error:
                try:
                    return error.code, error.read().decode("utf-8", "replace")
                finally:
                    error.close()

        users = [{"id": "capo", "is_owner": True, "group_ids": ["system-admin"]},
                 {"id": "vice", "is_owner": False, "group_ids": ["system-admin"]},
                 {"id": "operaio", "is_owner": False, "group_ids": ["system-users"]}]
        app.HA_ADMINS.update(at=-1e9, ids=set())
        prefix = "/api/hassio_ingress/token"
        (app.MEDIA_DIR / "Foto_riservata.jpg").write_bytes(b"jpg")
        try:
            with mock.patch.object(app.ha, "websocket_command", return_value=users) as listing:
                for path in ("/", "/operatori"):
                    status, html = call(prefix + path)
                    self.assertEqual(status, 200)
                    self.assertIn("api/staff/state", html, path)
                self.assertEqual(call(prefix + "/api/staff/state")[0], 200)
                for path in ("/api/state", "/classic", "/media/Foto_riservata.jpg"):
                    self.assertEqual(call(prefix + path)[0], 403, path)
                language = app.store.config.get("language")
                self.assertEqual(call(prefix + "/api/config", "POST", {"language": "de"})[0], 403)
                self.assertEqual(app.store.config.get("language"), language)
                self.assertEqual(call(prefix + "/api/media/x.mp3", "DELETE")[0], 403)
                # Gli amministratori (e il proprietario) vedono tutto il pannello.
                for admin in ("capo", "vice"):
                    status, html = call(prefix + "/", user=admin)
                    self.assertEqual(status, 200)
                    self.assertNotIn("api/staff/state", html)
                    self.assertEqual(call(prefix + "/api/state", user=admin)[0], 200)
                # L'elenco degli utenti non si rilegge a ogni richiesta.
                listing.assert_called_once()
            # Spot tolto dall'operatore: solo per oggi, anche se la richiesta non lo dice.
            saved_zones = json.loads(json.dumps(app.store.config.get("zones", [])))
            saved_audio = json.loads(json.dumps(app.store.config["audio"]))
            app.store.update({"zones": [{"id": "sala", "name": "Sala", "players": ["media_player.sala"]}],
                              "audio": {"players": ["media_player.sala"]}})
            try:
                self.assertEqual(call(prefix + "/api/zone/spots", "POST", {"zone_id": "sala", "on": False})[0], 200)
                self.assertEqual(app.store.config["audio"]["players"], ["media_player.sala"])
                self.assertIn("media_player.sala", app.spots_excluded_players())
            finally:
                app.SPOTS_EXCLUDED.clear()
                app.store.update({"zones": saved_zones, "audio": saved_audio})
            # Elenco non leggibile: nessuno è amministratore (si resta alla pagina degli operatori).
            app.HA_ADMINS.update(at=-1e9, ids=set())
            with mock.patch.object(app.ha, "websocket_command", side_effect=RuntimeError("no")):
                self.assertEqual(call(prefix + "/api/state", user="capo")[0], 403)
            # Senza l'ID di Home Assistant (rete locale) non cambia nulla.
            status, _ = self.request("/api/state")
            self.assertEqual(status, 200)
        finally:
            app.HA_ADMINS.update(at=-1e9, ids=set())
            (app.MEDIA_DIR / "Foto_riservata.jpg").unlink(missing_ok=True)

    def test_instagram_token_stays_on_server(self):
        self.app.INSTAGRAM_TOKEN_FILE.unlink(missing_ok=True)
        with mock.patch.object(self.app.InstagramImporter, "_get", return_value={"username": "carellas.ristorante"}) as get:
            status, payload = self.request("/api/social/instagram/token", "POST", {"token": "IGAA-SEGRETO-123"})
        self.assertEqual(payload["username"], "carellas.ristorante")
        self.assertEqual(get.call_args.args[1]["access_token"], "IGAA-SEGRETO-123")
        self.assertTrue(self.app.INSTAGRAM_TOKEN_FILE.is_file())
        # La chiave non arriva mai al pannello: solo "collegato a @...".
        with urllib.request.urlopen(self.base + "/api/state") as response:
            state = response.read().decode("utf-8")
        self.assertNotIn("IGAA-SEGRETO-123", state)
        self.assertIn('"username": "carellas.ristorante"', state)
        self.assertIn('"token_set": true', state)
        self.request("/api/social/instagram/token", "POST", {"token": ""})
        self.assertFalse(self.app.INSTAGRAM_TOKEN_FILE.exists())

    def test_instagram_videos_are_added_to_the_chosen_channel(self):
        saved_iptv = json.loads(json.dumps(self.app.store.config.get("iptv", {})))
        saved_social = json.loads(json.dumps(self.app.store.config.get("social", {})))
        importer = self.app.InstagramImporter()
        self.app.INSTAGRAM_TOKEN_FILE.write_text(json.dumps({"token": "T", "saved_at": self.app.time.time(), "username": "carellas"}))
        downloads = []

        def download(url, target):
            downloads.append(url)
            target.write_bytes(b"video")

        media = {"data": [
            {"id": "100", "media_type": "VIDEO", "media_url": "https://cdn/100.mp4", "timestamp": "2026-10-01T10:00:00+0000"},
            {"id": "200", "media_type": "IMAGE", "media_url": "https://cdn/200.jpg", "timestamp": "2026-10-02T10:00:00+0000"},
            {"id": "300", "media_type": "VIDEO", "media_url": "https://cdn/300.mp4", "timestamp": "2026-10-03T10:00:00+0000"},
        ]}
        try:
            self.app.store.update({
                "iptv": {"channels": [{"id": "ingresso", "name": "TV Ingresso", "enabled": True,
                                       "playlist": [{"name": "foto.jpg", "kind": "image", "duration": 12}]}]},
                "social": {"instagram": {"enabled": True, "channel_id": "ingresso", "keep": 1, "interval_minutes": 15}},
            })
            with mock.patch.object(self.app.InstagramImporter, "_get", return_value=media), \
                    mock.patch.object(self.app.InstagramImporter, "_download", side_effect=download), \
                    mock.patch.object(self.app.iptv, "build_async") as build:
                status = importer.check(force=True)
            self.assertEqual(status["last_error"], "")
            self.assertEqual(downloads, ["https://cdn/300.mp4"])  # solo il video più recente (keep=1)
            playlist = self.app.store.config["iptv"]["channels"][0]["playlist"]
            self.assertEqual([item["name"] for item in playlist], ["foto.jpg", "Instagram_300.mp4"])
            build.assert_called_once_with("ingresso")
            # Arriva un video più nuovo: sostituisce il vecchio, gli altri contenuti restano.
            media["data"].append({"id": "400", "media_type": "VIDEO", "media_url": "https://cdn/400.mp4",
                                  "timestamp": "2026-10-04T10:00:00+0000"})
            with mock.patch.object(self.app.InstagramImporter, "_get", return_value=media), \
                    mock.patch.object(self.app.InstagramImporter, "_download", side_effect=download), \
                    mock.patch.object(self.app.iptv, "build_async"):
                importer.check(force=True)
            playlist = self.app.store.config["iptv"]["channels"][0]["playlist"]
            self.assertEqual([item["name"] for item in playlist], ["foto.jpg", "Instagram_400.mp4"])
            # Senza canale scelto il controllo si ferma con un messaggio chiaro.
            self.app.store.update({"social": {"instagram": {"channel_id": ""}}})
            self.assertIn("Scegli il canale", importer.check(force=True)["last_error"])
        finally:
            for name in ("Instagram_300.mp4", "Instagram_400.mp4"):
                (self.app.MEDIA_DIR / name).unlink(missing_ok=True)
            self.app.INSTAGRAM_TOKEN_FILE.unlink(missing_ok=True)
            self.app.store.config["iptv"] = saved_iptv
            self.app.store.config["social"] = saved_social

    def test_instagram_photos_and_carousels(self):
        saved_iptv = json.loads(json.dumps(self.app.store.config.get("iptv", {})))
        saved_social = json.loads(json.dumps(self.app.store.config.get("social", {})))
        importer = self.app.InstagramImporter()
        self.app.INSTAGRAM_TOKEN_FILE.write_text(json.dumps({"token": "T", "saved_at": self.app.time.time(), "username": "c"}))
        media = {"data": [
            {"id": "450", "media_type": "IMAGE", "media_url": "https://cdn/450.jpg", "timestamp": "2026-10-05T10:00:00+0000"},
            {"id": "500", "media_type": "CAROUSEL_ALBUM", "timestamp": "2026-10-06T10:00:00+0000", "children": {"data": [
                {"id": "501", "media_type": "IMAGE", "media_url": "https://cdn/501.jpg"},
                {"id": "502", "media_type": "VIDEO", "media_url": "https://cdn/502.mp4"},
            ]}},
            {"id": "400", "media_type": "VIDEO", "media_url": "https://cdn/400.mp4", "timestamp": "2026-10-01T10:00:00+0000"},
        ]}
        created = []

        def download(url, target):
            created.append(target)
            target.write_bytes(b"x")

        try:
            self.app.store.update({
                "iptv": {"channels": [{"id": "ingresso", "name": "TV Ingresso", "enabled": True,
                                       "playlist": [{"name": "foto.jpg", "kind": "image", "duration": 12}]}]},
                "social": {"instagram": {"enabled": True, "channel_id": "ingresso", "keep": 2, "photo_seconds": 8}},
            })
            with mock.patch.object(self.app.InstagramImporter, "_get", return_value=media) as get, \
                    mock.patch.object(self.app.InstagramImporter, "_download", side_effect=download), \
                    mock.patch.object(self.app.iptv, "build_async"):
                importer.check(force=True)
            self.assertIn("children{id,media_type,media_url}", get.call_args.args[1]["fields"])
            playlist = self.app.store.config["iptv"]["channels"][0]["playlist"]
            # Gli ultimi 2 post: il carosello (foto e video nell'ordine del post) e la foto.
            self.assertEqual([item["name"] for item in playlist],
                             ["foto.jpg", "Instagram_501.jpg", "Instagram_502.mp4", "Instagram_450.jpg"])
            self.assertEqual(playlist[1], {"name": "Instagram_501.jpg", "kind": "image", "duration": 8,
                                           "fit": "smart", "effect": "fade"})
            self.assertEqual(playlist[2]["kind"], "video")
            # Senza foto: restano solo i video, anche quelli dentro i caroselli.
            self.app.store.update({"social": {"instagram": {"include_photos": False}}})
            with mock.patch.object(self.app.InstagramImporter, "_get", return_value=media), \
                    mock.patch.object(self.app.InstagramImporter, "_download", side_effect=download), \
                    mock.patch.object(self.app.iptv, "build_async"):
                importer.check(force=True)
            playlist = self.app.store.config["iptv"]["channels"][0]["playlist"]
            self.assertEqual([item["name"] for item in playlist], ["foto.jpg", "Instagram_502.mp4", "Instagram_400.mp4"])
        finally:
            for target in created:
                target.unlink(missing_ok=True)
            self.app.INSTAGRAM_TOKEN_FILE.unlink(missing_ok=True)
            self.app.store.config["iptv"] = saved_iptv
            self.app.store.config["social"] = saved_social

    def test_instagram_token_is_renewed_after_twenty_days(self):
        importer = self.app.InstagramImporter()
        old = {"token": "VECCHIA", "saved_at": self.app.time.time() - 21 * 86400, "username": "carellas"}
        self.app.INSTAGRAM_TOKEN_FILE.write_text(json.dumps(old))
        try:
            with mock.patch.object(self.app.InstagramImporter, "_get", return_value={"access_token": "NUOVA"}) as get:
                token = importer._refresh_if_needed(old)
            self.assertEqual(token, "NUOVA")
            self.assertEqual(get.call_args.args[1], {"grant_type": "ig_refresh_token", "access_token": "VECCHIA"})
            self.assertEqual(importer.token_info()["token"], "NUOVA")
        finally:
            self.app.INSTAGRAM_TOKEN_FILE.unlink(missing_ok=True)

    def test_instagram_settings_in_panel(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn("['instagram','Instagram']", html)
        self.assertIn("${instagramSettings()}", html)
        self.assertIn('type="password" autocomplete="off"', html)
        self.assertIn("'ig-check':async()=>", html)
        self.assertIn("data-bind=\"social.instagram.channel_id\"", html)
        self.assertIn("data-bind=\"social.instagram.photo_seconds\"", html)
        self.assertIn("data-bind=\"social.instagram.include_photos\"", html)
        # Istruzioni per il collegamento sempre visibili, non nascoste dietro un pulsante.
        self.assertIn("function instagramHowto(){", html)
        self.assertIn(":instagramHowto()}", html)
        self.assertNotIn("L('Come ottenere la chiave di accesso')", html)
        self.assertIn("['Come collegare Instagram','So verbindet ihr Instagram']", html)

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
        dashboard = (Path(__file__).parents[1] / "carellas_media_ads/app/index_classic.html").read_text(encoding="utf-8")
        self.assertIn('id="playerUsers"', dashboard)
        self.assertIn('id="playerUserSelect"', dashboard)
        self.assertIn('onchange="selectPlayerUser(this.value)"', dashboard)
        self.assertIn("function selectPlayerUser(value)", dashboard)
        self.assertIn("c.browser_player=", dashboard)
        self.assertIn('onclick="togglePasswordField(this)"', dashboard)
        self.assertIn('onclick="savePlayerUser(${i},this)"', dashboard)
        self.assertIn('onclick="return openPlayerTv(event)"', dashboard)
        self.assertIn('rel="external noopener noreferrer"', dashboard)
        self.assertIn("function openPlayerTv(event)", dashboard)
        self.assertIn("window.top", dashboard)
        self.assertIn("display:flex;align-items:center;justify-content:center", html)
        self.assertNotIn("body:hover #controls", html)
        self.assertIn("function hideControls()", html)
        self.assertIn("setTimeout(hideControls,3000)", html)
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
        # Le parti (in memoria per nome dell'impronta) seguono l'ordine salvato.
        keys = metadata["parts"]
        self.assertEqual(len(set(keys)), 3)
        self.assertLess(concat.index(keys[0]), concat.index(keys[1]))
        self.assertLess(concat.index(keys[1]), concat.index(keys[2]))

    def test_iptv_rebuild_reuses_unchanged_parts(self):
        # Dopo una modifica si riconverte solo l'elemento cambiato: le altre parti sono già pronte.
        names = ["riuso-a.jpg", "riuso-b.jpg", "riuso-c.jpg"]
        for name in names:
            (self.app.MEDIA_DIR / name).write_bytes(b"source")
        playlist = [{"name": name, "kind": "image", "duration": 4, "effect": "fade"} for name in names]
        self.app.store.update({"iptv": {"channels": [{"id": "riuso", "name": "Riuso", "enabled": True,
                                                      "playlist": playlist, "schedule": []}]}})
        encoded = []

        def fake_run(command, **kwargs):
            if command[0] == "ffprobe":
                return mock.Mock(stdout="h264\n" if "stream=codec_name" in command else "12.0\n")
            target = Path(command[-1])
            if "-loop" in command:
                encoded.append(command[command.index("-loop") + 3])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"built" * 300)
            return mock.Mock(stdout="")

        with mock.patch.object(self.app.subprocess, "run", side_effect=fake_run):
            self.app.iptv.build("riuso")
            self.assertEqual(len(encoded), 3)
            # Cambia solo la durata della seconda foto e l'ordine: FFmpeg converte una sola foto.
            encoded.clear()
            changed = [playlist[2], dict(playlist[1], duration=8), playlist[0]]
            self.app.store.update({"iptv": {"channels": [{"id": "riuso", "name": "Riuso", "enabled": True,
                                                          "playlist": changed, "schedule": []}]}})
            self.app.iptv.build("riuso")
            self.assertEqual([Path(source).name for source in encoded], ["riuso-b.jpg"])
            metadata = json.loads(self.app.iptv.build_metadata("riuso").read_text(encoding="utf-8"))
            self.assertEqual([item["name"] for item in metadata["sequence"]], ["riuso-c.jpg", "riuso-b.jpg", "riuso-a.jpg"])
            self.assertEqual([item["duration"] for item in metadata["sequence"]], [4, 8, 4])
            # La parte vecchia della seconda foto (4 secondi) non resta in memoria.
            cache = self.app.iptv.output("riuso").parent / "cache"
            self.assertEqual({p.name.split(".")[0] for p in cache.iterdir()}, set(metadata["parts"]))
            # Se il file della foto viene sostituito, quella parte si riconverte.
            encoded.clear()
            source = self.app.MEDIA_DIR / "riuso-a.jpg"
            source.write_bytes(b"nuova foto")
            os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 5_000_000_000))
            self.app.iptv.build("riuso")
            self.assertEqual([Path(source).name for source in encoded], ["riuso-a.jpg"])

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
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index_classic.html").read_text(encoding="utf-8")
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

    def test_sonos_refresh_never_imports_radio_browser_items_from_sonos_folder(self):
        root = {"title": "Audio", "children": [{
            "title": "I miei Sonos", "media_content_type": "favorites",
            "media_content_id": "my-sonos", "can_expand": True, "can_play": False,
        }]}
        my_sonos = {"title": "I miei Sonos", "children": [{
            "title": "Radio Browser infinita", "media_content_type": "audio/mpeg",
            "media_content_id": "media-source://radio_browser/example",
            "can_expand": False, "can_play": True,
        }, {
            "title": "Preferito vero", "media_content_type": "favorite_item_id",
            "media_content_id": "FV:2/31", "can_expand": False, "can_play": True,
        }]}

        def browse(_entity_id, _media_type=None, media_id=None):
            return my_sonos if media_id == "my-sonos" else root

        with mock.patch.object(self.app.ha, "states", return_value=[]), \
                mock.patch.object(self.app.ha, "browse_media", side_effect=browse):
            catalog = self.app.refresh_sonos_catalog("media_player.sala")

        self.assertEqual([item["id"] for item in catalog["items"]], ["FV:2/31"])

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
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index_classic.html").read_text(encoding="utf-8")
        self.assertIn("if(dirty&&!force)return", html)
        self.assertIn("Ordine di riproduzione, da sinistra a destra", html)
        self.assertIn("Sequenza realmente generata", html)
        self.assertIn("monitorIptvBuild", html)
        self.assertIn("Salva e genera sequenza", html)
        self.assertIn("images:[]", html)
        self.assertIn("foto selezionate su 6", html)
        self.assertNotIn('id="audioDriver"', html)

    def test_desktop_sonos_picker_and_ingress_music_controls(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index_classic.html").read_text(encoding="utf-8")
        self.assertIn('id="audioPlayers" class="speaker-picker"', html)
        self.assertIn('id="musicPlayers" class="speaker-picker"', html)
        self.assertIn("speakerValues('musicPlayers')", html)
        self.assertIn("api('api/music/start',{scheduled:useSchedule})", html)
        self.assertIn('onclick="startMusic(true)"', html)
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
        self.assertIn('id="musicSourceSearch"', html)
        self.assertIn('id="musicPlaylist"', html)
        self.assertIn('id="musicRadio"', html)
        self.assertIn("function filterSonosSourceOptions()", html)

    def test_dashboard_is_responsive_on_phone(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index_classic.html").read_text(encoding="utf-8")
        self.assertIn("@media(max-width:700px)", html)
        self.assertIn("overflow-x:hidden", html)
        self.assertIn(".speaker-option span{min-width:0;max-width:100%;flex:1;overflow:hidden}", html)
        self.assertIn("padding:8px 8px calc(88px + env(safe-area-inset-bottom))", html)
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", html)
        self.assertIn(".speaker-option strong,.speaker-option small{white-space:normal", html)
        self.assertIn(".savebar{position:static", html)

    def test_apple_time_fields_do_not_open_unstable_native_picker(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index_classic.html").read_text(encoding="utf-8")
        self.assertIn("function stabilizeAppleTimeInputs", html)
        self.assertIn("function normalizeClockInput", html)
        self.assertIn("input.type='text'", html)
        self.assertIn("input.placeholder='HH:MM'", html)
        self.assertIn("stableTimeObserver.observe", html)

    def test_language_switch_is_always_visible_and_persists_immediately(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index_classic.html").read_text(encoding="utf-8")
        self.assertIn('class="language-control"', html)
        self.assertIn('id="language" aria-label="Lingua / Sprache"', html)
        self.assertEqual(html.count('id="language"'), 1)
        self.assertIn("async function saveLanguage(lang)", html)
        self.assertIn("JSON.stringify({language:lang})", html)

    def test_audio_duration_is_automatic_in_the_interface(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index_classic.html").read_text(encoding="utf-8")
        self.assertIn("Automatica: viene letta direttamente dal file audio", html)
        self.assertIn("formatDuration(m.duration)", html)
        self.assertNotIn('id="spotDuration" type="number"', html)

    def test_stop_spot_button_is_available_on_dashboard_and_audio_page(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index_classic.html").read_text(encoding="utf-8")
        self.assertEqual(html.count('onclick="stopAudio()"'), 2)
        self.assertIn("api/audio/stop", html)

    def test_audio_frequency_and_repeat_controls_are_unambiguous(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index_classic.html").read_text(encoding="utf-8")
        self.assertIn("Riproduci uno spot ogni (minuti)", html)
        self.assertIn("A ogni intervallo viene riprodotto un solo spot", html)
        self.assertIn("Uno alla volta, nell’ordine della lista", html)
        self.assertNotIn("Quante volte consecutive", html)

    def test_complete_german_ui_and_audio_rotation_summary_are_available(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index_classic.html").read_text(encoding="utf-8")
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
        # La cassa è già da sola: niente distacco prima dello spot, resta solo l'attesa del ripristino.
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.6])
        self.assertAlmostEqual(
            wait.call_args_list[0].args[0],
            42.25 + self.app.SONOS_SPOT_TAIL_GUARD_SECONDS,
            places=2,
        )
        self.assertEqual(len([call for call in self.calls if call[1] == "play_media"]), 1)
        self.assertTrue(any(call[1] == "media_play" for call in self.calls))
        self.assertEqual(self.app.scheduler.audio_today(), count_before + 1)
        (self.app.MEDIA_DIR / ad).unlink()

    def test_spot_is_not_faded_or_cut_before_its_real_end(self):
        ad = "spot-senza-tagli.mp3"
        (self.app.MEDIA_DIR / ad).write_bytes(b"audio")
        self.app.store.update({"audio": {
            "players": ["media_player.sala"],
            "ads": [ad],
            "repeat_count": 1,
            "volume": 35,
            "transition_seconds": 3,
        }})
        self.calls.clear()
        with mock.patch.object(self.app, "probe_media_duration", return_value=10.0), \
                mock.patch.object(self.app, "fade_sonos") as fade, \
                mock.patch.object(self.app.audio_stop_event, "wait", return_value=False) as wait:
            self.app.play_audio(ad, manual=True)

        self.assertAlmostEqual(
            wait.call_args_list[0].args[0],
            10.0 + self.app.SONOS_SPOT_TAIL_GUARD_SECONDS,
            places=2,
        )
        self.assertFalse(any(call.args[1] == 0.35 for call in fade.call_args_list))
        play_index = next(i for i, call in enumerate(self.calls) if call[1] == "play_media")
        self.assertTrue(any(
            call[1] == "volume_set" and call[2].get("volume_level") == 0.35
            for call in self.calls[:play_index]
        ))
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

    def test_dashboard_music_start_uses_active_programmed_slot(self):
        slot = {
            "id": "pranzo",
            "name": "Radio pranzo",
            "players": ["media_player.sala"],
            "content_id": "Radio Italia Anni 60",
            "content_type": "sonos_radio_source",
            "volume": 25,
            "enabled": True,
            "days": [0, 1, 2, 3, 4, 5, 6],
            "start": "00:00",
            "duration_minutes": 1439,
        }
        self.app.store.update({"music": {
            "players": [],
            "content_id": "",
            "content_type": "music",
            "volume": 25,
            "slots": [slot],
        }})
        self.calls.clear()

        with mock.patch.object(self.app, "active_music_slot", return_value=slot):
            status, payload = self.request("/api/music/start", "POST", {"scheduled": True})

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source"], "Radio pranzo")
        self.assertIn(("media_player", "select_source", {
            "entity_id": "media_player.sala",
            "source": "Radio Italia Anni 60",
        }), self.calls)

    def test_restart_music_follows_automatic_mix(self):
        # «Riavvia la musica» con il Mix automatico acceso: riparte il blocco del mix su tutte le sue casse,
        # non la prima fascia del Palinsesto (che suonava su 4 casse e il motore non lo sapeva).
        app = self.app
        all_players = [f"media_player.cassa_{n}" for n in range(10)]
        block = {"id": "mix-2026-10-17-1170", "name": "Ristorante", "title": "Ristorante", "content_id": "FV:2/3",
                 "content_type": "favorite_item_id", "players": all_players, "volume": 25, "mix": True}
        palinsesto = {"id": "cocciante", "name": "Mix Cocciante und Raf", "players": all_players[:4],
                      "content_id": "FV:2/8", "content_type": "favorite_item_id", "volume": 30, "enabled": True,
                      "days": [0, 1, 2, 3, 4, 5, 6], "start": "00:00", "duration_minutes": 1439}
        original = json.loads(json.dumps(app.store.config["music"]))
        engine = app.scheduler
        try:
            app.store.config["music"] = {"enabled": True, "players": all_players, "slots": [palinsesto],
                                         "schedule": [], "volume": 25}
            engine.music_slot_key, engine.music_selection = "vecchia", dict(block)
            engine.music_override = {"mode": "paused", "slot_key": "vecchia", "name": "Ristorante"}
            with mock.patch.object(app, "mix_settings", return_value={"enabled": True, "players": all_players}), \
                    mock.patch.object(app, "active_mix_slot", return_value=dict(block)), \
                    mock.patch.object(app, "start_music", return_value=all_players) as start:
                status, payload = self.request("/api/music/start", "POST", {"scheduled": True})
                self.assertEqual(status, 200)
                self.assertEqual(payload["source"], "Ristorante")
                start.assert_not_called()
                self.assertIsNone(engine.music_override)
                self.assertIsNone(engine.music_slot_key)
                # Al giro successivo il motore fa ripartire il blocco del mix e lo segue.
                with mock.patch.object(app.ha, "states", return_value=[]), mock.patch.object(app.time, "sleep"):
                    engine.music_tick()
            start.assert_called_once()
            self.assertEqual(start.call_args.args[0]["name"], "Ristorante")
            self.assertEqual(start.call_args.args[0]["players"], all_players)
            self.assertEqual(engine.music_slot_id, block["id"])
        finally:
            app.store.config["music"] = original
            engine.music_override = None
            engine._forget_music()

    def test_music_start_uses_play_media_for_native_sonos_playlist(self):
        self.app.store.update({"music": {
            "players": ["media_player.sala"],
            "content_id": "Carellas Ristorante",
            "content_type": "sonos_playlist_source",
            "volume": 25,
        }})
        self.calls.clear()

        status, payload = self.request("/api/music/start", "POST", {})

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        play_calls = [call for call in self.calls if call[1] == "play_media"]
        self.assertEqual(len(play_calls), 1)
        self.assertEqual(play_calls[0][2]["media_content_type"], "playlist")
        self.assertEqual(play_calls[0][2]["media_content_id"], "Carellas Ristorante")
        self.assertFalse(any(call[1] == "select_source" for call in self.calls))

    def test_playlist_resumes_where_it_was_left(self):
        # Mix: Vasco → Ramazzotti → di nuovo Vasco. Vasco deve riprendere dal brano in cui era rimasta.
        app = self.app
        app.PLAYLIST_POSITIONS.clear()
        vasco = {"id": "mix-1", "name": "Vasco", "players": ["media_player.sala"], "content_id": "FV:2/10",
                 "content_type": "favorite_item_id", "volume": 25}
        ramazzotti = {**vasco, "id": "mix-2", "name": "Ramazzotti", "content_id": "FV:2/11"}
        radio = {**vasco, "id": "mix-3", "name": "Radio Kiss Kiss", "content_id": "FV:2/12",
                 "content_type": "sonos_radio_source"}
        state = {"entity_id": "media_player.sala", "state": "playing", "attributes": {
            "queue_position": 4, "queue_size": 30, "media_duration": 240, "media_position": 95,
            "media_position_updated_at": datetime.now().astimezone().isoformat(), "media_title": "Albachiara"}}
        calls = []
        try:
            with mock.patch.object(app.ha, "states", return_value=[state]):
                saved = app.remember_playlist_position(vasco, "media_player.sala")
                self.assertEqual((saved["index"], saved["title"]), (4, "Albachiara"))
                self.assertGreaterEqual(saved["position"], 95)
                # Le radio non hanno una coda: non si ricorda niente.
                self.assertIsNone(app.remember_playlist_position(radio, "media_player.sala"))
                # Brano quasi finito: la prossima volta si parte dal successivo.
                ending = {**state, "state": "paused", "attributes": {**state["attributes"], "media_position": 232}}
                with mock.patch.object(app.ha, "states", return_value=[ending]):
                    self.assertEqual(app.remember_playlist_position(ramazzotti, "media_player.sala")["index"], 5)
            self.assertTrue(app.PLAYLIST_POSITIONS_FILE.is_file())
            # Torna Vasco: dopo aver caricato la playlist salta al brano 5 e al punto in cui era.
            with mock.patch.object(app.ha, "service", side_effect=lambda d, n, p: calls.append((d, n, p))):
                self.assertTrue(app.resume_playlist_position("media_player.sala", vasco))
            self.assertEqual(calls[0], ("sonos", "play_queue", {"entity_id": "media_player.sala", "queue_position": 4}))
            self.assertEqual(calls[1][1], "media_seek")
            self.assertGreaterEqual(calls[1][2]["seek_position"], 95)
            self.assertTrue(any("ripresa dal brano 5" in item["message"] for item in app.store.logs[:3]))
            # Usato una volta: se Vasco riparte di nuovo da sola non si torna indietro a quel punto.
            self.assertNotIn("FV:2/10", app.PLAYLIST_POSITIONS)
            calls.clear()
            with mock.patch.object(app.ha, "service", side_effect=lambda d, n, p: calls.append((d, n, p))):
                self.assertFalse(app.resume_playlist_position("media_player.sala", vasco))
            self.assertEqual(calls, [])

            # Nel motore: al cambio di blocco si ricorda il punto della playlist che finisce.
            engine = app.Scheduler()
            engine.music_selection, engine.music_coordinator = dict(vasco), "media_player.sala"
            engine.music_slot_key, engine.music_players = "vecchia", ["media_player.sala"]
            original = json.loads(json.dumps(app.store.config["music"]))
            app.store.config["music"] = {"enabled": True, "players": ["media_player.sala"], "slots": [ramazzotti],
                                         "schedule": [], "volume": 25, "transition_seconds": 0}
            try:
                with mock.patch.object(app, "active_music_slot", return_value=dict(ramazzotti)), \
                        mock.patch.object(app.ha, "states", return_value=[state]), \
                        mock.patch.object(app, "start_music", return_value=["media_player.sala"]), \
                        mock.patch.object(app.time, "sleep"):
                    engine.music_tick()
            finally:
                app.store.config["music"] = original
            self.assertEqual(app.PLAYLIST_POSITIONS["FV:2/10"]["index"], 4)
        finally:
            app.PLAYLIST_POSITIONS.clear()
            app.PLAYLIST_POSITIONS_FILE.unlink(missing_ok=True)

    def test_music_start_falls_back_to_select_source_when_playlist_fails(self):
        selection = {
            "players": ["media_player.sala"],
            "content_id": "Radio Italia",
            "content_type": "sonos_playlist_source",
            "volume": 25,
        }

        def service(domain, action, payload):
            self.calls.append((domain, action, payload))
            if action == "play_media":
                raise RuntimeError("playlist non disponibile")
            return []

        self.calls.clear()
        with mock.patch.object(self.app.ha, "service", side_effect=service):
            players = self.app.start_music(selection)

        self.assertEqual(players, ["media_player.sala"])
        self.assertIn(("media_player", "select_source", {
            "entity_id": "media_player.sala",
            "source": "Radio Italia",
        }), self.calls)
        self.assertTrue(any(call[1] == "media_play" for call in self.calls))

    def test_music_start_uses_select_source_for_native_sonos_radio(self):
        selection = {
            "players": ["media_player.sala"],
            "content_id": "Radio Italia Anni 60",
            "content_type": "sonos_radio_source",
            "volume": 25,
        }
        self.calls.clear()

        players = self.app.start_music(selection)

        self.assertEqual(players, ["media_player.sala"])
        self.assertIn(("media_player", "select_source", {
            "entity_id": "media_player.sala",
            "source": "Radio Italia Anni 60",
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

    def test_settings_menu_and_channel_tv_targets(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        # Ogni sezione delle Impostazioni ha il suo indirizzo, raggiungibile dal menu laterale e dalla tendina.
        for key in ("collegamento", "canali", "player", "utenti", "schermo", "lingua"):
            self.assertIn(f"['{key}',", html)
            self.assertIn(f'id="set-{key}"', html)
        self.assertIn('href="#/impostazioni/${k}" data-set-key="${k}"', html)
        self.assertIn("data-settings-jump", html)
        self.assertIn("setTimeout(()=>scrollToSetting(S.param),0)", html)
        # Nella zona: su quali TV (utenti del Player) va il canale, con la scelta del canale per ogni TV.
        self.assertIn("${ch?tvTargets(ch):''}", html)
        self.assertIn('data-bind="browser_player.users.${i}.channel_id"', html)
        self.assertIn("'tv-add-here':el=>", html)
        self.assertIn("['Su quali TV va questo canale','Auf welchen TVs läuft dieser Kanal']", html)

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

    def test_slot_change_waits_out_long_701_and_checks_music_really_plays(self):
        busy = RuntimeError("UPnP Error 701 received: Transition not available from 192.168.178.12")
        calls, attempts = [], {"play_media": 0, "media_stop": 0}

        def service(domain, name, payload):
            calls.append((domain, name, payload))
            if name in attempts:
                attempts[name] += 1
                # Il cambio dalla radio alla playlist resta occupato per quattro tentativi;
                # anche l'arresto risponde 701 e non deve interrompere i tentativi.
                if attempts[name] <= 4:
                    raise busy

        with mock.patch.object(self.app.ha, "service", side_effect=service), mock.patch.object(self.app.time, "sleep"):
            self.app.play_sonos_media({"entity_id": "media_player.sala", "media_content_type": "playlist",
                                       "media_content_id": "S:/Pranzo"})
        self.assertEqual(attempts["play_media"], 5)

        # Il comando è stato accettato ma il Sonos resta fermo: dopo qualche secondo si prova con play.
        paused = [{"entity_id": "media_player.sala", "state": "paused", "attributes": {}}]
        playing = [{"entity_id": "media_player.sala", "state": "playing", "attributes": {}}]
        calls.clear()
        with mock.patch.object(self.app.ha, "states", side_effect=[paused] * 6 + [playing] * 3),                 mock.patch.object(self.app.ha, "service", side_effect=lambda d, n, p: calls.append((d, n, p))),                 mock.patch.object(self.app.time, "sleep"):
            self.assertTrue(self.app.ensure_sonos_playing("media_player.sala"))
        self.assertEqual(calls, [("media_player", "media_play", {"entity_id": "media_player.sala"})])
        with mock.patch.object(self.app.ha, "states", return_value=paused),                 mock.patch.object(self.app.ha, "service"), mock.patch.object(self.app.time, "sleep"):
            self.assertFalse(self.app.ensure_sonos_playing("media_player.sala"))

        # Se i Sonos non partono, la fascia lo segnala e si riprova presto, non dopo 5 minuti.
        original = json.loads(json.dumps(self.app.store.config["music"]))
        slot = {"id": "pl", "name": "Playlist", "players": ["media_player.sala"], "content_id": "S:/Pranzo",
                "content_type": "playlist", "volume": 20}
        engine = self.app.Scheduler()
        self.app.store.config["music"] = {"enabled": True, "players": ["media_player.sala"], "slots": [slot],
                                          "schedule": [], "stop_at_end": True, "volume": 25}
        try:
            with mock.patch.object(self.app, "active_music_slot", return_value=slot),                     mock.patch.object(self.app.ha, "states", return_value=paused),                     mock.patch.object(self.app.ha, "service"), mock.patch.object(self.app.time, "sleep"):
                engine.music_tick()
            self.assertIsNotNone(engine.music_failed_slot_key)
            wait = engine.music_retry_at - self.app.time.monotonic()
            self.assertLessEqual(wait, self.app.MUSIC_RETRY_SECONDS)
            self.assertGreater(wait, 30)
            self.assertTrue(any("non sono partiti" in item["message"] for item in self.app.store.logs[:5]))
        finally:
            self.app.store.config["music"] = original

    def test_music_start_check_ignores_stale_state_and_accepts_late_start(self):
        app = self.app
        playing = [{"entity_id": "media_player.sala", "state": "playing", "attributes": {}}]
        paused = [{"entity_id": "media_player.sala", "state": "paused", "attributes": {}}]
        buffering = [{"entity_id": "media_player.sala", "state": "buffering", "attributes": {}}]
        # Subito dopo il comando HA riporta ancora «playing» della musica di prima, poi il Sonos resta fermo:
        # la fascia non va considerata partita.
        with mock.patch.object(app, "SONOS_START_SETTLE_SECONDS", 2), \
                mock.patch.object(app.ha, "states", side_effect=[playing, playing] + [paused] * 30), \
                mock.patch.object(app.ha, "service"), mock.patch.object(app.time, "sleep"):
            self.assertFalse(app.ensure_sonos_playing("media_player.sala"))
        # «buffering» da solo non basta: serve «playing» per due controlli di fila.
        with mock.patch.object(app.ha, "states", side_effect=[buffering] * 3 + [playing] * 2), \
                mock.patch.object(app.ha, "service"), mock.patch.object(app.time, "sleep"):
            self.assertTrue(app.ensure_sonos_playing("media_player.sala"))

        # Playlist lunga: i Sonos partono dopo il controllo. La fascia si considera avviata,
        # senza errore e senza ricominciarla da capo.
        original = json.loads(json.dumps(app.store.config["music"]))
        slot = {"id": "nino", "name": "Nino Fiorello", "players": ["media_player.sala"], "content_id": "FV:2/9",
                "content_type": "favorite_item_id", "volume": 20}
        engine = app.Scheduler()
        app.store.config["music"] = {"enabled": True, "players": ["media_player.sala"], "slots": [slot],
                                     "schedule": [], "stop_at_end": True, "volume": 25}
        late = app.SonosNotStarted("I Sonos non sono partiti con «Nino Fiorello»: riprovo tra poco", ["media_player.sala"])
        try:
            with mock.patch.object(app, "active_music_slot", return_value=slot), \
                    mock.patch.object(app, "start_music", side_effect=late) as start, \
                    mock.patch.object(app.ha, "states", return_value=playing), \
                    mock.patch.object(app.ha, "service"), mock.patch.object(app.time, "sleep"):
                engine.music_tick()
                engine.music_tick()
            start.assert_called_once()
            self.assertEqual(engine.music_slot_id, "nino")
            self.assertIsNone(engine.music_failed_slot_key)
            self.assertTrue(engine.music_active)
            recent = [item["message"] for item in app.store.logs[:5]]
            self.assertTrue(any("partita con un po' di ritardo" in message for message in recent))
            self.assertFalse(any("Avvio musica non riuscito" in message for message in recent))
        finally:
            app.store.config["music"] = original

    def test_music_keeps_playing_after_addon_restart(self):
        # Aggiornamento dell'add-on: i Sonos continuano a suonare la fascia; al riavvio non va rimandata
        # (la playlist ripartiva dal primo brano a ogni aggiornamento).
        app = self.app
        slot = {"id": "mix-1", "name": "Mix Cocciante und Raf", "players": ["media_player.sala"],
                "content_id": "FV:2/8", "content_type": "favorite_item_id", "volume": 25}
        original = json.loads(json.dumps(app.store.config["music"]))
        app.store.config["music"] = {"enabled": True, "players": ["media_player.sala"], "slots": [slot],
                                     "schedule": [], "volume": 25, "transition_seconds": 0}
        playing = [{"entity_id": "media_player.sala", "state": "playing", "attributes": {}}]
        paused = [{"entity_id": "media_player.sala", "state": "paused", "attributes": {}}]
        try:
            with mock.patch.object(app, "active_music_slot", return_value=dict(slot)),                     mock.patch.object(app, "start_music", return_value=["media_player.sala"]) as start,                     mock.patch.object(app.time, "sleep"):
                with mock.patch.object(app.ha, "states", return_value=playing):
                    first = app.Scheduler()
                    first.music_tick()
                    self.assertEqual(start.call_count, 1)
                    self.assertTrue(app.MUSIC_STATE_FILE.is_file())
                    # Riavvio: un nuovo motore trova la fascia già in onda e la segue senza ripartire.
                    second = app.Scheduler()
                    second.music_tick()
                    self.assertEqual(start.call_count, 1)
                    self.assertEqual(second.music_slot_id, "mix-1")
                    self.assertEqual(second.music_coordinator, "media_player.sala")
                    self.assertTrue(any("continua senza ripartire" in item["message"] for item in app.store.logs[:3]))
                # Se dopo il riavvio i Sonos sono fermi, la fascia parte normalmente.
                with mock.patch.object(app.ha, "states", return_value=paused):
                    third = app.Scheduler()
                    third.music_tick()
                self.assertEqual(start.call_count, 2)
                # Stato di un altro giorno: non vale.
                app.MUSIC_STATE_FILE.write_text(json.dumps({"day": "2000-01-01", "slot_key": "x", "players": []}),
                                                encoding="utf-8")
                self.assertIsNone(app.Scheduler().music_resume)
        finally:
            app.store.config["music"] = original

    def test_music_stopped_during_slot_restarts_by_itself(self):
        app = self.app
        slot = {"id": "rist", "name": "Ristorante", "players": ["media_player.sala"], "content_id": "FV:2/3",
                "content_type": "favorite_item_id", "volume": 25}
        paused = [{"entity_id": "media_player.sala", "state": "paused", "attributes": {}}]
        playing = [{"entity_id": "media_player.sala", "state": "playing", "attributes": {}}]
        engine = app.Scheduler()
        engine.music_coordinator, engine.music_selection = "media_player.sala", dict(slot)
        clock = [1000.0]
        calls = []
        with mock.patch.object(app.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(app.time, "sleep"), \
                mock.patch.object(app.ha, "service", side_effect=lambda d, n, p: calls.append(n)):
            with mock.patch.object(app.ha, "states", return_value=paused):
                engine.music_watchdog(slot)          # si accorge che è ferma
                clock[0] += 10
                engine.music_watchdog(slot)          # ferma da 10 s: si aspetta ancora
                self.assertEqual(calls, [])
            # Ferma da più di 20 secondi: Play, e la musica riparte.
            states = iter([paused] + [playing] * 10)
            with mock.patch.object(app.ha, "states", side_effect=lambda: next(states)):
                clock[0] += 15
                engine.music_watchdog(slot)
            self.assertEqual(calls, ["media_play"])
            self.assertTrue(any("si era fermata da sola" in item["message"] for item in app.store.logs[:3]))
            # Se Play non basta (es. playlist finita), la fascia riparte dall'inizio.
            calls.clear()
            with mock.patch.object(app.ha, "states", return_value=paused), \
                    mock.patch.object(app, "start_music", return_value=["media_player.sala"]) as start:
                clock[0] += app.MUSIC_RETRY_SECONDS + 1
                engine.music_watchdog(slot)
                clock[0] += app.MUSIC_RESUME_SECONDS + 11
                engine.music_watchdog(slot)
            start.assert_called_once()
            # Durante uno spot non si controlla nulla.
            with mock.patch.object(app, "audio_playback_lock") as lock, \
                    mock.patch.object(app.ha, "states", return_value=paused):
                lock.locked.return_value = True
                clock[0] += app.MUSIC_RETRY_SECONDS + 100
                engine.music_watchdog(slot)
                self.assertIsNone(engine.music_stopped_since)

    def test_music_start_survives_701_on_join_and_volume(self):
        busy = RuntimeError("UPnP Error 701 received: Transition not available from 192.168.178.12")
        players = ["media_player.sala", "media_player.terrazza"]
        states = [{"entity_id": player, "state": "playing",
                   "attributes": {"group_members": [player], "volume_level": 0.3}} for player in players]
        failures = {"join": 2, "volume_set": 2}
        calls = []

        def service(domain, name, payload):
            calls.append(name)
            if failures.get(name):
                failures[name] -= 1
                raise busy

        selection = {"name": "RTL 102.5 FM", "players": players, "content_id": "FV:2/31",
                     "content_type": "favorite_item_id", "volume": 25}
        # Prima il 701 sul raggruppamento o sul volume interrompeva subito «Riavvia la musica».
        with mock.patch.object(self.app.ha, "states", return_value=states),                 mock.patch.object(self.app.ha, "service", side_effect=service),                 mock.patch.object(self.app.time, "sleep"):
            self.assertEqual(self.app.start_music(selection), players)
        self.assertEqual(calls.count("join"), 3)
        self.assertIn("play_media", calls)
        # Se Sonos resta occupato, il pannello mostra un messaggio comprensibile.
        original = json.loads(json.dumps(self.app.store.config["music"]))
        self.app.store.config["music"]["slots"] = [{"id": "radio", **selection}]
        try:
            with mock.patch.object(self.app, "start_music", side_effect=busy):
                with self.assertRaises(urllib.error.HTTPError) as refused:
                    self.request("/api/music/start", "POST", {"slot_id": "radio"})
            self.assertIn("riprova tra qualche secondo", json.loads(refused.exception.read())["error"])
        finally:
            self.app.store.config["music"] = original

    def test_internet_radio_when_sonos_rejects_the_favorite(self):
        rejected = RuntimeError("UPnP Error 402 received: Invalid Args from 192.168.178.12")
        stations = [
            {"name": "RTL 102.5 Best", "url_resolved": "https://streamingv2.shoutcast.com/rtl-1025-best",
             "codec": "MP3", "bitrate": 256, "countrycode": "IT"},
            {"name": "Radio Kiss Kiss Italia", "url_resolved": "http://wma06.fluidstream.net:3614/;listen.pls",
             "codec": "MP3", "bitrate": 128, "countrycode": "IT"},
            {"name": "RTL 102.5 [Mobile 48kbps]", "url_resolved": "https://streamingv2.shoutcast.com/rtl-1025_48.aac",
             "codec": "AAC+", "bitrate": 96, "countrycode": "IT"},
            {"name": "senza indirizzo", "url_resolved": "", "codec": "MP3", "bitrate": 0, "countrycode": "IT"},
        ]

        class Response:
            def __init__(self, url):
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(stations).encode()

        searched = []

        def fake_urlopen(request, timeout=0):
            searched.append(request.full_url)
            return Response(request.full_url)

        self.app.RADIO_STATION_CACHE.clear()
        try:
            with mock.patch.object(self.app.urllib.request, "urlopen", side_effect=fake_urlopen):
                # Ricerca dal Palinsesto: solo stazioni con un indirizzo valido.
                found = self.app.radio_browser_search("Kiss Kiss")
                self.assertEqual([st["name"] for st in found],
                                 ["RTL 102.5 Best", "Radio Kiss Kiss Italia", "RTL 102.5 [Mobile 48kbps]"])
                self.assertIn("radio-browser.info/json/stations/search?name=Kiss+Kiss", searched[0])
                # «RTL 102.5 FM» trova la stessa radio, non un altro canale con un nome simile.
                rtl = self.app.internet_radio_station({"name": "RTL 102.5 FM", "content_type": "favorite_item_id",
                                                        "content_id": "FV:2/9"})
                self.assertEqual(rtl["name"], "RTL 102.5 [Mobile 48kbps]")
                # Una playlist non viene mai scambiata per una radio.
                self.assertIsNone(self.app.internet_radio_station({"name": "Dinner Jazz", "content_type": "favorite_item_id",
                                                                   "content_id": "FV:2/3"}))
                states = [{"entity_id": "media_player.sala", "state": "playing",
                           "attributes": {"group_members": ["media_player.sala"], "volume_level": 0.3,
                                          "source_list": ["Radio Kiss Kiss Italia"]}}]
                calls = []

                def service(domain, name, payload):
                    calls.append((name, payload))
                    # Sonos rifiuta il Preferito sia per ID sia per nome (radio TuneIn).
                    if name in ("play_media", "select_source") and not str(payload.get("media_content_id", "")).startswith("x-rincon"):
                        raise rejected

                slot = {"name": "Radio Kiss Kiss Italia", "title": "Radio Kiss Kiss Italia", "players": ["media_player.sala"],
                        "content_id": "FV:2/31", "content_type": "favorite_item_id", "volume": 25}
                with mock.patch.object(self.app.ha, "states", return_value=states), \
                        mock.patch.object(self.app.ha, "service", side_effect=service), \
                        mock.patch.object(self.app.time, "sleep"):
                    self.assertEqual(self.app.start_music(slot), ["media_player.sala"])
                played = [payload for name, payload in calls if name == "play_media"]
                self.assertEqual(played[-1], {"entity_id": "media_player.sala", "media_content_type": "music",
                                              "media_content_id": "x-rincon-mp3radio://wma06.fluidstream.net:3614/;listen.pls"})
                self.assertTrue(any("la avvio come radio da Internet" in item["message"] for item in self.app.store.logs[:5]))
                # Radio scelta nel Palinsesto: parte direttamente dall'indirizzo.
                calls.clear()
                with mock.patch.object(self.app.ha, "states", return_value=states), \
                        mock.patch.object(self.app.ha, "service", side_effect=lambda d, n, p: calls.append((n, p))), \
                        mock.patch.object(self.app.time, "sleep"):
                    self.app.start_music({**slot, "content_type": "radio_url",
                                          "content_id": "https://str01.fluidstream.net/anni60.mp3"})
                self.assertIn(("play_media", {"entity_id": "media_player.sala", "media_content_type": "music",
                                              "media_content_id": "https://str01.fluidstream.net/anni60.mp3"}), calls)
        finally:
            self.app.RADIO_STATION_CACHE.clear()
        with mock.patch.object(self.app, "radio_browser_search", return_value=[{"name": "Radio Kiss Kiss Italia"}]) as search:
            status, body = self.request("/api/radio/search", "POST", {"q": "Kiss Kiss"})
        self.assertEqual((status, body["stations"][0]["name"]), (200, "Radio Kiss Kiss Italia"))
        search.assert_called_once_with("Kiss Kiss")
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn('data-act="src-tab" data-v="radio"', html)
        self.assertIn('data-act="src-tab" data-v="playlist"', html)
        self.assertIn("command('api/radio/search',{q})", html)
        self.assertIn("s.content_type='radio_url';s.content_id=st.url;", html)
        self.assertIn("['Playlist Sonos','Sonos-Playlists']", html)
        # Le due cartelle: Sonos dice se un Preferito è una radio o una playlist.
        kind = self.app.sonos_item_kind
        self.assertEqual(kind({"media_content_id": "FV:2/5", "media_class": "playlist"}), "playlist")
        self.assertEqual(kind({"media_content_id": "FV:2/6"}, {"media_content_id": "object.item.audioItem.audioBroadcast"}), "radio")
        self.assertEqual(kind({"media_content_id": "FV:2/7"}, {"title": "Playlists"}), "playlist")
        self.assertEqual(kind({"media_content_id": "FV:2/8"}, {"title": "Preferiti"}), "")

    def test_sonos_source_retries_701_then_succeeds(self):
        calls = []
        attempts = 0

        def service(domain, name, payload):
            nonlocal attempts
            calls.append((domain, name, payload))
            if name == "select_source":
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("UPnP Error 701 received: Transition not available")

        with mock.patch.object(self.app.ha, "service", side_effect=service), mock.patch.object(
            self.app.time, "sleep"
        ):
            self.app.select_sonos_source("media_player.sala", "Radio Italia Anni 60")

        self.assertEqual(attempts, 2)
        self.assertIn(("media_player", "media_stop", {"entity_id": "media_player.sala"}), calls)
        self.assertTrue(any(call[1] == "media_play" for call in calls))

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
            # La musica di prima si è fermata: viene riavviata.
            with mock.patch.object(self.app, "active_music_slot", side_effect=[first, second]), mock.patch.object(
                self.app, "fade_sonos"
            ), mock.patch.object(self.app, "sonos_playing", return_value=False), mock.patch.object(
                self.app, "start_music", side_effect=[["media_player.sala"], RuntimeError("Sonos occupato"), ["media_player.sala"]]
            ) as start:
                engine.music_tick()
                engine.music_tick()
        finally:
            self.app.store.config["music"] = original
        self.assertEqual(start.call_count, 3)
        self.assertEqual(start.call_args_list[-1].args[0]["id"], "one")
        self.assertEqual(engine.music_slot_id, "one")

    def test_rejected_source_keeps_previous_music_and_retries_rarely(self):
        original = json.loads(json.dumps(self.app.store.config["music"]))
        first = {"id": "radio", "name": "RTL 102.5 FM", "players": ["media_player.sala"],
                 "content_id": "FV:2/31", "content_type": "favorite_item_id", "volume": 30}
        second = {"id": "pl", "name": "Pranzo italiano", "players": ["media_player.sala"],
                  "content_id": "S:/Pranzo", "content_type": "playlist", "volume": 20}
        rejected = RuntimeError('Home Assistant 500: {"message":"Error on media_player.sala (UPnP error code 402): '
                                'UPnP Error 402 received: Invalid Args from 192.168.178.12"}')
        engine = self.app.Scheduler()
        self.app.store.config["music"] = {"enabled": True, "players": ["media_player.sala"], "slots": [first, second],
                                          "schedule": [], "stop_at_end": True, "volume": 25, "transition_seconds": 3}
        fades = []
        try:
            with mock.patch.object(self.app, "active_music_slot", side_effect=[first, second, second, second]),                     mock.patch.object(self.app, "fade_sonos", side_effect=lambda *a, **k: fades.append(a[1])),                     mock.patch.object(self.app, "sonos_volume_levels", return_value={"media_player.sala": 0.35}),                     mock.patch.object(self.app, "sonos_playing", return_value=True),                     mock.patch.object(self.app, "start_music", side_effect=[["media_player.sala"], rejected]) as start:
                engine.music_tick()
                logs = len(self.app.store.logs)
                engine.music_tick()
                engine.music_tick()
                engine.music_tick()
            # La radio di prima non viene riavviata: torna solo al suo volume (35).
            self.assertEqual(start.call_count, 2)
            self.assertEqual(fades[-1], {"media_player.sala": 0.35})
            new_logs = [item["message"] for item in self.app.store.logs[:len(self.app.store.logs) - logs]]
            self.assertEqual(sum("Avvio musica non riuscito" in message for message in new_logs), 1)
            self.assertTrue(any("Sonos non accetta la sorgente della fascia «Pranzo italiano» (errore 402)" in m
                                for m in new_logs))
            # Niente nuovi tentativi ogni 45 secondi: la musica non si abbassa e rialza di continuo.
            self.assertGreater(engine.music_retry_at - self.app.time.monotonic(), 600)
        finally:
            self.app.store.config["music"] = original
        # Se la stessa voce è tra i Preferiti Sonos, la fascia parte per nome.
        states = [{"entity_id": "media_player.sala", "state": "playing",
                   "attributes": {"group_members": ["media_player.sala"], "volume_level": 0.3,
                                  "source_list": ["RTL 102.5 FM", "Pranzo Italiano"]}}]
        calls = []

        def service(domain, name, payload):
            calls.append((name, payload))
            if name == "play_media":
                raise rejected

        with mock.patch.object(self.app.ha, "states", return_value=states),                 mock.patch.object(self.app.ha, "service", side_effect=service),                 mock.patch.object(self.app.time, "sleep"):
            self.app.start_music(second)
        self.assertIn(("select_source", {"entity_id": "media_player.sala", "source": "Pranzo Italiano"}), calls)

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
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index_classic.html").read_text(encoding="utf-8")
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
        self.assertIn("function sonosPlaybackType", html)
        self.assertIn("sonos_radio_source", html)
        self.assertIn("sonos_playlist_source", html)

    def test_upload_refreshes_library_without_discarding_unsaved_settings(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index_classic.html").read_text(encoding="utf-8")
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
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index_classic.html").read_text(encoding="utf-8")
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

    def test_spot_starts_at_once_on_existing_music_group(self):
        ad = "Carellas_Ristorante_Spot_DE_Maschile.mp3"
        players = ["media_player.sala", "media_player.terrazza", "media_player.bar"]
        # La musica del locale suona già su questi Sonos, raggruppati con la Terrazza come coordinatore.
        group = ["media_player.terrazza", "media_player.sala", "media_player.bar"]
        states = [{"entity_id": player, "state": "playing",
                   "attributes": {"friendly_name": player, "group_members": group, "volume_level": 0.3}}
                  for player in players]
        self.app.store.update({"audio": {"players": players, "ads": [ad], "repeat_count": 1, "volume": 40,
                                         "transition_seconds": 0}})
        self.calls.clear()
        with mock.patch.object(self.app.ha, "states", return_value=states), mock.patch.object(
            self.app, "probe_media_duration", return_value=20
        ), mock.patch.object(self.app.time, "sleep"), mock.patch.object(
            self.app.audio_stop_event, "wait", return_value=False
        ):
            self.app.play_audio(ad, manual=True)
        names = [call[1] for call in self.calls]
        # Niente distacco e nuovo raggruppamento (secondi di silenzio): lo spot va subito sul coordinatore.
        self.assertNotIn("unjoin", names[:names.index("play_media")])
        self.assertNotIn("join", names)
        play = next(call for call in self.calls if call[1] == "play_media")
        self.assertEqual(play[2]["entity_id"], "media_player.terrazza")
        # Tra la dissolvenza e lo spot c'è un solo comando: il volume dello spot.
        before = self.calls[names.index("play_media") - 1]
        self.assertEqual(before, ("media_player", "volume_set", {"entity_id": players, "volume_level": 0.4}))
        self.assertIn(("sonos", "restore", {"entity_id": ["media_player.terrazza"], "with_group": True}), self.calls)
        # Se il gruppo contiene anche un Sonos non scelto, i diffusori vanno ancora separati.
        self.assertIsNone(self.app.sonos_group_ready(players, {"media_player.terrazza": group + ["media_player.bagni"]}))
        self.assertIsNone(self.app.sonos_group_ready(players, {"media_player.sala": ["media_player.sala"],
                                                               "media_player.bar": ["media_player.bar", "media_player.terrazza"]}))
        self.assertEqual(self.app.sonos_group_ready(players, {"media_player.terrazza": group}), "media_player.terrazza")

    def test_each_sonos_returns_to_its_own_volume_after_spot(self):
        ad = "Carellas_Ristorante_Spot_DE_Maschile.mp3"
        players = ["media_player.sala", "media_player.terrazza", "media_player.bar"]
        own = {"media_player.sala": 0.35, "media_player.terrazza": 0.25, "media_player.bar": 0.55}
        self.app.SONOS_VOLUME_MEMORY.clear()
        # Il volume del Bar è stato scelto dal pannello, ma Home Assistant non lo riporta nello stato.
        self.request("/api/sonos/volume", "POST", {"entity_id": "media_player.bar", "volume": 55})
        states = [{"entity_id": player, "state": "playing",
                   "attributes": {"friendly_name": player, "group_members": players,
                                  **({} if player == "media_player.bar" else {"volume_level": own[player]})}}
                  for player in players]
        self.app.store.update({"audio": {"players": players, "ads": [ad], "repeat_count": 1, "volume": 40,
                                         "transition_seconds": 3}})
        self.calls.clear()
        try:
            with mock.patch.object(self.app.ha, "states", return_value=states), mock.patch.object(
                self.app, "probe_media_duration", return_value=20
            ), mock.patch.object(self.app.time, "sleep"), mock.patch.object(
                self.app.audio_stop_event, "wait", return_value=False
            ):
                self.app.play_audio(ad, manual=True)
            volumes = [call[2] for call in self.calls if call[1] == "volume_set"]
            final = {}
            for payload in volumes[-3:]:
                for player in payload["entity_id"]:
                    final[player] = payload["volume_level"]
            # Dopo lo spot ogni cassa è al suo volume (35, 25, 55), non a un valore uguale per tutte.
            self.assertEqual(final, own)
            # Senza musica da riprendere i Sonos tornano comunque al volume di prima.
            self.calls.clear()
            self.app.restore_sonos_playback(["media_player.sala"], [], players, own, 3)
            self.assertIn(("media_player", "volume_set", {"entity_id": ["media_player.bar"], "volume_level": 0.55}), self.calls)
            self.assertNotIn("media_play", [call[1] for call in self.calls])
        finally:
            self.app.SONOS_VOLUME_MEMORY.clear()
            self.app.SONOS_MANUAL_VOLUMES.clear()

    def test_sonos_rejoining_the_music_group_keeps_its_volume(self):
        states = [
            {"entity_id": "media_player.sala", "state": "playing",
             "attributes": {"group_members": ["media_player.sala"], "volume_level": 0.35}},
            {"entity_id": "media_player.bar", "state": "idle",
             "attributes": {"group_members": ["media_player.bar"], "volume_level": 0.01}},
        ]
        self.app.SONOS_VOLUME_MEMORY.clear()
        self.app.remember_sonos_volumes({"media_player.bar": 0.55})
        self.calls.clear()
        try:
            levels = self.app.remembered_sonos_volumes(["media_player.sala", "media_player.bar"], 0.25)
            self.assertEqual(levels, {"media_player.sala": 0.25, "media_player.bar": 0.55})
            with mock.patch.object(self.app.ha, "states", return_value=states), mock.patch.object(
                self.app, "SONOS_GROUP_SETTLE_SECONDS", 0
            ):
                result = self.app.repair_sonos_group(["media_player.sala", "media_player.bar"], "media_player.sala", levels)
            self.assertEqual(result["missing"], ["media_player.bar"])
            # Il Bar rientra con il suo 55, non con il volume della fascia musicale.
            self.assertIn(("media_player", "volume_set", {"entity_id": ["media_player.bar"], "volume_level": 0.55}), self.calls)
        finally:
            self.app.SONOS_VOLUME_MEMORY.clear()
        source = (Path(__file__).parents[1] / "carellas_media_ads/app/server.py").read_text(encoding="utf-8")
        self.assertIn("remembered_sonos_volumes(expected, self.music_volume)", source)

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

    def test_state_exposes_sonos_details_including_offline_configured_speakers(self):
        states = [
            {"entity_id": "media_player.sala", "state": "playing", "attributes": {
                "friendly_name": "Sonos Sala", "volume_level": 0.31, "media_title": "Dinner Jazz",
                "group_members": ["media_player.sala", "media_player.terrazza"],
            }},
            {"entity_id": "media_player.bar", "state": "unavailable", "attributes": {"friendly_name": "Sonos Bar"}},
            {"entity_id": "media_player.tv", "state": "on", "attributes": {"friendly_name": "TV"}},
        ]
        old_players = self.app.store.config["audio"]["players"]
        self.app.store.config["audio"]["players"] = ["media_player.sala", "media_player.bar"]
        try:
            with mock.patch.object(self.app.ha, "states", return_value=states):
                _, payload = self.request("/api/state")
        finally:
            self.app.store.config["audio"]["players"] = old_players
        sonos = {item["entity_id"]: item for item in payload["sonos"]}
        self.assertEqual(set(sonos), {"media_player.sala", "media_player.bar"})
        self.assertEqual(sonos["media_player.sala"]["volume"], 31)
        self.assertEqual(sonos["media_player.sala"]["media_title"], "Dinner Jazz")
        self.assertEqual(sonos["media_player.sala"]["group_members"], ["media_player.sala", "media_player.terrazza"])
        self.assertFalse(sonos["media_player.bar"]["available"])
        for key in ("next_spot", "next_spot_in_seconds", "spot_playing", "audio_paused_until", "audio_waiting"):
            self.assertIn(key, payload["runtime"])

    def test_next_spot_countdown_pause_and_skip(self):
        audio = self.app.store.config["audio"]
        saved = {key: audio.get(key) for key in ("enabled", "ads", "mode", "interval_minutes", "schedule", "only_when_playing")}
        audio.update(enabled=True, ads=["a.mp3", "b.mp3"], mode="rotate", interval_minutes=30, schedule=[], only_when_playing=False)
        scheduler = self.app.scheduler
        try:
            scheduler.audio_index = 0
            scheduler.audio_paused_until = 0.0
            scheduler.last_audio = self.app.time.monotonic() - 10 * 60
            status = scheduler.audio_status()
            self.assertEqual(status["next_spot"], "a.mp3")
            self.assertAlmostEqual(status["next_spot_in_seconds"], 20 * 60, delta=5)
            self.assertIsNone(status["audio_waiting"])
            # Saltare non riproduce nulla: riparte l'intervallo e passa allo spot successivo.
            self.assertEqual(scheduler.skip_audio(), "a.mp3")
            status = scheduler.audio_status()
            self.assertEqual(status["next_spot"], "b.mp3")
            self.assertAlmostEqual(status["next_spot_in_seconds"], 30 * 60, delta=5)
            status, payload = self.request("/api/audio/pause", "POST", {"minutes": 60})
            self.assertEqual(payload["audio_waiting"], "paused")
            self.assertIsNotNone(payload["audio_paused_until"])
            with mock.patch.object(self.app, "play_audio") as play:
                scheduler.last_audio = self.app.time.monotonic() - 3600
                scheduler.audio_tick()
            play.assert_not_called()
            self.request("/api/audio/pause", "POST", {"minutes": 0})
            self.assertIsNone(scheduler.audio_status()["audio_paused_until"])
        finally:
            audio.update(saved)
            scheduler.audio_paused_until = 0.0
            scheduler.audio_index = 0

    def test_zone_spot_test_uses_only_the_zone_speakers(self):
        with mock.patch.object(self.app, "play_audio") as play:
            self.request("/api/test/audio", "POST", {"name": "spot.mp3", "players": ["media_player.terrazza", "x"]})
            for _ in range(50):
                if play.called:
                    break
                self.app.time.sleep(0.02)
        play.assert_called_once_with("spot.mp3", True, ["media_player.terrazza"])

    def test_failed_manual_spot_is_written_to_the_log(self):
        with mock.patch.object(self.app, "play_audio", side_effect=RuntimeError("Nessun altoparlante selezionato")):
            self.request("/api/test/audio", "POST", {"name": "spot.mp3"})
            for _ in range(50):
                if any("Prova spot non riuscita" in log["message"] for log in self.app.store.logs):
                    break
                self.app.time.sleep(0.02)
        self.assertTrue(any("Prova spot non riuscita: Nessun altoparlante selezionato" in log["message"]
                            for log in self.app.store.logs))

    def test_zones_are_normalized_and_saved(self):
        self.app.store.update({"zones": [
            {"name": "Sala", "players": ["media_player.sala", 3, "switch.x"], "iptv_channels": ["TV Sala"], "main_tv": True},
            {"id": "bar", "name": " Bar ", "players": ["media_player.bar"]},
        ]})
        try:
            zones = self.app.store.config["zones"]
            self.assertEqual(zones[0], {"id": "sala", "name": "Sala", "players": ["media_player.sala"],
                                        "main_tv": True, "iptv_channels": ["tv-sala"]})
            self.assertEqual(zones[1]["name"], "Bar")
            self.assertFalse(zones[1]["main_tv"])
            with self.assertRaises(RuntimeError):
                self.app.store.update({"zones": [{"name": "Sala"}, {"name": "sala"}]})
        finally:
            self.app.store.update({"zones": []})

    def test_sonos_volume_and_zone_music_stop_endpoints(self):
        self.calls.clear()
        self.request("/api/sonos/volume", "POST", {"entity_id": "media_player.sala", "volume": 140})
        self.assertIn(("media_player", "volume_set", {"entity_id": "media_player.sala", "volume_level": 1.0}), self.calls)
        self.calls.clear()
        self.request("/api/music/stop", "POST", {"players": ["media_player.bar"]})
        self.assertIn(("media_player", "media_stop", {"entity_id": ["media_player.bar"]}), self.calls)
        music = self.app.store.config["music"]
        saved_slots, saved_players = music.get("slots", []), music.get("players", [])
        music["players"], music["slots"] = [], [{"id": "s1", "players": ["media_player.terrazza"]}]
        try:
            self.calls.clear()
            self.request("/api/music/stop", "POST", {"all": True})
            self.assertIn(("media_player", "media_stop", {"entity_id": ["media_player.terrazza"]}), self.calls)
        finally:
            music["slots"], music["players"] = saved_slots, saved_players

    def test_music_start_can_test_a_single_saved_slot(self):
        music = self.app.store.config["music"]
        saved = music.get("slots", [])
        music["slots"] = [{"id": "pranzo", "content_id": "FV:2/1", "players": ["media_player.sala"]}]
        try:
            with mock.patch.object(self.app, "start_music") as start:
                self.request("/api/music/start", "POST", {"slot_id": "pranzo"})
            start.assert_called_once_with(music["slots"][0])
            with self.assertRaises(urllib.error.HTTPError):
                self.request("/api/music/start", "POST", {"slot_id": "inesistente"})
        finally:
            music["slots"] = saved

    def test_new_interface_has_all_mock_screens_and_uses_existing_api(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        # Tavole A–E del mock: Oggi, Palinsesto, Zone, Configurazione guidata, telefono.
        for page in ("function pageOggi()", "function pagePalinsesto()", "function pageZona()",
                     "function pageGuidata()", "function pageContenuti()", "function pageRegistro()",
                     "function pageImpostazioni()", "function pageStorico()", "class=\"tabbar\""):
            self.assertIn(page, html)
        # Percorsi relativi: funzionano anche dietro Home Assistant Ingress.
        for path in ("'api/state", "'api/config'", "'api/test/audio'", "'api/audio/stop'", "'api/audio/skip'",
                     "'api/audio/pause'", "'api/music/start'", "'api/music/stop'", "'api/music/refresh'",
                     "'api/sonos/volume'", "'api/iptv/rebuild'", "'api/upload/chunk?'", "`api/spots/history?", "api/spots/history.csv?"):
            self.assertIn(path, html)
        self.assertNotIn("fetch('/api", html)
        # Niente dialoghi bloccanti del browser: conferme dentro la pagina.
        self.assertNotIn("confirm(", html.replace("S.confirm", "").replace("'confirm-no'", ""))
        self.assertIn("beforeunload", html)
        self.assertIn("Modifiche non salvate", html)
        self.assertIn("Ungespeicherte Änderungen", html)
        for label in ("Ken Burns diagonale", "Diagonaler Ken-Burns-Effekt", "Panoramica verso l’alto", "Schwenk nach oben"):
            self.assertIn(label, html)

    def test_today_page_shows_real_sonos_state(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        # Fascia attiva ma Sonos fermi: la pagina lo dice e propone di riavviare la musica.
        self.assertIn("function musicStoppedNow()", html)
        self.assertIn("if(musicStoppedNow())list.push(", html)
        self.assertIn("const musicOn=(r.music_active&&musicPlayersNow().some(sonosPlaying))", html)
        # La TV in onda non fa risultare "Suona" una zona con i Sonos fermi.
        self.assertIn("const st=zoneSonosStatus(z),tv=zoneTvOnAir(z)", html)
        # Il conteggio usa i Sonos della musica, non quelli degli spot.
        self.assertIn("Lf('{0} di {1} Sonos',playingSonos.length,musicSonos.length)", html)
        for label in ("['TV in onda','TV läuft']", "['Riavvia la musica','Musik neu starten']",
                      "['Musica ferma: {0}','Musik gestoppt: {0}']"):
            self.assertIn(label, html)

    def test_spot_history_records_played_stopped_and_failed_spots(self):
        ad = "history-test.mp3"
        (self.app.MEDIA_DIR / ad).write_bytes(b"audio")
        self.app.store.update({"audio": {"players": ["media_player.sala"], "ads": [ad], "transition_seconds": 0}})
        before = len(self.app.spot_history.entries)
        try:
            with mock.patch.object(self.app, "probe_media_duration", return_value=21.5), \
                    mock.patch.object(self.app.time, "sleep"), \
                    mock.patch.object(self.app.audio_stop_event, "wait", return_value=False):
                self.app.play_audio(ad, manual=True)
            played = self.app.spot_history.entries[-1]
            self.assertEqual(len(self.app.spot_history.entries), before + 1)
            self.assertEqual((played["spot"], played["status"], played["trigger"], played["duration"]),
                             (ad, "played", "manual", 21.5))
            self.assertEqual(played["players"], ["media_player.sala"])
            # Uno spot fermato a metà resta nello storico come interrotto.
            with mock.patch.object(self.app, "probe_media_duration", return_value=21.5), \
                    mock.patch.object(self.app.time, "sleep"), \
                    mock.patch.object(self.app.audio_stop_event, "wait", return_value=True):
                self.app.play_audio(ad, manual=False)
            stopped = self.app.spot_history.entries[-1]
            self.assertEqual((stopped["status"], stopped["trigger"]), ("stopped", "automatic"))
            # Anche una prova non riuscita viene registrata, con il motivo.
            with mock.patch.object(self.app, "play_audio", side_effect=RuntimeError("Sonos spento")):
                self.request("/api/test/audio", "POST", {"name": ad})
                failed = None
                for _ in range(100):
                    failed = next((e for e in self.app.spot_history.entries if e.get("reason") == "Sonos spento"), None)
                    if failed:
                        break
                    self.app.time.sleep(0.02)
            self.assertEqual((failed["status"], failed["spot"], failed["trigger"]), ("failed", ad, "manual"))
            # Lo storico sopravvive a un riavvio dell'add-on.
            with self.app.spot_history.lock:  # attende la fine della scrittura su disco
                reloaded = {e["id"]: e for e in self.app.SpotHistory(self.app.SPOT_HISTORY_FILE).entries}
            for entry in (played, stopped, failed):
                self.assertEqual(reloaded.get(entry["id"]), entry)
        finally:
            (self.app.MEDIA_DIR / ad).unlink()

    def test_automatic_spot_failure_records_the_selected_spot(self):
        audio = self.app.store.config["audio"]
        saved = {key: audio.get(key) for key in ("enabled", "ads", "mode", "players", "schedule", "only_when_playing")}
        audio.update(enabled=True, ads=["rotazione.mp3"], mode="rotate", players=["media_player.sala"],
                     schedule=[], only_when_playing=False)
        scheduler = self.app.scheduler
        try:
            scheduler.audio_paused_until = 0.0
            scheduler.last_audio = self.app.time.monotonic() - 3600
            # Tutti i Sonos risultano non disponibili: lo spot è già stato scelto ma non parte.
            with mock.patch.object(self.app, "available_sonos_players", return_value=[]),                     mock.patch.object(scheduler, "audio_today", return_value=0):
                scheduler.audio_tick()
            failed = self.app.spot_history.entries[-1]
            self.assertEqual((failed["status"], failed["spot"], failed["trigger"]), ("failed", "rotazione.mp3", "automatic"))
            self.assertIn("Nessun altoparlante Sonos disponibile", failed["reason"])
        finally:
            audio.update(saved)
            scheduler.audio_index = 0

    def test_spot_history_api_filters_by_day_and_exports_csv(self):
        history = self.app.spot_history
        saved = list(history.entries)
        history.entries = [
            {"id": "a", "time": "2026-10-01T12:00:00+02:00", "spot": "uno.mp3", "players": ["media_player.sala"],
             "trigger": "automatic", "status": "played", "duration": 20.5, "reason": ""},
            {"id": "b", "time": "2026-10-02T13:30:00+02:00", "spot": "due.mp3", "players": ["media_player.bar"],
             "trigger": "manual", "status": "failed", "duration": None, "reason": "Sonos spento"},
        ]
        zones = self.app.store.config.get("zones", [])
        self.app.store.config["zones"] = [{"id": "sala", "name": "Sala", "players": ["media_player.sala"],
                                           "iptv_channels": [], "main_tv": False}]
        try:
            _, payload = self.request("/api/spots/history?from=2026-10-02&to=2026-10-02")
            self.assertEqual([item["id"] for item in payload["entries"]], ["b"])
            _, payload = self.request("/api/spots/history")
            self.assertEqual([item["id"] for item in payload["entries"]], ["b", "a"])
            with urllib.request.urlopen(self.base + "/api/spots/history.csv?from=2026-10-01&to=2026-10-31") as response:
                self.assertIn("attachment", response.headers.get("Content-Disposition"))
                text = response.read().decode("utf-8")
            lines = text.lstrip("﻿").splitlines()
            self.assertEqual(lines[0], "Data;Ora;Spot;Zone;Tipo;Durata (s);Esito;Note")
            self.assertEqual(lines[1], "2026-10-01;12:00:00;uno.mp3;Sala;Automatico;20,5;Trasmesso;")
            self.assertEqual(lines[2], "2026-10-02;13:30:00;due.mp3;bar;Prova manuale;;Non riuscito;Sonos spento")
        finally:
            history.entries = saved
            self.app.store.config["zones"] = zones

    def test_classic_interface_stays_available(self):
        request = urllib.request.Request(self.base + "/classic")
        with urllib.request.urlopen(request) as response:
            html = response.read().decode("utf-8")
        self.assertIn('id="audioPlayers"', html)

    def remote_server(self):
        """Porta per l'accesso da Internet, avviata solo per il test."""
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.app.RemoteAdminHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        saved = json.loads(json.dumps(self.app.store.config.get("remote_admin", {})))
        delay = self.app.REMOTE_FAIL_DELAY_SECONDS
        self.app.REMOTE_FAIL_DELAY_SECONDS = 0

        def restore():
            self.app.REMOTE_FAIL_DELAY_SECONDS = delay
            self.app.store.config["remote_admin"] = saved
            self.app.REMOTE_SESSIONS.clear()
            self.app.REMOTE_FAILURES.clear()

        self.addCleanup(restore)
        return httpd.server_address[1]

    def remote_call(self, port, path, method="GET", body=None, headers=None, form=None):
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = dict(headers or {})
        if form is not None:
            body = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            body = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    def remote_login(self, port, username, password, headers=None):
        response, data = self.remote_call(port, "/login", "POST", form={"username": username, "password": password},
                                          headers=headers)
        cookie = response.getheader("Set-Cookie") or ""
        token = cookie.split(";", 1)[0].split("=", 1)[1] if response.status == 303 and "=" in cookie else ""
        return response, data, token

    def test_remote_admin_requires_login_and_protects_the_panel(self):
        port = self.remote_server()
        self.app.store.update({"remote_admin": {"enabled": False, "users": []}})
        # Spento di serie: nessuna pagina del pannello, neppure il login.
        response, data = self.remote_call(port, "/")
        self.assertEqual(response.status, 403)
        self.assertIn("spento", data.decode())
        response, _ = self.remote_call(port, "/api/state")
        self.assertEqual(response.status, 403)

        self.app.store.update({"remote_admin": {"enabled": True, "public_url": "https://ads.esempio.com/",
                                                "users": [{"username": "Carellas", "password": "Pizza-Forno-2026"}]}})
        remote = self.app.store.config["remote_admin"]
        self.assertEqual(remote["public_url"], "https://ads.esempio.com")
        self.assertEqual(remote["users"][0]["username"], "carellas")
        self.assertTrue(remote["users"][0]["password_hash"].startswith("pbkdf2_sha256$"))
        self.assertNotIn("password", remote["users"][0])
        self.assertNotIn("Pizza-Forno-2026", self.app.CONFIG_FILE.read_text(encoding="utf-8"))

        # Senza login: le pagine portano al login, le API rispondono 401 e non cambiano nulla.
        response, _ = self.remote_call(port, "/")
        self.assertEqual((response.status, response.getheader("Location")), (303, "/login"))
        response, _ = self.remote_call(port, "/api/state")
        self.assertEqual(response.status, 401)
        language = self.app.store.config.get("language")
        response, _ = self.remote_call(port, "/api/config", "POST", body={"language": "de" if language == "it" else "it"})
        self.assertEqual(response.status, 401)
        self.assertEqual(self.app.store.config.get("language"), language)
        response, data = self.remote_call(port, "/login")
        self.assertEqual(response.status, 200)
        self.assertIn('name="password" type="password"', data.decode())
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
        self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))

        response, data, token = self.remote_login(port, "carellas", "password-sbagliata")
        self.assertEqual(response.status, 401)
        self.assertIn("errati", data.decode())
        self.assertEqual(token, "")
        self.assertIn("Accesso da Internet: nome utente o password errati", self.app.store.logs[0]["message"])

        response, _, token = self.remote_login(port, "CARELLAS", "Pizza-Forno-2026", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual((response.status, response.getheader("Location")), (303, "/"))
        cookie = response.getheader("Set-Cookie")
        for attribute in ("HttpOnly", "SameSite=Strict", "Secure", "Path=/"):
            self.assertIn(attribute, cookie)
        self.assertTrue(token)
        # Il server conserva solo l'impronta della sessione, non il codice del browser.
        self.assertNotIn(token, self.app.REMOTE_SESSIONS)
        session = {"Cookie": f"carellas_admin={token}"}

        response, data = self.remote_call(port, "/api/state", headers=session)
        self.assertEqual(response.status, 200)
        state = json.loads(data)
        self.assertEqual(state["runtime"]["remote_user"], "carellas")
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
        response, data = self.remote_call(port, "/", headers=session)
        self.assertEqual(response.status, 200)
        self.assertIn("remoteSettings()", data.decode())
        # Sulla porta locale nessuno risulta entrato da Internet.
        self.assertIsNone(self.request("/api/state")[1]["runtime"]["remote_user"])

        # Moduli inviati da un altro sito: bloccati anche con la sessione valida.
        response, _ = self.remote_call(port, "/api/config", "POST", body={"language": language},
                                       headers={**session, "Origin": "https://sito-cattivo.example"})
        self.assertEqual(response.status, 403)
        response, _ = self.remote_call(port, "/api/config", "POST", body={"language": language},
                                       headers={**session, "Origin": "https://ads.esempio.com"})
        self.assertEqual(response.status, 200)

        # Esci: la sessione non vale più.
        response, _ = self.remote_call(port, "/logout", "POST", headers=session)
        self.assertEqual((response.status, response.getheader("Location")), (303, "/login"))
        self.assertIn("Max-Age=0", response.getheader("Set-Cookie"))
        response, _ = self.remote_call(port, "/api/state", headers=session)
        self.assertEqual(response.status, 401)

        # Password cambiata: chi era entrato con quella vecchia deve rientrare.
        _, _, token = self.remote_login(port, "carellas", "Pizza-Forno-2026")
        session = {"Cookie": f"carellas_admin={token}"}
        self.assertEqual(self.remote_call(port, "/api/state", headers=session)[0].status, 200)
        users = self.app.store.config["remote_admin"]["users"]
        self.app.store.update({"remote_admin": {"users": [{**users[0], "password": "Nuova-Password-99"}]}})
        self.assertEqual(self.remote_call(port, "/api/state", headers=session)[0].status, 401)
        # Uscita automatica dopo il periodo senza attività.
        _, _, token = self.remote_login(port, "carellas", "Nuova-Password-99")
        session = {"Cookie": f"carellas_admin={token}"}
        for item in self.app.REMOTE_SESSIONS.values():
            item["seen"] -= self.app.REMOTE_SESSION_IDLE_SECONDS + 1
        self.assertEqual(self.remote_call(port, "/api/state", headers=session)[0].status, 401)
        # Accesso spento: anche le sessioni aperte smettono di funzionare.
        _, _, token = self.remote_login(port, "carellas", "Nuova-Password-99")
        self.app.store.update({"remote_admin": {"enabled": False}})
        self.assertEqual(self.remote_call(port, "/api/state", headers={"Cookie": f"carellas_admin={token}"})[0].status, 403)

    def test_remote_admin_blocks_repeated_wrong_passwords(self):
        port = self.remote_server()
        self.app.store.update({"remote_admin": {"enabled": True,
                                                "users": [{"username": "carellas", "password": "Pizza-Forno-2026"}]}})
        attacker = {"CF-Connecting-IP": "203.0.113.7"}
        for _ in range(5):
            self.assertEqual(self.remote_login(port, "carellas", "sbagliata", headers=attacker)[0].status, 401)
        # Dopo 5 errori quell'indirizzo è bloccato, anche con la password giusta.
        response, data, token = self.remote_login(port, "carellas", "Pizza-Forno-2026", headers=attacker)
        self.assertEqual(response.status, 429)
        self.assertIn("15 minuti", data.decode())
        self.assertEqual(token, "")
        self.assertTrue(any("bloccato per 15 minuti" in item["message"] for item in self.app.store.logs[:3]))
        # Da un altro indirizzo si entra ancora.
        response, _, token = self.remote_login(port, "carellas", "Pizza-Forno-2026", headers={"CF-Connecting-IP": "198.51.100.2"})
        self.assertEqual(response.status, 303)
        # Tanti indirizzi diversi sullo stesso utente: dopo 10 errori si blocca anche l'utente.
        for number in range(10):
            self.remote_login(port, "carellas", "sbagliata", headers={"CF-Connecting-IP": f"192.0.2.{number + 1}"})
        response, _, _ = self.remote_login(port, "carellas", "Pizza-Forno-2026", headers={"CF-Connecting-IP": "198.51.100.3"})
        self.assertEqual(response.status, 429)

    def test_remote_admin_users_need_strong_passwords(self):
        saved = json.loads(json.dumps(self.app.store.config))
        try:
            cases = [
                ([{"username": "carellas", "password": "corta"}], "almeno 10 caratteri"),
                ([{"username": "carellas", "password": "carellas-2026"}], "non deve contenere il nome utente"),
                ([{"username": "carellas"}], "Imposta una password"),
                ([{"username": "x", "password": "Pizza-Forno-2026"}], "non valido"),
                ([{"username": "carellas", "password": "Pizza-Forno-2026"},
                  {"username": "Carellas", "password": "Pizza-Forno-2027"}], "duplicato"),
            ]
            for users, message in cases:
                with self.assertRaisesRegex(RuntimeError, message):
                    self.app.store.update({"remote_admin": {"enabled": True, "users": users}})
                self.app.store.config = json.loads(json.dumps(saved))
        finally:
            self.app.store.config = saved
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn("['remoto','Accesso da Internet']", html)
        self.assertIn('id="set-remoto"', html)
        self.assertIn('data-bind="remote_admin.public_url"', html)
        self.assertIn('<form method="post" action="logout"', html)
        self.assertIn("['Accesso da Internet','Zugang über das Internet']", html)
        config = (Path(__file__).parents[1] / "carellas_media_ads/config.yaml").read_text(encoding="utf-8")
        self.assertIn("8103/tcp: 8103", config)

    def test_website_event_dates(self):
        from datetime import date
        text = self.app.plain_text(
            '[et_pb_text _builder_version="4.18.1"]<p><strong>2</strong>4.12. geschlossen</p>'
            "<p>25.12. Mittags 11:30-15:00 Uhr</p><p>Preis 48,80€ · Tel. 09341-7030</p>[/et_pb_text]")
        self.assertEqual(self.app.event_dates(text, date(2026, 10, 6)), [date(2026, 12, 24), date(2026, 12, 25)])
        # Con l'anno scritto vale quello; senza anno, una data già passata da settimane è dell'anno dopo.
        self.assertEqual(self.app.event_dates("Freitag, 30.10.2026 / 17:30", date(2026, 4, 13)), [date(2026, 10, 30)])
        self.assertEqual(self.app.event_dates("Am 6.1. Dreikönig", date(2026, 12, 20)), [date(2027, 1, 6)])
        self.assertEqual(self.app.event_dates("Ab Herbst 2021 buchbar, 200 Personen", date(2021, 8, 6)), [])

    def test_website_events_on_tv_channel(self):
        from datetime import date
        saved_iptv = json.loads(json.dumps(self.app.store.config.get("iptv", {})))
        saved_social = json.loads(json.dumps(self.app.store.config.get("social", {})))
        posts = [
            {"id": 1050, "link": "https://carellas.de/events/italienischer-abend/", "modified": "2026-10-06T11:47:15",
             "title": {"rendered": "La Dolce Vita &#8211; Giro Pizza &#038; Pasta"},
             "content": {"rendered": '<p>Freitag, 30.10.2026 / 17:30 Uhr</p><a href="https://carellas.de/f/Flyer-2026.pdf">FLYER</a>'},
             "_embedded": {"wp:featuredmedia": [{"source_url": "https://carellas.de/f/Homepage.jpg"}]}},
            {"id": 1236, "link": "https://carellas.de/events/weihnachten/", "modified": "2026-10-06T11:46:45",
             "title": {"rendered": "Alles Rund um Weihnachten"},
             "content": {"rendered": "<p>25.12. Mittags Buffet</p>"},
             "_embedded": {"wp:featuredmedia": [{"source_url": "https://carellas.de/f/Weihnachten.jpg"}]}},
            {"id": 900, "link": "https://carellas.de/events/sommerfest/", "modified": "2026-06-01T10:00:00",
             "title": {"rendered": "Sommerfest"}, "content": {"rendered": "<p>Samstag, 20.06.2026</p>"}},
            {"id": 191, "link": "https://carellas.de/events/events-in-tauberbischofsheim/", "modified": "2021-08-06T14:00:53",
             "title": {"rendered": "Events in Tauberbischofsheim"}, "content": {"rendered": "<p>Ab Herbst 2021</p>"}},
        ]
        importer = self.app.WebsiteEventsImporter()
        calls = []

        def fake_get(url, params):
            calls.append((url, params))
            return [{"id": 11, "slug": "events"}] if url.endswith("/categories") else posts

        def fake_slides(event):
            return [f"Evento_{event['id']}_x_immagine.jpg"], True

        try:
            self.app.store.update({
                "iptv": {"channels": [{"id": "ingresso", "name": "TV Ingresso", "enabled": True, "playlist": [
                    {"name": "benvenuti.jpg", "kind": "image", "duration": 12},
                    {"name": "Instagram_1.mp4", "kind": "video", "fit": "smart"}]}]},
                "social": {"website_events": {"enabled": True, "channel_id": "ingresso", "seconds": 20}},
            })
            settings = self.app.events_settings()
            self.assertEqual((settings["url"], settings["category"], settings["keep"]), ("https://carellas.de", "events", 5))
            with mock.patch.object(importer, "_get", side_effect=fake_get):
                events = importer.fetch_events(settings, today=date(2026, 10, 16))
            self.assertEqual(calls[0][0], "https://carellas.de/wp-json/wp/v2/categories")
            self.assertEqual(calls[1][1]["categories"], 11)
            # Solo gli eventi futuri, dal più vicino; i titoli senza codici HTML.
            self.assertEqual([(e["id"], e["date"]) for e in events], [("1050", "2026-10-30"), ("1236", "2026-12-25")])
            self.assertEqual(events[0]["title"], "La Dolce Vita – Giro Pizza & Pasta")
            self.assertEqual(events[0]["flyer"], "https://carellas.de/f/Flyer-2026.pdf")
            self.assertEqual(events[1]["flyer"], "")

            with mock.patch.object(importer, "fetch_events", return_value=events), \
                    mock.patch.object(importer, "slides", side_effect=fake_slides), \
                    mock.patch.object(self.app.iptv, "build_async") as build:
                status = importer.check(force=True)
            self.assertEqual(status["last_error"], "")
            self.assertEqual([e["title"] for e in status["events"]], ["La Dolce Vita – Giro Pizza & Pasta", "Alles Rund um Weihnachten"])
            playlist = self.app.store.config["iptv"]["channels"][0]["playlist"]
            self.assertEqual([item["name"] for item in playlist],
                             ["benvenuti.jpg", "Instagram_1.mp4", "Evento_1050_x_immagine.jpg", "Evento_1236_x_immagine.jpg"])
            self.assertEqual(playlist[2], {"name": "Evento_1050_x_immagine.jpg", "kind": "image", "duration": 20,
                                           "fit": "smart", "effect": "fade"})
            build.assert_called_once_with("ingresso")
            # Evento passato: sparisce, e gli eventi restano nel loro punto del canale (niente rigenerazioni inutili).
            playlist.insert(2, playlist.pop(0))
            with mock.patch.object(importer, "fetch_events", return_value=events[1:]), \
                    mock.patch.object(importer, "slides", side_effect=fake_slides), \
                    mock.patch.object(self.app.iptv, "build_async"):
                importer.check(force=True)
            self.assertEqual([item["name"] for item in self.app.store.config["iptv"]["channels"][0]["playlist"]],
                             ["Instagram_1.mp4", "Evento_1236_x_immagine.jpg", "benvenuti.jpg"])
            with mock.patch.object(importer, "fetch_events", return_value=events[1:]), \
                    mock.patch.object(importer, "slides", side_effect=fake_slides), \
                    mock.patch.object(self.app.iptv, "build_async") as build:
                importer.check(force=True)
            build.assert_not_called()
            # Sito irraggiungibile: errore visibile, il canale non viene svuotato.
            with mock.patch.object(importer, "fetch_events", side_effect=RuntimeError("Sito non raggiungibile: timeout")):
                status = importer.check(force=True)
            self.assertEqual(status["last_error"], "Sito non raggiungibile: timeout")
            self.assertIn("Evento_1236_x_immagine.jpg",
                          [item["name"] for item in self.app.store.config["iptv"]["channels"][0]["playlist"]])
        finally:
            self.app.store.config["iptv"] = saved_iptv
            self.app.store.config["social"] = saved_social

    def test_website_event_slides_from_image_and_flyer(self):
        event = {"id": "1050", "title": "La Dolce Vita", "date": "2026-10-30", "modified": "2026-10-06T11:47:15",
                 "flyer": "https://carellas.de/f/Flyer.pdf", "image": "https://carellas.de/f/Homepage.jpg", "link": ""}
        importer = self.app.WebsiteEventsImporter()
        composed = []

        def fake_run(command, **kwargs):
            # pdftoppm finto: crea le due pagine del volantino.
            Path(command[-1] + "-1.png").write_bytes(b"p")
            Path(command[-1] + "-2.png").write_bytes(b"p")

        def fake_compose(pages, target):
            composed.append([page.name for page in pages])
            target.write_bytes(b"jpg")

        with mock.patch.object(self.app.shutil, "which", return_value="/usr/bin/pdftoppm"), \
                mock.patch.object(importer, "_download", side_effect=lambda url, target: target.write_bytes(b"x")), \
                mock.patch.object(self.app.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(self.app, "compose_event_slide", side_effect=fake_compose):
            names, created = importer.slides(event)
            again, created_again = importer.slides(event)
        try:
            self.assertTrue(created)
            self.assertEqual(len(names), 2)
            self.assertTrue(names[0].startswith("Evento_1050_") and names[0].endswith("_immagine.jpg"))
            self.assertTrue(names[1].endswith("_volantino.jpg"))
            self.assertEqual(composed, [["immagine.jpg"], ["pagina-1.png", "pagina-2.png"]])
            # La seconda volta i file ci sono già: niente nuovi download.
            self.assertEqual((again, created_again), (names, False))
            self.assertEqual(list(self.app.DATA_DIR.glob("evento-*")), [])
        finally:
            for name in names:
                (self.app.MEDIA_DIR / name).unlink(missing_ok=True)
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn('id="set-eventi"', html)
        self.assertIn('data-bind="social.website_events.channel_id"', html)
        self.assertIn("command('api/social/events/check',{})", html)
        self.assertIn("['Eventi dal sito','Events von der Website']", html)

    def test_side_menu_zones_in_dropdown(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        # Le zone del menu laterale stanno in una tendina chiusa di serie; il browser ricorda se è aperta.
        self.assertIn('data-act="side-zones" aria-expanded="${sideZonesOpen}"', html)
        self.assertIn("localStorage.getItem('carellas.sideZones')==='1'", html)
        self.assertIn("'side-zones':()=>{sideZonesOpen=!sideZonesOpen;", html)
        # Con la tendina chiusa resta visibile la zona aperta.
        self.assertIn("(cur?`<div class=\"zone-list\">${cur}</div>`:'')", html)
        self.assertIn("['{0} offline','{0} offline']", html)

    def test_play_now_counts_as_next_spot_and_never_overlaps(self):
        audio = self.app.store.config["audio"]
        saved = {key: audio.get(key) for key in ("enabled", "ads", "mode", "players", "schedule", "only_when_playing")}
        audio.update(enabled=True, ads=["primo.mp3", "secondo.mp3"], mode="rotate", players=["media_player.sala"],
                     schedule=[], only_when_playing=False)
        scheduler = self.app.scheduler
        started = []
        try:
            scheduler.audio_paused_until = 0.0
            scheduler.audio_index = 0
            scheduler.last_audio = self.app.time.monotonic() - 3600
            logs = len(self.app.store.logs)
            # Uno spot è già in onda: il programma aspetta, senza errore nel Registro né nello storico.
            self.app.audio_playback_lock.acquire()
            try:
                with mock.patch.object(self.app, "play_audio") as play,                         mock.patch.object(scheduler, "audio_today", return_value=0):
                    scheduler.audio_tick()
                play.assert_not_called()
                self.assertFalse(any(item["level"] == "error" for item in self.app.store.logs[:len(self.app.store.logs) - logs]))
                # Anche un secondo «Riproduci ora» viene rifiutato subito, con un messaggio chiaro.
                with self.assertRaises(urllib.error.HTTPError) as refused:
                    self.request("/api/test/audio", "POST", {"name": "primo.mp3", "next": True})
                self.assertEqual(refused.exception.code, 409)
                self.assertIn("già uno spot in onda", json.loads(refused.exception.read())["error"])
            finally:
                self.app.audio_playback_lock.release()
            # «Riproduci ora» trasmette lo spot in arrivo, la rotazione passa al successivo e l'intervallo riparte.
            done = threading.Event()

            def fake_play(name, manual, players):
                started.append((name, manual))
                done.set()

            with mock.patch.object(self.app, "play_audio", side_effect=fake_play):
                self.assertEqual(self.request("/api/test/audio", "POST", {"name": "primo.mp3", "next": True})[0], 200)
                self.assertTrue(done.wait(5))
            self.assertEqual(started, [("primo.mp3", True)])
            self.assertEqual(scheduler.peek_audio(audio["ads"], "rotate"), "secondo.mp3")
            self.assertLess(self.app.time.monotonic() - scheduler.last_audio, 5)
            with mock.patch.object(self.app, "play_audio") as play,                     mock.patch.object(scheduler, "audio_today", return_value=0):
                scheduler.audio_tick()
            play.assert_not_called()
            # Una prova di zona non cambia la rotazione.
            done.clear()
            with mock.patch.object(self.app, "play_audio", side_effect=fake_play):
                self.request("/api/test/audio", "POST", {"name": "primo.mp3", "players": ["media_player.sala"]})
                self.assertTrue(done.wait(5))
            self.assertEqual(scheduler.peek_audio(audio["ads"], "rotate"), "secondo.mp3")
        finally:
            audio.update(saved)
            scheduler.audio_index = 0
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn("'spot-now':()=>command('api/test/audio',{name:rt().next_spot||cfg().audio.ads?.[0],next:true}", html)
        self.assertIn('data-act="spot-test"', html)

    def test_schedule_blocks_are_easy_to_tell_apart(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        # Ogni fascia ha il suo colore, spazio tra i blocchi, nome e orario dentro il blocco e le linee delle ore.
        self.assertIn("const SLOT_COLORS=[", html)
        self.assertIn("""<span class="t">${esc(slotTitle(s))}</span><span class="h">${esc(s.start+'–'+slotEnd(s))}</span>""", html)
        self.assertIn("width:calc(${span(a,b)} - 4px);${slotColor(s)}", html)
        self.assertIn("""<div class="lane">${hourLines.join('')}""", html)
        self.assertIn("['Musica (un colore per fascia)','Musik (eine Farbe pro Zeitraum)']", html)

    def test_short_schedule_blocks_stay_readable(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        # Zoom automatico: la fascia più corta resta larga abbastanza; con − e + si cambia a mano.
        self.assertIn("const autoZoom=Math.max(1,Math.min(6,Math.ceil(90*(H1-H0)/(Math.max(5,shortest)*(S.laneBase||560)))));", html)
        self.assertIn("'zoom-auto':()=>{S.sel.zoom=null;render()}", html)
        # Le fasce brevi mostrano iniziali e ora e si allargano al passaggio o quando sono scelte.
        self.assertIn("narrow=(b-a)/(H1-H0)*zoom<0.1", html)
        self.assertIn(".lane .mblock.narrow:hover,.lane .mblock.narrow:focus-visible,.lane .mblock.narrow.sel{z-index:6;min-width:170px", html)
        self.assertIn("['Zoom automatico','Automatischer Zoom']", html)

    def test_slow_restore_after_spot_is_not_repeated(self):
        group = ["media_player.sala", "media_player.bar"]
        regrouped = [{"entity_id": p, "state": "playing", "attributes": {"group_members": group}} for p in group]
        split = [{"entity_id": p, "state": "paused", "attributes": {"group_members": [p]}} for p in group]
        calls = []

        def service(domain, name, payload):
            calls.append(name)
            if name == "restore":
                # Con 10 Sonos e la radio Home Assistant risponde dopo il tempo limite (ma completa il ripristino).
                raise TimeoutError("timed out")

        with mock.patch.object(self.app.ha, "service", side_effect=service),                 mock.patch.object(self.app.ha, "states", side_effect=[split, split, regrouped]) as states,                 mock.patch.object(self.app.time, "sleep"):
            self.app.restore_sonos_playback(["media_player.sala"], ["media_player.sala"], group, None, 0,
                                            groups={"media_player.sala": group})
        # Un solo ripristino (prima veniva ripetuto, raddoppiando il silenzio), poi la musica riparte.
        self.assertEqual(calls.count("restore"), 1)
        self.assertEqual(states.call_count, 3)
        self.assertEqual(calls[-1], "media_play")
        self.assertTrue(any("Ripristino Sonos lento" in item["message"] for item in self.app.store.logs[:3]))
        self.assertEqual(self.app.HA_SERVICE_TIMEOUT, 20)

    def test_volumes_chosen_by_hand_survive_slot_changes(self):
        app = self.app
        for store_ in (app.SONOS_MANUAL_VOLUMES, app.SONOS_LAST_SET, app.SONOS_LAST_SET_AT, app.SONOS_MANUAL_CANDIDATES, app.SONOS_VOLUME_MEMORY):
            store_.clear()
        players = ["media_player.sala", "media_player.terrazza", "media_player.bar"]
        states = [{"entity_id": p, "state": "playing", "attributes": {"group_members": players, "volume_level": 0.25}}
                  for p in players]
        slot = {"name": "RTL Radio", "players": players, "content_id": "FV:2/9", "content_type": "favorite_item_id",
                "volume": 25}
        try:
            # Cursore del pannello: la Sala a 35 per oggi.
            self.request("/api/sonos/volume", "POST", {"entity_id": "media_player.sala", "volume": 35})
            self.assertEqual(app.manual_sonos_volumes(players), {"media_player.sala": 0.35})
            self.assertIn("media_player.sala", app.SONOS_MANUAL_FILE.read_text(encoding="utf-8"))
            # Volume di ieri: non vale più.
            app.SONOS_MANUAL_VOLUMES["media_player.bar"] = (0.55, "2000-01-01")
            # Cambiato dall'app Sonos: la Terrazza resta a 40 per due controlli di fila.
            app.SONOS_LAST_SET["media_player.terrazza"] = 0.25
            self.assertEqual(app.detect_manual_sonos_volumes({"media_player.terrazza": 0.40}), [])
            self.assertEqual(app.detect_manual_sonos_volumes({"media_player.terrazza": 0.40}), [("media_player.terrazza", 0.40)])
            # Uno sbalzo isolato (es. stato non ancora aggiornato) non conta.
            app.SONOS_LAST_SET["media_player.bar"] = 0.25
            app.detect_manual_sonos_volumes({"media_player.bar": 0.01})
            app.detect_manual_sonos_volumes({"media_player.bar": 0.25})
            self.assertNotIn("media_player.bar", app.manual_sonos_volumes(players))
            # Subito dopo un cambio fatto dall'add-on (es. dissolvenza) i volumi letti non contano.
            app.SONOS_LAST_SET["media_player.bar"] = 0.25
            app.SONOS_LAST_SET_AT["media_player.bar"] = app.time.monotonic()
            app.detect_manual_sonos_volumes({"media_player.bar": 0.11})
            app.detect_manual_sonos_volumes({"media_player.bar": 0.11})
            self.assertNotIn("media_player.bar", app.manual_sonos_volumes(players))
            calls = []
            with mock.patch.object(app.ha, "states", return_value=states),                     mock.patch.object(app.ha, "service", side_effect=lambda d, n, p: calls.append((n, p))),                     mock.patch.object(app.time, "sleep"):
                app.start_music(slot, fade_in=True)
            final = {}
            for name, payload in calls:
                if name == "volume_set":
                    for player in payload["entity_id"]:
                        final[player] = payload["volume_level"]
            # Nuova fascia: Sala e Terrazza restano come scelte a mano, il Bar prende il volume della fascia.
            self.assertEqual(final, {"media_player.sala": 0.35, "media_player.terrazza": 0.4, "media_player.bar": 0.25})
            self.assertTrue(any("Volumi scelti a mano mantenuti" in item["message"] for item in app.store.logs[:3]))
        finally:
            for store_ in (app.SONOS_MANUAL_VOLUMES, app.SONOS_LAST_SET, app.SONOS_LAST_SET_AT, app.SONOS_MANUAL_CANDIDATES, app.SONOS_VOLUME_MEMORY):
                store_.clear()
            app.SONOS_MANUAL_FILE.unlink(missing_ok=True)
        source = (Path(__file__).parents[1] / "carellas_media_ads/app/server.py").read_text(encoding="utf-8")
        # Durante uno spot i volumi non vengono letti né scambiati per scelte a mano.
        self.assertIn("if not result[\"missing\"] and not audio_playback_lock.locked():", source)

    def _flaky_speaker(self, error="timed out", when=lambda name, targets: True):
        """Bar con un volume tutto suo che non risponde: gli altri due sono nello stesso gruppo."""
        app = self.app
        app.SONOS_UNRESPONSIVE.clear()
        app.SONOS_UNRESPONSIVE_STRIKES.clear()
        players = ["media_player.sala", "media_player.terrazza", "media_player.bar"]
        states = [{"entity_id": p, "state": "playing",
                   "attributes": {"group_members": players, "friendly_name": p.split(".")[1].title(),
                                  "volume_level": 0.5 if p.endswith("bar") else 0.3}} for p in players]
        calls = []

        def service(domain, name, payload):
            targets = payload.get("entity_id")
            targets = targets if isinstance(targets, list) else [targets]
            if "media_player.bar" in targets + list(payload.get("group_members") or []) and when(name, targets):
                raise TimeoutError(error) if error == "timed out" else RuntimeError(error)
            calls.append((name, payload))
        self.addCleanup(app.SONOS_UNRESPONSIVE.clear)
        self.addCleanup(app.SONOS_UNRESPONSIVE_STRIKES.clear)
        return players, states, calls, service

    def test_speaker_with_own_volume_does_not_block_the_others(self):
        app = self.app
        players, states, calls, service = self._flaky_speaker(
            "Home Assistant 500: Error on media_player.bar: HTTPConnectionPool(host='192.168.178.18', port=1400): "
            "Read timed out. (read timeout=9.5)")
        with mock.patch.object(app.ha, "service", side_effect=service):
            app.set_sonos_volume_levels({"media_player.sala": 0.3, "media_player.terrazza": 0.3, "media_player.bar": 0.5})
            self.assertTrue(app.sonos_unresponsive("media_player.bar"))
            before = len(calls)
            # Durante la pausa la cassa non viene neanche provata: niente più attese a ogni passo.
            app.set_sonos_volume_levels({"media_player.bar": 0.2, "media_player.sala": 0.2})
        self.assertEqual(calls[before:], [("volume_set", {"entity_id": ["media_player.sala"], "volume_level": 0.2})])
        # Tutte le casse insieme che falliscono possono essere Home Assistant: l'errore resta visibile.
        app.SONOS_UNRESPONSIVE.clear()
        with mock.patch.object(app.ha, "service", side_effect=TimeoutError("timed out")):
            with self.assertRaises(TimeoutError):
                app.set_sonos_volume_levels({"media_player.bar": 0.5})

    def test_volume_that_never_arrived_is_not_a_manual_choice(self):
        # Registro reale: la cassa dei bagni risultava «scelta a mano» a 17 perché il comando non le arrivava.
        app = self.app
        app.SONOS_UNRESPONSIVE.clear()
        self.addCleanup(app.SONOS_UNRESPONSIVE.clear)
        self.addCleanup(app.SONOS_UNRESPONSIVE_STRIKES.clear)
        player = "media_player.bar"
        with app.SONOS_VOLUME_MEMORY_LOCK:
            app.SONOS_LAST_SET[player] = 0.25
            app.SONOS_LAST_SET_AT[player] = -1e9
        app.mark_sonos_unresponsive(player, "timed out")
        with mock.patch.object(app, "set_manual_sonos_volume") as manual:
            app.detect_manual_sonos_volumes({player: 0.17})
            app.detect_manual_sonos_volumes({player: 0.17})
        manual.assert_not_called()

    def test_unresponsive_speaker_pause_grows_and_is_forgotten(self):
        app = self.app
        app.SONOS_UNRESPONSIVE.clear()
        app.SONOS_UNRESPONSIVE_STRIKES.clear()
        self.addCleanup(app.SONOS_UNRESPONSIVE.clear)
        self.addCleanup(app.SONOS_UNRESPONSIVE_STRIKES.clear)
        now = [1000.0]
        with mock.patch.object(app.time, "monotonic", side_effect=lambda: now[0]):
            for minutes in (3, 10, 30, 30):
                app.mark_sonos_unresponsive("media_player.bar", "timed out")
                self.assertEqual(app.SONOS_UNRESPONSIVE["media_player.bar"], now[0] + minutes * 60)
                self.assertIn(f"lo salto per {minutes} minuti", app.store.logs[0]["message"])
                # Già in pausa: nessuna riga in più nel Registro.
                count = len(app.store.logs)
                app.mark_sonos_unresponsive("media_player.bar", "timed out")
                self.assertEqual(len(app.store.logs), count)
                now[0] += minutes * 60 + 1
            # Un'ora senza errori: si ricomincia da 3 minuti.
            now[0] += app.SONOS_UNRESPONSIVE_FORGET_SECONDS + 1
            app.mark_sonos_unresponsive("media_player.bar", "timed out")
            self.assertEqual(app.SONOS_UNRESPONSIVE["media_player.bar"], now[0] + 180)

    def _spot(self, service, states, players):
        app = self.app
        ad = "Carellas_Ristorante_Spot_DE_Maschile.mp3"
        saved = json.loads(json.dumps(app.store.config["audio"]))
        self.addCleanup(lambda: app.store.config.__setitem__("audio", saved))
        app.store.update({"audio": {"players": players, "ads": [ad], "repeat_count": 1, "volume": 40,
                                    "transition_seconds": 2}})
        with mock.patch.object(app.ha, "states", return_value=states), \
                mock.patch.object(app.ha, "service", side_effect=service), \
                mock.patch.object(app, "probe_media_duration", return_value=10), \
                mock.patch.object(app.time, "sleep"), \
                mock.patch.object(app.audio_stop_event, "wait", return_value=False):
            return app.play_audio()

    def test_spot_goes_on_air_when_grouped_speaker_times_out(self):
        # Registro reale: «Spot audio non riuscito: timed out» a ogni giro per colpa di una sola cassa del gruppo.
        app = self.app
        players, states, calls, service = self._flaky_speaker()
        real = service

        def snapshot_touches_the_group(domain, name, payload):
            # Lo snapshot con il gruppo interroga anche i membri, compreso quello che non risponde.
            if name == "snapshot" and payload.get("with_group"):
                raise TimeoutError("timed out")
            return real(domain, name, payload)

        self.assertIsNotNone(self._spot(snapshot_touches_the_group, states, players))
        names = [name for name, _ in calls]
        self.assertEqual(names.count("play_media"), 1)
        self.assertTrue(app.sonos_unresponsive("media_player.bar"))
        snapshot = next(payload for name, payload in calls if name == "snapshot")
        restore = next(payload for name, payload in calls if name == "restore")
        self.assertFalse(snapshot["with_group"])
        self.assertFalse(restore["with_group"])
        self.assertNotIn("media_player.bar", snapshot["entity_id"])
        # Le casse sane non pagano per quella guasta, anche se lo snapshot del loro gruppo è fallito.
        self.assertFalse(app.sonos_unresponsive("media_player.sala"))
        self.assertFalse(app.sonos_unresponsive("media_player.terrazza"))
        # Il gruppo della musica va bene così: nessun distacco che lascerebbe silenzio prima dello spot.
        self.assertNotIn("unjoin", names)

    def test_spot_not_repeated_after_it_went_on_air(self):
        # Se l'errore arriva con lo spot già in onda, riprovare lo manderebbe due volte.
        app = self.app
        players, states, calls, service = self._flaky_speaker(when=lambda name, targets: False)
        real = service

        def fails_after_air(domain, name, payload):
            real(domain, name, payload)
            if name == "play_media":
                app.scheduler.record_audio_play()
                raise TimeoutError("timed out")

        with self.assertRaises(TimeoutError):
            self._spot(fails_after_air, states, players)
        self.assertEqual([name for name, _ in calls].count("play_media"), 1)

    def test_music_starts_when_speaker_with_own_volume_is_unreachable(self):
        # Registro reale: «Avvio musica non riuscito» e «Cambio sorgente annullato» per la cassa dei bagni.
        app = self.app
        players, states, calls, service = self._flaky_speaker(
            "Home Assistant 500: Error on media_player.bar: HTTPConnectionPool(host='192.168.178.18', port=1400): "
            "Max retries exceeded with url: /MediaRenderer/RenderingControl/Control",
            when=lambda name, targets: name == "volume_set")
        saved = dict(app.SONOS_MANUAL_VOLUMES)
        self.addCleanup(lambda: (app.SONOS_MANUAL_VOLUMES.clear(), app.SONOS_MANUAL_VOLUMES.update(saved)))
        with mock.patch.object(app.ha, "states", return_value=states), \
                mock.patch.object(app.ha, "service", side_effect=service), \
                mock.patch.object(app, "manual_sonos_volumes", return_value={"media_player.bar": 0.5}), \
                mock.patch.object(app, "ensure_sonos_playing", return_value=True), \
                mock.patch.object(app.time, "sleep"):
            result = app.start_music({"name": "Mix", "players": players, "content_id": "FV:2/9",
                                      "content_type": "favorite_item_id", "volume": 25})
        self.assertEqual(result[0], "media_player.sala")
        self.assertIn("play_media", [name for name, _ in calls])
        self.assertTrue(app.sonos_unresponsive("media_player.bar"))

    def test_two_speakers_group_without_the_one_not_answering(self):
        app = self.app
        players, states, calls, service = self._flaky_speaker()
        pair = players[0], players[2]
        alone = [{"entity_id": p, "state": "playing",
                  "attributes": {"group_members": [p], "volume_level": 0.3}} for p in pair]
        with mock.patch.object(app.ha, "states", return_value=alone), \
                mock.patch.object(app.ha, "service", side_effect=service), mock.patch.object(app.time, "sleep"):
            self.assertEqual(app.prepare_sonos_group(list(pair)), "media_player.sala")
        self.assertTrue(app.sonos_unresponsive("media_player.bar"))
        self.assertFalse(app.sonos_unresponsive("media_player.sala"))

    def test_music_resumes_when_group_restore_cannot_reach_a_speaker(self):
        # Registro reale: «Ripristino Sonos non riuscito» per la cassa dei bagni, e la musica restava ferma.
        app = self.app
        players, states, calls, service = self._flaky_speaker(when=lambda name, targets: False)
        real = service

        def restore(domain, name, payload):
            if name == "restore" and payload.get("with_group"):
                raise RuntimeError("Home Assistant 500: Error on media_player.bar: Max retries exceeded with url")
            return real(domain, name, payload)

        with mock.patch.object(app.ha, "states", return_value=states), \
                mock.patch.object(app.ha, "service", side_effect=restore), mock.patch.object(app.time, "sleep"):
            app.restore_sonos_playback(["media_player.sala"], ["media_player.sala"], players)
            self.assertTrue(app.wait_sonos_groups({"media_player.sala": players}, seconds=1))
        names = [name for name, _ in calls]
        self.assertFalse(next(payload for name, payload in calls if name == "restore")["with_group"])
        self.assertIn("media_play", names)
        self.assertTrue(app.sonos_unresponsive("media_player.bar"))

    def test_ignored_speaker_is_logged_once(self):
        # Registro reale: la stessa riga ogni 5 secondi per una cassa che non è un Sonos.
        app = self.app
        app.SONOS_IGNORED_LOGGED.clear()
        self.addCleanup(app.SONOS_IGNORED_LOGGED.clear)
        states = [{"entity_id": "media_player.sala", "state": "idle", "attributes": {"group_members": ["media_player.sala"]}},
                  {"entity_id": "media_player.terasse_2", "state": "idle", "attributes": {}}]
        wanted = ["media_player.sala", "media_player.terasse_2"]
        now = [5000.0]

        def lines():
            return sum("terasse_2" in item["message"] for item in app.store.logs)

        start = lines()
        with mock.patch.object(app.ha, "states", return_value=states), \
                mock.patch.object(app.time, "monotonic", side_effect=lambda: now[0]):
            for _ in range(5):
                self.assertEqual(app.available_sonos_players(wanted), ["media_player.sala"])
                now[0] += 5
            self.assertEqual(lines(), start + 1)
            # Passata un'ora lo si ricorda una volta.
            now[0] += app.SONOS_IGNORED_LOG_SECONDS
            app.available_sonos_players(wanted)
            self.assertEqual(lines(), start + 2)

    def test_music_on_outside_schedule_is_not_an_error(self):
        app = self.app
        before = sum("Fuori programma" in item["message"] for item in app.store.logs)
        with mock.patch.object(app.scheduler, "current_slot_key", return_value=None):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request("/api/music/manual", "POST", {"action": "on"})
            self.assertEqual(caught.exception.code, 409)
            self.assertIn("Fuori programma", json.loads(caught.exception.read())["error"])
            caught.exception.close()
            self.assertEqual(sum("Fuori programma" in item["message"] for item in app.store.logs), before)
            _, state = self.request("/api/staff/state")
            self.assertIs(state["runtime"]["music_scheduled_now"], False)

    def test_switched_off_sonos_does_not_stop_the_others(self):
        app = self.app
        app.SONOS_UNRESPONSIVE.clear()
        ad = "Carellas_Ristorante_Spot_DE_Maschile.mp3"
        players = ["media_player.sala", "media_player.terrazza", "media_player.bar"]
        # Il Bar è spento ma Home Assistant lo mostra ancora «playing».
        states = [{"entity_id": p, "state": "playing", "attributes": {"group_members": [p], "volume_level": 0.3}}
                  for p in players]
        calls = []

        def service(domain, name, payload):
            targets = payload.get("entity_id")
            targets = targets if isinstance(targets, list) else [targets]
            if "media_player.bar" in targets + list(payload.get("group_members") or []):
                raise RuntimeError("Home Assistant 500: Unable to connect to 192.168.178.20 (media_player.bar)")
            calls.append((name, payload))

        app.store.update({"audio": {"players": players, "ads": [ad], "repeat_count": 1, "volume": 40,
                                    "transition_seconds": 2}})
        try:
            with mock.patch.object(app.ha, "states", return_value=states),                     mock.patch.object(app.ha, "service", side_effect=service),                     mock.patch.object(app, "probe_media_duration", return_value=10),                     mock.patch.object(app.time, "sleep"),                     mock.patch.object(app.audio_stop_event, "wait", return_value=False):
                app.play_audio(ad, manual=True)
            # Lo spot va sugli altri due Sonos e la musica riparte: il Bar viene saltato.
            names = [name for name, _ in calls]
            self.assertIn("play_media", names)
            self.assertIn("restore", names)
            self.assertTrue(app.sonos_unresponsive("media_player.bar"))
            self.assertTrue(any("Il Sonos bar non risponde" in item["message"] for item in app.store.logs[:30]))
            with mock.patch.object(app.ha, "states", return_value=states):
                self.assertEqual(app.available_sonos_players(players), ["media_player.sala", "media_player.terrazza"])

            # Si spegne il Sonos che guidava il gruppo della musica: la musica riparte sugli altri.
            engine = app.Scheduler()
            slot = {"id": "radio", "name": "RTL Radio", "players": players, "content_id": "FV:2/9",
                    "content_type": "favorite_item_id", "volume": 25}
            original = json.loads(json.dumps(app.store.config["music"]))
            app.store.config["music"] = {"enabled": True, "players": players, "slots": [slot], "schedule": [],
                                         "stop_at_end": True, "volume": 25}
            engine.music_slot_key = None
            gone = [{"entity_id": "media_player.sala", "state": "unavailable", "attributes": {}}] + [
                {"entity_id": p, "state": "paused", "attributes": {"group_members": [p], "volume_level": 0.3}}
                for p in players[1:]]
            try:
                with mock.patch.object(app, "active_music_slot", return_value=slot),                         mock.patch.object(app, "start_music", return_value=players) as start,                         mock.patch.object(app.time, "sleep"):
                    engine.music_tick()
                    engine.music_coordinator = "media_player.sala"
                    engine.music_group_check_at = 0
                    with mock.patch.object(app.ha, "states", return_value=gone),                             mock.patch.object(app.ha, "service"):
                        engine.music_tick()
                self.assertEqual(start.call_count, 2)
                self.assertTrue(any("che guidava il gruppo non risponde" in item["message"] for item in app.store.logs[:10]))
            finally:
                app.store.config["music"] = original
        finally:
            app.SONOS_UNRESPONSIVE.clear()

    def test_automatic_mix_is_never_the_same(self):
        from datetime import date, timedelta
        app = self.app
        music = app.store.config["music"]
        saved = json.loads(json.dumps(music))
        sources = [
            {"content_id": "FV:2/1", "content_type": "favorite_item_id", "name": "Pranzo italiano", "kind": "playlist"},
            {"content_id": "FV:2/2", "content_type": "favorite_item_id", "name": "Dinner Jazz", "kind": "playlist"},
            {"content_id": "FV:2/3", "content_type": "favorite_item_id", "name": "Aperitivo Lounge", "kind": "playlist"},
            {"content_id": "http://kiss", "content_type": "radio_url", "name": "Radio Kiss Kiss Italia", "kind": "radio"},
            {"content_id": "FV:2/9", "content_type": "favorite_item_id", "name": "RTL 102.5", "kind": "radio"},
        ]
        try:
            music["schedule"] = [{"days": [0, 1, 2, 3, 4, 5], "start": "10:00", "end": "23:00", "enabled": True}]
            music["slots"] = [{"id": "x", "players": ["media_player.sala", "media_player.bar"], "start": "10:00",
                               "duration_minutes": 60, "days": [0], "content_id": "FV:2/1"}]
            music["mix"] = {"enabled": True, "block_minutes": 60, "sources": sources, "players": [], "volume": 30}
            start = date(2026, 10, 1)
            sequences, previous = set(), None
            for offset in range(45):
                day = start + timedelta(days=offset)
                plan = app.mix_plan(day)
                if day.weekday() == 6:
                    # Domenica chiuso: nessun blocco; il lunedì evita le sorgenti del sabato.
                    self.assertEqual(plan, [])
                    continue
                # Tutto l'orario di apertura, a blocchi di un'ora.
                self.assertEqual((plan[0]["start"], plan[-1]["end"], len(plan)), (600, 1380, 13))
                for one, two in zip(plan, plan[1:]):
                    self.assertNotEqual(one["content_id"], two["content_id"])
                    self.assertNotEqual(one["kind"], two["kind"])  # radio e playlist si alternano
                if previous:
                    for block in plan:
                        same = [p for p in previous if p["start"] == block["start"]]
                        if same:
                            self.assertNotEqual(same[0]["content_id"], block["content_id"])
                sequence = tuple(block["content_id"] for block in plan)
                self.assertNotIn(sequence, sequences)
                sequences.add(sequence)
                previous = plan
            # Sempre lo stesso mix per lo stesso giorno (anteprima = quello che suona).
            app.MIX_CACHE["signature"] = None
            self.assertEqual(app.mix_plan(start + timedelta(days=10)), app.mix_plan(start + timedelta(days=10)))
            # La fascia in onda viene dal mix, con le casse delle fasce se il mix non ne ha.
            moment = datetime(2026, 10, 14, 15, 30).astimezone()
            slot = app.active_mix_slot(moment)
            block = next(b for b in app.mix_plan(moment.date()) if b["start"] <= 930 < b["end"])
            self.assertEqual((slot["content_id"], slot["players"], slot["volume"]),
                             (block["content_id"], ["media_player.sala", "media_player.bar"], 30))
            self.assertEqual(slot["id"], "mix-2026-10-14-900")
            self.assertEqual(len(app.mix_week(moment.date())), 7)
            # Con meno di due sorgenti il mix resta spento.
            music["mix"]["sources"] = sources[:1]
            self.assertIsNone(app.active_mix_slot(moment))
        finally:
            app.store.config["music"] = saved
            app.MIX_CACHE["signature"] = None
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn("const editor=S.sel.mix?mixEditor():", html)
        self.assertIn("${mixOn?mixBlocks(d):''}", html)
        self.assertIn("['Mix automatico','Automatischer Mix']", html)

    def test_main_speaker_leads_the_group_and_dead_member_is_skipped(self):
        app = self.app
        app.SONOS_UNRESPONSIVE.clear()
        music = app.store.config["music"]
        saved_leader = music.get("leader")
        players = ["media_player.terrazza", "media_player.sala", "media_player.bar"]
        states = [{"entity_id": p, "state": "playing", "attributes": {"group_members": [p], "volume_level": 0.3}}
                  for p in players]
        calls = []
        try:
            # La Sala è la cassa principale: guida il gruppo anche se nella fascia è la seconda.
            music["leader"] = "media_player.sala"
            with mock.patch.object(app.ha, "states", return_value=states),                     mock.patch.object(app.ha, "service", side_effect=lambda d, n, p: calls.append((n, p))),                     mock.patch.object(app.time, "sleep"):
                result = app.start_music({"name": "RTL", "players": players, "content_id": "FV:2/9",
                                          "content_type": "favorite_item_id", "volume": 25})
            self.assertEqual(result[0], "media_player.sala")
            join = next(p for n, p in calls if n == "join")
            self.assertEqual(join["entity_id"], "media_player.sala")
            play = next(p for n, p in calls if n == "play_media")
            self.assertEqual(play["entity_id"], "media_player.sala")
            # Il Bar è appena stato spento (HA lo mostra ancora acceso): il join fallisce, viene saltato
            # per qualche minuto e il controllo successivo non riprova (prima si bloccava ogni volta).
            group = [{"entity_id": "media_player.sala", "state": "playing",
                      "attributes": {"group_members": ["media_player.sala", "media_player.terrazza"]}},
                     {"entity_id": "media_player.terrazza", "state": "playing",
                      "attributes": {"group_members": ["media_player.sala", "media_player.terrazza"]}},
                     {"entity_id": "media_player.bar", "state": "playing", "attributes": {"group_members": ["media_player.bar"]}}]
            attempts = []

            def service(domain, name, payload):
                attempts.append(name)
                if name == "join":
                    raise RuntimeError("Home Assistant 500: Unable to connect to 192.168.178.20")

            with mock.patch.object(app.ha, "states", return_value=group),                     mock.patch.object(app.ha, "service", side_effect=service), mock.patch.object(app.time, "sleep"):
                first = app.repair_sonos_group(players, "media_player.sala")
                second = app.repair_sonos_group(players, "media_player.sala")
            self.assertEqual(first["missing"], [])
            self.assertIn("media_player.bar", first["unavailable"])
            self.assertEqual(attempts.count("join"), 1)
            self.assertNotIn("media_play", attempts)
            self.assertIn("media_player.bar", second["unavailable"])
            self.assertEqual(app.SONOS_GROUP_CHECK_SECONDS, 8)
        finally:
            music["leader"] = saved_leader
            app.SONOS_UNRESPONSIVE.clear()
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn('data-bind="music.leader"', html)
        self.assertIn("['Cassa principale (guida il gruppo)','Hauptlautsprecher (führt die Gruppe)']", html)

    def test_today_timeline_is_as_clear_as_the_schedule(self):
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        # Oggi → «La giornata»: blocchi colorati con nome e orario, zoom automatico, mix e ora attuale.
        self.assertIn("const todayBlocks=mixOnNow()?mixToday():slotsOn(day);", html)
        self.assertIn("const tlZoom=Math.max(1,Math.min(6,Math.ceil(90*(h1-h0)/(Math.max(5,shortestToday)*(S.tlBase||560)))));", html)
        self.assertIn("""<div class="tl-block music ${narrow?'narrow':''} """, html)
        self.assertIn("tw.onscroll=()=>{S.tlScroll=tw.scrollLeft}", html)
        self.assertIn("['Musica (mix)','Musik (Mix)']", html)

    def test_music_switched_on_and_off_by_hand(self):
        app = self.app
        original = json.loads(json.dumps(app.store.config["music"]))
        first = {"id": "pranzo", "name": "Pranzo", "players": ["media_player.sala"], "content_id": "FV:2/1",
                 "content_type": "favorite_item_id", "volume": 25, "start": "10:00", "duration_minutes": 240, "days": [0]}
        second = {**first, "id": "sera", "name": "Sera", "content_id": "FV:2/2", "start": "14:00"}
        engine = app.Scheduler()
        app.store.config["music"] = {"enabled": True, "players": ["media_player.sala"], "slots": [first, second],
                                     "schedule": [], "stop_at_end": True, "volume": 25, "transition_seconds": 0}
        try:
            current = {"slot": first}
            with mock.patch.object(app, "active_music_slot", side_effect=lambda *a, **k: current["slot"]),                     mock.patch.object(app, "start_music", return_value=["media_player.sala"]) as start,                     mock.patch.object(app, "stop_music") as stop, mock.patch.object(app.time, "sleep"):
                engine.music_tick()
                self.assertEqual(start.call_count, 1)
                # Spenta a mano durante la fascia: si ferma e il motore non la fa ripartire.
                engine.music_off()
                stop.assert_called_once()
                for _ in range(3):
                    engine.music_tick()
                self.assertEqual(start.call_count, 1)
                self.assertEqual(engine.music_override["mode"], "off")
                # Inizia la fascia dopo (o il blocco successivo del Mix): resta spenta finché non la riaccendi.
                current["slot"] = second
                engine.music_tick()
                engine.music_tick()
                self.assertEqual(start.call_count, 1)
                self.assertEqual(engine.music_override["mode"], "off")
                # Riaccesa a mano: riparte la fascia in corso.
                engine.music_on()
                engine.music_tick()
                self.assertEqual(start.call_count, 2)
                self.assertIsNone(engine.music_override)
                # Spenta ieri: oggi il programma riparte da solo.
                engine.music_off()
                engine.music_override["day"] = "2000-01-01"
                engine.music_tick()
                self.assertEqual(start.call_count, 3)
                self.assertIsNone(engine.music_override)
                # Fuori programma: senza scelta non parte niente, con una radio scelta sì e resta accesa.
                current["slot"] = None
                engine.music_tick()
                with self.assertRaisesRegex(RuntimeError, "scegli cosa suonare"):
                    engine.music_on()
                stop.reset_mock()
                engine.music_on({"name": "Radio Kiss Kiss Italia", "content_id": "http://kiss", "content_type": "radio_url",
                                 "players": ["media_player.sala"], "volume": 30})
                self.assertEqual(start.call_args.args[0]["content_id"], "http://kiss")
                engine.music_tick()
                engine.music_tick()
                stop.assert_not_called()
                self.assertEqual(engine.music_override["mode"], "on")
                # Spenta di nuovo a mano.
                engine.music_off()
                stop.assert_called_once()
        finally:
            app.store.config["music"] = original
        with mock.patch.object(app.scheduler, "music_off") as off:
            self.assertEqual(self.request("/api/music/manual", "POST", {"action": "off"})[0], 200)
        off.assert_called_once()
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn("'music-off':()=>command('api/music/manual',{action:'off'},'Musica spenta')", html)
        self.assertIn('data-pick="music-on"', html)
        self.assertIn("if(['off','paused'].includes(rt().music_override?.mode))return false;", html)
        # «Ferma tutto» spegne la musica a mano: il motore non la fa ripartire da solo.
        with mock.patch.object(app.scheduler, "music_off") as off:
            self.assertEqual(self.request("/api/music/stop", "POST", {"all": True})[0], 200)
        off.assert_called_once()

    def test_staff_tablet_page_can_only_manage_music_speakers_and_ads(self):
        app = self.app
        port = self.remote_server()
        app.store.update({"remote_admin": {"enabled": True, "users": [
            {"username": "carellas", "password": "Pizza-Forno-2026"},
            {"username": "sala", "password": "Tablet-Bistro-2026", "role": "staff"},
        ]}})
        self.assertEqual([u["role"] for u in app.store.config["remote_admin"]["users"]], ["admin", "staff"])
        _, _, token = self.remote_login(port, "sala", "Tablet-Bistro-2026")
        staff = {"Cookie": f"carellas_admin={token}"}
        # Dopo il login l'operatore vede la pagina per il tablet, non il pannello.
        response, data = self.remote_call(port, "/", headers=staff)
        self.assertEqual(response.status, 200)
        self.assertIn("api/staff/state", data.decode())
        self.assertNotIn("pageImpostazioni", data.decode())
        response, data = self.remote_call(port, "/api/staff/state", headers=staff)
        self.assertEqual(response.status, 200)
        text = data.decode()
        for secret in ("password_hash", "session_secret", "remote_admin", "browser_player", "iptv"):
            self.assertNotIn(secret, text)
        state = json.loads(text)
        self.assertIn("sonos", state)
        self.assertIn("next_spot", state["runtime"])
        # Tutto il resto è bloccato.
        for path in ("/api/state", "/classic", "/api/iptv/status"):
            self.assertEqual(self.remote_call(port, path, headers=staff)[0].status, 403)
        language = app.store.config.get("language")
        self.assertEqual(self.remote_call(port, "/api/config", "POST", body={"language": "de"}, headers=staff)[0].status, 403)
        self.assertEqual(app.store.config.get("language"), language)
        self.assertEqual(self.remote_call(port, "/api/media/x.mp3", "DELETE", headers=staff)[0].status, 403)
        # Musica, casse e pubblicità sì.
        self.assertEqual(self.remote_call(port, "/api/sonos/volume", "POST", body={"entity_id": "media_player.sala", "volume": 30},
                                          headers=staff)[0].status, 200)
        with mock.patch.object(app.scheduler, "music_off") as off:
            self.assertEqual(self.remote_call(port, "/api/music/manual", "POST", body={"action": "off"}, headers=staff)[0].status, 200)
        off.assert_called_once()
        with mock.patch.object(app.scheduler, "pause_audio", return_value=0) as pause:
            self.assertEqual(self.remote_call(port, "/api/audio/pause", "POST", body={"minutes": 30}, headers=staff)[0].status, 200)
        pause.assert_called_once_with(30)
        # L'amministratore vede ancora tutto; sulla rete locale la pagina c'è anche come anteprima.
        _, _, admin_token = self.remote_login(port, "carellas", "Pizza-Forno-2026")
        self.assertEqual(self.remote_call(port, "/api/state", headers={"Cookie": f"carellas_admin={admin_token}"})[0].status, 200)
        with urllib.request.urlopen(self.base + "/operatori") as response:
            self.assertIn("api/staff/state", response.read().decode())
        app.SONOS_MANUAL_VOLUMES.clear()
        app.SONOS_MANUAL_FILE.unlink(missing_ok=True)
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn("data-bind=\"${p}.role\"", html)

    def test_website_event_image_fills_the_screen(self):
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b"jpg")

        target = self.app.MEDIA_DIR / "Evento_test_immagine.jpg"
        try:
            for aspect in (2560 / 1439, 1.0):
                commands.clear()
                with mock.patch.object(self.app, "image_aspect", return_value=aspect),                         mock.patch.object(self.app.subprocess, "run", side_effect=fake_run):
                    self.app.compose_event_slide([Path("immagine.jpg")], target)
                filters = " ".join(commands[0])
                if aspect > 1.4:
                    # Immagine orizzontale del sito: riempie tutto lo schermo, senza bande sfocate.
                    self.assertIn("force_original_aspect_ratio=increase,crop=1920:1080", filters)
                    self.assertNotIn("boxblur", filters)
                else:
                    # Immagine quadrata: resta intera sopra lo sfondo sfocato.
                    self.assertIn("boxblur", filters)
                    self.assertIn("force_original_aspect_ratio=decrease", filters)
        finally:
            target.unlink(missing_ok=True)

    def test_operators_zone_music_spots_and_player_controls(self):
        app = self.app
        app.MUSIC_EXCLUDED.clear()
        saved_zones = json.loads(json.dumps(app.store.config.get("zones", [])))
        saved_audio = list(app.store.config["audio"].get("players", []))
        engine = app.Scheduler()
        players = ["media_player.sala", "media_player.terrazza", "media_player.bar"]
        engine.music_players, engine.music_coordinator, engine.music_active = list(players), "media_player.sala", True
        engine.music_expected_players = list(players)
        engine.music_selection = {"name": "Pranzo", "players": players, "content_id": "FV:2/1"}
        calls = []
        try:
            app.store.update({"zones": [{"id": "terrazza", "name": "Terrazza", "players": ["media_player.terrazza"]}],
                              "audio": {"players": players}})
            with mock.patch.object(app.ha, "service", side_effect=lambda d, n, p: calls.append((n, p))),                     mock.patch.object(app, "available_sonos_players", side_effect=lambda p: list(p)),                     mock.patch.object(app.time, "sleep"):
                # Musica tolta solo dalla Terrazza: esce dal gruppo e si ferma, le altre continuano.
                engine.zone_music(["media_player.terrazza"], False)
                self.assertIn(("unjoin", {"entity_id": ["media_player.terrazza"]}), calls)
                self.assertIn(("media_stop", {"entity_id": ["media_player.terrazza"]}), calls)
                self.assertEqual(app.music_excluded_players(), {"media_player.terrazza"})
                self.assertNotIn("media_player.terrazza", engine.music_expected_players)
                # La prossima fascia non la raggruppa.
                with mock.patch.object(app.ha, "states", return_value=[]), mock.patch.object(app, "prepare_sonos_group",
                                                                                              return_value="media_player.sala") as prep,                         mock.patch.object(app, "ensure_sonos_playing", return_value=True):
                    result = app.start_music({"name": "Sera", "players": players, "content_id": "FV:2/2",
                                              "content_type": "favorite_item_id", "volume": 25})
                self.assertNotIn("media_player.terrazza", prep.call_args.args[0])
                self.assertNotIn("media_player.terrazza", result)
                # Rimessa: rientra nel gruppo e riparte.
                calls.clear()
                engine.zone_music(["media_player.terrazza"], True)
                self.assertIn(("join", {"entity_id": "media_player.sala", "group_members": ["media_player.terrazza"]}), calls)
                self.assertEqual(app.music_excluded_players(), set())
                # Pausa: il motore non la fa ripartire; Play la riprende.
                calls.clear()
                engine.music_control("pause")
                self.assertIn(("media_pause", {"entity_id": "media_player.sala"}), calls)
                self.assertEqual(engine.music_override["mode"], "paused")
                engine.music_control("play")
                self.assertIsNone(engine.music_override)
                # Musica accesa a mano fuori programma: dopo Pausa e Play resta accesa a mano.
                engine.music_override = {"mode": "on", "slot_key": None, "name": "Radio"}
                engine.music_control("pause")
                engine.music_control("play")
                self.assertEqual(engine.music_override["mode"], "on")
                engine.music_override = None
                engine.music_control("next")
                self.assertIn(("media_next_track", {"entity_id": "media_player.sala"}), calls)

            def radio(domain, name, payload):
                raise RuntimeError("Home Assistant 500: not supported")

            with mock.patch.object(app.ha, "service", side_effect=radio):
                with self.assertRaisesRegex(RuntimeError, "non ha brani da saltare"):
                    engine.music_control("next")
            # Spot sì/no per zona dal tablet.
            self.assertEqual(self.request("/api/zone/spots", "POST", {"zone_id": "terrazza", "on": False})[0], 200)
            self.assertNotIn("media_player.terrazza", app.store.config["audio"]["players"])
            self.assertEqual(self.request("/api/zone/spots", "POST", {"zone_id": "terrazza", "on": True})[0], 200)
            self.assertIn("media_player.terrazza", app.store.config["audio"]["players"])
            self.assertTrue({"/api/zone/music", "/api/zone/spots", "/api/music/control"} <= app.STAFF_POST_PATHS)
        finally:
            app.MUSIC_EXCLUDED.clear()
            app.store.update({"zones": saved_zones, "audio": {"players": saved_audio}})
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/operatori.html").read_text(encoding="utf-8")
        for marker in ("data-zmusic=", "data-zspots=", "data-pvol=", 'data-ctl="next"'):
            self.assertIn(marker, html)
        # Stessa grafica di «Ora in onda» e scelta della lingua IT/DE (ricordata sul tablet).
        for marker in ('<section class="card now">', "L('Ora in onda')", 'data-lang="de"', "localStorage.setItem('carellas.oplang',S.lang)",
                       "'Ora in onda':'Jetzt läuft'", "function icon(name,size=18){"):
            self.assertIn(marker, html)
        # Come nel pannello: riepilogo (spot oggi, musica, prossimo cambio) e «La giornata».
        for marker in ("L('Spot oggi')", "L('Prossimo cambio')", "L('La giornata')", 'class="tl-block music', "data-tl-wrap"):
            self.assertIn(marker, html)

    def test_now_playing_shows_current_track_and_artist(self):
        # «Ora in onda» mostra il brano che sta suonando, con l'artista.
        states = [{"entity_id": "media_player.sala", "state": "playing", "attributes": {
            "friendly_name": "Sala", "group_members": ["media_player.sala"], "volume_level": 0.3,
            "media_title": "Albachiara", "media_artist": "Vasco Rossi"}}]
        with mock.patch.object(self.app.ha, "states", return_value=states):
            status, payload = self.request("/api/state")
        self.assertEqual(status, 200)
        sala = next(s for s in payload["sonos"] if s["entity_id"] == "media_player.sala")
        self.assertEqual((sala["media_title"], sala["media_artist"]), ("Albachiara", "Vasco Rossi"))
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn("function nowTrack(){", html)
        self.assertIn('class="now-track"', html)
        self.assertIn("['Brano in onda','Titel läuft']", html)
        staff = (Path(__file__).parents[1] / "carellas_media_ads/app/operatori.html").read_text(encoding="utf-8")
        self.assertIn("(c.media_artist?' — '+c.media_artist:'')", staff)

    def test_playlist_tracks_are_listed_and_can_be_played(self):
        # Elenco dei brani della playlist in onda (coda dei Sonos), con il brano in corso; toccandone uno parte.
        app = self.app
        states = [{"entity_id": "media_player.sala", "state": "playing", "attributes": {
            "group_members": ["media_player.sala"], "queue_position": 1, "queue_size": 3, "media_title": "Vita spericolata"}}]
        # Come lo restituisce Home Assistant (sonos.get_queue): media_title, media_artist, media_album_name.
        queue = {"media_player.sala": [{"media_title": "Albachiara", "media_artist": "Vasco Rossi", "media_album_name": "Best"},
                                       {"media_title": "Vita spericolata", "media_artist": "Vasco Rossi"},
                                       {"media_title": "Sally"}]}
        calls = []
        with mock.patch.object(app.ha, "states", return_value=states), \
                mock.patch.object(app.ha, "service_response", return_value=queue) as get_queue, \
                mock.patch.object(app.ha, "service", side_effect=lambda d, n, p: calls.append((d, n, p))), \
                mock.patch.object(app.scheduler, "music_coordinator", "media_player.sala"):
            status, payload = self.request("/api/music/queue")
            self.assertEqual(status, 200)
            get_queue.assert_called_once_with("sonos", "get_queue", {"entity_id": "media_player.sala"})
            self.assertEqual([t["title"] for t in payload["tracks"]], ["Albachiara", "Vita spericolata", "Sally"])
            self.assertEqual(payload["position"], 1)
            self.assertEqual((payload["tracks"][0]["artist"], payload["tracks"][0]["album"]), ("Vasco Rossi", "Best"))
            status, _ = self.request("/api/music/queue/play", "POST", {"index": 2})
            self.assertEqual(status, 200)
        self.assertIn(("sonos", "play_queue", {"entity_id": "media_player.sala", "queue_position": 2}), calls)
        # Home Assistant troppo vecchio: lo dice invece di dare errore.
        with mock.patch.object(app.ha, "states", return_value=states), \
                mock.patch.object(app.ha, "service_response", side_effect=RuntimeError("Home Assistant 400")), \
                mock.patch.object(app.scheduler, "music_coordinator", "media_player.sala"):
            status, payload = self.request("/api/music/queue")
        self.assertEqual(status, 200)
        self.assertIn("2024.12", payload["error"])
        # Anche gli operatori vedono l'elenco e possono scegliere un brano.
        self.assertIn("/api/music/queue", app.STAFF_GET_PATHS)
        self.assertIn("/api/music/queue/play", app.STAFF_POST_PATHS)
        for page in ("index.html", "operatori.html"):
            html = (Path(__file__).parents[1] / "carellas_media_ads/app" / page).read_text(encoding="utf-8")
            self.assertIn("function queueCard(){", html, page)
            self.assertIn('data-act="queue-toggle"', html, page)
            self.assertIn("api/music/queue/play", html, page)

    def test_playlist_tracks_when_leading_sonos_does_not_answer(self):
        # Il Sonos che guidava il gruppo è scollegato: si legge la coda da un altro e, se nessuno risponde,
        # il messaggio dice che la cassa non risponde (non che Home Assistant è vecchio).
        app = self.app
        offline = "HTTPConnectionPool(host='192.168.178.12', port=1400): Max retries exceeded (NewConnectionError)"
        states = [
            {"entity_id": "media_player.wein", "state": "playing", "attributes": {
                "friendly_name": "Restaurant auf Weinkühlschrank", "group_members": ["media_player.wein"],
                "queue_position": 0}},
            {"entity_id": "media_player.sala", "state": "playing", "attributes": {
                "friendly_name": "Sala", "group_members": ["media_player.sala"], "queue_position": 1}}]

        def get_queue(domain, service, payload):
            if payload["entity_id"] == "media_player.wein":
                raise RuntimeError("Home Assistant 500: " + offline)
            return {"media_player.sala": [{"media_title": "Albachiara"}, {"media_title": "Sally"}]}

        with mock.patch.object(app.ha, "states", return_value=states), \
                mock.patch.object(app.ha, "service_response", side_effect=get_queue), \
                mock.patch.object(app.scheduler, "music_coordinator", "media_player.wein"):
            payload = app.music_queue()
        self.assertEqual(payload["coordinator"], "media_player.sala")
        self.assertEqual([t["title"] for t in payload["tracks"]], ["Albachiara", "Sally"])
        self.assertEqual(payload["position"], 1)
        with mock.patch.object(app.ha, "states", return_value=states[:1]), \
                mock.patch.object(app.ha, "service_response", side_effect=get_queue), \
                mock.patch.object(app.scheduler, "music_coordinator", "media_player.wein"):
            payload = app.music_queue()
        self.assertIn("«Restaurant auf Weinkühlschrank» non risponde", payload["error"])
        self.assertNotIn("2024.12", payload["error"])

    def test_now_playing_shows_real_sonos_volume(self):
        # «Ora in onda» mostra il volume letto dai Sonos, non quello della fascia, e dice se è scelto a mano.
        app = self.app
        app.SONOS_MANUAL_VOLUMES.clear()
        try:
            app.SONOS_MANUAL_VOLUMES["media_player.sala"] = (0.25, datetime.now().date().isoformat())
            app.SONOS_MANUAL_VOLUMES["media_player.bar"] = (0.55, "2000-01-01")
            status, payload = self.request("/api/state")
            self.assertEqual(status, 200)
            self.assertEqual(payload["runtime"]["manual_volumes"], {"media_player.sala": 25})
            status, payload = self.request("/api/staff/state")
            self.assertEqual(payload["runtime"]["manual_volumes"], {"media_player.sala": 25})
        finally:
            app.SONOS_MANUAL_VOLUMES.clear()
        for page in ("index.html", "operatori.html"):
            html = (Path(__file__).parents[1] / "carellas_media_ads/app" / page).read_text(encoding="utf-8")
            self.assertIn("function musicVolumeLabel(players,planned)", html, page)
            self.assertIn("musicVolumeLabel(slot.players,", html, page)
            self.assertIn("'scelto a mano, fascia {0}'", html, page)

    def test_operators_remove_spots_only_for_today(self):
        app = self.app
        app.SPOTS_EXCLUDED.clear()
        port = self.remote_server()
        saved_zones = json.loads(json.dumps(app.store.config.get("zones", [])))
        saved_audio = json.loads(json.dumps(app.store.config["audio"]))
        players = ["media_player.sala", "media_player.terrazza"]
        try:
            app.store.update({"zones": [{"id": "terrazza", "name": "Terrazza", "players": ["media_player.terrazza"]}],
                              "audio": {"players": players},
                              "remote_admin": {"enabled": True, "users": [
                                  {"username": "bistro", "password": "Pizza-Sera-2026", "role": "staff"}]}})
            _, _, token = self.remote_login(port, "bistro", "Pizza-Sera-2026")
            staff = {"Cookie": f"carellas_admin={token}"}
            # Anche se l'operatore non lo chiede, dal tablet vale solo per oggi.
            response, _ = self.remote_call(port, "/api/zone/spots", "POST", body={"zone_id": "terrazza", "on": False}, headers=staff)
            self.assertEqual(response.status, 200)
            self.assertEqual(app.store.config["audio"]["players"], players)  # configurazione invariata
            self.assertEqual(app.spots_excluded_players(), {"media_player.terrazza"})
            self.assertTrue(any("Spot tolti per oggi nella zona Terrazza da bistro" in item["message"] for item in app.store.logs[:3]))
            # Lo spot automatico va solo in Sala.
            with mock.patch.object(app, "available_sonos_players", side_effect=lambda p: list(p)) as available,                     mock.patch.object(app, "audio_playback_lock") as lock:
                lock.acquire.return_value = False
                with self.assertRaises(RuntimeError):
                    app.play_audio("spot.mp3", manual=True)
            self.assertEqual(available.call_args_list[0].args[0], ["media_player.sala"])
            # Domani la zona torna come l'ha impostata l'amministratore.
            app.SPOTS_EXCLUDED["media_player.terrazza"] = "2000-01-01"
            self.assertEqual(app.spots_excluded_players(), set())
            # Dal pannello amministrativo il cambio resta per sempre.
            self.assertEqual(self.request("/api/zone/spots", "POST", {"zone_id": "terrazza", "on": False})[0], 200)
            self.assertEqual(app.store.config["audio"]["players"], ["media_player.sala"])
        finally:
            app.SPOTS_EXCLUDED.clear()
            app.store.update({"zones": saved_zones, "audio": saved_audio})
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/operatori.html").read_text(encoding="utf-8")
        self.assertIn("on:!spotsOnIn(z),today:true", html)
        panel = (Path(__file__).parents[1] / "carellas_media_ads/app/index.html").read_text(encoding="utf-8")
        self.assertIn("Lf('Spot tolti per oggi: {0}'", panel)

    def test_operators_spot_page_history_and_log(self):
        app = self.app
        app.ADS_EXCLUDED.clear()
        port = self.remote_server()
        saved_audio = json.loads(json.dumps(app.store.config["audio"]))
        (app.MEDIA_DIR / "Spot_uno.mp3").write_bytes(b"ID3uno")
        (app.MEDIA_DIR / "Foto_menu.jpg").write_bytes(b"jpg")
        try:
            app.store.update({"audio": {"ads": ["Spot_uno.mp3", "Spot_due.mp3"], "mode": "rotate"},
                              "remote_admin": {"enabled": True, "users": [
                                  {"username": "bistro", "password": "Pizza-Sera-2026", "role": "staff"}]}})
            _, _, token = self.remote_login(port, "bistro", "Pizza-Sera-2026")
            staff = {"Cookie": f"carellas_admin={token}"}
            # Spot tolto dalla rotazione solo per oggi: la configurazione non cambia.
            response, _ = self.remote_call(port, "/api/spot/today", "POST", body={"name": "Spot_uno.mp3", "on": False},
                                           headers=staff)
            self.assertEqual(response.status, 200)
            self.assertEqual(app.store.config["audio"]["ads"], ["Spot_uno.mp3", "Spot_due.mp3"])
            self.assertEqual(app.active_ads(app.store.config["audio"]), ["Spot_due.mp3"])
            engine = app.Scheduler()
            for _ in range(3):
                self.assertEqual(engine.take_next_audio(), "Spot_due.mp3")
            state = json.loads(self.remote_call(port, "/api/staff/state", headers=staff)[1])
            self.assertEqual(state["runtime"]["ads_excluded"], ["Spot_uno.mp3"])
            self.assertEqual(state["runtime"]["next_spot"], "Spot_due.mp3")
            self.assertTrue(any("Spot tolto per oggi: Spot_uno.mp3 da bistro" in item["message"] for item in app.store.logs[:3]))
            # Uno spot che non è in rotazione non si tocca.
            response, _ = self.remote_call(port, "/api/spot/today", "POST", body={"name": "Altro.mp3", "on": False},
                                           headers=staff)
            self.assertGreaterEqual(response.status, 400)
            self.assertNotIn("Altro.mp3", app.ADS_EXCLUDED)
            # Rimesso; e comunque domani torna da solo.
            self.assertEqual(self.remote_call(port, "/api/spot/today", "POST", body={"name": "Spot_uno.mp3", "on": True},
                                              headers=staff)[0].status, 200)
            self.assertEqual(app.ads_excluded_today(), set())
            app.ADS_EXCLUDED["Spot_uno.mp3"] = "2000-01-01"
            self.assertEqual(app.active_ads(app.store.config["audio"]), ["Spot_uno.mp3", "Spot_due.mp3"])
            # Ascolto degli spot, storico e registro sì; foto e video no.
            response, data = self.remote_call(port, "/media/Spot_uno.mp3", headers=staff)
            self.assertEqual((response.status, data), (200, b"ID3uno"))
            self.assertEqual(self.remote_call(port, "/media/Foto_menu.jpg", headers=staff)[0].status, 403)
            self.assertEqual(self.remote_call(port, "/api/spots/history?from=2026-01-01&to=2026-01-02",
                                              headers=staff)[0].status, 200)
            app.store.log("info", "Canale TV Sala generato")
            app.store.log("info", "Spot trasmesso: Spot_due.mp3")
            state = json.loads(self.remote_call(port, "/api/staff/state", headers=staff)[1])
            messages = [entry["message"] for entry in state["logs"]]
            self.assertIn("Spot trasmesso: Spot_due.mp3", messages)
            self.assertNotIn("Canale TV Sala generato", messages)
            self.assertEqual([m["name"] for m in state["media"]], ["Spot_uno.mp3"])
        finally:
            app.ADS_EXCLUDED.clear()
            app.store.update({"audio": saved_audio})
            for name in ("Spot_uno.mp3", "Foto_menu.jpg"):
                (app.MEDIA_DIR / name).unlink(missing_ok=True)
        html = (Path(__file__).parents[1] / "carellas_media_ads/app/operatori.html").read_text(encoding="utf-8")
        # Menu come nel pannello: tutto su musica e spot, niente impostazioni né TV.
        for marker in ("['palinsesto','Palinsesto','cal']", "function pageSpot()", "function pageStorico()",
                       "function pageRegistro()", "data-spot-today=", "'Togli per oggi':'Heute herausnehmen'"):
            self.assertIn(marker, html)
        self.assertNotIn("pageImpostazioni", html)
        self.assertNotIn("['tv','TV']", html)

    def test_music_list_scrolls_and_zones_button_on_small_screens(self):
        base = Path(__file__).parents[1] / "carellas_media_ads/app"
        staff = (base / "operatori.html").read_text(encoding="utf-8")
        panel = (base / "index.html").read_text(encoding="utf-8")
        # La barra in basso copriva la fine della lista: il foglio deve starle sopra e scorrere al suo interno.
        self.assertIn(".overlay{position:fixed;inset:0;z-index:35;", staff)
        self.assertIn("overflow-y:auto;overscroll-behavior:contain", staff)
        self.assertIn('<div class="sheet-body list" data-sheet-body>', staff)
        self.assertIn("overflow-y:auto;overscroll-behavior:contain", panel)
        self.assertIn("view-pop.offsetHeight-16", panel)
        # Agli operatori gli interruttori valgono solo per oggi: nessuna scrittura della configurazione.
        self.assertIn('data-act="zones-sheet"', staff)
        self.assertIn("function zonesSheet()", staff)
        # Dalla plancia si arriva direttamente alla tabella, che resta aggiornata mentre è aperta.
        self.assertIn("get('open')==='zones')S.sheet='zones'", staff)
        self.assertIn("S.sheet!==true", staff)
        self.assertIn("{zone_id:z.id,on:!spotsOnIn(z),today:true}", staff)
        self.assertNotIn("api/config", staff)
        self.assertIn("'Musica e spot per zona':'Musik und Spots pro Zone'", staff)

    def test_operators_without_saved_zones_get_one_zone_per_sonos(self):
        # Impianto reale: zone mai confermate nel pannello, e la tabella degli operatori diceva «Nessuna».
        app = self.app
        saved = app.store.config.get("zones")
        app.store.config["zones"] = []
        self.addCleanup(app.store.config.__setitem__, "zones", saved)
        self.addCleanup(app.SPOTS_EXCLUDED.clear)
        states = [
            {"entity_id": "media_player.sala", "state": "playing",
             "attributes": {"friendly_name": "Sonos Sala", "device_class": "speaker", "group_members": []}},
            {"entity_id": "media_player.tv", "state": "on", "attributes": {"friendly_name": "TV"}},
        ]
        with mock.patch.object(app.ha, "states", return_value=states):
            self.assertEqual(app.find_zone("media_player.sala")["players"], ["media_player.sala"])
            # Un'entità che non è un Sonos non diventa una zona.
            self.assertIsNone(app.find_zone("media_player.tv"))
            self.assertIsNone(app.find_zone("light.sala"))
            self.assertEqual(self.request("/api/zone/spots", "POST",
                                          {"zone_id": "media_player.sala", "on": False, "today": True})[0], 200)
            self.assertIn("media_player.sala", app.SPOTS_EXCLUDED)
        # Con zone salvate il Sonos da solo non è una zona.
        app.store.config["zones"] = [{"id": "sala", "name": "Sala", "players": ["media_player.sala"]}]
        with mock.patch.object(app.ha, "states", return_value=states):
            self.assertIsNone(app.find_zone("media_player.sala"))

    def test_overnight_schedule(self):
        schedule = [{"days": [6], "start": "22:00", "end": "02:00", "enabled": True}]
        self.assertTrue(self.app.is_schedule_active(schedule, datetime(2026, 9, 13, 23, 0)))
        self.assertTrue(self.app.is_schedule_active(schedule, datetime(2026, 9, 14, 1, 0)))
        self.assertFalse(self.app.is_schedule_active(schedule, datetime(2026, 9, 14, 3, 0)))


if __name__ == "__main__":
    unittest.main()
