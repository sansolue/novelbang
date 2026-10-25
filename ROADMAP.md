# Roadmap

What NovelBang might do next, grouped by area. Sizes are rough: **S** is a day or so,
**M** a few days, **L** a week or more. Numbers are stable, so an item can be referred to
by number; finished items move to [Done](#done).

## Recommended order

1. **Quick fixes and safety:** 13 (split and merge chapters), 14 (restore deleted
   chapters and notes), 15 (rename notes) and 29 (automatic backups).
2. **3. Daily goals.**
3. **20. AI beta-reader**, which builds on comments.
4. **26. Book design themes**, the biggest step toward replacing Vellum or Atticus.

## Writing day to day

| # | Feature | Size | Notes |
|---|---------|------|-------|
| 3 | Daily goals and session stats | M | Words written today, a daily target, a streak and an optional deadline. Version history already keeps snapshots, so the daily counts could come from those. |
| 4 | Scenes inside chapters | L | Each scene gets its own point-of-view character, status and synopsis. Changes how chapters are stored. |
| 12 | Show formatting in the editor | L | `*italic*` and `**bold**` show as raw asterisks. Real italics need an editor component in place of the plain text box. |
| 13 | Split and merge chapters | S | Split at the cursor, or merge with the next chapter. Imports often break chapters in the wrong place. |
| 14 | Restore deleted chapters and notes | S | They're moved to the project's `trash/` folder, but nothing in the app brings them back. |
| 15 | Rename notes | S | Only chapters can be renamed today. |
| 16 | Read aloud | S | The computer's own voice reads from the cursor, for proofreading by ear. |
| 17 | Note beside the chapter | M | Keep the outline or a character sheet open next to the text while writing. |

## Revising

| # | Feature | Size | Notes |
|---|---------|------|-------|
| 18 | Manuscript check, without AI | M | Overused words, repeated words close together, adverb and filler counts, very long sentences, dialogue tags. Shown as highlights to step through, like find. |
| 19 | Name consistency | S–M | Takes names from the character notes and flags near-misses ("Marra" for "Mara") and names that never appear. |
| 20 | AI beta-reader | M | The co-writer reads a chapter and leaves its notes as comments on the passages. |

## The co-writer

| # | Feature | Size | Notes |
|---|---------|------|-------|
| 21 | Story bible | M–L | Character, place and object entries with alternative names. The co-writer gets only the entries the current scene mentions, which helps local models with small context windows. |
| 22 | Your own AI actions | S | Save your own prompts ("describe the setting", "sharpen the dialogue") as buttons. |
| 23 | Blurb and keywords | S | Draft the book description and store keywords from the chapter synopses. |

## Working with editors

| # | Feature | Size | Notes |
|---|---------|------|-------|
| 24 | Comments in the Word export | S | An option to turn comments into real Word comments, for beta readers. |
| 25 | Bring an editor's changes back | L | Import the tracked changes from an edited `.docx` and accept or reject each one. |

## Publishing

| # | Feature | Size | Notes |
|---|---------|------|-------|
| 26 | Book design themes | M–L | A few looks for the EPUB and PDF: chapter heading styles, drop caps, scene-break ornaments, font choices. Today there is one look. |
| 27 | Ready-made extra pages | S | "Also by", "About the author" and a newsletter sign-up page, filled in from Book details. |
| 28 | Paperback cover template | S–M | Spine width from the page count and paper type, and a template at the right size for KDP or IngramSpark. |

## Safety and platform

| # | Feature | Size | Notes |
|---|---------|------|-------|
| 29 | Automatic backups | S | Zip the novel to another folder on a schedule. Version history lives inside the project folder, so a lost disk takes it too. |
| 30 | Linux desktop app and in-app updates | M | Today the app only says an update exists. |

## Smaller gaps in finished features

- **Import:** Scrivener projects (`.scriv`) aren't read directly. The app asks you to
  compile to Word first.
- **Comments:** they work in chapters only, not in notes.

## Before the next releases

- **Mac signing:** add the five Apple secrets (see [Signing the Mac app](README.md#signing-the-mac-app))
  so releases are signed and notarized.
- **Windows signing:** a certificate, plus the signing provider's setup step in the
  release workflow (see [Signing the Windows app](README.md#signing-the-windows-app)).
- **Windows on real hardware:** so far only CI's automatic install-and-launch check has
  run the Windows app.
- **Microsoft Word:** the Word manuscript has been checked in LibreOffice, not in Word.

## Done

| # | Feature | Version |
|---|---------|---------|
| 1 | Find and replace in a chapter or note | 0.2.0 |
| 2 | Search and replace across the whole book | 0.2.0 |
| 5 | Comments that stay out of the book | next release |
| 6 | Rename, duplicate, archive and delete novels | 0.2.0 |
| 7 | Import from Word, Markdown or plain text | 0.2.0 |
| 8 | Word manuscript export | 0.2.0 |
| 9 | Print-ready PDF export | 0.2.0 |
| 10 | Update check | 0.2.0 |
| 11 | Light and dark appearance | 0.2.0 |
