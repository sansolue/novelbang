# NovelBang

A local-first novel writing studio. Write in a calm browser UI, get help from a
language model running on your own machine, and export a store-ready ebook.

- **Your novel is just files** — a folder of Markdown chapters and notes plus one
  `novel.toml`. Edit it with any editor, keep it in git, sync it with anything.
- **Local AI co-writer** — continue from the cursor, rewrite a selection, summarize a
  chapter, or brainstorm in a chat. Works with any OpenAI-compatible server:
  Ollama, llama.cpp `llama-server`, LM Studio, vLLM.
- **Publish** — EPUB 3 (passes EPUBCheck; accepted by KDP, Kobo, Apple Books, Google
  Play, Draft2Digital), print-ready HTML (→ PDF), a static website, a per-chapter
  serial pack for Royal Road / Wattpad / AO3 / Substack, and a Markdown manuscript.

## Quick start

```sh
# 1. Run a local model (any one of these)
ollama serve & ollama pull llama3.1:8b         # → http://localhost:11434/v1
llama-server -m model.gguf --port 8080          # → http://localhost:8080/v1

# 2. Start NovelBang (opens your browser)
./start.sh                       # your novels folder (see below)
./start.sh ~/Writing             # or anywhere you like
```

Without a folder argument, NovelBang uses the folder chosen in the desktop app (saved in
`~/.config/novelbang/app.toml`), else `~/Novels`. `NOVELBANG_WORKSPACE` overrides both.

`start.sh` starts Ollama in the background if it's installed but not running
(set `NOVELBANG_NO_OLLAMA=1` to skip that), then runs `uv run novelbang` with
your arguments (`--port`, `--host`, `--no-browser`).

Then open **Settings**, pick your server preset, click **Detect**, choose a model.

## Desktop app (macOS)

NovelBang also runs as a normal Mac app, in its own window, with no browser and no
terminal. It runs the same server as `start.sh` behind a native window.

```sh
packaging/build-macos.sh      # → dist/NovelBang.app and dist/NovelBang-<version>-<arch>.dmg
```

Open the `.dmg` and drag NovelBang into Applications. The app:

- asks where your novels should live on first launch (`~/Novels` is suggested). Any folder
  works, including one in iCloud Drive or Dropbox. **Change…** next to the folder on the
  home screen switches folders later. If you pick a single novel's folder by mistake, the
  app uses the folder that contains it.
- starts Ollama if the co-writer is set to Ollama and it isn't already running, and
  stops it again on quit (an Ollama that was already running is left alone)
