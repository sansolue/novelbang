from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import prompts
from .export import EXPORTERS
from .history import HistoryTracker, ProjectHistory, git_available, word_diff
from .llm import THINKING, LLMError, LLMSettings, list_models, stream_chat
from .project import CHAPTER_KINDS, CHAPTER_STATUSES, Project, ProjectError, Workspace, check_filename

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("novelbang")


class NewProject(BaseModel):
    title: str
    author: str = ""


class NewChapter(BaseModel):
    title: str = "Untitled chapter"
    kind: str = "chapter"
    after: str | None = None


class ChapterPatch(BaseModel):
    title: str | None = None
    kind: str | None = None
    status: str | None = None
    synopsis: str | None = None


class TextBody(BaseModel):
    text: str


class Order(BaseModel):
    files: list[str]


class NewNote(BaseModel):
    name: str


class ModelProbe(BaseModel):
    base_url: str | None = None
    api_key: str | None = None


class Snapshot(BaseModel):
    kind: str = "milestone"
    message: str = ""


class Restore(BaseModel):
    version: str
    type: str
    name: str


class AIRequest(BaseModel):
    file: str | None = None
    before: str = ""  # text before the cursor / selection
    after: str = ""  # text after the cursor / selection
    selection: str = ""
    instruction: str = ""
    history: list[dict] = []
    max_tokens: int | None = None


