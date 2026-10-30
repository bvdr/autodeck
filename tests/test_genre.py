import pytest

from autodeck.genre import combine, guess_genre, metadata_hint

G = ["Pop", "Hip-hop", "Romanian hip-hop", "Romanian sârbe"]


def test_metadata_hint_sarba_ignores_case_and_diacritics():
    assert metadata_hint("Sârba de la Moldova", "Taraf X", G) == "Romanian sârbe"
    assert metadata_hint("SARBA", "x", G) == "Romanian sârbe"
    assert metadata_hint("Hora mare", "x", G) == "Romanian sârbe"
    assert metadata_hint("Super manele 2024", "x", G) == "Romanian sârbe"
    assert metadata_hint("Sărbători fericite", "x", G) is None  # "holidays", not a sârbă


def test_metadata_hint_needs_genre_in_list():
    assert metadata_hint("Sârba", "x", ["Pop"]) is None


@pytest.mark.parametrize("styles,lang,hint,want", [
    (["Hip Hop---Trap"], "ro", None, "Romanian hip-hop"),
    (["Hip Hop---Boom Bap"], "en", None, "Hip-hop"),
    (["Pop---Dance-pop"], "en", None, "Pop"),
    (["Folk, World, & Country---Romani"], "ro", None, "Romanian sârbe"),
    (["Hip Hop---Trap"], "ro", "Romanian sârbe", "Romanian sârbe"),  # hint beats model
    (["Jazz---Bebop"], "en", None, None),
])
def test_combine(styles, lang, hint, want):
    assert combine(styles, lang, hint, G) == want


def test_combine_never_returns_genre_outside_list():
    assert combine(["Hip Hop---Trap"], "ro", None, ["Pop"]) is None


def test_no_model_falls_back_to_hint(monkeypatch, tmp_library, click_track):
    import autodeck.genre as g

    def boom(*a, **k):
        raise RuntimeError("no model")

    monkeypatch.setattr(g, "_styles", boom)
    monkeypatch.setattr(g, "_language", boom)
    assert guess_genre(click_track(seconds=20), "Sârba", "x", G) == ("Romanian sârbe", None)


@pytest.mark.slow
def test_model_runs(click_track):
    out = guess_genre(click_track(seconds=40), "click", "x", G)
    assert isinstance(out, tuple) and len(out) == 2
