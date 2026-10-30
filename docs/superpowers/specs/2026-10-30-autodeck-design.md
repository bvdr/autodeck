# autodeck: design

Date: 2026-10-30
Status: draft, awaiting owner approval

## Intent

A personal DJ app. You hand it songs from YouTube or YouTube Music, like a playlist, your Liked Music, or single links, and it plays them as one continuous mix, the way a good DJ would. No gaps between songs and no clashing beats. It picks the order, the moment each blend starts, and how long the blend lasts.

Why: the owner wants their own music to flow like a DJ set without having to DJ it themselves. This is round one of two. Round two is a **sampler** (see "Later"), and it reuses the analysis engine built here.

## Goals (v1)

1. **Add songs.** Paste a YouTube or YouTube Music URL (a single video or a playlist) or import the owner's Liked Music (`https://music.youtube.com/playlist?list=LM`).
2. **Prepare ahead, in a rolling window.** A big import (Liked Music can hold hundreds of songs) becomes a **pool**. The playlist's own order doesn't matter. The engine downloads and analyzes about the next 10 songs it is likely to play (my call), not the whole pool at once, which keeps rate limits and disk use sane, and it keeps topping that window up. Each song shows its status: queued, downloading, analyzing, ready, or failed.
3. **Analyze like a DJ.** For each song, find BPM, beat and downbeat positions (so we know where the bars are), musical key, an energy curve, section boundaries (intro, main and drop parts, outro), and whether it has a reliable beat grid at all. Live drumming, songs with no beat, and long video intros or outros with skits or silence must be detected and trimmed, not trusted.
4. **Genre labels.** Every song gets a genre label. The owner's genres include ones no standard model knows, such as **Pop, Hip-hop, Romanian hip-hop, and Romanian sârbe**, and the list can be edited. The app guesses the label itself (owner agreed), from a pre-trained genre model plus clues in the YouTube title and channel, and the language of the lyrics. The owner can fix a wrong label with one click, or assign a genre to a whole playlist at import ("import this as Sârbe").
5. **Genre switching.** The owner picks a genre ("now we switch to Romanian hip-hop") with a button per genre. At the next good phrase boundary the app moves into that genre, choosing an entry song and a transition that suit the jump, and then stays inside that genre until told otherwise. "Any" mixes freely across genres.
6. **Order like a smart DJ.** Within the active genre, the planner orders the upcoming songs so neighbors have close BPM, compatible keys (Camelot wheel), and a sensible energy flow. The next 2 songs are fixed so the queue doesn't jump around in front of the owner; only songs beyond those get reordered. A song the owner drags into place stays there.
7. **Blend like a DJ, with several kinds of transitions.** The library spans genres from about 70 to 180 BPM, so many pairs can't be beat-matched. Treating that as a rare fallback would be wrong; it is a main path. The planner picks one transition per pair:
   - **Beat-matched blend** when BPMs are within about 8%: tempo-matched and bar-aligned, with the crossfader moving over the planned bars and the blend length taken from the outro and intro lengths.
   - **Half-time or double-time blend** when one BPM is about twice the other (for example 85 and 170).
   - **Cut on the downbeat**, **echo-out into the next song's drop**, or a **quick drop-swap** for the rest. These still land on a phrase boundary of the outgoing song, so even transitions that aren't beat-matched sound on time.
   - For songs without a reliable grid (fade-out endings, live tempo): trim silence and skits, then crossfade over a short, energy-aware window.
   Every transition starts on a phrase boundary (a multiple of 8 or 16 bars) wherever a grid exists. The cardinal rule is no gap and no train-wreck.
8. **Two-deck UI** (layout B, chosen by the owner). Deck A and Deck B each show title, artist, genre, BPM (original → playing), key, a waveform with a playhead, and bar or section markers. Between them is a crossfader that moves by itself during blends. Below are the genre-switch buttons and the queue, which can be dragged to reorder, with a drop zone for links.
9. **Manual takeover, kept small.** The owner can drag the crossfader, skip with a "mix now" that blends at the next phrase boundary, remove songs, and reorder the queue. My call: grabbing the crossfader during a blend hands that one blend to the owner, and auto stops moving it. When they let go, whichever side the fader is nearer becomes the playing song, the other deck fades out over one bar, and auto-mixing picks up again at the next transition.

## Success looks like

The owner imports Liked Music, presses play, and listens for an hour. They never notice a gap, never hear two beats fighting, and the blends land on the beat at phrase boundaries. Music starts within a reasonable time of pressing play: once the first two songs are ready, not after the whole playlist has been processed.

## Anti-goals (fail even if it "works")

- An audible gap or silence between songs.
- Train-wrecks: two beats out of phase, or blending in the middle of a phrase.
- Chipmunk or slowed-down voices. Tempo changes must keep the pitch.
- Waiting for an entire playlist to download before anything plays.
- Re-downloading or re-analyzing a song it has already processed.

## Non-goals (v1)

- The sampler tab (round two).
- Layering extra sounds or samples over the mix, and effects beyond what a transition needs.
- Recording or exporting the mix to a file.
- Hosting, multiple users, mobile, or sharing. It runs only on the owner's Mac.
- Training our own ML models. We use existing pre-trained analysis tools.

