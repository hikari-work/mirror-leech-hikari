"""Tests for the FTP settings as configuration values.

The four credentials and the folder are read from ``config.py`` or written
through ``/bsetting``, so how they are stored matters as much as what they hold:
the type of the default is what a string typed into the settings screen is
converted to, and a key that is not in ``get_all()`` never reaches that screen.
"""

from __future__ import annotations

import pytest

from bot.core.config_manager import Config

FTP_KEYS = (
    "FTP_HOST",
    "FTP_PORT",
    "FTP_USERNAME",
    "FTP_PASSWORD",
    "FTP_PATH",
    "FTP_TLS",
)
"""Everything the FTP uploader reads, by name."""


def test_every_setting_the_uploader_reads_exists():
    for key in FTP_KEYS:
        assert Config.get(key) is not None, key


def test_every_setting_is_offered_to_the_settings_screen():
    """/bsetting is built from this mapping, and only from it."""
    offered = Config.get_all()

    for key in FTP_KEYS:
        assert key in offered, key


def test_a_settings_screen_string_becomes_the_number_it_means(monkeypatch):
    monkeypatch.setattr(Config, "FTP_PORT", 21)

    Config.set("FTP_PORT", "2121")

    assert Config.FTP_PORT == 2121


@pytest.mark.parametrize("typed", ["true", "1", "yes", "TRUE"])
def test_a_settings_screen_string_becomes_a_switch(monkeypatch, typed):
    monkeypatch.setattr(Config, "FTP_TLS", False)

    Config.set("FTP_TLS", typed)

    assert Config.FTP_TLS is True


def test_a_password_is_left_exactly_as_it_was_given():
    """Slash-stripping is an allowlist, and a password is not on it."""
    assert Config._process_config_value("FTP_PASSWORD", "/p+w/") == "/p+w/"
