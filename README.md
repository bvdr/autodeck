# autodeck

Local auto-DJ: import YouTube songs, analyze them, and play a beatmatched mix in the browser.
Runs on macOS (Apple Silicon), local only: the server binds `127.0.0.1`.

## One-time setup

    brew install uv rubberband ffmpeg deno
    uv sync

`deno` is the JavaScript runtime yt-dlp uses to solve YouTube's player challenges; without it
yt-dlp warns and some formats are missing.

## Run

    uv run python -m autodeck      # serves http://127.0.0.1:8765 and opens it in the browser

Set `AUTODECK_NO_BROWSER=1` to skip opening the tab.

## First run

- **Model downloads, about 100–170 MB in total, once:** the beat_this checkpoint (~77 MB, torch hub
  cache), the Discogs-EffNet genre model (~18 MB, into `<library>/models/`) and Whisper tiny
  (~75 MB, Hugging Face cache). The first analysis waits for them. After that everything runs offline
  except the YouTube downloads.
- **Keychain prompt:** yt-dlp reads your YouTube login from Chrome, and macOS asks for Keychain
  access to "Chrome Safe Storage". Click **Always Allow**, or it asks again on every download.
  Cookies are read live by yt-dlp and never written to disk or logged by autodeck.
- **`AUTODECK_BROWSER`** picks the browser to read cookies from (default `chrome`; also e.g.
  `firefox`, `safari`, `brave`). `AUTODECK_BROWSER=none` uses no cookies at all, which works for
  public videos and playlists but not for Liked Music. Use `none` for unattended runs and tests, since
  the Keychain prompt blocks them.
- **`AUTODECK_LIBRARY`** moves the library cache (default `~/autodeck-library/`). It holds one folder per
  song (`audio.flac`, `analysis.json`, `peaks.json`, renders), `pool.json`, `genres.json` and the
  models, plus `session.json` (what is playing, what has played, and the tempo maps of the current
  renders, so a server restart doesn't break the mix). Deleting a song's folder removes the song from
  the pool (re-add its URL to get it back). Delete `session.json` with the server stopped to start a
  fresh session in which every song can play again.

## Importing music

Paste a URL in the import bar and press **Add**. Optionally pick an "import as" genre first: every song in
that import gets that genre, ahead of the app's own guess (a genre you set on a single song still wins).
Re-adding the URL of a song that failed to download or analyze queues it for another try.

- `https://www.youtube.com/watch?v=…` adds one song. A `&list=…` on a watch link is ignored, so you get
  that song and not the radio mix.
- `https://www.youtube.com/playlist?list=…` adds the whole playlist.
- **Liked Music:** first log in to YouTube Music in Chrome, then add
  `https://music.youtube.com/playlist?list=LM`. This needs cookies, so `AUTODECK_BROWSER` must not be
  `none`. Songs download and analyze in a rolling window of 10. **Play** works as soon as 1 song is
  ready; it doesn't wait for the whole pool (the mix needs a second ready song to transition into).

## Tests

    uv run pytest                                    # everything except network (default addopts)
    uv run pytest -m "not slow and not network"      # also skip the heavy real-model tests
                                                     # (a -m on the command line replaces the default, so keep "not network")
    AUTODECK_BROWSER=none uv run pytest -m network   # hits real YouTube, public content only
    node --test web/schedule.test.mjs                # transition timing / gain-curve math

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
- **BPM.** With a reliable grid the tempo is the slope of the whole beat grid, which averages out the
  tracker's 20 ms beat-time steps (the median beat gap snapped to values like 125 or 130.43 bpm, up to
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
