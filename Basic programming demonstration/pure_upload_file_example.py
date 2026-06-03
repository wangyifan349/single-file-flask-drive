"""
This program is a single-file Flask file manager that combines the frontend and backend in one Python script. It provides a complete browser-based file management interface with hierarchical directory browsing, breadcrumb navigation, multi-file upload, file download, folder download, multi-selection, drag-and-drop movement, renaming, deletion, and directory creation. All file operations are restricted to a fixed storage directory to reduce path traversal risks.
The program is designed for teaching and practical local deployment. It includes strong fault tolerance for unstable network conditions, browser-side SHA-256 verification, backend hash validation, safe temporary upload handling, and disk-based archive buffering. For downloading folders or multiple selected items, it creates compressed .7z archives in a temporary buffer directory before sending them to the browser, avoiding in-memory archive generation and supporting efficient compressed downloads.
pip install flask py7zr
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable

import py7zr
from flask import Flask, Response, jsonify, render_template_string, request, send_file, send_from_directory

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = (BASE_DIR / "storage").resolve()
BUFFER_DIR = (BASE_DIR / "file_manager_buffer").resolve()
UPLOAD_BUFFER_DIR = (BUFFER_DIR / "uploads").resolve()
ARCHIVE_BUFFER_DIR = (BUFFER_DIR / "archives").resolve()

# py7zr 最大压缩参数。LZMA2 preset=9 是 py7zr/lzma 侧的最高预设等级。
PY7ZR_PRESET = int(os.environ.get("PY7ZR_PRESET", "9"))
ARCHIVE_MAX_AGE_SECONDS = int(os.environ.get("ARCHIVE_MAX_AGE_SECONDS", str(6 * 60 * 60)))

for p in (ROOT_DIR, UPLOAD_BUFFER_DIR, ARCHIVE_BUFFER_DIR):
    p.mkdir(parents=True, exist_ok=True)

BAD_SEGMENT_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status
        super().__init__(message)


@app.errorhandler(ApiError)
def handle_api_error(exc: ApiError):
    return jsonify({"ok": False, "error": exc.message}), exc.status


def fail(message: str, status: int = 400):
    raise ApiError(message, status)


def sanitize_segment(segment: str) -> str:
    segment = segment.strip()
    segment = BAD_SEGMENT_CHARS.sub("_", segment)
    if not segment or segment in {".", ".."}:
        fail("非法文件名")
    return segment


def clean_rel_path(raw: str | None, *, allow_empty: bool = True) -> str:
    if raw is None:
        return "" if allow_empty else fail("路径不能为空")
    raw = str(raw).replace("\\", "/").strip()
    raw = raw.lstrip("/")
    if raw in {"", "."}:
        if allow_empty:
            return ""
        fail("路径不能为空")
    parts: list[str] = []
    for part in raw.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            fail("路径不能包含 ..")
        parts.append(sanitize_segment(part))
    if not parts and not allow_empty:
        fail("路径不能为空")
    return "/".join(parts)


def resolve_in_root(rel: str | None, *, allow_empty: bool = True) -> Path:
    rel_clean = clean_rel_path(rel, allow_empty=allow_empty)
    target = (ROOT_DIR / rel_clean).resolve()
    try:
        target.relative_to(ROOT_DIR)
    except ValueError:
        fail("路径越界")
    return target


def rel_from_path(path: Path) -> str:
    if path.resolve() == ROOT_DIR:
        return ""
    return path.resolve().relative_to(ROOT_DIR).as_posix()


def item_info(path: Path) -> dict:
    st = path.stat()
    is_dir = path.is_dir()
    return {
        "name": path.name,
        "path": rel_from_path(path),
        "type": "dir" if is_dir else "file",
        "size": None if is_dir else st.st_size,
        "mtime": int(st.st_mtime),
    }


def list_dir(rel: str) -> dict:
    folder = resolve_in_root(rel)
    if not folder.exists():
        fail("目录不存在", 404)
    if not folder.is_dir():
        fail("不是目录")
    entries = [item_info(p) for p in folder.iterdir()]
    entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))

    rel_clean = rel_from_path(folder)
    crumbs = [{"name": "根目录", "path": ""}]
    acc: list[str] = []
    for part in rel_clean.split("/") if rel_clean else []:
        acc.append(part)
        crumbs.append({"name": part, "path": "/".join(acc)})
    return {"ok": True, "path": rel_clean, "breadcrumbs": crumbs, "items": entries}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_tree(path: Path) -> str:
    """给目录做稳定摘要：相对路径 + 文件内容哈希都参与。"""
    h = hashlib.sha256()
    base = path.resolve()
    for p in sorted(base.rglob("*"), key=lambda x: x.relative_to(base).as_posix()):
        rel = p.relative_to(base).as_posix().encode("utf-8", "surrogateescape")
        if p.is_dir():
            h.update(b"D\0" + rel + b"\0")
        elif p.is_file():
            h.update(b"F\0" + rel + b"\0")
            h.update(sha256_file(p).encode("ascii"))
            h.update(b"\0")
    return h.hexdigest()


def same_payload(a: Path, b: Path) -> bool:
    if not a.exists() or not b.exists():
        return False
    if a.is_file() and b.is_file():
        return sha256_file(a) == sha256_file(b)
    if a.is_dir() and b.is_dir():
        return sha256_tree(a) == sha256_tree(b)
    return False


def timestamped_path(path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    parent = path.parent
    if path.suffix:
        stem, suffix = path.stem, path.suffix
        candidate = parent / f"{stem}_{ts}{suffix}"
    else:
        candidate = parent / f"{path.name}_{ts}"
    n = 2
    while candidate.exists():
        if path.suffix:
            candidate = parent / f"{path.stem}_{ts}_{n}{path.suffix}"
        else:
            candidate = parent / f"{path.name}_{ts}_{n}"
        n += 1
    return candidate


def remove_any(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def place_with_collision(src: Path, dest: Path) -> tuple[Path, str]:
    """移动 src 到 dest。dest 已存在时：同哈希替换，不同哈希加时间戳。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    action = "moved"
    final = dest
    if dest.exists():
        if same_payload(src, dest):
            remove_any(dest)
            action = "replaced_same_hash"
        else:
            final = timestamped_path(dest)
            action = "renamed_with_timestamp"
    shutil.move(str(src), str(final))
    return final, action


