# autodeck

Local auto-DJ: import YouTube songs, analyze them, and play a beatmatched mix in the browser.
Runs on macOS (Apple Silicon), local only: the server binds `127.0.0.1`.

## One-time setup

    brew install uv rubberband ffmpeg
    uv sync

`deno` (installed by `uv sync` as the `deno` package, with `yt-dlp-ejs`) is what yt-dlp uses to solve
YouTube's player challenges; without them yt-dlp warns and some formats are missing.

## Run

    uv run python -m autodeck      # serves http://127.0.0.1:8765 and opens it in the browser

Set `AUTODECK_NO_BROWSER=1` to skip opening the tab.

## Mac app

Two ways to get an `autodeck.app`:

**Thin launcher (dev, this Mac):** `scripts/make_mac_app.sh [--notarize] [--install]` builds a tiny
app that runs `uv run python -m autodeck.desktop` in this repo, so it always runs the current code.
It needs the repo, uv and the Homebrew tools on the Mac.

**Standalone app / DMG (any Apple Silicon Mac, nothing installed):**

    scripts/build_standalone.sh                 # dist/autodeck.app + dist/autodeck-<version>.dmg, notarized
    scripts/build_standalone.sh --no-notarize   # signed only, for quick iterations

Users open the DMG (~26 MB), drag autodeck to Applications and open it. Build requirements (build
Mac only): uv with its managed CPython 3.12 (`uv python install 3.12`), Xcode command line tools,
the Developer ID Application certificate (`AUTODECK_SIGN_ID`, SHA-1, defaults to ours) and
notarytool credentials under the keychain profile `autodeck` (`AUTODECK_NOTARY_PROFILE`):
`xcrun notarytool store-credentials autodeck --apple-id <apple id> --team-id HRP3W3WRHZ`.

- **What's in the app (~58 MB):** a trimmed python-build-standalone CPython 3.12 (the Python uv
  manages), the official `uv` binary (PyPI wheel, hash-checked), the official Rubber Band CLI
  (breakfastquay.com, pinned sha256), autodeck's source, `pyproject.toml` and `uv.lock`. Everything
  is signed with the hardened runtime; the only entitlements are on the Python interpreter
  (`disable-library-validation`, `allow-unsigned-executable-memory`; see
  `scripts/standalone/entitlements.plist` for why).
- **First launch = setup** (`autodeck/standalone.py`): a "Setting up autodeck" window with progress
  (bytes, current step, log, Cancel/Retry) installs about 1.5 GB into
  `~/Library/Application Support/autodeck/`: `runtime/` (a copy of the Python + a venv made by
  `uv sync --frozen --no-dev` from the bundled lock: exact versions, hashes checked; ffmpeg comes from
  the `imageio-ffmpeg` wheel, deno from the `deno` wheel) and `models/` (beat_this, Discogs-EffNet,
  Whisper tiny; pinned URLs and sha256). uv's download cache is `~/Library/Caches/autodeck/uv`.
  Setup is resumable: an interrupted run continues next launch (downloads resume from `.part`
  files), and it only counts as done once `runtime/setup.json` is written. A new app version with a
  different `uv.lock` re-runs the sync (uv only changes the difference).
- **Settings → Installation** shows the Python version and installed size; **Repair installation**
  reinstalls the packages, re-checks the models and restarts the app.
- **Data:** library and settings stay in `~/Library/Application Support/autodeck/`, caches in
  `~/Library/Caches/autodeck/`, the log in `~/Library/Logs/autodeck.log`. Nothing is written inside the app.
- **yt-dlp updates itself:** YouTube breaks old yt-dlp within weeks, so once a day (in the
  background) the app asks PyPI for a newer yt-dlp and the yt-dlp-ejs version it pins, checks the
  sha256 and unpacks both into `~/Library/Application Support/autodeck/pylib/<version>/`. The next
  launch uses it; the two newest are kept and one that won't import is dropped.
- **First run:** after setup, an empty library shows a "Start from a playlist" screen (paste a
  YouTube / YouTube Music playlist, or use Liked Music) until at least one song is ready. Pick where
  your YouTube login comes from there or in **Settings → YouTube login from** (Chrome by default;
  Safari needs Full Disk Access for autodeck; "none" = public videos only). macOS asks once for
  Keychain access to the browser's cookies: click Always Allow.
- **Test it clean:** `AUTODECK_HOME`, `AUTODECK_CACHES` and `AUTODECK_LIBRARY` move every folder the
  app writes, e.g. `env -i HOME=$HOME PATH=/usr/bin:/bin AUTODECK_PORT=8767 AUTODECK_HOME=/tmp/x/home
  AUTODECK_CACHES=/tmp/x/caches AUTODECK_LIBRARY=/tmp/x/lib dist/autodeck.app/Contents/MacOS/autodeck`.

