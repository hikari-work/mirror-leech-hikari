"""Gateway wiring and result paging for the torrent search module.

The module is loaded against a stubbed bot package: the real one opens a log
file, installs uvloop and builds an event loop on import, none of which a page
renderer needs.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent


class _Logger:
    @staticmethod
    def error(msg):
        pass

    info = warning = debug = error


class _Recorder:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_message(self, message, text, buttons=None, block=True):
        self.sent.append((text, buttons))
        return "sent"

    async def edit_message(
        self, message, text=None, buttons=None, block=True, **kwargs
    ):
        self.edited.append((text, buttons))
        return "edited"


@pytest.fixture
def search(monkeypatch):
    """Load ``bot/modules/search.py`` with everything but keyboards stubbed."""
    path = _ROOT / "bot" / "modules" / "search.py"
    recorder = _Recorder()
    bot_pkg = ModuleType("search_stub")
    bot_pkg.__path__ = []
    bot_pkg.LOGGER = _Logger()
    modules_pkg = ModuleType("search_stub.modules")
    modules_pkg.__path__ = []
    helper_pkg = ModuleType("search_stub.helper")
    helper_pkg.__path__ = []
    net_pkg = ModuleType("search_stub.helper.net")
    net_pkg.__path__ = []
    gateway = ModuleType("search_stub.helper.net.gateway")
    gateway.gateway_url = lambda path="": f"https://gw.test{path}"
    gateway.gateway_headers = lambda accept_json=True: {"accept": "application/json"}
    util_pkg = ModuleType("search_stub.helper.util")
    util_pkg.__path__ = []
    bot_utils = ModuleType("search_stub.helper.util.bot_utils")
    bot_utils.new_task = lambda func: func
    telegram_pkg = ModuleType("search_stub.helper.telegram")
    telegram_pkg.__path__ = []
    message_utils = ModuleType("search_stub.helper.telegram.message_utils")
    message_utils.send_message = recorder.send_message
    message_utils.edit_message = recorder.edit_message
    message_utils.message_text = lambda message: message.text
    for name, module in {
        "search_stub": bot_pkg,
        "search_stub.modules": modules_pkg,
        "search_stub.helper": helper_pkg,
        "search_stub.helper.net": net_pkg,
        "search_stub.helper.net.gateway": gateway,
        "search_stub.helper.util": util_pkg,
        "search_stub.helper.util.bot_utils": bot_utils,
        "search_stub.helper.telegram": telegram_pkg,
        "search_stub.helper.telegram.message_utils": message_utils,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location(
        "search_stub.helper.telegram.button_build",
        _ROOT / "bot" / "helper" / "telegram" / "button_build.py",
    )
    button_build = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(
        sys.modules, "search_stub.helper.telegram.button_build", button_build
    )
    spec.loader.exec_module(button_build)
    spec = importlib.util.spec_from_file_location(
        "search_stub.modules.search", path
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "search_stub.modules.search", module)
    spec.loader.exec_module(module)
    module.recorder = recorder
    return module


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class _Client:
    """Captures every gateway GET and answers with ``payload``."""

    payload = {}
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return _Response(self.payload)


async def test_initiate_search_tools_lists_providers(search, monkeypatch):
    monkeypatch.setattr(search, "AsyncClient", _Client)
    _Client.calls = []
    _Client.payload = {
        "success": True,
        "count": 1,
        "providers": [
            {"id": "piratebay", "name": "The Pirate Bay", "url": "https://tpb.test"}
        ],
        "timestamp": 1,
    }
    await search.initiate_search_tools()
    assert search.PROVIDERS == {"piratebay": "The Pirate Bay", "all": "All"}
    assert _Client.calls[0]["url"] == "https://gw.test/api/v1/scrape/torrent/providers"


async def test_search_sends_query_and_provider(search, monkeypatch):
    monkeypatch.setattr(search, "AsyncClient", _Client)
    _Client.calls = []
    item = {
        "provider": "piratebay",
        "name": "Ubuntu 22.04",
        "size": "3.40 GB",
        "seeder": 34,
        "leecher": 11,
        "magnet": "magnet:?xt=urn:btih:abc",
        "url": "https://tpb.test/1",
        "uploaded": 1,
    }
    _Client.payload = {
        "success": True,
        "results": [item],
        "errors": [{"provider": "eztv", "error": "upstream returned HTTP 403"}],
    }
    results, failed = await search._search("ubuntu", "piratebay")
    assert _Client.calls[0]["url"] == "https://gw.test/api/v1/scrape/torrent/search"
    assert _Client.calls[0]["params"] == {"q": "ubuntu", "provider": "piratebay"}
    assert results == [item]
    assert failed == ["eztv"]


def test_render_page_pages_and_links_results(search):
    search.PROVIDERS = {"piratebay": "The Pirate Bay", "all": "All"}
    results = [
        {
            "provider": "piratebay",
            "name": f"Result {n}",
            "size": "1 GB",
            "seeder": n,
            "leecher": 0,
            "magnet": f"magnet:?xt=urn:btih:{n}",
            "url": f"https://tpb.test/{n}",
        }
        for n in range(1, 13)
    ]
    search._SEARCHES[7] = search._SearchState(
        key="ubuntu", provider="piratebay", results=results, failed=[]
    )
    text, buttons = search.render_page(7, 2)
    assert "Result 6" in text
    assert "Result 10" in text
    assert "Result 11" not in text
    labels = [b.text for row in buttons.inline_keyboard for b in row]
    assert "Prev" in labels and "Next" in labels
    callbacks = [
        b.callback_data
        for row in buttons.inline_keyboard
        for b in row
        if b.callback_data
    ]
    assert "torser:7:p:1" in callbacks
    assert "torser:7:p:3" in callbacks
    assert "torser:7:m:5" in callbacks
    magnet_row = next(
        row
        for row in buttons.inline_keyboard
        if any(b.url == "https://tpb.test/6" for b in row)
    )
    assert [b.text for b in magnet_row] == ["Magnet", "Torrent"]
    clamped, _ = search.render_page(7, 99)
    assert "3/3" in clamped


async def test_magnet_button_sends_the_link(search):
    search._SEARCHES[7] = search._SearchState(
        key="ubuntu",
        provider="piratebay",
        results=[{"name": "Ubuntu", "magnet": "magnet:?xt=urn:btih:abc"}],
        failed=[],
    )
    answered = []

    class _Query:
        message = SimpleNamespace()

        @staticmethod
        async def answer(text=None, show_alert=False):
            answered.append(text)

    await search._send_magnet(_Query(), SimpleNamespace(), 7, 0)
    assert "magnet:?xt=urn:btih:abc" in search.recorder.sent[0][0]
    assert answered == ["Magnet sent"]
