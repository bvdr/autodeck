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
    for name in ("play_next", "mix_now", "set_genre", "start_party", "remove_song", "reject_suggestion"):
        assert not by[name].read_only_hint and by[name].destructive_hint
    assert by["add_songs"].destructive_hint is False  # purely additive
    for name in ("search_songs", "get_suggestions", "play_next", "add_songs", "accept_suggestion"):
        assert by[name].open_world_hint  # reach YouTube / Last.fm


def test_no_action_switch_parameters():
    # One tool per operation: no generic action/mode enum parameters.
    for t in _tools():
        assert not {"action", "mode", "operation"} & set(t.input_schema.get("properties", {})), t.name


def test_unreachable_server_gives_a_clear_error(monkeypatch):
    monkeypatch.setattr(dj_mcp, "BASE", "http://127.0.0.1:9")
    with pytest.raises(RuntimeError, match="isn't running"):
        dj_mcp.now_playing()
