import asyncio

import pytest

from autodeck import dj_mcp


def _tools():
    return asyncio.run(dj_mcp.server.list_tools())


def test_every_tool_declares_all_three_hints_and_a_title():
    # Owner's MCP rule: readOnlyHint, destructiveHint, openWorldHint explicit booleans + title.
    tools = _tools()
    assert len(tools) >= 15
    for t in tools:
        a = t.annotations
        assert t.title and a and a.title, t.name
        for hint in ("read_only_hint", "destructive_hint", "open_world_hint"):
            assert isinstance(getattr(a, hint), bool), (t.name, hint)


def test_read_only_tools_are_not_destructive_and_writes_are():
    by = {t.name: t.annotations for t in _tools()}
    for name in ("now_playing", "list_library", "get_history", "search_songs", "get_suggestions"):
        assert by[name].read_only_hint and not by[name].destructive_hint
    for name in ("play_next", "mix_now", "set_genre", "start_party", "remove_song", "reject_suggestion",
                 "love_pair", "avoid_pair"):
        assert not by[name].read_only_hint and by[name].destructive_hint
    assert by["add_songs"].destructive_hint is False  # purely additive
    for name in ("love_pair", "avoid_pair"):
        assert by[name].open_world_hint is False  # local pair memory only
    for name in ("search_songs", "get_suggestions", "play_next", "add_songs", "accept_suggestion"):
        assert by[name].open_world_hint  # reach YouTube / Last.fm


def test_playlist_tool_hints():
    by = {t.name: t.annotations for t in _tools()}
    assert by["list_playlists"].read_only_hint and not by["list_playlists"].destructive_hint
    assert not by["list_playlists"].open_world_hint
    assert by["save_playlist"].open_world_hint and not by["save_playlist"].destructive_hint  # additive
    assert by["sync_playlist"].open_world_hint and by["sync_playlist"].destructive_hint  # drops removed ids
    assert by["play_from_playlist"].destructive_hint and not by["play_from_playlist"].read_only_hint
    assert by["remove_playlist"].destructive_hint and not by["remove_playlist"].open_world_hint


def test_no_action_switch_parameters():
    # One tool per operation: no generic action/mode enum parameters.
    for t in _tools():
        assert not {"action", "mode", "operation"} & set(t.input_schema.get("properties", {})), t.name


def test_unreachable_server_gives_a_clear_error(monkeypatch):
    monkeypatch.setattr(dj_mcp, "BASE", "http://127.0.0.1:9")
    with pytest.raises(dj_mcp.ToolError, match="isn't running"):
        dj_mcp.now_playing()


def test_mcp_is_served_by_the_app_at_slash_mcp(tmp_library):
    # Owner wants the MCP at <app url>/mcp. Full MCP handshake over streamable HTTP.
    from fastapi.testclient import TestClient
    from autodeck.engine import Engine
    from autodeck.server import create_app
    app = create_app(Engine())
    # base_url with the port: real clients send Host: 127.0.0.1:8765, which the SDK checks.
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:  # `with` runs the lifespan
        hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        r = c.post("/mcp", headers=hdr, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        assert r.status_code == 200, r.text
        assert "autodeck" in r.text


def test_play_next_means_the_very_next_song(monkeypatch):
    # Same action as the deck button: /api/play_next (the idle deck), not /api/request ("soon").
    calls = []
    monkeypatch.setattr(dj_mcp, "_api", lambda path, method="GET", body=None: calls.append((path, body)) or {
        "play_next": {"id": "aaaaaaaaaa1", "ready": False, "planned": None}})
    out = dj_mcp.play_next("aaaaaaaaaa1", "Hit")
    assert calls[0][0] == "/api/play_next" and calls[0][1]["source"] == "assistant"
    assert out["play_next"] == {"id": "aaaaaaaaaa1", "ready": False, "planned": None}
