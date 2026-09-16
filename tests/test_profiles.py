from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fingerprint_terminal.profiles import (
    ProfileError,
    build_environment,
    clone_profile,
    load_store,
    validate_document,
)


def document() -> dict:
    return {
        "version": 1,
        "profiles": [
            {
                "id": "local",
                "name": "Local",
                "terminal": {"backend": "auto", "shell": "/bin/sh", "cwd": "/"},
                "identity": {"timezone": "UTC", "locale": "C.UTF-8"},
                "environment": {"EXAMPLE": "yes", "REMOVE_ME": None},
                "proxy": {"mode": "off"},
                "home": {"mode": "inherit"},
            }
        ],
    }


class ProfileTests(unittest.TestCase):
    def test_validate_document_accepts_minimal_profile(self) -> None:
        validate_document(document())

    def test_duplicate_ids_rejected(self) -> None:
        doc = document()
        doc["profiles"].append(dict(doc["profiles"][0]))
        with self.assertRaisesRegex(ProfileError, "duplicate profile id"):
            validate_document(doc)

    def test_build_environment_applies_identity_custom_env_and_proxy_off(self) -> None:
        profile = document()["profiles"][0]
        env = build_environment(
            profile,
            {
                "HTTP_PROXY": "http://bad.example",
                "http_proxy": "http://bad.example",
                "REMOVE_ME": "old",
                "HOME": "/home/test",
            },
            create_home=False,
        )
        self.assertEqual(env["TZ"], "UTC")
        self.assertEqual(env["LANG"], "C.UTF-8")
        self.assertEqual(env["LC_ALL"], "C.UTF-8")
        self.assertEqual(env["EXAMPLE"], "yes")
        self.assertNotIn("REMOVE_ME", env)
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("http_proxy", env)
        self.assertEqual(env["HOME"], "/home/test")
        self.assertEqual(env["FT_PROFILE_ID"], "local")

    def test_environment_proxy_sets_upper_and_lowercase(self) -> None:
        profile = document()["profiles"][0]
        profile["proxy"] = {
            "mode": "environment",
            "http": "http://127.0.0.1:8080",
            "https": "http://127.0.0.1:8080",
            "all": "socks5://127.0.0.1:1080",
            "no_proxy": "localhost",
        }
        env = build_environment(profile, {}, create_home=False)
        self.assertEqual(env["HTTP_PROXY"], env["http_proxy"])
        self.assertEqual(env["HTTP_PROXY"], "http://127.0.0.1:8080")
        self.assertEqual(env["ALL_PROXY"], env["all_proxy"])
        self.assertEqual(env["ALL_PROXY"], "socks5://127.0.0.1:1080")
        self.assertEqual(env["NO_PROXY"], env["no_proxy"])
        self.assertEqual(env["NO_PROXY"], "localhost")

    def test_clone_profile_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profiles.json"
            path.write_text(json.dumps(document()), encoding="utf-8")
            store = load_store(path, create=False)
            clone_profile(store, "local", "copy", name="Copy")
            saved = load_store(path, create=False)
            self.assertEqual(saved.get("copy")["name"], "Copy")


if __name__ == "__main__":
    unittest.main()

