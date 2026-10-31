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
    for name in ("now_playing", "list_library", "get_history", "search_songs", "get_suggestions", "list_pairs"):
        assert by[name].read_only_hint and not by[name].destructive_hint
    for name in ("play_next", "mix_now", "set_genre", "start_party", "remove_song", "reject_suggestion",
                 "love_pair", "avoid_pair", "forget_pair"):
        assert not by[name].read_only_hint and by[name].destructive_hint
    assert by["add_songs"].destructive_hint is False  # purely additive
    for name in ("love_pair", "avoid_pair", "forget_pair", "list_pairs"):
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


def test_pair_tools_call_one_endpoint_each(monkeypatch):
    calls = []
    monkeypatch.setattr(dj_mcp, "_api", lambda path, method="GET", body=None: calls.append((path, method, body)) or {
        "pairs": []})
    monkeypatch.setattr(dj_mcp, "_summary", lambda s: s)
    dj_mcp.avoid_pair("a", "b", why="effect")
    dj_mcp.avoid_pair("a", "b")
    dj_mcp.forget_pair("a", "b")
    assert dj_mcp.list_pairs() == []
    assert calls == [
        ("/api/pair", "POST", {"from": "a", "to": "b", "verdict": "avoided", "why": "effect"}),
        ("/api/pair", "POST", {"from": "a", "to": "b", "verdict": "avoided", "why": None}),
        ("/api/pair/forget", "POST", {"from": "a", "to": "b"}),
        ("/api/pairs", "GET", None)]


# --- MCP settings, gate, activity (design m6p10) ---------------------------------------------
from autodeck import config, mcp_activity  # noqa: E402


def _call(name, args=None):
    """A tool call the way an MCP client makes it: through the server's call_tool (the gate)."""
    return asyncio.run(dj_mcp.server.call_tool(name, args or {}))


def _fake_api(monkeypatch, state=None):
    """dj_mcp._api stand-in: records (path, method, body, headers) and answers like the app."""
    calls = []
    st = {"pool": [{"id": "aaaaaaaaaa1", "title": "Smooth Criminal", "channel": "MJ", "status": "ready"}],
          "queue": [], "genre": "Any", "party": None, "next_effect": None, **(state or {})}

    def api(path, method="GET", body=None):
        calls.append((path, method, body, dj_mcp._headers()))
        if path == "/api/play_next":
            return {**st, "play_next": {"id": body["id"], "ready": True, "planned": None}}
        if path == "/api/history?limit=20":
            return {"history": []}
        return st
    monkeypatch.setattr(dj_mcp, "_api", api)
    return calls


def _reports(calls):
    return [c[2] for c in calls if c[0] == "/api/mcp/activity"]


def test_off_refuses_every_tool_with_the_message_and_calls_nothing(tmp_library, monkeypatch):
    mcp_activity.set_switches(enabled=False)
    calls = _fake_api(monkeypatch)
    for t in _tools():
        with pytest.raises(dj_mcp.ToolError, match="MCP is turned off in autodeck"):
            _call(t.name)
    assert calls == []  # nothing else changes: not even the activity log


def test_only_read_refuses_steering_tools_but_allows_reads(tmp_library, monkeypatch):
    mcp_activity.set_switches(mode="read")
    calls = _fake_api(monkeypatch)
    reads = [t.name for t in _tools() if t.annotations.read_only_hint]
    steers = [t.name for t in _tools() if not t.annotations.read_only_hint]
    assert "now_playing" in reads and "play_next" in steers and "add_songs" in steers
    for name in steers:
        with pytest.raises(dj_mcp.ToolError, match="Only read"):
            _call(name)
    assert all(c[0] == "/api/mcp/activity" for c in calls)  # refused before touching the session
    assert all(not r["ok"] for r in _reports(calls))
    _call("now_playing")
    _call("get_history")
    assert _reports(calls)[-1]["ok"] and _reports(calls)[-1]["tool"] == "get_history"