- saves any unsaved text before the window closes or the app quits
- saves exports through a Save dialog; **Show in Finder** in Publish opens the exports folder
- writes a log for debugging (see [Logs](#logs))

To run it from source without building: `uv run --extra desktop novelbang-desktop`.

The build only runs on the Mac you build it on, for that Mac's chip (`arm64` or
`x86_64`). It's ad-hoc signed, so it opens on the Mac that built it. On any other Mac,
macOS blocks it as coming from an unidentified developer. To share it, sign it with an
Apple Developer ID and notarize it. Until then, others can allow it once under
**System Settings → Privacy & Security → Open Anyway**.

### Releasing

Pushing a version tag builds the app on GitHub and publishes a release:

```sh
# set version = "0.2.0" in pyproject.toml, commit, then:
git tag v0.2.0 && git push origin v0.2.0
```

`.github/workflows/release.yml` runs the tests, then builds on Apple silicon and Intel
runners. It checks that the tag matches the version in `pyproject.toml`, confirms the
built app starts, and creates a GitHub Release with both DMGs and notes generated from
the commits. Tags such as `v0.3.0rc1` are marked as prereleases. **Actions → Release →
Run workflow** runs the same build without releasing, to try the pipeline.

## Project layout

```
the-salt-road/
  novel.toml            title, author, blurb, ISBN… and chapter order/status/synopses
  chapters/
    001-chapter-one.md  prose only, Markdown (*italic*, **bold**, *** = scene break)
    002-the-harbour.md
  notes/
    style.md            POV, tense, voice — the AI reads this on every request
    characters.md
    world.md
    outline.md          add any other .md notes you like
  assets/cover.jpg      optional cover (JPEG/PNG, ideally 1600×2560)
  assets/images/        pictures used in chapters (JPEG/PNG/GIF)
  exports/              generated files (git-ignored)
  trash/                deleted chapters and notes end up here, never erased
  .novelbang/           version history and synopsis tracking (see below)
```

Chapter files are named once and never renamed; order lives in `novel.toml`, so
reordering doesn't churn your git history. A `.md` file dropped into `chapters/` by
hand is picked up and added to the end of the book.

Chapters have a **type**: *front matter* (dedication, prologue, epigraph), *chapter*
(numbered), or *back matter* (afterword, acknowledgements, about the author).

## Pictures

Click **Picture…** above a chapter, or paste or drop an image into the text. The file
goes into `assets/images/`, and a Markdown line is inserted where the cursor is:

```markdown
![Map of the coast road](../assets/images/map.jpg "The coast road, as Mara drew it")
```

The text in brackets is the alt text that screen readers announce. The quoted part is
optional and becomes a caption. A picture on its own line is centred and scaled to fit
the reader's screen. Each export packs pictures in the way its format needs: inside the
EPUB, embedded in the print HTML, and as an `assets/` folder in the website and serial
pack. Use JPEG for photos and PNG for line art, at about 1600 px on the long side.
Publish checks warn about oversized pictures and flag missing ones.

Pictures are shrunk to 1600 px on the long side when you add them (2400 px or full
size are options in **Book details**). Shrinking also drops camera metadata such as GPS
location. GIFs are kept as they are, so animations survive. **Preview** above the text
shows the chapter as the book will render it, using the ebook's own stylesheet, with
pictures and captions in place and any missing pictures listed.

## Chapter synopses (the co-writer's memory)

A local model can't read your whole book, so it relies on a short synopsis
per chapter to remember what happened earlier. NovelBang keeps them honest:

- **Out-of-date detection.** When a synopsis is saved, NovelBang records a
  fingerprint of the chapter's paragraphs. Once more than a quarter of the text
  is new or gone, the chapter gets a ↻ marker and a hint under its synopsis.
  Chapters with 50+ words and no synopsis are flagged too.
- **↻ Synopses** (under the chapter list) regenerates flagged chapters in book
  order, each one reading the new synopses of the chapters before it. Review
  and edit them, then apply; a history version is saved first. Local models
  sometimes misstate details, so read before applying.
- Synopses are written for continuity: key events, then where things stand at
  the end (who is where, what's been revealed, open threads). Long chapters are
  trimmed from the middle so the ending is always included.
- When you **Continue** near the start of a chapter, the co-writer also sees
  the closing lines of the previous chapter.

## Version history

Every chapter and note has a **History** button showing earlier versions with
word counts, a word-by-word diff against the current text, and one-click
restore. Versions are saved automatically:

- a minute after you stop typing, and every 5 minutes during long sessions
- when you open a project (catching edits made in other editors)
- right before an AI continuation or rewrite is inserted, and before a restore
- when NovelBang shuts down

**★ Save milestone…** records a named version of the whole project
(“First draft complete”). Restoring never loses anything: the current text is
saved as a version first.

History is a private git repository in `.novelbang/history.git`, separate from
any git repo you keep yourself (it's hidden from yours, and ignores your global
git config). It needs `git` installed; without it the History button is hidden.
You can browse it with normal tools:

```sh
git --git-dir=.novelbang/history.git --work-tree=. log --stat
```

## How the co-writer uses context

Local models have small context windows, so each request is trimmed to the
**context budget** in Settings (characters, ≈4 per token). It includes, in priority
order: book title/genre/blurb, your notes (style → characters → world → outline →
others), synopses of the previous chapters, and the text around your cursor.
Writing a short synopsis per chapter (or clicking **Summarize**) is the single
best way to keep a long novel consistent. `<think>` blocks from reasoning models
are stripped automatically.

Shortcuts: `⌘/Ctrl+J` continue · `⌘/Ctrl+I` italic · `⌘/Ctrl+B` bold ·
`⌘/Ctrl+S` save now · **Focus** hides the side panels (`Esc` to leave).

## Logs

NovelBang keeps a log for tracking down problems. **Settings** shows where it is, and
in the desktop app **Show in Finder** opens it. The desktop app, `start.sh` and errors
the UI runs into all write to the same file:

| | |
|---|---|
| macOS | `~/Library/Logs/NovelBang/novelbang.log` |
| Linux | `~/.local/state/novelbang/novelbang.log` |
| Windows | `%LOCALAPPDATA%\NovelBang\Logs\novelbang.log` |

It records:
- startup, with the version, OS, workspace folder and model server
- each AI request: the model, prompt size, time to first output, duration, and whether
  it finished, failed (and why) or was stopped
- exports, restores, milestones, added pictures and settings changes
- every error, including full tracebacks for crashes and errors seen in the UI

It never records what you wrote: no chapter text, notes, prompts, model replies or
API keys. That makes it safe to attach to a bug report. Some model servers quote the
whole request back in their error messages. Before an AI error goes into the log, it's
checked against the text sent to and received from the model. If any of that is quoted,
the log notes that the message was left out (you still see the full message in the app). The file rotates at 2 MB and
keeps four old copies. `NOVELBANG_DEBUG=1` also logs every HTTP request, and
`NOVELBANG_LOG_DIR` puts the log somewhere else. When running from a terminal,
warnings and errors also print there.

## Development

```sh
uv run pytest
uv run novelbang /tmp/ws --no-browser --port 8765
```

- `src/novelbang/project.py` — the on-disk format
- `src/novelbang/llm.py` — streaming OpenAI-compatible client; settings in
  `~/.config/novelbang/settings.toml`
- `src/novelbang/prompts.py` — prompt building and context trimming
- `src/novelbang/history.py` — version history (private git repo per project) and word diff
- `src/novelbang/export/` — EPUB and web exporters (pure Python, no pandoc)
- `src/novelbang/server.py` — FastAPI JSON API; `static/` — the UI (no build step)
- `src/novelbang/desktop.py` — the desktop app (pywebview window around the server);
  `packaging/` — PyInstaller spec, build script and icon (`make_icon.py` draws it)

The server binds to `127.0.0.1` and rejects requests with a foreign `Host` header.
To reach it from another machine, bind with `--host 0.0.0.0` and name the host clients
will use with `--allow-host my-laptop.local`.
It has no authentication — don't expose it to a network you don't trust.
