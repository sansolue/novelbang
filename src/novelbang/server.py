from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, comments, config, prompts
from .export import EXPORTERS
from .export.render import BOOK_CSS, RenderedChapter, render_markdown
from .history import HistoryTracker, ProjectHistory, git_available, word_diff
from .importer import MAX_IMPORT_BYTES, import_manuscript
from .logs import log_path, quotes_writing, without_writing
from .llm import THINKING, LLMError, LLMSettings, list_models, stream_chat
from .project import CHAPTER_KINDS, CHAPTER_STATUSES, Project, ProjectError, Workspace, check_filename
from .search import pattern, replace_in, search
from .updates import UpdateChecker

STATIC = Path(__file__).parent / "static"
IMAGE_MIME = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif"}
log = logging.getLogger("novelbang")


class NewProject(BaseModel):
    title: str
    author: str = ""


class Rename(BaseModel):
    title: str


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


class DocRef(BaseModel):
    type: str
    name: str


class ReplaceRequest(BaseModel):
    query: str
    replacement: str = ""
    match_case: bool = False
    whole_word: bool = False
    docs: list[DocRef] | None = None  # None: every chapter and note


class ResolveComment(BaseModel):
    file: str
    index: int  # the comment's place among the chapter's comments
    raw: str  # its markup, checked before it's taken out


class UpdatePrefs(BaseModel):
    enabled: bool


class PreviewRequest(BaseModel):
    text: str
    file: str | None = None  # the chapter it belongs to, for its heading


class ClientLog(BaseModel):
    level: str = "error"
    message: str
    stack: str = ""
    where: str = ""  # the page's URL hash: which view it happened in


class AIRequest(BaseModel):
    file: str | None = None
    before: str = ""  # text before the cursor / selection
    after: str = ""  # text after the cursor / selection
    selection: str = ""
    instruction: str = ""
    history: list[dict] = []
    max_tokens: int | None = None
    synopses: dict[str, str] = {}  # summarize: synopses regenerated earlier in a batch