def is_child_or_same(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def cleanup_old_archives() -> None:
    now = time.time()
    ARCHIVE_BUFFER_DIR.mkdir(parents=True, exist_ok=True)
    for p in ARCHIVE_BUFFER_DIR.iterdir():
        try:
            if now - p.stat().st_mtime > ARCHIVE_MAX_AGE_SECONDS:
                remove_any(p)
        except FileNotFoundError:
            pass


def make_7z_archive(items: Iterable[str]) -> Path:
    """
    用 py7zr 在磁盘缓冲目录中生成 .7z，再由 Flask 发送文件。
    注意：这里不会把最终压缩包构造在内存中；archive_path 是真实落盘文件。
    """
    rels: list[str] = []
    seen: set[str] = set()
    for raw in items:
        rel = clean_rel_path(raw, allow_empty=False)
        p = resolve_in_root(rel, allow_empty=False)
        if not p.exists():
            fail(f"不存在：{rel}", 404)
        if rel not in seen:
            rels.append(rel)
            seen.add(rel)
    if not rels:
        fail("没有选择任何文件")

    cleanup_old_archives()
    job_dir = ARCHIVE_BUFFER_DIR / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=False)
    archive_name = f"download_{datetime.now().strftime('%Y%m%d-%H%M%S')}.7z"
    archive_path = job_dir / archive_name

    preset = max(0, min(9, PY7ZR_PRESET))
    filters = [{"id": py7zr.FILTER_LZMA2, "preset": preset}]

    try:
        with py7zr.SevenZipFile(archive_path, mode="w", filters=filters) as archive:
            for rel in rels:
                src = resolve_in_root(rel, allow_empty=False)
                # arcname 使用相对 ROOT_DIR 的路径，避免把服务器绝对路径写入压缩包。
                if src.is_dir():
                    archive.writeall(src, arcname=rel)
                else:
                    archive.write(src, arcname=rel)
    except Exception as exc:
        remove_any(job_dir)
        fail(f"py7zr 压缩失败：{exc}", 500)

    if not archive_path.exists() or archive_path.stat().st_size <= 0:
        remove_any(job_dir)
        fail("py7zr 压缩失败：没有生成有效压缩包", 500)
    return archive_path