## Constraints

- **Local only, personal use.** Downloading from YouTube is against YouTube's terms. That's the owner's call for a private tool, and it's why this never becomes a hosted service. Don't commit downloaded audio or cookies.
- **Liked Music is private.** yt-dlp reads it using the owner's browser cookies (`--cookies-from-browser chrome`). Cookies are never copied into the repo or a config file.
- Runs on macOS (Apple Silicon).
- No secrets in the repo.

## How it's built (the owner delegated these choices; each one is my call, with the reason)

**Shape: a local Python engine plus a browser UI.**
Python is where the good music-analysis tools are. The browser gives us drag-and-drop, canvas waveforms, and the Web Audio API, which can schedule audio sample-accurately. That is what beat-matching needs. The owner runs one command, and it opens `http://localhost:<port>`.

**Engine (Python):**
- **Web server: FastAPI + uvicorn.** It serves the UI, a small JSON API, and the audio files. Status updates reach the UI by polling or SSE (builder's choice).
- **Download: yt-dlp,** best audio only, using browser cookies for private playlists. Playlist URLs are expanded into separate songs.
- **Analysis:**
  - Beats and downbeats: **beat_this** (CPJKU, 2024). It's a current, accurate beat and downbeat tracker, and downbeats give us bars.
  - Key: **Essentia** `KeyExtractor`, mapped to Camelot notation.
  - Energy curve and section boundaries: **librosa** (RMS energy and novelty-based segmentation, snapped to bar lines).
  - If one of these won't install cleanly on Apple Silicon, the builder may swap it for an equivalent. Record why in a code comment.
- **Cache:** one folder per song in `~/autodeck-library/<youtube-id>/`, with the audio, an `analysis.json` file, and waveform peaks. It lives outside the repo so audio never ends up in git. If `analysis.json` exists, the song is never re-processed. A version field in that file allows re-analysis when the analysis logic changes.
- **Planner:** orders the unpinned upcoming songs in the active genre greedily, picking the next song by a weighted score for BPM distance (half or double time counts as close), Camelot compatibility, and energy change. For each neighboring pair it picks the transition type (goal 7), the outgoing song's exit point, the incoming song's entry point, and the length in bars. On a genre switch, it picks the entry song in the new genre whose BPM, energy and intro give the cleanest jump from what's playing.
- **Tempo matching: Rubber Band** (via `pyrubberband` or the `rubberband` CLI) with a **time map**, so the stretch can change over the song. Rendering happens ahead of time and keeps the pitch, which keeps the browser simple and the sound clean.
  - Rule (my call): during the blend, the incoming song plays at the outgoing song's tempo. After the blend it **eases back to its own original tempo** over about 16 bars, the way a DJ lets the pitch fader drift home. That way the set never stays locked to one BPM, and the planner always scores pairs against each song's original BPM. Bar markers and the beat grid are remapped through the time map so the deck display and the next transition stay accurate.
  - A pair more than about 8% apart (after trying half or double time) never gets stretched further. It uses one of the transitions that aren't beat-matched (see goal 7).
  - Rendered files are cached, keyed by song, tempo map, and analysis version.
- **Genre guessing:** Essentia's pre-trained genre or style models (Discogs-EffNet family) for the broad style; the YouTube title, channel and tags for hints (for example, Romanian channel names, "sârba" or "manele" in titles); and lyric language from a small language-ID pass over the vocals (for example, Whisper's language detection on a short clip) to tell Romanian hip-hop from hip-hop. Combine them with simple rules that map onto the owner's genre list. Labels the owner sets always win and are stored in `analysis.json`. This is a naive heuristic with a known ceiling; refine it when wrong labels become annoying.

**UI (browser, plain HTML/CSS/JS, no build step):**
- Web Audio API. Each deck is an `AudioBufferSourceNode` going through a gain node. The incoming deck's start is scheduled against `AudioContext.currentTime` so its downbeat lands exactly on the outgoing song's downbeat. The crossfader animates both gains (equal-power curve) over the planned bars. Optional (builder's call): a low-cut on the incoming deck during the blend so the two bass lines don't stack.
- Waveforms are drawn on a canvas from the engine's precomputed peaks.

## Later: round two, the sampler (not in v1, but don't block it)

A separate tab where the owner pulls pieces out of songs: bar ranges (for example 16 or 32 bars, using the bar grid from the analysis) or stems (drums, vocals, and so on, likely via Demucs). They name and tag them in a library, and later use that library to auto-create something (not yet defined). The only v1 requirement: keep download and analysis as their own module with a clear interface, and keep the bar grid in `analysis.json`, so the sampler can reuse both.

## Left to the builder

- Exact planner weights and scoring, and the exact thresholds (8%, 16 bars to ease back, a 10-song window) where tuning shows better values.
- How to detect that a song has no reliable beat grid.
- Which test tracks to use. Include at least one sârba (fast, 2/4), one Romanian hip-hop track, one pop song with a fade-out ending, and one music video with a spoken intro.
- How the engine pushes updates (polling or SSE).
- Visual styling of the decks.
- Project layout, test approach, and the run command.
