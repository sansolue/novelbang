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
./start.sh                       # projects live in ~/Novels
./start.sh ~/Writing             # or anywhere you like
```

`start.sh` starts Ollama in the background if it's installed but not running
(set `NOVELBANG_NO_OLLAMA=1` to skip that), then runs `uv run novelbang` with
your arguments (`--port`, `--host`, `--no-browser`).

Then open **Settings**, pick your server preset, click **Detect**, choose a model.

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
  exports/              generated files (git-ignored)
  trash/                deleted chapters and notes end up here, never erased
```

Chapter files are named once and never renamed; order lives in `novel.toml`, so
reordering doesn't churn your git history. A `.md` file dropped into `chapters/` by
hand is picked up and added to the end of the book.

Chapters have a **type**: *front matter* (dedication, prologue, epigraph), *chapter*
(numbered), or *back matter* (afterword, acknowledgements, about the author).

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

## Development

```sh
uv run pytest
uv run novelbang /tmp/ws --no-browser --port 8765
```

- `src/novelbang/project.py` — the on-disk format
- `src/novelbang/llm.py` — streaming OpenAI-compatible client; settings in
  `~/.config/novelbang/settings.toml`
- `src/novelbang/prompts.py` — prompt building and context trimming
- `src/novelbang/export/` — EPUB and web exporters (pure Python, no pandoc)
- `src/novelbang/server.py` — FastAPI JSON API; `static/` — the UI (no build step)

The server binds to `127.0.0.1` and rejects requests with a foreign `Host` header.
To reach it from another machine, bind with `--host 0.0.0.0` and name the host clients
will use with `--allow-host my-laptop.local`.
It has no authentication — don't expose it to a network you don't trust.