@app.route("/")
def index():
    return render_template_string(HTML)


@app.get("/api/list")
def api_list():
    return jsonify(list_dir(request.args.get("path", "")))


@app.post("/api/mkdir")
def api_mkdir():
    data = request.get_json(force=True)
    base = resolve_in_root(data.get("path", ""))
    if not base.is_dir():
        fail("目标不是目录")
    name = sanitize_segment(str(data.get("name", "")).strip())
    final = base / name
    if final.exists():
        final = timestamped_path(final)
    final.mkdir(parents=True, exist_ok=False)
    return jsonify({"ok": True, "item": item_info(final)})


@app.post("/api/upload")
def api_upload():
    target_dir = resolve_in_root(request.args.get("path", ""))
    if not target_dir.exists() or not target_dir.is_dir():
        fail("上传目标不是目录")

    uploaded = request.files.getlist("files")
    if not uploaded:
        fail("没有收到文件")

    results = []
    for storage in uploaded:
        rel_name = clean_rel_path(storage.filename, allow_empty=False)
        dest = target_dir / rel_name
        dest.parent.mkdir(parents=True, exist_ok=True)

        tmp = UPLOAD_BUFFER_DIR / f"upload_{uuid.uuid4().hex}.tmp"
        storage.save(tmp)
        final, action = place_with_collision(tmp, dest)
        results.append({"path": rel_from_path(final), "action": action})

    return jsonify({"ok": True, "uploaded": results})


@app.post("/api/delete")
def api_delete():
    data = request.get_json(force=True)
    items = data.get("items") or []
    if not isinstance(items, list):
        fail("items 必须是列表")
    deleted = []
    for raw in items:
        rel = clean_rel_path(raw, allow_empty=False)
        p = resolve_in_root(rel, allow_empty=False)
        if not p.exists():
            continue
        remove_any(p)
        deleted.append(rel)
    return jsonify({"ok": True, "deleted": deleted})


@app.post("/api/move")
def api_move():
    data = request.get_json(force=True)
    items = data.get("items") or []
    dest_rel = data.get("dest", "")
    if not isinstance(items, list):
        fail("items 必须是列表")
    dest_dir = resolve_in_root(dest_rel)
    if not dest_dir.exists() or not dest_dir.is_dir():
        fail("移动目标不是目录")

    moved = []
    for raw in items:
        rel = clean_rel_path(raw, allow_empty=False)
        src = resolve_in_root(rel, allow_empty=False)
        if not src.exists():
            continue
        if src.resolve() == ROOT_DIR:
            fail("不能移动根目录")
        if src.resolve() == dest_dir.resolve():
            continue
        if src.is_dir() and is_child_or_same(src, dest_dir):
            fail(f"不能把目录移动到自己或自己的子目录中：{rel}")
        target = dest_dir / src.name
        if src.resolve() == target.resolve():
            continue
        final, action = place_with_collision(src, target)
        moved.append({"from": rel, "to": rel_from_path(final), "action": action})
    return jsonify({"ok": True, "moved": moved})


@app.post("/api/rename")
def api_rename():
    data = request.get_json(force=True)
    rel = clean_rel_path(data.get("path"), allow_empty=False)
    src = resolve_in_root(rel, allow_empty=False)
    if not src.exists():
        fail("源路径不存在", 404)
    if src.resolve() == ROOT_DIR:
        fail("不能重命名根目录")
    new_name = sanitize_segment(str(data.get("name", "")).strip())
    target = src.parent / new_name
    if target.resolve() == src.resolve():
        return jsonify({"ok": True, "item": item_info(src), "action": "noop"})
    final, action = place_with_collision(src, target)
    return jsonify({"ok": True, "item": item_info(final), "action": action})


@app.get("/api/download")
def api_download_one():
    rel = clean_rel_path(request.args.get("path"), allow_empty=False)
    p = resolve_in_root(rel, allow_empty=False)
    if not p.exists():
        fail("文件不存在", 404)
    if p.is_dir():
        archive = make_7z_archive([rel])
        return send_file(archive, as_attachment=True, download_name=archive.name)
    # send_from_directory 会把 path 限定在 ROOT_DIR 下；rel 已经过本地二次校验。
    return send_from_directory(ROOT_DIR, rel, as_attachment=True, download_name=p.name)


