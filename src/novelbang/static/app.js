"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const enc = encodeURIComponent;

const state = {
  project: null,        // project summary from the server
  view: null,           // {type: "chapter"|"note"|"book", id}
  dirty: false,
  saveTimer: null,
  metaTimer: null,
  settings: null,
  ai: { controller: null, action: null, range: null, text: "" },
  chat: [],
};

// ---------------------------------------------------------------- utilities

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.json();
}

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), isError ? 6000 : 2500);
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null) node.append(c);
  return node;
}

const countWords = (s) => (s.match(/[\p{L}\p{N}'’-]+/gu) || []).length;
const fmt = (n) => n.toLocaleString();
const pid = () => enc(state.project.id);

function autogrow(ta) {
  const pane = $("#editor-pane");
  const top = pane.scrollTop;
  ta.style.height = "auto";
  ta.style.height = ta.scrollHeight + "px";
  pane.scrollTop = top;
}

// Small modal form. fields: [{name, label, value, type, options}]
function ask(title, fields) {
  const dlg = $("#ask-dialog");
  $("#ask-title").textContent = title;
  const box = $("#ask-fields");
  box.replaceChildren(
    ...fields.map((f) => {
      const input = f.options
        ? el("select", { name: f.name }, f.options.map(([v, l]) => el("option", { value: v, selected: v === f.value }, l)))
        : el("input", { name: f.name, value: f.value || "", placeholder: f.placeholder || "", required: f.required });
      return el("label", {}, f.label, input);
    })
  );
  dlg.showModal();
  box.querySelector("input,select")?.select?.();
  return new Promise((resolve) => {
    dlg.addEventListener("close", function done() {
      dlg.removeEventListener("close", done);
      if (dlg.returnValue !== "ok") return resolve(null);
      resolve(Object.fromEntries(fields.map((f) => [f.name, box.querySelector(`[name="${f.name}"]`).value])));
    });
  });
}

// ---------------------------------------------------------------- routing

async function route() {
  const hash = decodeURIComponent(location.hash.slice(1));
  if (!hash) return showHome();
  const [projectId, type, id] = hash.split("/");
  if (!state.project || state.project.id !== projectId) {
    try {
      state.project = await api("GET", `/api/projects/${enc(projectId)}`);
    } catch (e) {
      toast(e.message, true);
      return (location.hash = "");
    }
    state.chat = [];
    renderChat();
  }
  showStudio();
  await openView(type || "", id || "");
}

function go(type, id) {
  const parts = [state.project.id, type, id].filter(Boolean);
  location.hash = parts.map(enc).join("/");
}

// ---------------------------------------------------------------- home

async function showHome() {
  await flushSave();
  state.project = null;
  state.view = null;
  $("#studio").hidden = true;
  $("#home").hidden = false;
  document.title = "NovelBang";
  const data = await api("GET", "/api/projects");
  $("#ws-path").textContent = data.workspace;
  const grid = $("#project-grid");
  grid.replaceChildren(
    ...data.projects.map((p) =>
      el("button", { class: "project-card", onclick: () => (location.hash = enc(p.id)) },
        el("div", { class: "spine" },
          p.has_cover ? el("img", { src: `/api/projects/${enc(p.id)}/cover`, alt: "" }) : p.title),
        el("strong", {}, p.title),
        el("span", {}, `${p.author || "—"} · ${fmt(p.words)} words`))
    ),
    el("button", { class: "project-card", onclick: newProject },
      el("div", { class: "spine new" }, "+"),
      el("strong", {}, "New novel"),
      el("span", {}, "Start a project"))
  );
}

async function newProject() {
  const r = await ask("New novel", [
    { name: "title", label: "Title", required: true },
    { name: "author", label: "Author", value: localStorage.getItem("nb-author") || "" },
  ]);
  if (!r) return;
  localStorage.setItem("nb-author", r.author);
  const p = await api("POST", "/api/projects", r);
  state.project = p;
  location.hash = enc(p.id) + "/chapter/" + enc(p.chapters[0].file);
}

// ---------------------------------------------------------------- studio

function showStudio() {
  $("#home").hidden = true;
  $("#studio").hidden = false;
  renderProject();
}

function renderProject() {
  const p = state.project;
  document.title = `${p.meta.title} — NovelBang`;
  $("#book-title").textContent = p.meta.title;
  $("#total-words").textContent = `${fmt(p.words)} / ${fmt(p.meta.target_words || 0)}`;
  $("#progress-fill").style.width = Math.min(100, (100 * p.words) / (p.meta.target_words || 1)) + "%";
  $("#project-path").textContent = p.path;

  let n = 0;
  $("#chapter-list").replaceChildren(
    ...p.chapters.map((c) => {
      const num = c.kind === "chapter" ? ++n : "";
      const li = el("li", {
          class: (c.kind !== "chapter" ? "matter " : "") + (isActive("chapter", c.file) ? "active" : ""),
          draggable: "true", title: c.synopsis || c.title,
          onclick: () => go("chapter", c.file),
        },
        el("span", { class: "num" }, String(num)),
        el("span", { class: "name" }, c.title),
        el("span", { class: "wc", title: c.missing ? "File is missing from chapters/" : null }, c.missing ? "missing" : c.words ? fmt(c.words) : ""),
        el("span", { class: "dot " + c.status, title: c.status }));
      li.dataset.file = c.file;
      return li;
    })
  );
  $("#note-list").replaceChildren(
    ...p.notes.map((name) =>
      el("li", { class: isActive("note", name) ? "active" : "", onclick: () => go("note", name) },
        "🗒 ", name.replace(/\.md$/, "")))
  );
  $(".book-item").classList.toggle("active", isActive("book"));
}

const isActive = (type, id) => state.view && state.view.type === type && (id === undefined || state.view.id === id);

async function refreshProject() {
  state.project = await api("GET", `/api/projects/${pid()}`);
  renderProject();
}

// ---------------------------------------------------------------- views

async function openView(type, id) {
  await flushSave();
  const p = state.project;
  if (!type) {
    const first = p.chapters.find((c) => c.kind === "chapter") || p.chapters[0];
    return first ? go("chapter", first.file) : go("book");
  }
  state.view = { type, id };
  $("#doc-view").hidden = $("#book-view").hidden = $("#empty-view").hidden = true;

  if (type === "book") {
    fillBookForm();
    $("#book-view").hidden = false;
  } else if (type === "chapter" || type === "note") {
    const chapter = type === "chapter" ? p.chapters.find((c) => c.file === id) : null;
    if (type === "chapter" && !chapter) return go("");
    const url = type === "chapter" ? `/api/projects/${pid()}/chapters/${enc(id)}/text` : `/api/projects/${pid()}/notes/${enc(id)}`;
    const { text } = await api("GET", url);
    $("#doc-title").value = chapter ? chapter.title : id.replace(/\.md$/, "");
    $("#doc-title").readOnly = !chapter;
    $("#chapter-meta").querySelectorAll("select").forEach((s) => (s.hidden = !chapter));
    $("#doc-synopsis").hidden = !chapter;
    if (chapter) {
      $("#doc-kind").value = chapter.kind;
      $("#doc-status").value = chapter.status;
      $("#doc-synopsis").value = chapter.synopsis;
    }
    const ta = $("#doc-text");
    ta.value = text;
    ta.placeholder = chapter ? "Start writing…  (Markdown: *italic*, **bold**, *** alone on a line = scene break)" : "Notes…";
    $("#doc-view").hidden = false;
    updateDocWords();
    requestAnimationFrame(() => { autogrow(ta); autogrow($("#doc-synopsis")); });
    $("#editor-pane").scrollTop = 0;
    if (!text) ta.focus();
  } else {
    $("#empty-view").hidden = false;
  }
  renderProject();
}

function updateDocWords() {
  $("#doc-words").textContent = fmt(countWords($("#doc-text").value)) + " words";
}

// ---------------------------------------------------------------- saving

function setSaveState(s) { $("#save-state").textContent = s; }

function markDirty() {
  state.dirty = true;
  setSaveState("Editing…");
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(flushSave, 800);
}

// Saves run one at a time: a second save that starts while the first PUT is in
// flight could otherwise reach the server first and be overwritten by older text.
let saveQueue = Promise.resolve();
function flushSave() {
  saveQueue = saveQueue.then(doFlushSave, doFlushSave);
  return saveQueue;
}

async function doFlushSave() {
  if (state.project) await flushMeta();
  clearTimeout(state.saveTimer);
  if (!state.dirty || !state.view || !state.project) return;
  state.dirty = false;
  const { type, id } = state.view;
  const text = $("#doc-text").value;
  const url = type === "chapter" ? `/api/projects/${pid()}/chapters/${enc(id)}/text` : `/api/projects/${pid()}/notes/${enc(id)}`;
  try {
    setSaveState("Saving…");
    await api("PUT", url, { text });
    setSaveState("Saved");
    if (type === "chapter") {
      const ch = state.project.chapters.find((c) => c.file === id);
      if (ch) {
        state.project.words += countWords(text) - ch.words;
        ch.words = countWords(text);
        renderProject();
      }
    }
  } catch (e) {
    state.dirty = true;
    setSaveState("Not saved!");
    toast("Save failed: " + e.message, true);
  }
}

async function saveChapterMeta(patch) {
  const { id } = state.view;
  state.project = await api("PUT", `/api/projects/${pid()}/chapters/${enc(id)}`, patch);
  renderProject();
}

// ---------------------------------------------------------------- book details

function fillBookForm() {
  const f = $("#book-view");
  const m = state.project.meta;
  for (const input of f.elements) {
    if (!input.name) continue;
    if (input.type === "checkbox") input.checked = !!m[input.name];
    else input.value = m[input.name] ?? "";
  }
  fillCover();
}

function fillCover() {
  const img = $("#cover-img");
  img.hidden = !state.project.has_cover;
  $("#cover-empty").hidden = state.project.has_cover;
  if (state.project.has_cover) img.src = `/api/projects/${pid()}/cover?t=${Date.now()}`;
}

function saveBookForm() {
  clearTimeout(state.metaTimer);
  setSaveState("Editing…");
  state.metaTimer = setTimeout(flushMeta, 600);
}

async function flushMeta() {
  if (!state.metaTimer) return;
  clearTimeout(state.metaTimer);
  state.metaTimer = null;
  const data = {};
  for (const input of $("#book-view").elements) {
    if (!input.name) continue;
    data[input.name] = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value || 0) : input.value;
  }
  try {
    state.project = await api("PUT", `/api/projects/${pid()}/meta`, data);
    setSaveState("Saved");
    renderProject();
  } catch (e) {
    setSaveState("Not saved!");
    toast("Save failed: " + e.message, true);
  }
}

async function uploadCover(file) {
  await flushMeta();
  const res = await fetch(`/api/projects/${pid()}/cover`, { method: "PUT", headers: { "Content-Type": file.type }, body: file });
  if (!res.ok) return toast((await res.json()).detail, true);
  state.project = await res.json();
  fillCover();
  toast("Cover updated");
}

// ---------------------------------------------------------------- chapters & notes

async function newChapter() {
  const after = state.view?.type === "chapter" ? state.view.id : null;
  const r = await ask("New chapter", [
    { name: "title", label: "Title", value: "", required: true },
    { name: "kind", label: "Type", value: "chapter", options: [["chapter", "Chapter"], ["front", "Front matter (dedication, prologue…)"], ["back", "Back matter (afterword, about the author…)"]] },
  ]);
  if (!r) return;
  const res = await api("POST", `/api/projects/${pid()}/chapters`, { ...r, after });
  state.project = res.project;
  go("chapter", res.file);
}

async function newNote() {
  const r = await ask("New note", [{ name: "name", label: "Name", placeholder: "e.g. timeline, magic-system", required: true }]);
  if (!r) return;
  const res = await api("POST", `/api/projects/${pid()}/notes`, r);
  state.project = res;
  go("note", res.name);
}

async function deleteDoc() {
  const { type, id } = state.view;
  const label = type === "chapter" ? state.project.chapters.find((c) => c.file === id)?.title : id;
  if (!confirm(`Delete “${label}”?\n\nIt will be moved to the project's trash/ folder.`)) return;
  state.dirty = false;
  state.project = await api("DELETE", `/api/projects/${pid()}/${type === "chapter" ? "chapters" : "notes"}/${enc(id)}`);
  go("");
}

function setupDragAndDrop() {
  const list = $("#chapter-list");
  let dragging = null;
  list.addEventListener("dragstart", (e) => {
    dragging = e.target.closest("li");
    dragging?.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
  });
  list.addEventListener("dragover", (e) => {
    const li = e.target.closest("li");
    if (!dragging || !li || li === dragging) return;
    e.preventDefault();
    $$("li", list).forEach((x) => x.classList.remove("drop-above", "drop-below"));
    const below = e.offsetY > li.offsetHeight / 2;
    li.classList.add(below ? "drop-below" : "drop-above");
  });
  list.addEventListener("dragend", () => {
    $$("li", list).forEach((x) => x.classList.remove("drop-above", "drop-below", "dragging"));
    dragging = null;
  });
  list.addEventListener("drop", async (e) => {
    e.preventDefault();
    const li = e.target.closest("li");
    if (!dragging || !li || li === dragging) return;
    const below = li.classList.contains("drop-below");
    below ? li.after(dragging) : li.before(dragging);
    const files = $$("li", list).map((x) => x.dataset.file);
    try {
      state.project = await api("PUT", `/api/projects/${pid()}/order`, { files });
    } catch (err) { toast(err.message, true); }
    renderProject();
  });
}

// ---------------------------------------------------------------- AI: streaming

async function streamAI(action, payload, onText, onThinking = () => {}) {
  const controller = new AbortController();
  state.ai.controller = controller;
  const res = await fetch(`/api/projects/${pid()}/ai/${action}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload), signal: controller.signal,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail; } catch {}
    throw new Error(msg);
  }
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += value;
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i);
      buf = buf.slice(i + 1);
      if (!line.trim()) continue;
      const msg = JSON.parse(line);
      if (msg.error) throw new Error(msg.error);
      if (msg.thinking) onThinking();
      if (msg.t) onText(msg.t);
    }
  }
}

function setBusy(busy) {
  $$("[data-ai]").forEach((b) => (b.disabled = busy));
  $("#chat-form button[type=submit]").disabled = busy;
  $("#ai-stop").hidden = !busy;
}

async function runWriteAction(action) {
  if (state.view?.type !== "chapter") return toast("Open a chapter first", true);
  if (!state.settings?.model) { toast("Choose a model first", true); return openSettings(); }
  await flushSave();
  const ta = $("#doc-text");
  let start = ta.selectionStart, end = ta.selectionEnd;
  if (action === "continue") start = end;
  if (action === "rewrite" && start === end) return toast("Select the passage to rewrite first", true);
  const text = ta.value;
  state.ai.action = action;
  state.ai.range = [start, end];
  state.ai.text = "";

  const out = $("#ai-output");
  out.textContent = "";
  out.dataset.status = "";
  out.hidden = false;
  out.classList.add("streaming");
  $("#ai-output-actions").hidden = true;
  $("[data-action=ai-accept]").textContent = { continue: "Insert at cursor", rewrite: "Replace selection", summarize: "Use as synopsis" }[action];
  setBusy(true);
  try {
    await streamAI(action, {
      file: state.view.id,
      before: text.slice(0, start),
      after: text.slice(end),
      selection: text.slice(start, end),
      instruction: $("#ai-instruction").value,
      max_tokens: action === "summarize" ? null : Number($("#ai-length").value),
    }, (t) => {
      state.ai.text += t;
      out.dataset.status = "";
      out.textContent = state.ai.text.replace(/^\s+/, "");
      out.scrollTop = out.scrollHeight;
    }, () => { if (!state.ai.text) out.dataset.status = "Thinking…"; });
  } catch (e) {
    if (e.name !== "AbortError") toast(e.message, true);
  } finally {
    out.classList.remove("streaming");
    out.dataset.status = "";
    setBusy(false);
    state.ai.controller = null;
    $("#ai-output-actions").hidden = !state.ai.text.trim();
    if (!state.ai.text.trim()) out.hidden = true;
  }
}

async function acceptAI() {
  const { action, range } = state.ai;
  let insert = state.ai.text.trim();
  if (action === "summarize") {
    $("#doc-synopsis").value = insert;
    autogrow($("#doc-synopsis"));
    await saveChapterMeta({ synopsis: insert });
  } else {
    const ta = $("#doc-text");
    let [start, end] = range;
    if (action === "continue") {
      // Join naturally: new paragraph if the model started one, else a space mid-paragraph.
      const before = ta.value.slice(0, start);
      const startsPara = /^\n/.test(state.ai.text.replace(/^[ \t]+/, ""));
      if (before && !/\s$/.test(before)) insert = (startsPara ? "\n\n" : " ") + insert;
      else if (before && startsPara && !/\n\n$/.test(before)) insert = "\n" + insert;
    }
    ta.focus();
    ta.setSelectionRange(start, end);
    // execCommand keeps the browser's undo stack working (⌘Z undoes the insert).
    if (!document.execCommand("insertText", false, insert)) {
      ta.setRangeText(insert, start, end, "end");
    }
    ta.dispatchEvent(new Event("input"));
  }
  discardAI();
}

function discardAI() {
  state.ai.text = "";
  $("#ai-output").hidden = true;
  $("#ai-output-actions").hidden = true;
}

// ---------------------------------------------------------------- AI: chat

function renderChat() {
  const log = $("#chat-log");
  const intro = log.firstElementChild?.matches("p.muted") ? log.firstElementChild : null;
  log.replaceChildren(...(intro ? [intro] : []), ...state.chat.map(renderMsg));
  log.scrollTop = log.scrollHeight;
}

function renderMsg(m) {
  const node = el("div", { class: "msg " + m.role }, m.content);
  if (m.role === "assistant" && m.content && !m.streaming) {
    node.append(el("div", { class: "msg-actions" },
      el("button", { class: "small ghost", onclick: () => navigator.clipboard.writeText(m.content).then(() => toast("Copied")) }, "Copy"),
      el("button", { class: "small ghost", onclick: () => appendToNote(m.content) }, "Save to notes")));
  }
  if (m.streaming) node.classList.add("streaming");
  return node;
}

async function appendToNote(text) {
  const notes = state.project.notes;
  const r = await ask("Save to note", [{ name: "note", label: "Note", value: "outline.md", options: notes.map((n) => [n, n.replace(/\.md$/, "")]) }]);
  if (!r) return;
  if (isActive("note", r.note)) await flushSave();
  const { text: existing } = await api("GET", `/api/projects/${pid()}/notes/${enc(r.note)}`);
  const updated = existing.replace(/\s*$/, "") + "\n\n" + text.trim() + "\n";
  await api("PUT", `/api/projects/${pid()}/notes/${enc(r.note)}`, { text: updated });
  if (isActive("note", r.note)) { $("#doc-text").value = updated; autogrow($("#doc-text")); }
  toast(`Added to ${r.note}`);
}

async function sendChat(e) {
  e.preventDefault();
  const input = $("#chat-input");
  const content = input.value.trim();
  if (!content) return;
  if (!state.settings?.model) { toast("Choose a model first", true); return openSettings(); }
  await flushSave();
  input.value = "";
  state.chat.push({ role: "user", content });
  const reply = { role: "assistant", content: "", streaming: true };
  state.chat.push(reply);
  renderChat();
  const replyNode = $("#chat-log").lastElementChild;
  setBusy(true);
  try {
    const chapter = state.view?.type === "chapter" ? state.view.id : null;
    await streamAI("chat", {
      file: chapter,
      before: state.view?.type === "chapter" || state.view?.type === "note" ? $("#doc-text").value : "",
      history: state.chat.filter((m) => m !== reply).map(({ role, content }) => ({ role, content })),
      max_tokens: 1200,
    }, (t) => {
      reply.content += t;
      replyNode.dataset.status = "";
      replyNode.textContent = reply.content.replace(/^\s+/, "");
      $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
    }, () => { if (!reply.content) replyNode.dataset.status = "Thinking…"; });
  } catch (err) {
    if (err.name !== "AbortError") toast(err.message, true);
    if (!reply.content) state.chat.splice(state.chat.indexOf(reply), 1);
  } finally {
    reply.streaming = false;
    reply.content = reply.content.trim();
    setBusy(false);
    renderChat();
  }
}

// ---------------------------------------------------------------- settings

async function loadSettings() {
  state.settings = await api("GET", "/api/settings");
  $("#model-badge").textContent = state.settings.model || "no model";
}

async function openSettings() {
  const f = $("#settings-form");
  for (const input of f.elements) {
    if (!input.name) continue;
    if (input.type === "checkbox") input.checked = !!state.settings[input.name];
    else input.value = state.settings[input.name] ?? "";
  }
  $("#model-status").textContent = "";
  $("#settings-dialog").showModal();
  detectModels(true);
}

async function detectModels(quiet = false) {
  const f = $("#settings-form");
  const status = $("#model-status");
  status.textContent = "Looking for models…";
  try {
    const { models } = await api("POST", "/api/llm/models", { base_url: f.base_url.value, api_key: f.api_key.value });
    $("#model-options").replaceChildren(...models.map((m) => el("option", { value: m })));
    status.textContent = models.length ? `✓ Connected — ${models.length} model(s): ${models.slice(0, 6).join(", ")}${models.length > 6 ? "…" : ""}` : "Connected, but the server lists no models.";
    if (!f.model.value && models.length) f.model.value = models[0];
  } catch (e) {
    status.textContent = quiet ? `Not connected: ${e.message}` : `✕ ${e.message}`;
  }
}

async function saveSettings() {
  const f = $("#settings-form");
  const data = {};
  for (const input of f.elements) {
    if (!input.name) continue;
    data[input.name] = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value;
  }
  state.settings = await api("PUT", "/api/settings", data);
  $("#model-badge").textContent = state.settings.model || "no model";
  toast("Settings saved");
}

// ---------------------------------------------------------------- publish

async function openPublish() {
  await flushSave();
  await renderPublish();
  $("#publish-dialog").showModal();
}

async function renderPublish() {
  const info = await api("GET", `/api/projects/${pid()}/publish`);
  const checks = info.checks.length ? info.checks : [{ level: "good", message: "Everything looks ready." }];
  $("#checks").replaceChildren(...checks.map((c) => el("li", { class: c.level }, c.message)));
  $("#formats").replaceChildren(
    ...info.formats.map((f) =>
      el("div", { class: "format" },
        el("div", {}, el("strong", {}, f.label), el("span", {}, f.help)),
        el("button", { class: "small", onclick: (e) => doExport(f.id, e.currentTarget) }, "Export")))
  );
  $("#exports").replaceChildren(
    ...(info.exports.length
      ? info.exports.map((x) =>
          el("li", {},
            el("a", { href: `/api/projects/${pid()}/exports/${enc(x.name)}`, target: x.name.endsWith(".html") ? "_blank" : null, download: x.name.endsWith(".html") ? null : x.name }, x.name),
            el("span", { class: "muted" }, `${(x.size / 1024).toFixed(0)} KB · ${new Date(x.mtime * 1000).toLocaleString()}`)))
      : [el("li", { class: "muted" }, "Nothing exported yet.")])
  );
  $("#exports-dir").textContent = info.exports_dir;
}

async function doExport(fmt, btn) {
  btn.disabled = true;
  btn.textContent = "Building…";
  try {
    const r = await api("POST", `/api/projects/${pid()}/export/${fmt}`);
    toast(`Created ${r.name}`);
    await renderPublish();
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
    btn.textContent = "Export";
  }
}

// ---------------------------------------------------------------- wiring

function wrapSelection(ta, mark) {
  const { selectionStart: s, selectionEnd: e, value } = ta;
  const sel = value.slice(s, e);
  ta.focus();
  document.execCommand("insertText", false, mark + sel + mark) || ta.setRangeText(mark + sel + mark, s, e, "end");
  if (!sel) ta.setSelectionRange(s + mark.length, s + mark.length);
}

function wire() {
  const actions = {
    home: () => (location.hash = ""),
    settings: openSettings,
    publish: openPublish,
    focus: () => document.body.classList.toggle("focus"),
    "toggle-ai": () => {
      document.body.classList.toggle("no-ai");
      localStorage.setItem("nb-no-ai", document.body.classList.contains("no-ai") ? "1" : "");
    },
    "new-chapter": newChapter,
    "new-note": newNote,
    "delete-doc": deleteDoc,
    "ai-accept": acceptAI,
    "ai-discard": discardAI,
    "ai-retry": () => runWriteAction(state.ai.action),
    "ai-stop": () => state.ai.controller?.abort(),
    "chat-clear": () => { state.chat = []; renderChat(); },
    "load-models": () => detectModels(),
  };
  document.addEventListener("click", (e) => {
    const a = e.target.closest("[data-action]");
    if (a && actions[a.dataset.action]) { e.preventDefault(); actions[a.dataset.action](); }
    const ai = e.target.closest("[data-ai]");
    if (ai) runWriteAction(ai.dataset.ai);
    const view = e.target.closest("[data-view]");
    if (view) go(view.dataset.view);
    const tab = e.target.closest("[data-tab]");
    if (tab) {
      $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      $("#tab-write").hidden = tab.dataset.tab !== "write";
      $("#tab-chat").hidden = tab.dataset.tab !== "chat";
    }
    const preset = e.target.closest("[data-preset]");
    if (preset) { $("#settings-form").base_url.value = preset.dataset.preset; detectModels(); }
  });

  const ta = $("#doc-text");
  ta.addEventListener("input", () => { markDirty(); autogrow(ta); updateDocWords(); });
  ta.addEventListener("keydown", (e) => {
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.key === "i") { e.preventDefault(); wrapSelection(ta, "*"); }
    if (mod && e.key === "b") { e.preventDefault(); wrapSelection(ta, "**"); }
  });
  $("#doc-title").addEventListener("change", (e) => state.view?.type === "chapter" && saveChapterMeta({ title: e.target.value }));
  $("#doc-kind").addEventListener("change", (e) => saveChapterMeta({ kind: e.target.value }));
  $("#doc-status").addEventListener("change", (e) => saveChapterMeta({ status: e.target.value }));
  const syn = $("#doc-synopsis");
  syn.addEventListener("input", () => autogrow(syn));
  syn.addEventListener("change", () => saveChapterMeta({ synopsis: syn.value }));

  $("#book-view").addEventListener("input", saveBookForm);
  $("#book-view").addEventListener("submit", (e) => e.preventDefault());
  $("#cover-file").addEventListener("change", (e) => e.target.files[0] && uploadCover(e.target.files[0]));
  $("#settings-dialog").addEventListener("close", () => $("#settings-dialog").returnValue === "ok" && saveSettings());
  $("#chat-form").addEventListener("submit", sendChat);
  $("#chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#chat-form").requestSubmit(); }
  });

  document.addEventListener("keydown", (e) => {
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.key === "s") { e.preventDefault(); flushSave(); }
    if (mod && e.key === "j" && state.project) { e.preventDefault(); runWriteAction("continue"); }
    if (e.key === "Escape" && document.body.classList.contains("focus")) document.body.classList.remove("focus");
  });
  window.addEventListener("beforeunload", (e) => {
    if (state.dirty) {
      const { type, id } = state.view;
      const url = type === "chapter" ? `/api/projects/${pid()}/chapters/${enc(id)}/text` : `/api/projects/${pid()}/notes/${enc(id)}`;
      fetch(url, { method: "PUT", keepalive: true, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: $("#doc-text").value }) });
    }
  });
  window.addEventListener("hashchange", () => route().catch((e) => toast(e.message, true)));
  setupDragAndDrop();
  if (localStorage.getItem("nb-no-ai")) document.body.classList.add("no-ai");
}

wire();
loadSettings().catch(() => {}).finally(() => route().catch((e) => toast(e.message, true)));
