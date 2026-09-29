"""Prompt construction for the co-writer actions.

Local models have small context windows, so everything is trimmed to a
character budget (LLMSettings.context_chars): project notes get roughly a
third, the manuscript text near the cursor gets the rest.
"""

from __future__ import annotations

from .project import Project

SYSTEM = (
    "You are a skilled fiction co-writer helping an author with their novel. "
    "Match the author's voice, tense, point of view and style exactly. "
    "Stay consistent with the characters, world and outline you are given. "
    "Never add commentary, headings, notes or explanations unless asked — "
    "when asked for prose, reply with prose only."
)

# Notes the model always sees, in priority order; other notes are included if room.
PRIORITY_NOTES = ("style.md", "characters.md", "world.md", "outline.md")


def _head(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " […]"


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else "[…] " + text[-limit:].split(" ", 1)[-1]


def _ends(text: str, limit: int) -> str:
    """Keep the opening and (mostly) the ending: endings matter most for what comes next."""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = _head(text, limit * 2 // 5).removesuffix(" […]")
    return f"{head}\n\n[… middle of the chapter omitted …]\n\n{_tail(text, limit * 3 // 5)}"


def book_context(project: Project, budget: int) -> str:
    m = project.meta
    parts = [f"Title: {m.title}"]
    if m.genre:
        parts.append(f"Genre: {m.genre}")
    if m.description:
        parts.append(f"Blurb: {_head(m.description, 800)}")
    header = "\n".join(parts)

    notes = project.list_notes()
    ordered = [n for n in PRIORITY_NOTES if n in notes] + [n for n in notes if n not in PRIORITY_NOTES]
    remaining = budget - len(header)
    sections = []
    for name in ordered:
        if remaining < 200:
            break
        body = project.read_note(name).strip()
        if not body:
            continue
        chunk = _head(body, max(remaining // 2, 400))
        sections.append(f"## Notes: {name[:-3]}\n{chunk}")
        remaining -= len(chunk)
    return header + ("\n\n" + "\n\n".join(sections) if sections else "")


def story_so_far(
    project: Project, file: str, budget: int = 2000, overrides: dict[str, str] | None = None
) -> str:
    """Synopses of the chapters before `file`, most recent kept when trimming.

    `overrides` replaces stored synopses, e.g. ones regenerated earlier in a batch.
    """
    overrides = overrides or {}
    lines = []
    for ch in project.chapters:
        if ch.file == file:
            break
        synopsis = overrides.get(ch.file, ch.synopsis).strip()
        if synopsis:
            lines.append(f"- {ch.title}: {synopsis}")
    return _tail("\n".join(lines), budget)


def previous_ending(project: Project, file: str, limit: int = 1500) -> tuple[str, str] | None:
    """(title, closing text) of the nearest earlier chapter that has any text."""
    files = [c.file for c in project.chapters]
    for ch in reversed(project.chapters[: files.index(file)]):
        if project.is_missing(ch.file):
            continue
        text = project.read_chapter(ch.file).strip()
        if text:
            return ch.title, _tail(text, limit)
    return None


def _context_block(project: Project, file: str | None, budget: int) -> str:
    block = "# About the book\n" + book_context(project, budget // 3)
    if file:
        so_far = story_so_far(project, file, budget // 6)
        if so_far:
            block += "\n\n# Story so far (chapter synopses)\n" + so_far
        ch = project.chapter(file)
        block += f"\n\n# Current chapter: {ch.title}"
        if ch.synopsis:
            block += f"\nPlanned synopsis: {ch.synopsis}"
    return block


def continue_messages(
    project: Project, file: str, before: str, after: str, instruction: str, budget: int
) -> list[dict]:
    ctx = _context_block(project, file, budget)
    # Near the start of a chapter, show how the previous one ended so the story flows on.
    if len(before) < 3000 and (prev := previous_ending(project, file, min(1500, budget // 8))):
        ctx += f"\n\n# End of the previous chapter ({prev[0]})\n{prev[1]}"
    text_budget = max(budget - len(ctx), 2000)
    user = f"{ctx}\n\n# Chapter text so far\n{_tail(before, text_budget)}"
    if after.strip():
        user += f"\n\n# Text that follows (your passage must lead into it)\n{_head(after, 1500)}"
    user += "\n\n# Task\nContinue the chapter from exactly where the text stops."
    if instruction.strip():
        user += f" Direction from the author: {instruction.strip()}"
    user += " Write only the new prose, picking up mid-sentence if needed. Do not repeat existing text."
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def rewrite_messages(
    project: Project, file: str, selection: str, before: str, after: str, instruction: str, budget: int
) -> list[dict]:
    ctx = _context_block(project, file, budget // 2)
    task = instruction.strip() or "Improve the prose: tighten it, sharpen imagery, keep the meaning."
    user = (
        f"{ctx}\n\n# Surrounding text (for context only)\n"
        f"{_tail(before, 1500)}\n[[PASSAGE]]\n{_head(after, 800)}\n\n"
        f"# Passage to rewrite\n{selection}\n\n"
        f"# Task\nRewrite the passage. {task} "
        "Reply with only the rewritten passage, ready to replace the original."
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def summarize_messages(
    project: Project, file: str, text: str, budget: int, overrides: dict[str, str] | None = None
) -> list[dict]:
    """Messages asking for a synopsis that later chapters can rely on for continuity."""
    ch = project.chapter(file)
    so_far = story_so_far(project, file, budget // 5, overrides)
    user = f"Novel: “{project.meta.title}”\n\n"
    if so_far:
        user += f"# Story so far (earlier chapters)\n{so_far}\n\n"
    user += (
        f"# Chapter to summarise: {ch.title}\n{_ends(text, max(budget - len(so_far) - 600, 2000))}\n\n"
        "# Task\nWrite a synopsis of this chapter for the author's reference, in 3–5 sentences of "
        "plain present-tense prose. Cover the key events and what changes, then end with where "
        "things stand as the chapter closes: where the main characters are, what has been "
        "revealed or decided, and any open threads. Use the same names as the text and the "
        "earlier synopses. Be literal: state only what happens on the page, exactly as it "
        "happens — who does what, and who has what at the end. Do not infer motives or events "
        "the text doesn't show. No preamble, headings or lists."
    )
    return [
        {
            "role": "system",
            "content": "You write precise, factual chapter synopses that help a novelist keep "
            "continuity across a long book. Never invent events that are not in the text.",
        },
        {"role": "user", "content": user},
    ]


def chat_messages(
    project: Project, file: str | None, excerpt: str, history: list[dict], budget: int
) -> list[dict]:
    ctx = _context_block(project, file, budget // 2)
    system = (
        "You are a thoughtful story editor and brainstorming partner for a novelist. "
        "Be concrete and specific, offer options, and keep answers focused. "
        "Use the project context below.\n\n" + ctx
    )
    if excerpt.strip():
        system += "\n\n# Excerpt the author is looking at\n" + _tail(excerpt, budget // 3)
    turns = [
        {"role": t["role"], "content": str(t["content"])}
        for t in history[-20:]
        if t.get("role") in ("user", "assistant")
    ]
    return [{"role": "system", "content": system}, *turns]