@app.post("/api/download")
def api_download_many():
    data = request.get_json(force=True)
    items = data.get("items") or []
    if not isinstance(items, list):
        fail("items 必须是列表")
    if len(items) == 1:
        rel = clean_rel_path(items[0], allow_empty=False)
        p = resolve_in_root(rel, allow_empty=False)
        if p.is_file():
            return send_from_directory(ROOT_DIR, rel, as_attachment=True, download_name=p.name)
    archive = make_7z_archive(items)
    return send_file(archive, as_attachment=True, download_name=archive.name)


HTML = r'''
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Flask 文件管理器</title>
  <style>
    :root {
      --bg: #f6f7f8;
      --panel: #ffffff;
      --line: #e6e8eb;
      --text: #1f2937;
      --muted: #6b7280;
      --hover: #f1f5f9;
      --selected: #dbeafe;
      --selected-line: #93c5fd;
      --menu-shadow: 0 12px 40px rgba(0,0,0,.18);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background: var(--bg);
      user-select: none;
    }
    #app { min-height: 100vh; display: flex; flex-direction: column; }
    .crumbbar {
      height: 42px;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      gap: 4px;
      padding: 0 12px;
      overflow: auto;
      white-space: nowrap;
    }
    .crumb {
      display: inline-flex;
      align-items: center;
      height: 26px;
      padding: 0 8px;
      border-radius: 6px;
      color: #2563eb;
      cursor: pointer;
    }
    .crumb:hover, .crumb.drop-target { background: #eff6ff; }
    .slash { color: var(--muted); }
    .main {
      flex: 1;
      position: relative;
      overflow: auto;
      padding: 12px;
    }
    .file-list {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      min-height: calc(100vh - 68px);
      overflow: hidden;
    }
    .header, .row {
      display: grid;
      grid-template-columns: 36px minmax(220px, 1fr) 120px 180px;
      align-items: center;
      gap: 8px;
      min-height: 42px;
      padding: 0 12px;
      border-bottom: 1px solid var(--line);
    }
    .header {
      min-height: 34px;
      color: var(--muted);
      font-size: 12px;
      background: #fafafa;
    }
    .row { cursor: default; }
    .row:hover { background: var(--hover); }
    .row.selected {
      background: var(--selected);
      box-shadow: inset 3px 0 0 var(--selected-line);
    }
    .row.drop-target { outline: 2px solid #60a5fa; outline-offset: -2px; }
    .icon { font-size: 20px; text-align: center; }
    .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .muted { color: var(--muted); }
    .empty {
      padding: 60px 16px;
      text-align: center;
      color: var(--muted);
    }
    .selection-box {
      position: fixed;
      border: 1px solid #2563eb;
      background: rgba(37, 99, 235, .12);
      pointer-events: none;
      z-index: 30;
    }
    .menu {
      position: fixed;
      z-index: 100;
      min-width: 170px;
      padding: 6px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel);
      box-shadow: var(--menu-shadow);
      display: none;
    }
    .menu.show { display: block; }
    .menu button {
      width: 100%;
      display: block;
      border: 0;
      background: transparent;
      color: var(--text);
      text-align: left;
      padding: 8px 10px;
      border-radius: 8px;
      cursor: pointer;
      font: inherit;
    }
    .menu button:hover { background: var(--hover); }
    .menu button.danger { color: #dc2626; }
    .menu .sep { height: 1px; background: var(--line); margin: 5px 4px; }
    .toast {
      position: fixed;
      left: 50%;
      bottom: 22px;
      transform: translateX(-50%);
      background: rgba(17,24,39,.92);
      color: white;
      padding: 9px 12px;
      border-radius: 999px;
      display: none;
      z-index: 120;
      max-width: min(720px, calc(100vw - 30px));
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .toast.show { display: block; }
    input[type=file] { display: none; }
  </style>
</head>
<body>
<div id="app">
  <div id="crumbbar" class="crumbbar"></div>
  <div id="main" class="main">
    <div id="fileList" class="file-list"></div>
  </div>
</div>

<div id="menu" class="menu"></div>
<div id="toast" class="toast"></div>
<input id="fileInput" type="file" multiple />
<input id="folderInput" type="file" webkitdirectory directory multiple />

<script>
(() => {
  let currentPath = "";
  let items = [];
  const selected = new Set();
  let menuContext = { targetPath: "" };

  const crumbbar = document.getElementById("crumbbar");
  const fileList = document.getElementById("fileList");
  const main = document.getElementById("main");
  const menu = document.getElementById("menu");
  const toast = document.getElementById("toast");
  const fileInput = document.getElementById("fileInput");
  const folderInput = document.getElementById("folderInput");

  function qs(path) { return encodeURIComponent(path || ""); }
  function fmtSize(n) {
    if (n == null) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let v = n, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
  }
  function fmtTime(sec) {
    if (!sec) return "—";
    return new Date(sec * 1000).toLocaleString();
  }
  function showToast(msg) {
    toast.textContent = msg;
    toast.classList.add("show");
    clearTimeout(showToast.t);
    showToast.t = setTimeout(() => toast.classList.remove("show"), 2200);
  }
  async function api(url, opts = {}) {
    const res = await fetch(url, opts);
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).error || msg; } catch (_) {}
      throw new Error(msg);
    }
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) return res.json();
    return res;
  }
  async function load(path = currentPath) {
    try {
      const data = await api(`/api/list?path=${qs(path)}`);
      currentPath = data.path || "";
      items = data.items || [];
      selected.clear();
      renderCrumbs(data.breadcrumbs || []);
      renderList();
    } catch (e) { showToast(e.message); }
  }

  function renderCrumbs(crumbs) {
    crumbbar.innerHTML = "";
    crumbs.forEach((c, i) => {
      if (i) {
        const sep = document.createElement("span");
        sep.className = "slash";
        sep.textContent = "/";
        crumbbar.appendChild(sep);
      }
      const el = document.createElement("span");
      el.className = "crumb";
      el.textContent = c.name;
      el.dataset.path = c.path;
      el.addEventListener("click", () => load(c.path));
      el.addEventListener("dragover", onDragOverDest);
      el.addEventListener("dragleave", () => el.classList.remove("drop-target"));
      el.addEventListener("drop", async (ev) => {
        el.classList.remove("drop-target");
        await handleDrop(ev, c.path);
      });
      crumbbar.appendChild(el);
    });
  }

  function renderList() {
    fileList.innerHTML = `
      <div class="header">
        <div></div><div>名称</div><div>大小</div><div>修改时间</div>
      </div>`;
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "空目录。右键空白区域上传文件或新建文件夹。";
      fileList.appendChild(empty);
      return;
    }
    for (const item of items) {
      const row = document.createElement("div");
      row.className = "row";
      row.draggable = true;
      row.dataset.path = item.path;
      row.dataset.type = item.type;
      row.innerHTML = `
        <div class="icon">${item.type === "dir" ? "📁" : "📄"}</div>
        <div class="name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</div>
        <div class="muted">${fmtSize(item.size)}</div>
        <div class="muted">${fmtTime(item.mtime)}</div>`;
      row.addEventListener("click", (ev) => onRowClick(ev, item));
      row.addEventListener("contextmenu", (ev) => onRowContext(ev, item));
      row.addEventListener("dragstart", (ev) => onDragStart(ev, item));
      if (item.type === "dir") {
        row.addEventListener("dragover", onDragOverDest);
        row.addEventListener("dragleave", () => row.classList.remove("drop-target"));
        row.addEventListener("drop", async (ev) => {
          row.classList.remove("drop-target");
          await handleDrop(ev, item.path);
        });
      }
      fileList.appendChild(row);
    }
    syncSelectionView();
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[ch]));
  }
  function syncSelectionView() {
    fileList.querySelectorAll(".row").forEach(row => {
      row.classList.toggle("selected", selected.has(row.dataset.path));
    });
  }
  function selectedPaths() { return [...selected]; }
  function selectedItems() { return items.filter(x => selected.has(x.path)); }

  function onRowClick(ev, item) {
    closeMenu();
    if (ev.ctrlKey || ev.metaKey) {
      selected.has(item.path) ? selected.delete(item.path) : selected.add(item.path);
      syncSelectionView();
      return;
    }
    if (item.type === "dir") load(item.path);
    else downloadOne(item.path);
  }
  function onRowContext(ev, item) {
    ev.preventDefault();
    if (!selected.has(item.path)) {
      selected.clear();
      selected.add(item.path);
      syncSelectionView();
    }
    menuContext.targetPath = item.path;
    showMenu(ev.clientX, ev.clientY, menuForSelection());
  }

  function menuForSelection() {
    const chosen = selectedItems();
    const one = chosen.length === 1 ? chosen[0] : null;
    const actions = [];
    if (one && one.type === "dir") actions.push(["打开", () => load(one.path)]);
    actions.push([chosen.length > 1 || (one && one.type === "dir") ? "下载为 7z" : "下载", () => downloadSelected()]);
    if (one) actions.push(["重命名", () => renameItem(one.path)]);
    if (one && one.type === "dir") {
      actions.push(["上传文件到此文件夹", () => pickFiles(one.path)]);
      actions.push(["上传文件夹到此文件夹", () => pickFolder(one.path)]);
    }
    actions.push(["sep"]);
    actions.push([`删除${chosen.length > 1 ? "选中项" : ""}`, () => deleteSelected(), "danger"]);
    return actions;
  }
  function menuForBlank() {
    return [
      ["上传文件", () => pickFiles(currentPath)],
      ["上传文件夹", () => pickFolder(currentPath)],
      ["新建文件夹", () => mkdir()],
      ["刷新", () => load(currentPath)],
    ];
  }
  function showMenu(x, y, actions) {
    menu.innerHTML = "";
    for (const a of actions) {
      if (a[0] === "sep") {
        const sep = document.createElement("div");
        sep.className = "sep";
        menu.appendChild(sep);
        continue;
      }
      const b = document.createElement("button");
      b.textContent = a[0];
      if (a[2]) b.classList.add(a[2]);
      b.addEventListener("click", async () => {
        closeMenu();
        try { await a[1](); } catch (e) { showToast(e.message); }
      });
      menu.appendChild(b);
    }
    menu.classList.add("show");
    const rect = menu.getBoundingClientRect();
    menu.style.left = Math.min(x, window.innerWidth - rect.width - 8) + "px";
    menu.style.top = Math.min(y, window.innerHeight - rect.height - 8) + "px";
  }
  function closeMenu() { menu.classList.remove("show"); }

  main.addEventListener("contextmenu", (ev) => {
    if (ev.target.closest(".row")) return;
    ev.preventDefault();
    selected.clear();
    syncSelectionView();
    showMenu(ev.clientX, ev.clientY, menuForBlank());
  });
  document.addEventListener("click", (ev) => {
    if (!ev.target.closest(".menu")) closeMenu();
  });

  function pickFiles(destPath) {
    fileInput.value = "";
    fileInput.onchange = () => uploadFileList(fileInput.files, destPath, false);
    fileInput.click();
  }
  function pickFolder(destPath) {
    folderInput.value = "";
    folderInput.onchange = () => uploadFileList(folderInput.files, destPath, true);
    folderInput.click();
  }
  async function uploadFileList(fileListObj, destPath, keepRelative) {
    const files = [...fileListObj];
    if (!files.length) return;
    const fd = new FormData();
    for (const f of files) {
      const rel = keepRelative && f.webkitRelativePath ? f.webkitRelativePath : f.name;
      fd.append("files", f, rel);
    }
    showToast("正在上传...");
    await api(`/api/upload?path=${qs(destPath)}`, { method: "POST", body: fd });
    await load(currentPath);
    showToast("上传完成");
  }

  async function mkdir() {
    const name = prompt("文件夹名称");
    if (!name) return;
    await api("/api/mkdir", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ path: currentPath, name })
    });
    await load(currentPath);
  }
  async function renameItem(path) {
    const current = path.split("/").pop();
    const name = prompt("新名称", current);
    if (!name || name === current) return;
    await api("/api/rename", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ path, name })
    });
    await load(currentPath);
  }
  async function deleteSelected() {
    const paths = selectedPaths();
    if (!paths.length) return;
    if (!confirm(`确认删除 ${paths.length} 项？`)) return;
    await api("/api/delete", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ items: paths })
    });
    await load(currentPath);
    showToast("删除完成");
  }
  function downloadOne(path) {
    window.location.href = `/api/download?path=${qs(path)}`;
  }
  async function downloadSelected() {
    const paths = selectedPaths();
    if (!paths.length) return;
    if (paths.length === 1) {
      const it = items.find(x => x.path === paths[0]);
      if (it && it.type === "file") {
        downloadOne(paths[0]);
        return;
      }
    }
    showToast("正在用 7z 最大压缩，请稍等...");
    const res = await fetch("/api/download", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ items: paths })
    });
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).error || msg; } catch (_) {}
      throw new Error(msg);
    }
    const blob = await res.blob();
    const cd = res.headers.get("content-disposition") || "";
    let name = "download.7z";
    const m = cd.match(/filename\*?=(?:UTF-8''|\")?([^\";]+)/i);
    if (m) name = decodeURIComponent(m[1].replace(/\"/g, ""));
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    showToast("下载已开始");
  }

  function onDragStart(ev, item) {
    if (!selected.has(item.path)) {
      selected.clear();
      selected.add(item.path);
      syncSelectionView();
    }
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("application/x-file-manager-items", JSON.stringify(selectedPaths()));
    ev.dataTransfer.setData("text/plain", selectedPaths().join("\n"));
  }
  function onDragOverDest(ev) {
    if (ev.dataTransfer.types.includes("application/x-file-manager-items") || ev.dataTransfer.files.length) {
      ev.preventDefault();
      ev.currentTarget.classList.add("drop-target");
      ev.dataTransfer.dropEffect = ev.dataTransfer.files.length ? "copy" : "move";
    }
  }
  async function handleDrop(ev, destPath) {
    ev.preventDefault();
    closeMenu();
    const internal = ev.dataTransfer.getData("application/x-file-manager-items");
    try {
      if (internal) {
        const paths = JSON.parse(internal);
        await api("/api/move", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ items: paths, dest: destPath })
        });
        await load(currentPath);
        showToast("移动完成");
      } else if (ev.dataTransfer.files && ev.dataTransfer.files.length) {
        await uploadFileList(ev.dataTransfer.files, destPath, false);
      }
    } catch (e) { showToast(e.message); }
  }
  main.addEventListener("dragover", (ev) => {
    if (ev.dataTransfer.files && ev.dataTransfer.files.length) {
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "copy";
    }
  });
  main.addEventListener("drop", async (ev) => {
    if (ev.target.closest(".row") || ev.target.closest(".crumb")) return;
    if (ev.dataTransfer.files && ev.dataTransfer.files.length) {
      ev.preventDefault();
      await uploadFileList(ev.dataTransfer.files, currentPath, false);
    }
  });

  // 框选：从任意方向拖都可以，例如右下向左上。
  let box = null;
  let boxStart = null;
  main.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0 || ev.target.closest(".row") || ev.target.closest(".crumb") || ev.target.closest(".menu")) return;
    closeMenu();
    boxStart = { x: ev.clientX, y: ev.clientY };
    box = document.createElement("div");
    box.className = "selection-box";
    document.body.appendChild(box);
    selected.clear();
    syncSelectionView();
    main.setPointerCapture(ev.pointerId);
    ev.preventDefault();
  });
  main.addEventListener("pointermove", (ev) => {
    if (!box || !boxStart) return;
    const x1 = Math.min(boxStart.x, ev.clientX), y1 = Math.min(boxStart.y, ev.clientY);
    const x2 = Math.max(boxStart.x, ev.clientX), y2 = Math.max(boxStart.y, ev.clientY);
    Object.assign(box.style, {
      left: x1 + "px", top: y1 + "px", width: (x2 - x1) + "px", height: (y2 - y1) + "px"
    });
    const boxRect = { left: x1, top: y1, right: x2, bottom: y2 };
    selected.clear();
    fileList.querySelectorAll(".row").forEach(row => {
      const r = row.getBoundingClientRect();
      const hit = !(r.right < boxRect.left || r.left > boxRect.right || r.bottom < boxRect.top || r.top > boxRect.bottom);
      if (hit) selected.add(row.dataset.path);
    });
    syncSelectionView();
  });
  main.addEventListener("pointerup", (ev) => {
    if (!box) return;
    box.remove();
    box = null;
    boxStart = null;
    try { main.releasePointerCapture(ev.pointerId); } catch (_) {}
  });

  load("");
})();
</script>
</body>
</html>
'''

app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
