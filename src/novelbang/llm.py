"""Client for any OpenAI-compatible chat endpoint.

Works with Ollama (http://localhost:11434/v1), llama.cpp's llama-server
(http://localhost:8080/v1), LM Studio (http://localhost:1234/v1), vLLM, etc.
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import httpx
import tomli_w

from .project import convert_field


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "novelbang"


@dataclass
class LLMSettings:
    base_url: str = "http://localhost:11434/v1"
    model: str = ""
    api_key: str = ""
    temperature: float = 0.8
    max_tokens: int = 800
    context_chars: int = 12000
    # Let reasoning models (qwen3, deepseek-r1, gpt-oss…) think before answering.
    # Off by default: thinking is slow and eats the max_tokens budget.
    thinking: bool = False
    timeout: float = 300.0

    @classmethod
    def load(cls, path: Path | None = None) -> LLMSettings:
        path = path or config_dir() / "settings.toml"
        if not path.is_file():
            return cls()
        data = tomllib.loads(path.read_text("utf-8")).get("llm", {})
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Path | None = None) -> None:
        """Write atomically and owner-only: the file holds the API key."""
        path = path or config_dir() / "settings.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(tomli_w.dumps({"llm": asdict(self)}))
            os.chmod(tmp, 0o600)  # in case a stale tmp already existed with wider permissions
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def update(self, data: dict) -> None:
        """Apply the known, non-None fields of `data`; all-or-nothing, LLMError on a bad value."""
        changes = {}
        for f in fields(self):
            if data.get(f.name) is not None:
                try:
                    changes[f.name] = convert_field(getattr(self, f.name), data[f.name])
                except ValueError as e:
                    raise LLMError(f"Invalid {f.name}: {e}") from None
        for key, value in changes.items():
            setattr(self, key, value)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path


class LLMError(Exception):
    pass


async def list_models(s: LLMSettings) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(s.url("/models"), headers=s.headers)
    except httpx.HTTPError as e:
        raise LLMError(f"Cannot reach {s.base_url}: {e}") from e
    if r.status_code != 200:
        raise LLMError(f"{r.status_code} from {s.url('/models')}: {r.text[:300]}")
    return sorted(m["id"] for m in r.json().get("data", []))


# Ways to switch reasoning off: Ollama and vLLM read reasoning_effort,
# llama.cpp's llama-server passes chat_template_kwargs to the Jinja template.
NO_THINKING = {"reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False}}

THINKING = object()  # yielded (at most every chunk) while the model reasons silently


async def stream_chat(
    s: LLMSettings,
    messages: list[dict],
    *,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> AsyncIterator[str | object]:
    """Yield text chunks of the reply, or THINKING while the model is reasoning."""
    if not s.model:
        raise LLMError("No model selected. Open Settings and pick one.")
    payload = {
        "model": s.model,
        "messages": messages,
        "stream": True,
        "temperature": s.temperature if temperature is None else temperature,
        "max_tokens": max_tokens or s.max_tokens,
    }
    extras = {} if s.thinking else NO_THINKING
    timeout = httpx.Timeout(10, read=s.timeout)
    think = _ThinkFilter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await _open_stream(client, s, payload | extras)
            if r.status_code == 400 and extras:
                # A strict server that rejects the unknown fields: retry without them.
                await r.aclose()
                r = await _open_stream(client, s, payload)
            try:
                if r.status_code != 200:
                    body = (await r.aread()).decode(errors="replace")
                    raise LLMError(f"{r.status_code} from model server: {body[:500]}")
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    delta = (choices[0].get("delta") or {}) if choices else {}
                    content = delta.get("content")
                    if content:
                        text = think.feed(content)
                        if text:
                            yield text
                        elif think.inside:
                            yield THINKING
                    elif delta.get("reasoning") or delta.get("reasoning_content"):
                        yield THINKING
            finally:
                await r.aclose()
    except httpx.HTTPError as e:
        raise LLMError(f"Cannot reach {s.base_url}: {e}") from e


async def _open_stream(client: httpx.AsyncClient, s: LLMSettings, payload: dict) -> httpx.Response:
    request = client.build_request("POST", s.url("/chat/completions"), json=payload, headers=s.headers)
    return await client.send(request, stream=True)


class _ThinkFilter:
    """Drop <think>...</think> blocks that reasoning models emit inline."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.inside = False
        self.buf = ""

    def feed(self, text: str) -> str:
        self.buf += text
        out = []
        while self.buf:
            tag = self.CLOSE if self.inside else self.OPEN
            idx = self.buf.find(tag)
            if idx >= 0:
                if not self.inside:
                    out.append(self.buf[:idx])
                self.buf = self.buf[idx + len(tag) :]
                self.inside = not self.inside
                continue
            # Keep a possible partial tag at the end for the next chunk.
            keep = next(
                (k for k in range(len(tag) - 1, 0, -1) if self.buf.endswith(tag[:k])), 0
            )
            if not self.inside:
                out.append(self.buf[: len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep :] if keep else ""
            break
        return "".join(out)
