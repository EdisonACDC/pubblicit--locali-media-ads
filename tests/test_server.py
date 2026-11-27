"""Test dell'add-on stabile.

Dalla 0.6.0 la stabile usa lo stesso codice della beta: si eseguono gli stessi test sulla sua
cartella e con le sue porte, più i controlli specifici del pacchetto stabile.
"""
import re
import unittest
from pathlib import Path

import test_beta_server


class StableServerTest(test_beta_server.CarellasServerTest):
    ADDON = "carellas_media_ads"
    PORTS = (8099, 8101, 8103)

    def test_stable_package_uses_its_own_ports(self):
        config = (self.addon_dir / "config.yaml").read_text(encoding="utf-8")
        dockerfile = (self.addon_dir / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("stage: stable", config)
        self.assertIn("ingress_port: 8099", config)
        for name, port in (("CARELLAS_PORT", 8099), ("CARELLAS_PLAYER_PORT", 8101), ("CARELLAS_REMOTE_PORT", 8103)):
            self.assertIn(f"{port}/tcp: {port}", config)
            self.assertRegex(dockerfile, rf"{name}={port}\b")
        self.assertIn("EXPOSE 8099 8101 8103", dockerfile)
        # Menu in PDF e testi delle pagine: stessi pacchetti della beta.
        self.assertIn("font-dejavu poppler-utils", dockerfile)
        # I media demo li installa la stabile: le parti degli MP3 vanno ricomposte.
        self.assertIn("bundled_media", dockerfile)
        self.assertTrue(any((self.addon_dir / "app/bundled_media").glob("*.part.*")))

    def test_stable_version_is_a_release(self):
        config = (self.addon_dir / "config.yaml").read_text(encoding="utf-8")
        version = re.search(r'^version: "([^"]+)"', config, re.M).group(1)
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