def create_app(
    workspace_dir: Path, settings_path: Path | None = None, extra_hosts: tuple[str, ...] = ()
) -> FastAPI:
    tracker = HistoryTracker()
    histories: dict[Path, ProjectHistory] = {}

    def history_for(p: Project) -> ProjectHistory:
        return histories.setdefault(p.root, ProjectHistory(p.root))

    def describe(p: Project):
        titles = {f"chapters/{c.file}": c.title for c in p.chapters}

        def label(changes: list[tuple[str, str]]) -> str:
            names: list[str] = []
            for status, path in changes:
                if path in titles or path.startswith("chapters/"):
                    name = titles.get(path, path.removeprefix("chapters/"))
                elif path.startswith("notes/"):
                    name = f"{path.removeprefix('notes/').removesuffix('.md')} (note)"
                elif path == "novel.toml":
                    name = "book details"
                elif path.startswith("assets/"):
                    name = "cover"
                else:
                    name = path
                if status == "D":
                    name = f"removed {name}"
                elif status == "A" and path != "novel.toml":
                    name = f"new {name}"
                if name not in names:
                    names.append(name)
            shown = ", ".join(names[:4])
            return shown + (f" and {len(names) - 4} more" if len(names) > 4 else "")

        return label

    def snapshot(p: Project, kind: str, message: str = "", allow_empty: bool = False) -> str | None:
        if not git_available():
            return None
        return history_for(p).snapshot(kind, message, describe(p), allow_empty)

    def flush_history(force: bool = False) -> None:
        for root in tracker.due(force):
            try:
                snapshot(Project.load(root), "auto")
            except Exception:
                log.exception("Automatic snapshot of %s failed", root)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async def loop():
            while True:
                await asyncio.sleep(15)
                await asyncio.to_thread(flush_history)

        task = asyncio.create_task(loop())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.to_thread(flush_history, True)

    app = FastAPI(title="NovelBang", lifespan=lifespan)
    # Only answer requests addressed to this machine (blocks DNS-rebinding from websites).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "::1", *extra_hosts])
    ws = Workspace(workspace_dir, on_change=lambda p: tracker.touched(p.root))
    settings = LLMSettings.load(settings_path)

    @app.exception_handler(ProjectError)
    async def project_error(_: Request, exc: ProjectError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def summary(p: Project) -> dict:
        counts = p.word_counts()
        return {
            "id": p.id,
            "path": str(p.root),
            "meta": p.meta.__dict__,
            "words": sum(counts.values()),
            "chapters": [
                {**c.__dict__, "words": counts[c.file], "missing": p.is_missing(c.file)} for c in p.chapters
            ],
            "notes": p.list_notes(),
            "has_cover": p.cover_path() is not None,
            "history": git_available(),
        }

    # ---- projects ----------------------------------------------------------

    @app.get("/api/projects")
    def projects():
        return {
            "workspace": str(ws.root),
            "projects": [
                {"id": p.id, "title": p.meta.title, "author": p.meta.author,
                 "words": sum(p.word_counts().values()), "chapters": len(p.chapters),
                 "has_cover": p.cover_path() is not None}
                for p in ws.list()
            ],
            "kinds": CHAPTER_KINDS,
            "statuses": CHAPTER_STATUSES,
        }

    @app.post("/api/projects")
    def create_project(body: NewProject):
        return summary(ws.create(body.title.strip() or "Untitled", body.author.strip()))

    @app.get("/api/projects/{pid}")
    def get_project(pid: str):
        p = ws.get(pid)
        if tracker.first_visit(p.root):
            # Captures edits made while NovelBang wasn't running (other editors, sync).
            try:
                snapshot(p, "session")
            except ProjectError:
                log.exception("Session snapshot of %s failed", p.root)
        return summary(p)

    @app.put("/api/projects/{pid}/meta")
    def put_meta(pid: str, body: dict):
        with ws.edit(pid) as p:
            p.meta.update(body)
            p.save()
            return summary(p)

    # ---- chapters ----------------------------------------------------------

    @app.post("/api/projects/{pid}/chapters")
    def add_chapter(pid: str, body: NewChapter):
        with ws.edit(pid) as p:
            ch = p.add_chapter(body.title.strip() or "Untitled chapter", body.kind, body.after)
            return {"file": ch.file, "project": summary(p)}

    @app.put("/api/projects/{pid}/chapters/{file}")
    def patch_chapter(pid: str, file: str, body: ChapterPatch):
        with ws.edit(pid) as p:
            p.update_chapter(file, **body.model_dump())
            return summary(p)

    @app.delete("/api/projects/{pid}/chapters/{file}")
    def delete_chapter(pid: str, file: str):
        with ws.edit(pid) as p:
            p.delete_chapter(file)
            return summary(p)

    @app.get("/api/projects/{pid}/chapters/{file}/text")
    def chapter_text(pid: str, file: str):
        return {"text": ws.get(pid).read_chapter(file)}

    @app.put("/api/projects/{pid}/chapters/{file}/text")
    def save_chapter_text(pid: str, file: str, body: TextBody):
        with ws.edit(pid) as p:
            p.write_chapter(file, body.text)
        return {"ok": True}

    @app.put("/api/projects/{pid}/order")
    def reorder(pid: str, body: Order):
        with ws.edit(pid) as p:
            p.reorder(body.files)
            return summary(p)

    # ---- notes -------------------------------------------------------------

    @app.post("/api/projects/{pid}/notes")
    def new_note(pid: str, body: NewNote):
        name = body.name.strip()
        name = name if name.endswith(".md") else name + ".md"
        check_filename(name)
        with ws.edit(pid) as p:
            if name not in p.list_notes():
                p.write_note(name, f"# {name[:-3].replace('-', ' ').title()}\n\n")
            return summary(p) | {"name": name}

    @app.get("/api/projects/{pid}/notes/{name}")
    def get_note(pid: str, name: str):
        return {"text": ws.get(pid).read_note(name)}

    @app.put("/api/projects/{pid}/notes/{name}")
    def put_note(pid: str, name: str, body: TextBody):
        with ws.edit(pid) as p:
            p.write_note(name, body.text)
        return {"ok": True}

    @app.delete("/api/projects/{pid}/notes/{name}")
    def delete_note(pid: str, name: str):
        with ws.edit(pid) as p:
            p.delete_note(name)
            return summary(p)

    # ---- cover -------------------------------------------------------------

    @app.get("/api/projects/{pid}/cover")
    def get_cover(pid: str):
        path = ws.get(pid).cover_path()
        if not path:
            raise HTTPException(404)
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    @app.put("/api/projects/{pid}/cover")
    async def put_cover(pid: str, request: Request):
        ctype = request.headers.get("content-type", "")
        ext = {"image/jpeg": "jpg", "image/png": "png"}.get(ctype.split(";")[0])
        if not ext:
            raise HTTPException(400, "Cover must be image/jpeg or image/png")
        data = await request.body()
        if len(data) > 50 * 1024 * 1024:
            raise HTTPException(413, "Cover image is too large (50 MB max)")
        with ws.edit(pid) as p:
            p.set_cover(data, ext)
            return summary(p)

    # ---- history -----------------------------------------------------------

    def doc_path(p: Project, type: str, name: str) -> str:
        if type == "chapter":
            p.chapter(name)
            return f"chapters/{check_filename(name)}"
        if type == "note":
            return f"notes/{check_filename(name)}"
        raise HTTPException(400, "type must be chapter or note")

    @app.get("/api/projects/{pid}/history")
    def history(pid: str, type: str, name: str):
        p = ws.get(pid)
        path = doc_path(p, type, name)
        return {"versions": [v.__dict__ for v in history_for(p).versions(path)]}

    @app.get("/api/projects/{pid}/history/{version}")
    def history_version(pid: str, version: str, type: str, name: str):
        p = ws.get(pid)
        path = doc_path(p, type, name)
        old = history_for(p).read(version, path)
        if old is None:
            raise HTTPException(404, "This document didn't exist in that version")
        current = (p.root / path).read_text("utf-8") if (p.root / path).is_file() else ""
        return {"text": old, "diff": word_diff(old, current)}

    @app.post("/api/projects/{pid}/history")
    def take_snapshot(pid: str, body: Snapshot):
        p = ws.get(pid)
        if body.kind not in ("milestone", "checkpoint"):
            raise HTTPException(400, "kind must be milestone or checkpoint")
        version = snapshot(p, body.kind, body.message.strip(), allow_empty=body.kind == "milestone")
        return {"version": version}

    @app.post("/api/projects/{pid}/history/restore")
    def restore(pid: str, body: Restore):
        with ws.edit(pid) as p:
            path = doc_path(p, body.type, body.name)
            h = history_for(p)
            old = h.read(body.version, path)
            if old is None:
                raise HTTPException(404, "This document didn't exist in that version")
            title = p.chapter(body.name).title if body.type == "chapter" else body.name.removesuffix(".md")
            when = datetime.fromtimestamp(h.timestamp(body.version)).strftime("%Y-%m-%d %H:%M")
            snapshot(p, "checkpoint", f"Before restoring {title}")
            if body.type == "chapter":
                p.write_chapter(body.name, old)
            else:
                p.write_note(body.name, old)
            snapshot(p, "restore", f"Restored {title} to {when}")
            return {"text": old, "project": summary(p)}

    # ---- export ------------------------------------------------------------

    @app.get("/api/projects/{pid}/publish")
    def publish_info(pid: str):
        p = ws.get(pid)
        exports = []
        if p.exports_dir.is_dir():
            for f in sorted(p.exports_dir.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True):
                if f.is_file() and not f.name.endswith(".tmp"):
                    exports.append({"name": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime})
        return {
            "checks": p.publish_checks(),
            "formats": [{"id": k, "label": v[0], "help": v[1]} for k, v in EXPORTERS.items()],
            "exports": exports,
            "exports_dir": str(p.exports_dir),
        }

    @app.post("/api/projects/{pid}/export/{fmt}")
    def export(pid: str, fmt: str):
        if fmt not in EXPORTERS:
            raise HTTPException(404, f"Unknown format {fmt}")
        p = ws.get(pid)
        out = EXPORTERS[fmt][2](p)
        return {"name": out.name, "path": str(out)}

    @app.get("/api/projects/{pid}/exports/{name}")
    def download(pid: str, name: str):
        p = ws.get(pid)
        path = (p.exports_dir / name).resolve()
        if path.parent != p.exports_dir.resolve() or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, filename=name)

    # ---- AI ----------------------------------------------------------------

    @app.get("/api/settings")
    def get_settings():
        return settings.__dict__

    @app.put("/api/settings")
    def put_settings(body: dict):
        try:
            settings.update(body)  # all-or-nothing, so a bad field leaves settings untouched
        except LLMError as e:
            raise HTTPException(400, str(e))
        settings.save(settings_path)
        return settings.__dict__

    @app.post("/api/llm/models")
    async def models(body: ModelProbe):
        # A JSON POST (not a GET) so a cross-origin page can't trigger it without a
        # CORS preflight, and the saved key only ever goes to the saved server.
        probe = LLMSettings(**settings.__dict__)
        if body.base_url is not None and body.base_url.rstrip("/") != settings.base_url.rstrip("/"):
            probe.api_key = ""
        probe.update({"base_url": body.base_url, "api_key": body.api_key})
        try:
            return {"models": await list_models(probe)}
        except LLMError as e:
            raise HTTPException(502, str(e))

    @app.post("/api/projects/{pid}/ai/{action}")
    async def ai(pid: str, action: str, body: AIRequest):
        p = ws.get(pid)
        budget = settings.context_chars
        max_tokens = body.max_tokens
        match action:
            case "continue":
                if not body.file:
                    raise HTTPException(400, "Open a chapter first")
                msgs = prompts.continue_messages(p, body.file, body.before, body.after, body.instruction, budget)
            case "rewrite":
                if not (body.file and body.selection.strip()):
                    raise HTTPException(400, "Select some text in a chapter to rewrite")
                msgs = prompts.rewrite_messages(
                    p, body.file, body.selection, body.before, body.after, body.instruction, budget
                )
                max_tokens = max_tokens or max(settings.max_tokens, len(body.selection) // 2)
            case "summarize":
                if not body.file:
                    raise HTTPException(400, "Open a chapter first")
                msgs = prompts.summarize_messages(p, body.file, p.read_chapter(body.file), budget)
                max_tokens = 250
            case "chat":
                msgs = prompts.chat_messages(p, body.file, body.before, body.history, budget)
            case _:
                raise HTTPException(404, f"Unknown action {action}")

        async def ndjson():
            try:
                thinking_sent = False
                async for text in stream_chat(settings, msgs, max_tokens=max_tokens):
                    if text is THINKING:
                        if not thinking_sent:
                            thinking_sent = True
                            yield json.dumps({"thinking": True}) + "\n"
                    else:
                        yield json.dumps({"t": text}) + "\n"
            except LLMError as e:
                yield json.dumps({"error": str(e)}) + "\n"
            yield json.dumps({"done": True}) + "\n"

        return StreamingResponse(ndjson(), media_type="application/x-ndjson")

    # ---- UI ----------------------------------------------------------------

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