## First run

- **Model downloads, about 100–170 MB in total, once:** the beat_this checkpoint (~77 MB, torch hub
  cache), the Discogs-EffNet genre model (~18 MB, into `<library>/models/`) and Whisper tiny
  (~75 MB, Hugging Face cache). The first analysis waits for them. After that everything runs offline
  except the YouTube downloads.
- **Keychain prompt:** yt-dlp reads your YouTube login from Chrome, and macOS asks for Keychain
  access to "Chrome Safe Storage". Click **Always Allow**, or it asks again on every download.
  Cookies are read live by yt-dlp and never written to disk or logged by autodeck.
- **Settings → YouTube login from** picks the browser to read cookies from (Chrome, Safari,
  Firefox, Brave, Edge or none; default Chrome; saved in `settings.json` in the app's data folder).
  The **`AUTODECK_BROWSER`** env var overrides it (any yt-dlp browser name). `AUTODECK_BROWSER=none` uses no cookies at all, which works for
  public videos and playlists but not for Liked Music. Use `none` for unattended runs and tests, since
  the Keychain prompt blocks them.
- **`AUTODECK_LIBRARY`** moves the library cache (default `~/Library/Application Support/autodeck/library/`). It holds one folder per
  song (`audio.opus`, `analysis.json`, `peaks.json`, renders), `pool.json`, `genres.json` and the
  models, plus `session.json` (what is playing, what has played, and the tempo maps of the current
  renders, so a server restart doesn't break the mix). Deleting a song's folder removes the song from
  the pool (re-add its URL to get it back). Delete `session.json` with the server stopped to start a
  fresh session in which every song can play again.

## Importing music

With an empty library the page shows only a **Start from a playlist** screen: paste a YouTube or
YouTube Music playlist (or press **Use my Liked Music**), watch the first songs get ready, then
**Start**. It comes back if the library is ever emptied. After that, paste a URL in the import bar and press **Add**. Optionally pick an "import as" genre first: every song in
that import gets that genre, ahead of the app's own guess (a genre you set on a single song still wins).
Re-adding the URL of a song that failed to download or analyze queues it for another try.

- `https://www.youtube.com/watch?v=…` adds one song. A `&list=…` on a watch link is ignored, so you get
  that song and not the radio mix.
- `https://www.youtube.com/playlist?list=…` adds the whole playlist.
- **Liked Music:** first log in to YouTube Music in Chrome, then add
  `https://music.youtube.com/playlist?list=LM`. This needs cookies, so `AUTODECK_BROWSER` must not be
  `none`. Songs download and analyze in a rolling window of 10. **Play** works as soon as 1 song is
  ready; it doesn't wait for the whole pool (the mix needs a second ready song to transition into).

## Loudness

Every song is played at the same perceived loudness (-14 LUFS, EBU R128, the level YouTube and
Spotify normalize to), so transitions don't jump in volume. Real libraries span ~11 dB (-8 to -19
LUFS). Songs are never boosted into clipping: a quiet master with loud peaks is raised only as far
as its peaks allow.

## Sources & suggestions

**Playing from** says where the next songs come from. One source at a time: the **Vibe** dropdown
(Any, a genre or a mood) or the **Party** dropdown (Off, Hits, Oldies, Latino). Picking a party flavor
resets Vibe to Any; picking a genre or mood turns Party off (Off doesn't bring the old vibe back).

The **Suggestions** tab (first of Suggestions · Played · Pairs · Library) is a draft from that source,
not the queue: a mood drafts its songs that aren't in Up next or played tonight, best-voted first ("in 5 of 6
playlists"), ready or not (✓ prepares it first); a genre its songs ranked by how well they mix from
the end of Up next, ready first; a party flavor songs you don't have (below).
**✓** puts a song in Up next (it shows "kept from Balkan party" there until it plays); **Add all N**
keeps the rest in the order shown; **✕** means "never suggest this for this source" (Undo for a few
seconds). Excludes are saved per source in `<library>/rejected.json` (`{"mood:Balkan party": […],
"party:Hits": […]}`; the old flat list became a `"party"` bucket every flavor reads). With Vibe Any and
Party Off there's no draft.

**Up next** is only what you keep: draft picks, search's *Play next*, the assistant's play_next, and
anything you drag into place, in your order (saved across restarts). When fewer than 3 ready songs
are lined up, autodeck adds **Auto** picks from the active source (best mix from the end of the
list; any genre, with a notice, if the source has nothing ready) so the music never stops; ✓ on an
Auto row keeps it. The first two rows lock once the next mix is planned.

## Moods

Type a theme under **Mood** (Vibe → **+ Build a mood**) ("Balkan party", "Greek taverna", "90s eurodance") and press **Build**.
autodeck reads the top public YouTube playlists for it (and Deezer's, as a second vote), ranks songs
by how many of those playlists include them — the crowd's picks — imports the top 30, adds the mood
to the Vibe dropdown and switches to it. Its songs download first. **↻** on a mood adds
more songs from the playlists; **✕** deletes the mood (songs stay). Stored in `<library>/moods.json` (playlist votes in `mood-votes.json`).
A song keeps its own genre and can be in several moods.

## Party mode

Pick **Hits**, **Oldies** or **Latino** in the *Party* dropdown (*Off* returns to the whole library):
- The queue plays only library songs that fit the flavor, crowd-pleasers first and energy building,
  still beat/key matched. Facts come from Last.fm (listener counts, track and artist tags), looked
  up in the background for every song (~10 min for 500 songs, cached a week).
- **Suggestions** lists songs you don't have that people play with the current song, plus the
  flavor's top chart. **✓** finds it on YouTube, preps it and plays it next; **✕** hides it for that
  flavor (see Sources & suggestions).
- Each suggestion shows its **BPM** (from Deezer's free API; `?` when Deezer doesn't know it) and
  a ✓ when it beat-matches the current song; those are listed first. Library songs also get a
  **danceability** score measured from the audio (shown in the queue), which party mode favors.
- Needs a free Last.fm API key (https://www.last.fm/api/account/create): paste it in **Settings**
  (the gear in the header, or "…" on a phone), which checks it with Last.fm and stores it in the app's
  data folder. The `LASTFM_API_KEY` env var and the old `~/.config/autodeck/lastfm.env` still work.
  Without a key party mode can't tell flavors apart and shows no suggestions.

## Played log

**Played** (next to *Pairs* and *Library*) lists every song that started, newest first, with time,
BPM, genre and the effect it came in with. Stored as JSON Lines in `<library>/history.jsonl`.

## Screen

The **ON AIR** sign says what comes next and when (with the effect picker), or which effect is
playing and how many bars are left. Below it, both decks share one time axis: the NOW line, the
transition window with each deck's gain curve, and the vertical crossfader. Before the first song the
lamp is the **PLAY** button (Space plays): it holds the song to resume after a reload, or the first in
Up next; **Or start with** loads another one. **Library** shows why a song failed, with **Retry**.
On a phone (≤ 600 px wide) the same page splits into Now / Vibe / Queue / Search tabs (the Vibe
tab keeps genre, mood and party buttons; Suggestions is in the Queue tab).

## Pair memory

Rate a hand-over with **🔥** (nailed it) or **👎** (didn't work): on its Played row, or on the sign's
"How was that mix?" question that shows for 30 s after each mix. Tap again to undo. Verdicts are
directional (A → B) and stored in `<library>/pairs.json` (`{"A>B": "loved" | "avoided"}`), written on
every rating. The planner gives a loved pair a bonus (half of it for B → A) and never places an
avoided pair back to back unless nothing else is ready. **Pairs** lists them: *Play pair* queues
both songs next, ✕ forgets the pair.

## Search

Type a song or artist in the search field (press **/** to jump there). Library matches show as you
type; **Enter** adds YouTube results.
**Play next** downloads and analyzes that song right away (ahead of everything else, whatever genre
is active) and plays it next once it's ready.

## Mixing modes

- **Beat to beat** (button next to *Mix now*): blends skip the outgoing song's outro and the
  incoming song's intro, so the mix goes from the main beat of one song straight into the main beat
  of the next. Songs without a separate main section blend as usual. The setting survives restarts.
- Blends are matched on each song's tempo **where the blend happens** (not one tempo per song), so
  songs that speed up live stay locked; where the beat grid at the blend is irregular (breakdowns,
  spoken parts) the app uses a phrase-aligned echo, drop-swap or cut instead of beat-matching.

## DJ assistant (MCP)

autodeck serves an MCP endpoint at **http://127.0.0.1:8765/mcp** (streamable HTTP) that lets an
assistant DJ: it reads what's playing and steers the app through the same API the page uses.
Register it with Claude Code:

    claude mcp add --transport http --scope user autodeck http://127.0.0.1:8765/mcp

(A stdio variant exists for clients without HTTP support:
`uv --directory ~/gits/bvdr/autodeck run python -m autodeck.dj_mcp`.)

Tools: now_playing, list_library, get_history, search_songs, get_suggestions (read-only);
play_next, mix_now, set_next_effect, set_genre, start_party, stop_party, set_beat_to_beat,
accept_suggestion, reject_suggestion, set_song_genre, remove_song, add_songs, love_pair, avoid_pair. autodeck itself must be
running (`AUTODECK_URL`, default http://127.0.0.1:8765). The forced next effect and "mix now" live on
the server, so the page, the API and the assistant stay in sync.

## Tests

    uv run pytest                                    # everything except network (default addopts)
    uv run pytest -m "not slow and not network"      # also skip the heavy real-model tests
                                                     # (a -m on the command line replaces the default, so keep "not network")
    AUTODECK_BROWSER=none uv run pytest -m network   # hits real YouTube, public content only
    node --test web/*.test.mjs   # transition timing, lane geometry, drag drop index

## Known limits

From the acceptance smoke (6 real songs) and the `ponytail:` notes in the code:

**Analysis**
- **4/4 assumed.** The bar grid is every 4th beat. 3/4 songs and 12/16-feel sârbe get phrase
  boundaries in the wrong place, or no reliable grid.
- **2/4 music** (most sârbe) is fine: a 4-beat "bar" is two 2/4 bars, so phrase boundaries still land
  on real bar lines. Bar counts are halved, so a "16-bar" blend is 32 bars of 2/4.
- **Some sârbe get no reliable grid.** Live folk recordings with tempo drift or a 12/16 feel give
  irregular beats ("Sarba De La Bucuresti": unreliable). Their transitions fall back to a 4 s fade.
  "Sârba de la Nord" did get a grid. Whether its tempo is the felt tempo or double it hasn't been
  checked by ear.
- **BPM.** With a reliable grid the tempo is the median slope of the beat grid over 16-bar windows,
  which averages out the tracker's 20 ms beat-time steps and ignores short passages where the tracker
  runs fast (the median beat gap snapped to values like 125 or 130.43 bpm, up to
  2 % off). Songs without a reliable grid still use the median beat gap, so they carry that error, but
  they only ever fade.
- **One grid-reliability check for the whole song**, applied to everything after the first steady
  16 bars: CV of the 4-beat bars under `STEADY_TOL`, at most 5 % odd-length bars, and at least 60 % of
  bar lines on one of the tracker's own downbeats. One badly drifting passage disables beatmatching
  for the whole song. The bar phase is voted per segment between beat slips, so one missed beat can't
  shift the rest of the song onto beat 2.
- **Pulse gate** uses one onset-contrast threshold (`PULSE_CONTRAST`), so very soft or legato music
  may read as beatless.
- **Spoken intros are trimmed, mid-song skits are not.** A music video's dialogue intro is skipped
  (Telephone video: song starts at 189.8 s, `play_start` 184.3 s). Interludes in the middle of a video
  still play.
- **Sections are labelled by position only** (first = intro, last = outro, the rest body).
  The clustering's cuts can be far from the real structure, and the outro is capped at 32 bars so an
  early cut can't skip most of a song.
- **Fade-out endings:** `play_end` is where energy drops below 10 % of the loudest second, so a long fade's
  tail is trimmed, and the mix leaves at the start of the outro, before the fade.

**Genre**
- **Hand-written rule table** (Discogs style → 4 default genres, Romanian vs not). Pop-rock ("Africa")
  and electronic-styled pop (Telephone: "Experimental / House") get no guess. Set the genre on the song.
- **The Romanian hip-hop label depends on Whisper tiny** detecting `ro` over the beat, using the mean of
  three 30 s windows against a 0.5 bar. B.U.G. Mafia passes at 0.52, which is a thin margin. Correct it
  per song if needed; owner-set genres survive re-analysis.
- **The language windows span the whole file** (at 25/50/75 %), not just the trimmed song. In a music
  video with long dialogue, the speech can sway the language guess either way.
- **Sârbe are found mostly from title keywords** (sârba, hora, lăutari, …). An instrumental sârba with a
  plain title needs the style model plus Romanian lyrics, so it usually stays unlabelled.

**Mixing / playback**
- Planner pairing weights are hand-tuned (tempo, then key, then energy) and need retuning by ear.
- Tempo ease-back is stepped per bar (each bar at its midpoint speed), which may sound steppy on big ratios.
- When a song is rendered its own exit isn't planned yet, so a short song's ease can be cut short.
- The waveform shows the played span (`play_start` to `play_end`) of the source. For tempo-eased renders
  it is stretched linearly, with a sub-second visual error only.
- Queue pins only follow the common case (playing a queued song). Jumping to a song outside the queue
  leaves pin positions as they were.
- One prep thread, one song at a time. If prep falls behind playback, use two threads.
- **Restart:** what is playing, what has played, and each played song's tempo map and entry transition
  survive a server restart (`session.json`), so the next transition stays on the beat and played songs
  don't repeat. Not kept: the queue order and pins (rebuilt from the playing song), the genre selection
  (back to Any) and plans the player hadn't started yet (refetched).

**Not yet verified:** a 30+ minute listening session across genre switches, and the Liked Music import
with a real login. Both are left to the owner.
