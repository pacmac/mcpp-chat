"""Shared test fixtures.

Every test drives the real host bootstrap (mcpptool._pkg) against a throwaway
database pointed at by MCPP_CHAT_DB, so nothing here can touch a live channel.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

MODULE_DIR = Path(__file__).resolve().parent.parent


def _load_tool():
    name = "mcpp_chat_mcpptool"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, MODULE_DIR / "mcpptool.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def tool(tmp_path, monkeypatch):
    """The mcpptool module with an isolated DB."""
    monkeypatch.setenv("MCPP_CHAT_DB", str(tmp_path / "chat.db"))
    return _load_tool()


@pytest.fixture()
def mods(tool):
    """(config, db, chat) submodules."""
    return tool._pkg()


@pytest.fixture()
def repos(tmp_path):
    """Two workspace dirs standing in for two repos."""
    a = tmp_path / "mesh-gw"
    b = tmp_path / "node-dash"
    a.mkdir()
    b.mkdir()
    return a, b


@pytest.fixture()
def conn(mods):
    _config, db, _chat = mods
    c = db.open_db()
    yield c
    c.close()


@pytest.fixture()
def parties(mods, conn, repos):
    """(party_a, party_b) rows for the two repos."""
    _config, _db, chat = mods
    a, b = repos
    return chat.party_for(conn, str(a)), chat.party_for(conn, str(b))


@pytest.fixture()
def channel(mods, conn, parties):
    """A channel named 'link' with both parties seated."""
    _config, _db, chat = mods
    pa, pb = parties
    chat.create_channel(conn, pa, "link", "test channel", [pb["name"]])
    return "link"


def call(tool, name, args, workspace):
    """Invoke a tool the way the host does."""
    return tool.execute(name, args, {"workspace_dir": str(workspace),
                                     "module_dir": str(MODULE_DIR),
                                     "module_scope": "local"})