def create_app(
    workspace_dir: Path,
    settings_path: Path | None = None,
    extra_hosts: tuple[str, ...] = (),
    updates: UpdateChecker | None = None,
) -> FastAPI:
    """`updates`: look for new releases (the desktop app); the command line doesn't."""
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
                elif path.startswith("assets/images/"):
                    name = f"picture {path.removeprefix('assets/images/')}"
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
    app.state.workspace = ws  # the desktop app switches folders through this
    settings = LLMSettings.load(settings_path)
    log.info(
        "Workspace %s; model server %s, model %s; version history %s",
        workspace_dir, settings.base_url, settings.model or "(none)", "on" if git_available() else "off (no git)",
    )

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Bugs: keep the traceback, and tell the UI where to find it.
            log.exception("%s %s crashed", request.method, request.url.path)
            return JSONResponse({"detail": f"Something went wrong. Details are in the log: {log_path()}"}, 500)
        log.debug("%s %s → %d in %.0f ms", request.method, request.url.path, response.status_code,
                  (time.perf_counter() - start) * 1000)
        return response

    @app.exception_handler(ProjectError)
    async def project_error(request: Request, exc: ProjectError):
        log.warning("%s %s: %s", request.method, request.url.path, exc)
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        level = logging.INFO if exc.status_code == 404 else logging.WARNING
        log.log(level, "%s %s → %d: %s", request.method, request.url.path, exc.status_code, exc.detail)
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        log.warning("%s %s → 422: %s", request.method, request.url.path, problems)
        return await request_validation_exception_handler(request, exc)

    def summary(p: Project) -> dict:
        stats = p.chapter_stats()
        return {
            "id": p.id,
            "path": str(p.root),
            "meta": p.meta.__dict__,
            "words": sum(s["words"] for s in stats.values()),
            "chapters": [
                {**c.__dict__, **stats[c.file], "missing": p.is_missing(c.file)} for c in p.chapters
            ],
            "notes": p.list_notes(),
            "has_cover": p.cover_path() is not None,
            "history": git_available(),
        }

    # ---- projects ----------------------------------------------------------

    def card(p: Project) -> dict:
        return {"id": p.id, "title": p.meta.title, "author": p.meta.author,
                "words": sum(p.word_counts().values()), "chapters": len(p.chapters),
                "has_cover": p.cover_path() is not None}

    @app.get("/api/projects")
    def projects():
        archived = []
        for p in ws.list_archived():
            try:
                archived.append(card(p))
            except Exception:
                log.exception("Can't read archived novel %s", p.root)
        return {
            "workspace": str(ws.root),
            "projects": [card(p) for p in ws.list()],
            "archived": archived,
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

    # ---- managing whole novels (from the home screen) ------------------------

    def before_moving(p: Project, what: str) -> None:
        """Keep the history whole: record pending edits, and stop tracking the old folder."""
        tracker.forget(p.root)
        try:
            snapshot(p, "checkpoint", f"Before {what}")
        except ProjectError:
            log.exception("Snapshot before %s of %s failed", what, p.id)

    @app.post("/api/import")
    async def import_file(name: str, request: Request, author: str = ""):
        data = await request.body()
        if len(data) > MAX_IMPORT_BYTES:
            raise HTTPException(413, f"That file is over {MAX_IMPORT_BYTES // 2**20} MB")
        start = time.perf_counter()
        p = await asyncio.to_thread(import_manuscript, ws, name, data, author)
        words = sum(p.word_counts().values())
        # The file name can be a working title, so only its type is logged.
        log.info("Imported a %s file (%d KB) as %s: %d chapters, %d words in %.1f s", Path(name).suffix.lower(),
                 len(data) // 1024, p.id, len(p.chapters), words, time.perf_counter() - start)
        return {**card(p), "chapter_count": len(p.chapters)}

    @app.post("/api/projects/{pid}/rename")
    def rename_project(pid: str, body: Rename):
        before_moving(ws.get(pid), "renaming the novel")
        p = ws.rename(pid, body.title)
        log.info("Renamed novel %s to %s", pid, p.id)
        return card(p)

    @app.post("/api/projects/{pid}/duplicate")
    def duplicate_project(pid: str):
        p = ws.duplicate(pid)
        log.info("Duplicated novel %s as %s", pid, p.id)
        return card(p)

    @app.post("/api/projects/{pid}/archive")
    def archive_project(pid: str):
        before_moving(ws.get(pid), "archiving the novel")
        ws.archive(pid)
        log.info("Archived novel %s", pid)
        return {"ok": True}

    @app.post("/api/archived/{pid}/restore")
    def unarchive_project(pid: str):
        p = ws.unarchive(pid)
        log.info("Restored archived novel %s", p.id)
        return card(p)

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: str):
        before_moving(ws.get(pid), "deleting the novel")
        dest = ws.delete(pid)
        log.info("Moved novel %s to %s", pid, dest)
        return {"moved_to": str(dest)}

    @app.delete("/api/archived/{pid}")
    def delete_archived(pid: str):
        dest = ws.delete(pid, archived=True)
        log.info("Moved archived novel %s to %s", pid, dest)
        return {"moved_to": str(dest)}

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

    # ---- search --------------------------------------------------------------
    # The search text is the writing's, so it's never logged (see logs.py).

    @app.get("/api/projects/{pid}/search")
    def search_project(pid: str, q: str, match_case: bool = False, whole_word: bool = False):
        return search(ws.get(pid), pattern(q, match_case, whole_word))

    @app.post("/api/projects/{pid}/replace")
    def replace_project(pid: str, body: ReplaceRequest):
        pat = pattern(body.query, body.match_case, body.whole_word)
        only = None if body.docs is None else {(d.type, d.name) for d in body.docs}
        with ws.edit(pid) as p:
            snapshot(p, "checkpoint", f"Before replacing “{body.query}” with “{body.replacement}”")
            result = replace_in(p, pat, body.replacement, only)
            log.info("Replaced %d matches in %d documents of %s", result["replaced"], len(result["changed"]), pid)
            return {**result, "project": summary(p)}

    # ---- comments ------------------------------------------------------------
    # Like search, comments are the writing's own words and are never logged.

    @app.get("/api/projects/{pid}/comments")
    def list_comments(pid: str):
        p = ws.get(pid)
        docs = []
        for c in p.chapters:
            if not p.is_missing(c.file) and (found := comments.find(p.read_chapter(c.file))):
                docs.append({"name": c.file, "title": c.title, "comments": found})
        return {"total": sum(len(d["comments"]) for d in docs), "docs": docs}

    @app.post("/api/projects/{pid}/comments/resolve")
    def resolve_comment(pid: str, body: ResolveComment):
        with ws.edit(pid) as p:
            try:
                text = comments.resolve(p.read_chapter(body.file), body.index, body.raw)
            except ValueError as e:
                raise HTTPException(409, str(e)) from None
            p.write_chapter(body.file, text)
            log.info("Resolved a comment in %s/%s", pid, body.file)
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

    # ---- pictures ----------------------------------------------------------

    @app.post("/api/projects/{pid}/images")
    async def add_image(pid: str, name: str, request: Request):
        ext = IMAGE_MIME.get(request.headers.get("content-type", "").split(";")[0])
        if not ext:
            raise HTTPException(400, "Pictures must be JPEG, PNG or GIF")
        data = await request.body()
        if len(data) > 50 * 1024 * 1024:
            raise HTTPException(413, "Picture is too large (50 MB max)")
        with ws.edit(pid) as p:
            # The browser's type wins over the file's extension (pasted screenshots are "image.png").
            src = p.add_image(data, Path(name).stem + ext)
        log.info("Picture added to %s: %s (%d KB)", pid, src.rsplit("/", 1)[-1], len(data) // 1024)
        return {"src": src}

    @app.get("/api/projects/{pid}/assets/{path:path}")
    def get_asset(pid: str, path: str):
        # Only pictures inside assets/, checked the same way the exporters check them.
        found = ws.get(pid).image_path(f"../assets/{path}")
        if not found:
            raise HTTPException(404)
        return FileResponse(found)

    @app.post("/api/projects/{pid}/preview")
    def preview(pid: str, body: PreviewRequest):
        p = ws.get(pid)
        assets = p.assets_dir.resolve()
        html, _, missing = render_markdown(
            p, body.text, lambda path: f"/api/projects/{quote(pid)}/assets/{quote(path.relative_to(assets).as_posix())}"
        )
        if body.file:
            num = dict((c.file, n) for c, n in p.numbered()).get(body.file)
            html = RenderedChapter(p.chapter(body.file), num, html).heading_html(p.meta.number_chapters) + html
        return {"html": html, "css": BOOK_CSS, "missing": missing}

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
        log.info("%s of %s: %s", body.kind.capitalize(), pid, version or "nothing to save")
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
            log.info("Restored %s/%s to version %s (%s)", pid, path, body.version[:10], when)
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
        start = time.perf_counter()
        out = EXPORTERS[fmt][2](p)
        log.info("Exported %s as %s: %s (%d KB) in %.1f s", pid, fmt, out.name, out.stat().st_size // 1024,
                 time.perf_counter() - start)
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
        log.info(
            "Settings saved: server %s, model %s, temperature %s, max tokens %s, context %s chars, thinking %s",
            settings.base_url, settings.model or "(none)", settings.temperature, settings.max_tokens,
            settings.context_chars, "on" if settings.thinking else "off",
        )
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
            found = await list_models(probe)
        except LLMError as e:
            raise HTTPException(502, str(e))
        log.info("%d models at %s", len(found), probe.base_url)
        return {"models": found}

    @app.post("/api/projects/{pid}/ai/{action}")
    async def ai(pid: str, action: str, body: AIRequest):
        p = ws.get(pid)
        budget = settings.context_chars
        max_tokens = body.max_tokens
        temperature = None  # the user's setting
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
                msgs = prompts.summarize_messages(
                    p, body.file, p.read_chapter(body.file), budget, body.synopses
                )
                # Reasoning models think within the same token budget, so give them room.
                max_tokens = 2500 if settings.thinking else 350
                temperature = 0.3  # synopses must be factual, not creative
            case "chat":
                msgs = prompts.chat_messages(p, body.file, body.before, body.history, budget)
            case _:
                raise HTTPException(404, f"Unknown action {action}")

        # Sizes and timings only: prompts and replies are the author's writing.
        prompt_chars = sum(len(m.get("content") or "") for m in msgs)
        log.info("AI %s: model %s at %s, %d messages, %d prompt chars, max %s tokens",
                 action, settings.model or "(none)", settings.base_url, len(msgs), prompt_chars,
                 max_tokens or settings.max_tokens)

        async def ndjson():
            start, first, outcome = time.perf_counter(), None, "stopped by the user"
            received: list[str] = []

            def writing() -> list[str]:
                # What an error message must not quote in the log: the prompt sent and the reply so far.
                return [m.get("content") or "" for m in msgs] + ["".join(received)]

            try:
                thinking_sent = False
                async for text in stream_chat(settings, msgs, max_tokens=max_tokens, temperature=temperature):
                    if first is None:
                        first = time.perf_counter() - start
                    if text is THINKING:
                        if not thinking_sent:
                            thinking_sent = True
                            yield json.dumps({"thinking": True}) + "\n"
                    else:
                        received.append(text)
                        yield json.dumps({"t": text}) + "\n"
                outcome = "finished"
            except LLMError as e:
                # The UI shows the server's full message; the log gets it only if it doesn't quote the writing
                # (some servers echo the whole request back in their errors).
                outcome = f"failed: {without_writing(str(e), writing())}"
                yield json.dumps({"error": str(e)}) + "\n"
            except Exception as e:
                trace = "".join(traceback.format_exception(e)).rstrip()
                if quotes_writing(trace, writing()):
                    trace = f"{type(e).__name__} (details left out: they quote the text sent to or received from the model)"
                log.error("AI %s crashed: %s", action, trace)
                outcome = "crashed"
                yield json.dumps({"error": f"Something went wrong ({e}). Details are in the log: {log_path()}"}) + "\n"
            finally:
                # Also runs when the client hangs up (Stop): the generator is closed mid-stream.
                level = logging.WARNING if outcome.startswith(("failed", "crashed")) else logging.INFO
                first_s = f"{first:.1f} s" if first is not None else "never"
                log.log(level, "AI %s %s after %.1f s (first output %s, %d chars%s)", action, outcome,
                        time.perf_counter() - start, first_s, sum(map(len, received)),
                        ", thought first" if thinking_sent else "")
            yield json.dumps({"done": True}) + "\n"

        return StreamingResponse(ndjson(), media_type="application/x-ndjson")

    # ---- diagnostics -------------------------------------------------------

    @app.get("/api/about")
    def about():
        return {"version": __version__, "log": str(log_path())}

    @app.get("/api/update")
    async def update_status():
        if updates is None:
            return {"supported": False}
        if not config.check_updates():
            return {"supported": True, "enabled": False}
        return {"supported": True, "enabled": True, **await updates.status()}

    @app.put("/api/update")
    def set_update_prefs(body: UpdatePrefs):
        if updates is None:
            raise HTTPException(400, "Update checks are part of the desktop app")
        config.save_check_updates(body.enabled)
        log.info("Update checks %s", "on" if body.enabled else "off")
        return {"supported": True, "enabled": body.enabled}

    @app.post("/api/client-log")
    def client_log(body: ClientLog):
        """Errors the UI ran into, so they land in the same log as the server's."""
        level = logging.WARNING if body.level == "warning" else logging.ERROR
        detail = f"\n{body.stack[:4000]}" if body.stack else ""
        where = body.where[:200] or "home"
        logging.getLogger("novelbang.ui").log(level, "%s [%s]%s", body.message[:1000], where, detail)
        return {"ok": True}

    # ---- UI ----------------------------------------------------------------

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