def test_steering_tools_say_one_plain_sentence_built_from_the_result(tmp_library, monkeypatch):
    calls = _fake_api(monkeypatch, {"genre": "Balkan party", "next_effect": "echo"})
    _call("play_next", {"song_id": "aaaaaaaaaa1"})  # no title passed: the sentence uses the app's
    _call("set_genre", {"genre": "whatever the assistant typed"})
    _call("set_next_effect", {"effect": "echo"})
    said = [(r["tool"], r["text"], r["song"]) for r in _reports(calls)]
    assert said == [("play_next", "Put Smooth Criminal next", "aaaaaaaaaa1"),
                    ("set_genre", "Switched the vibe to Balkan party", None),
                    ("set_next_effect", "Set the next effect to Echo out", None)]


def test_api_calls_of_a_tool_carry_the_mcp_headers(tmp_library, monkeypatch):
    calls = _fake_api(monkeypatch)
    _call("now_playing")
    hdr = calls[0][3]
    assert hdr["X-Autodeck-Client"] == "mcp" and hdr["X-Autodeck-Tool"] == "now_playing"
    assert hdr["X-Autodeck-Call"] == _reports(calls)[0]["call"]
    assert dj_mcp._headers() == {}  # outside a tool call: a plain client


def test_a_failed_tool_reports_the_reason(tmp_library, monkeypatch):
    calls = _fake_api(monkeypatch)

    def boom(path, method="GET", body=None):
        if path == "/api/mcp/activity":
            return calls.append((path, method, body, {}))
        raise dj_mcp.ToolError("Couldn't find that song")
    monkeypatch.setattr(dj_mcp, "_api", boom)
    with pytest.raises(dj_mcp.ToolError):
        _call("play_next", {"song_id": "aaaaaaaaaa1"})
    assert _reports(calls) == [{"call": _reports(calls)[0]["call"], "tool": "play_next", "client": "An assistant",
                                "ok": False, "text": "Couldn't find that song", "song": None}]


def test_activity_ring_buffer_keeps_the_newest_50_newest_first(tmp_library):
    a = mcp_activity.Activity()
    for n in range(60):
        a.finish(f"c{n}", "set_genre", "Claude Code", "switching the vibe", False, True, f"said {n}")
    log = a.state()["log"]
    assert len(log) == mcp_activity.LOG_SIZE and log[0]["text"] == "said 59" and log[-1]["text"] == "said 10"


def test_mcp_state_fields_and_the_ten_minute_connected_window(tmp_library):
    t = [1000.0]
    a = mcp_activity.Activity(clock=lambda: t[0])
    s = a.state()
    assert {k: s[k] for k in ("enabled", "mode", "client", "last_call_at", "connected", "running",
                              "last_error", "ever_connected")} == {
        "enabled": True, "mode": "steer", "client": None, "last_call_at": None, "connected": False,
        "running": None, "last_error": None, "ever_connected": False}
    a.begin("c1", "now_playing", "Claude Code", "now playing", True)
    s = a.state()
    assert s["connected"] and s["running"] is None  # a read only flashes the dot
    a.begin("c2", "play_next", "Claude Code", "playing next", False)
    assert a.state()["running"] == {"tool": "play_next", "text": "playing next", "client": "Claude Code"}
    a.finish("c2", "play_next", "Claude Code", "playing next", False, False, "Couldn't find that song")
    s = a.state()
    assert s["running"] is None and s["last_error"] == "Couldn't find that song" and s["client"] == "Claude Code"
    assert s["log"][0]["status"] == "error" and len(s["log"]) == 2
    t[0] += mcp_activity.CONNECTED_S - 1
    assert a.state()["connected"]
    t[0] += 2
    assert not a.state()["connected"] and a.state()["last_error"]  # the error stays until seen
    a.seen()
    assert a.state()["last_error"] is None
    assert mcp_activity.Activity().ever_connected  # persisted: first run is over for good


def test_enabled_and_mode_persist_across_restarts(tmp_library):
    assert mcp_activity.switches() == (True, "steer")  # default keeps the owner's Claude Code working
    mcp_activity.set_switches(enabled=False, mode="read")
    assert config.settings()["mcp_enabled"] is False and mcp_activity.switches() == (False, "read")
    with pytest.raises(ValueError):
        mcp_activity.set_switches(mode="admin")
