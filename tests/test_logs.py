import logging
import sys
import threading

import pytest
from fastapi.testclient import TestClient

from novelbang import logs, server
from novelbang.export import EXPORTERS
from novelbang.llm import LLMError
from novelbang.server import create_app


@pytest.fixture
def client(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("NOVELBANG_LOG_DIR", str(tmp_path / "logs"))
    caplog.set_level(logging.INFO)
    app = create_app(tmp_path / "ws", settings_path=tmp_path / "settings.toml")
    c = TestClient(app, base_url="http://127.0.0.1")
    p = c.post("/api/projects", json={"title": "Ash"}).json()
    return c, p["id"], p["chapters"][0]["file"]


def fake_stream(*chunks, error=None):
    async def stream_chat(*args, **kwargs):
        for chunk in chunks:
            yield chunk
        if error:
            raise error

    return stream_chat


def test_log_never_contains_the_writing(client, monkeypatch, caplog):
    c, pid, file = client
    c.put(f"/api/projects/{pid}/chapters/{file}/text", json={"text": "PROSE-SECRET " * 40})
    c.put("/api/settings", json={"model": "llama3", "api_key": "KEY-SECRET"})
    monkeypatch.setattr(server, "stream_chat", fake_stream("REPLY-SECRET ", "more"))
    r = c.post(f"/api/projects/{pid}/ai/continue",
               json={"file": file, "before": "BEFORE-SECRET", "instruction": "ASK-SECRET"})
    assert "REPLY-SECRET" in r.text
    assert "AI continue: model llama3" in caplog.text
    assert "AI continue finished" in caplog.text and "17 chars" in caplog.text
    for secret in ("PROSE-SECRET", "KEY-SECRET", "BEFORE-SECRET", "ASK-SECRET", "REPLY-SECRET"):
        assert secret not in caplog.text


def test_ai_failures_are_logged(client, monkeypatch, caplog):
    c, pid, file = client
    monkeypatch.setattr(server, "stream_chat", fake_stream(error=LLMError("Cannot reach http://x: refused")))
    r = c.post(f"/api/projects/{pid}/ai/continue", json={"file": file})
    assert '"error"' in r.text
    failed = [rec for rec in caplog.records if "AI continue failed" in rec.getMessage()]
    assert failed and failed[0].levelno == logging.WARNING and "refused" in failed[0].getMessage()


def test_crashes_are_logged_with_traceback(client, monkeypatch, caplog):
    c, pid, _ = client

    def broken(project, out_dir=None):
        raise RuntimeError("exporter exploded")

    monkeypatch.setitem(EXPORTERS, "epub", (*EXPORTERS["epub"][:2], broken))
    r = c.post(f"/api/projects/{pid}/export/epub")
    assert r.status_code == 500 and "novelbang.log" in r.json()["detail"]
    crash = next(rec for rec in caplog.records if "crashed" in rec.getMessage())
    assert crash.levelno == logging.ERROR and crash.exc_info and "exporter exploded" in caplog.text


def test_handled_errors_say_why(client, caplog):
    c, pid, _ = client
    c.get(f"/api/projects/{pid}/chapters/nope.md/text")
    c.post(f"/api/projects/{pid}/ai/continue", json={})
    c.post(f"/api/projects/{pid}/history", json={"kind": "bogus"})
    c.post(f"/api/projects/{pid}/notes", json={})
    assert "No chapter 'nope.md'" in caplog.text
    assert "→ 400: Open a chapter first" in caplog.text
    assert "→ 400: kind must be milestone or checkpoint" in caplog.text
    assert "→ 422: body.name: Field required" in caplog.text


def test_ui_errors_and_about(client, caplog):
    c, _, _ = client
    c.post("/api/client-log", json={"message": "x" * 5000, "stack": "at f (app.js:1)", "where": "#ash"})
    rec = next(r for r in caplog.records if r.name == "novelbang.ui")
    assert rec.levelno == logging.ERROR and len(rec.getMessage()) < 1100 and "app.js:1" in rec.getMessage()
    about = c.get("/api/about").json()
    assert about["log"].endswith("novelbang.log") and about["version"]


@pytest.fixture
def clean_logging():
    root = logging.getLogger()
    saved = root.handlers[:], root.level, sys.excepthook, threading.excepthook
    yield
    for h in root.handlers[:]:
        if h not in saved[0]:
            root.removeHandler(h)
            h.close()
    root.handlers[:], (root.level, sys.excepthook, threading.excepthook) = saved[0], saved[1:]


def test_setup_logging_writes_the_file(tmp_path, monkeypatch, clean_logging):
    monkeypatch.setenv("NOVELBANG_LOG_DIR", str(tmp_path / "logs"))
    path = logs.setup_logging(console=False)
    assert logs.setup_logging(console=False) == path  # a second call adds nothing
    assert sum(h.get_name() == "novelbang-file" for h in logging.getLogger().handlers) == 1
    try:
        raise ValueError("boom")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    t = threading.Thread(target=lambda: 1 / 0, name="worker")
    t.start()
    t.join()
    for h in logging.getLogger().handlers:
        h.flush()
    text = path.read_text()
    assert "NovelBang" in text and "starting" in text
    assert "Uncaught exception" in text and "ValueError: boom" in text
    assert "in thread worker" in text and "ZeroDivisionError" in text


# ---- nothing written or generated reaches the log ----------------------------

import json as _json
import socket as _socket
import time as _time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from novelbang.logs import quotes_writing, without_writing

SECRETS = ("PROSE-SECRET", "ASK-SECRET", "REPLY-SECRET", "KEY-SECRET", "BLURB-SECRET")


@pytest.fixture(scope="module")
def model_server():
    """An OpenAI-compatible server. Model "echo" fails the way vLLM does, quoting the request."""
    fake = FastAPI()

    @fake.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        if body["model"] == "echo":
            # Quotes the end of the request, where the chapter text and instruction are.
            quoted = _json.dumps(body["messages"][-1]["content"][-250:])
            return JSONResponse({"object": "error", "message": f"invalid input: {quoted}"}, 400)

        async def stream():
            for word in ("Once ", "REPLY-SECRET ", "upon a time."):
                yield f"data: {_json.dumps({'choices': [{'delta': {'content': word}}]})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    with _socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(fake, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        _time.sleep(0.02)
    yield f"http://127.0.0.1:{port}/v1"
    srv.should_exit = True
    t.join(5)


def ai_request(c, pid, file, model_server, model):
    c.put(f"/api/projects/{pid}/chapters/{file}/text", json={"text": "PROSE-SECRET walked home. " * 30})
    c.put("/api/settings", json={"base_url": model_server, "model": model, "api_key": "KEY-SECRET"})
    return c.post(f"/api/projects/{pid}/ai/continue",
                  json={"file": file, "before": "PROSE-SECRET walked home.", "instruction": "ASK-SECRET"})


def test_real_reply_is_not_logged(client, model_server, caplog):
    c, pid, file = client
    r = ai_request(c, pid, file, model_server, "llama")
    assert "REPLY-SECRET" in r.text  # the author gets the reply…
    assert "AI continue finished" in caplog.text and "first output" in caplog.text
    for secret in SECRETS:
        assert secret not in caplog.text  # …the log doesn't


def test_server_error_quoting_the_prompt_is_left_out(client, model_server, caplog):
    c, pid, file = client
    r = ai_request(c, pid, file, model_server, "echo")
    assert "PROSE-SECRET" in r.text and "ASK-SECRET" in r.text  # the author still sees the full error
    failed = next(rec.getMessage() for rec in caplog.records if "AI continue failed" in rec.getMessage())
    assert "left out" in failed
    for secret in SECRETS:
        assert secret not in caplog.text


def test_crash_quoting_the_prompt_is_left_out(client, monkeypatch, caplog):
    c, pid, file = client
    c.put(f"/api/projects/{pid}/chapters/{file}/text", json={"text": "PROSE-SECRET walked home. " * 30})

    async def broken(*args, **kwargs):
        yield "partial REPLY-SECRET text "
        raise ValueError("could not parse: PROSE-SECRET walked home")

    monkeypatch.setattr(server, "stream_chat", broken)
    c.post(f"/api/projects/{pid}/ai/continue", json={"file": file, "before": "PROSE-SECRET walked home."})
    assert "AI continue crashed: ValueError (details left out" in caplog.text
    for secret in SECRETS:
        assert secret not in caplog.text


def test_invalid_values_are_not_logged(client, caplog):
    c, pid, _ = client
    assert c.put("/api/settings", json={"api_key": ["KEY-SECRET"]}).status_code == 400
    assert c.put(f"/api/projects/{pid}/meta", json={"description": {"x": "BLURB-SECRET"}}).status_code == 400
    assert "Invalid api_key: expected str, got list" in caplog.text
    assert "Invalid description: expected str, got dict" in caplog.text
    for secret in SECRETS:
        assert secret not in caplog.text


@pytest.mark.parametrize("message, quotes", [
    ("400: {\"input\": {\"content\": \"Mara found the letter\"}}", True),
    ("400: {\"input\": \"It\\u2019s fine, she said\"}", True),  # JSON escapes decoded
    ("400: [{'input': {'messages': [{'content': 'found the letter'}]}}]", True),  # Python repr (vLLM)
    ("Error: DARKER is not a valid option", True),  # a short instruction, matched whole
    ("400: model is required", False),
    ("Cannot reach http://localhost:11434/v1: connection refused", False),
])
def test_quotes_writing(message, quotes):
    writing = ["Mara found the letter under the floorboard. It’s fine, she said.", "darker"]
    assert quotes_writing(message, writing) is quotes
    assert ("left out" in without_writing(message, writing)) is quotes
