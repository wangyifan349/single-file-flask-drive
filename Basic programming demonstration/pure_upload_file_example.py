"""
This program is a single-file Flask file manager that combines the frontend
and backend in one Python script. It provides a complete browser-based file
management interface with hierarchical directory browsing, breadcrumb
navigation, multi-file upload, file download, folder download, multi-selection,
drag-and-drop movement, renaming, deletion, and directory creation. All file
operations are restricted to a fixed storage directory to reduce path traversal
risks.

The program is designed for teaching and practical local deployment. It
includes strong fault tolerance for unstable network conditions, browser-side
SHA-256 verification, backend hash validation, safe temporary upload handling,
and disk-based archive buffering. For downloading folders or multiple selected
items, it creates compressed .7z archives in a temporary buffer directory
before sending them to the browser, avoiding in-memory archive generation and
supporting efficient compressed downloads.

Dependencies:

pip install flask py7zr
"""

from __future__ import annotations

import hashlib
import lzma
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable

import py7zr
from flask import Flask, jsonify, render_template_string, request, send_file, send_from_directory

app = Flask(__name__)

# Paths and runtime settings.
BASE_DIRECTORY = Path(__file__).resolve().parent
STORAGE_ROOT = (BASE_DIRECTORY / "storage").resolve()
BUFFER_ROOT = (BASE_DIRECTORY / "file_manager_buffer").resolve()
UPLOAD_BUFFER_ROOT = (BUFFER_ROOT / "uploads").resolve()
ARCHIVE_BUFFER_ROOT = (BUFFER_ROOT / "archives").resolve()

ARCHIVE_MAX_AGE_SECONDS = int(os.environ.get("ARCHIVE_MAX_AGE_SECONDS", str(6 * 60 * 60)))
PY7ZR_PRESET_LEVEL = max(0, min(9, int(os.environ.get("PY7ZR_PRESET", "9"))))
PY7ZR_EXTREME_MODE = os.environ.get("PY7ZR_EXTREME", "1") not in {"0", "false", "False", "no", "NO"}

for required_directory in (STORAGE_ROOT, UPLOAD_BUFFER_ROOT, ARCHIVE_BUFFER_ROOT):
    required_directory.mkdir(parents=True, exist_ok=True)

INVALID_PATH_SEGMENT_PATTERN = re.compile(r'[<>:"|?*\x00-\x1f]')
ARCHIVE_JOB_PATTERN = re.compile(r"^[a-f0-9]{32}$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
STORAGE_WRITE_LOCK = threading.RLock()


# JSON API error handling.
class ApiError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


@app.errorhandler(ApiError)
def handle_api_error(error: ApiError):
    return jsonify({"ok": False, "error": error.message}), error.status_code


def raise_api_error(message: str, status_code: int = 400):
    raise ApiError(message, status_code)


# Path normalization keeps every operation inside STORAGE_ROOT.
def normalize_path_segment(path_segment: str) -> str:
    cleaned_segment = INVALID_PATH_SEGMENT_PATTERN.sub("_", path_segment.strip())
    if not cleaned_segment or cleaned_segment in {".", ".."}:
        raise_api_error("非法文件名")
    return cleaned_segment


def normalize_relative_path(raw_path: str | None, *, allow_empty: bool = True) -> str:
    if raw_path is None:
        if allow_empty:
            return ""
        raise_api_error("路径不能为空")

    normalized_path = str(raw_path).replace("\\", "/").strip().lstrip("/")
    if normalized_path in {"", "."}:
        if allow_empty:
            return ""
        raise_api_error("路径不能为空")

    normalized_segments: list[str] = []
    for raw_segment in normalized_path.split("/"):
        if raw_segment in {"", "."}:
            continue
        if raw_segment == "..":
            raise_api_error("路径不能包含 ..")
        normalized_segments.append(normalize_path_segment(raw_segment))

    if not normalized_segments and not allow_empty:
        raise_api_error("路径不能为空")
    return "/".join(normalized_segments)


def resolve_storage_path(relative_path: str | None, *, allow_empty: bool = True) -> Path:
    normalized_relative_path = normalize_relative_path(relative_path, allow_empty=allow_empty)
    resolved_path = (STORAGE_ROOT / normalized_relative_path).resolve()
    try:
        resolved_path.relative_to(STORAGE_ROOT)
    except ValueError:
        raise_api_error("路径越界")
    return resolved_path


def get_relative_path(absolute_path: Path) -> str:
    resolved_path = absolute_path.resolve()
    if resolved_path == STORAGE_ROOT:
        return ""
    return resolved_path.relative_to(STORAGE_ROOT).as_posix()


def build_item_metadata(item_path: Path) -> dict:
    item_status = item_path.stat()
    item_is_directory = item_path.is_dir()
    return {
        "name": item_path.name,
        "path": get_relative_path(item_path),
        "type": "dir" if item_is_directory else "file",
        "size": None if item_is_directory else item_status.st_size,
        "mtime": int(item_status.st_mtime),
    }


def list_directory_contents(relative_path: str) -> dict:
    directory_path = resolve_storage_path(relative_path)
    if not directory_path.exists():
        raise_api_error("目录不存在", 404)
    if not directory_path.is_dir():
        raise_api_error("不是目录")

    directory_items = [build_item_metadata(child_path) for child_path in directory_path.iterdir()]
    directory_items.sort(key=lambda item: (item["type"] != "dir", item["name"].lower()))

    clean_relative_path = get_relative_path(directory_path)
    breadcrumbs = [{"name": "根目录", "path": ""}]
    breadcrumb_segments: list[str] = []
    for path_segment in clean_relative_path.split("/") if clean_relative_path else []:
        breadcrumb_segments.append(path_segment)
        breadcrumbs.append({"name": path_segment, "path": "/".join(breadcrumb_segments)})

    return {
        "ok": True,
        "path": clean_relative_path,
        "breadcrumbs": breadcrumbs,
        "items": directory_items,
    }


# Hashing is used to decide whether a collision can be replaced safely.
def calculate_file_sha256(file_path: Path) -> str:
    hash_context = hashlib.sha256()
    with file_path.open("rb") as file_stream:
        for file_chunk in iter(lambda: file_stream.read(1024 * 1024), b""):
            hash_context.update(file_chunk)
    return hash_context.hexdigest()


def calculate_directory_sha256(directory_path: Path) -> str:
    hash_context = hashlib.sha256()
    directory_root = directory_path.resolve()
    sorted_paths = sorted(directory_root.rglob("*"), key=lambda child_path: child_path.relative_to(directory_root).as_posix())

    for child_path in sorted_paths:
        child_relative_bytes = child_path.relative_to(directory_root).as_posix().encode("utf-8", "surrogateescape")
        if child_path.is_dir():
            hash_context.update(b"D\0" + child_relative_bytes + b"\0")
            continue
        if child_path.is_file():
            hash_context.update(b"F\0" + child_relative_bytes + b"\0")
            hash_context.update(calculate_file_sha256(child_path).encode("ascii"))
            hash_context.update(b"\0")

    return hash_context.hexdigest()


def paths_have_same_content(first_path: Path, second_path: Path) -> bool:
    if not first_path.exists() or not second_path.exists():
        return False
    if first_path.is_file() and second_path.is_file():
        return calculate_file_sha256(first_path) == calculate_file_sha256(second_path)
    if first_path.is_dir() and second_path.is_dir():
        return calculate_directory_sha256(first_path) == calculate_directory_sha256(second_path)
    return False


def build_timestamped_path(target_path: Path) -> Path:
    timestamp_suffix = datetime.now().strftime("%Y%m%d-%H%M%S")
    target_parent = target_path.parent

    if target_path.suffix:
        timestamped_path = target_parent / f"{target_path.stem}_{timestamp_suffix}{target_path.suffix}"
    else:
        timestamped_path = target_parent / f"{target_path.name}_{timestamp_suffix}"

    conflict_counter = 2
    while timestamped_path.exists():
        if target_path.suffix:
            timestamped_path = target_parent / f"{target_path.stem}_{timestamp_suffix}_{conflict_counter}{target_path.suffix}"
        else:
            timestamped_path = target_parent / f"{target_path.name}_{timestamp_suffix}_{conflict_counter}"
        conflict_counter += 1

    return timestamped_path


def remove_path(target_path: Path) -> None:
    if not target_path.exists():
        return
    if target_path.is_dir():
        shutil.rmtree(target_path)
        return
    target_path.unlink()


def move_or_replace_with_collision_handling(source_path: Path, target_path: Path) -> tuple[Path, str]:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    final_path = target_path
    operation_result = "moved"

    if target_path.exists() and paths_have_same_content(source_path, target_path):
        remove_path(target_path)
        operation_result = "replaced_same_hash"
    elif target_path.exists():
        final_path = build_timestamped_path(target_path)
        operation_result = "renamed_with_timestamp"

    shutil.move(str(source_path), str(final_path))
    return final_path, operation_result


def path_is_inside_or_equal(parent_path: Path, child_path: Path) -> bool:
    try:
        child_path.resolve().relative_to(parent_path.resolve())
        return True
    except ValueError:
        return False


# Archive files are written to disk first and served later by URL.
def remove_expired_archives() -> None:
    current_timestamp = time.time()
    ARCHIVE_BUFFER_ROOT.mkdir(parents=True, exist_ok=True)

    for archive_job_directory in ARCHIVE_BUFFER_ROOT.iterdir():
        try:
            archive_age_seconds = current_timestamp - archive_job_directory.stat().st_mtime
        except FileNotFoundError:
            continue
        if archive_age_seconds > ARCHIVE_MAX_AGE_SECONDS:
            remove_path(archive_job_directory)


def unique_existing_item_paths(raw_item_paths: Iterable[str]) -> list[str]:
    unique_paths: list[str] = []
    seen_paths: set[str] = set()

    for raw_item_path in raw_item_paths:
        relative_path = normalize_relative_path(raw_item_path, allow_empty=False)
        storage_path = resolve_storage_path(relative_path, allow_empty=False)
        if not storage_path.exists():
            raise_api_error(f"不存在：{relative_path}", 404)
        if relative_path in seen_paths:
            continue
        unique_paths.append(relative_path)
        seen_paths.add(relative_path)

    if not unique_paths:
        raise_api_error("没有选择任何文件")
    return unique_paths


def build_py7zr_filters() -> list[dict]:
    preset_value = PY7ZR_PRESET_LEVEL
    if PY7ZR_EXTREME_MODE:
        preset_value = preset_value | lzma.PRESET_EXTREME
    return [{"id": py7zr.FILTER_LZMA2, "preset": preset_value}]


def create_7z_archive(item_paths: Iterable[str]) -> tuple[str, Path]:
    archive_item_paths = unique_existing_item_paths(item_paths)
    remove_expired_archives()

    archive_job_id = uuid.uuid4().hex
    archive_job_directory = ARCHIVE_BUFFER_ROOT / archive_job_id
    archive_job_directory.mkdir(parents=True, exist_ok=False)

    archive_file_name = f"download_{datetime.now().strftime('%Y%m%d-%H%M%S')}.7z"
    archive_path = archive_job_directory / archive_file_name

    try:
        with py7zr.SevenZipFile(archive_path, mode="w", filters=build_py7zr_filters()) as archive_file:
            for relative_path in archive_item_paths:
                storage_path = resolve_storage_path(relative_path, allow_empty=False)
                if storage_path.is_dir():
                    archive_file.writeall(storage_path, arcname=relative_path)
                    continue
                archive_file.write(storage_path, arcname=relative_path)
    except Exception as error:
        remove_path(archive_job_directory)
        raise_api_error(f"py7zr 压缩失败：{error}", 500)

    if not archive_path.exists() or archive_path.stat().st_size <= 0:
        remove_path(archive_job_directory)
        raise_api_error("py7zr 压缩失败：没有生成有效压缩包", 500)

    return archive_job_id, archive_path


def resolve_archive_download_path(archive_job_id: str, archive_file_name: str) -> Path:
    if not ARCHIVE_JOB_PATTERN.match(archive_job_id):
        raise_api_error("非法下载任务", 404)

    safe_archive_file_name = normalize_path_segment(archive_file_name)
    archive_path = (ARCHIVE_BUFFER_ROOT / archive_job_id / safe_archive_file_name).resolve()
    try:
        archive_path.relative_to(ARCHIVE_BUFFER_ROOT)
    except ValueError:
        raise_api_error("路径越界", 404)

    if not archive_path.exists() or not archive_path.is_file():
        raise_api_error("压缩包不存在或已过期", 404)
    return archive_path


# Flask routes.
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.get("/api/list")
def api_list():
    return jsonify(list_directory_contents(request.args.get("path", "")))


@app.post("/api/mkdir")
def api_mkdir():
    request_data = request.get_json(force=True)
    parent_directory = resolve_storage_path(request_data.get("path", ""))
    if not parent_directory.is_dir():
        raise_api_error("目标不是目录")

    directory_name = normalize_path_segment(str(request_data.get("name", "")).strip())
    with STORAGE_WRITE_LOCK:
        new_directory_path = parent_directory / directory_name
        if new_directory_path.exists():
            new_directory_path = build_timestamped_path(new_directory_path)
        new_directory_path.mkdir(parents=True, exist_ok=False)

    return jsonify({"ok": True, "item": build_item_metadata(new_directory_path)})


@app.post("/api/upload")
def api_upload():
    target_directory = resolve_storage_path(request.args.get("path", ""))
    if not target_directory.exists() or not target_directory.is_dir():
        raise_api_error("上传目标不是目录")

    uploaded_file = request.files.get("file")
    if uploaded_file is None:
        raise_api_error("没有收到文件")

    expected_file_hash = str(request.form.get("sha256", "")).lower().strip()
    if not SHA256_PATTERN.match(expected_file_hash):
        raise_api_error("缺少或非法 sha256")

    relative_file_path = normalize_relative_path(
        request.form.get("relativePath") or uploaded_file.filename,
        allow_empty=False,
    )
    target_path = target_directory / relative_file_path
    temporary_upload_path = UPLOAD_BUFFER_ROOT / f"upload_{uuid.uuid4().hex}.tmp"

    try:
        uploaded_file.save(temporary_upload_path)
        actual_file_hash = calculate_file_sha256(temporary_upload_path)

        if actual_file_hash != expected_file_hash:
            remove_path(temporary_upload_path)
            return jsonify({
                "ok": False,
                "code": "hash_mismatch",
                "error": "hash is not match",
                "expectedSha256": expected_file_hash,
                "actualSha256": actual_file_hash,
                "relativePath": relative_file_path,
            }), 409

        with STORAGE_WRITE_LOCK:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            final_path, operation_result = move_or_replace_with_collision_handling(temporary_upload_path, target_path)
    except ApiError:
        remove_path(temporary_upload_path)
        raise
    except Exception as error:
        remove_path(temporary_upload_path)
        raise_api_error(f"上传失败：{error}", 500)

    return jsonify({
        "ok": True,
        "item": build_item_metadata(final_path),
        "action": operation_result,
        "expectedSha256": expected_file_hash,
    })


@app.post("/api/delete")
def api_delete():
    request_data = request.get_json(force=True)
    selected_items = request_data.get("items") or []
    if not isinstance(selected_items, list):
        raise_api_error("items 必须是列表")

    deleted_paths = []
    with STORAGE_WRITE_LOCK:
        for raw_item_path in selected_items:
            relative_path = normalize_relative_path(raw_item_path, allow_empty=False)
            storage_path = resolve_storage_path(relative_path, allow_empty=False)
            if not storage_path.exists():
                continue
            remove_path(storage_path)
            deleted_paths.append(relative_path)

    return jsonify({"ok": True, "deleted": deleted_paths})


@app.post("/api/move")
def api_move():
    request_data = request.get_json(force=True)
    selected_items = request_data.get("items") or []
    destination_relative_path = request_data.get("dest", "")
    if not isinstance(selected_items, list):
        raise_api_error("items 必须是列表")

    destination_directory = resolve_storage_path(destination_relative_path)
    if not destination_directory.exists() or not destination_directory.is_dir():
        raise_api_error("移动目标不是目录")

    moved_items = []
    with STORAGE_WRITE_LOCK:
        for raw_item_path in selected_items:
            relative_path = normalize_relative_path(raw_item_path, allow_empty=False)
            source_path = resolve_storage_path(relative_path, allow_empty=False)
            if not source_path.exists():
                continue
            if source_path.resolve() == STORAGE_ROOT:
                raise_api_error("不能移动根目录")
            if source_path.resolve() == destination_directory.resolve():
                continue
            if source_path.is_dir() and path_is_inside_or_equal(source_path, destination_directory):
                raise_api_error(f"不能把目录移动到自己或自己的子目录中：{relative_path}")

            target_path = destination_directory / source_path.name
            if source_path.resolve() == target_path.resolve():
                continue

            final_path, operation_result = move_or_replace_with_collision_handling(source_path, target_path)
            moved_items.append({"from": relative_path, "to": get_relative_path(final_path), "action": operation_result})

    return jsonify({"ok": True, "moved": moved_items})


@app.post("/api/rename")
def api_rename():
    request_data = request.get_json(force=True)
    relative_path = normalize_relative_path(request_data.get("path"), allow_empty=False)
    source_path = resolve_storage_path(relative_path, allow_empty=False)
    if not source_path.exists():
        raise_api_error("源路径不存在", 404)
    if source_path.resolve() == STORAGE_ROOT:
        raise_api_error("不能重命名根目录")

    new_name = normalize_path_segment(str(request_data.get("name", "")).strip())
    target_path = source_path.parent / new_name
    if target_path.resolve() == source_path.resolve():
        return jsonify({"ok": True, "item": build_item_metadata(source_path), "action": "noop"})

    with STORAGE_WRITE_LOCK:
        final_path, operation_result = move_or_replace_with_collision_handling(source_path, target_path)
    return jsonify({"ok": True, "item": build_item_metadata(final_path), "action": operation_result})


@app.get("/api/download")
def api_download_single_item():
    relative_path = normalize_relative_path(request.args.get("path"), allow_empty=False)
    storage_path = resolve_storage_path(relative_path, allow_empty=False)
    if not storage_path.exists():
        raise_api_error("文件不存在", 404)

    if storage_path.is_dir():
        archive_job_id, archive_path = create_7z_archive([relative_path])
        return send_file(archive_path, as_attachment=True, download_name=archive_path.name)

    return send_from_directory(STORAGE_ROOT, relative_path, as_attachment=True, download_name=storage_path.name)


@app.post("/api/archive")
def api_create_archive():
    request_data = request.get_json(force=True)
    selected_items = request_data.get("items") or []
    if not isinstance(selected_items, list):
        raise_api_error("items 必须是列表")

    archive_job_id, archive_path = create_7z_archive(selected_items)
    download_url = f"/api/archive/{archive_job_id}/{archive_path.name}"
    return jsonify({"ok": True, "downloadUrl": download_url})


@app.get("/api/archive/<archive_job_id>/<archive_file_name>")
def api_download_archive(archive_job_id: str, archive_file_name: str):
    archive_path = resolve_archive_download_path(archive_job_id, archive_file_name)
    return send_file(archive_path, as_attachment=True, download_name=archive_path.name)


HTML_TEMPLATE = r'''
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Flask 文件管理器</title>
  <style>
    :root {
      --background: rgb(20, 5, 0);
      --surface: rgb(52, 14, 0);
      --line: rgb(96, 40, 0);
      --text: rgb(255, 218, 0);
      --muted: rgb(210, 126, 0);
      --hover: rgb(76, 24, 0);
      --selected: rgb(112, 46, 0);
      --selected-border: rgb(255, 196, 0);
      --menu-shadow: 0 12px 40px rgba(0, 0, 0, .35);
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; }
    body {
      margin: 0;
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background: var(--background);
      user-select: none;
    }
    #app {
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }
    .breadcrumb-bar {
      height: 38px;
      flex: 0 0 auto;
      display: flex;
      align-items: center;
      gap: 4px;
      padding: 0 10px;
      overflow-x: auto;
      white-space: nowrap;
      background: var(--surface);
      border-bottom: 1px solid var(--line);
    }
    .breadcrumb-item {
      display: inline-flex;
      align-items: center;
      height: 24px;
      padding: 0 8px;
      border-radius: 5px;
      color: rgb(255, 196, 0);
      cursor: pointer;
    }
    .breadcrumb-item:hover,
    .breadcrumb-item.drop-target {
      background: rgb(76, 24, 0);
    }
    .breadcrumb-separator {
      color: var(--muted);
    }
    .workspace {
      flex: 1;
      min-height: 0;
      position: relative;
      overflow: auto;
      background: var(--surface);
    }
    .file-table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }
    .file-table thead {
      position: sticky;
      top: 0;
      z-index: 5;
      background: rgb(40, 10, 0);
      color: var(--muted);
      font-size: 12px;
    }
    .file-table th,
    .file-table td {
      height: 38px;
      padding: 0 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: middle;
    }
    .file-table th:first-child,
    .file-table td:first-child {
      width: 42px;
      text-align: center;
    }
    .file-table th:nth-child(3),
    .file-table td:nth-child(3) {
      width: 120px;
    }
    .file-table th:nth-child(4),
    .file-table td:nth-child(4) {
      width: 180px;
    }
    .file-row {
      cursor: default;
    }
    .file-row:hover {
      background: var(--hover);
    }
    .file-row.selected {
      background: var(--selected);
      box-shadow: inset 3px 0 0 var(--selected-border);
    }
    .file-row.drop-target {
      outline: 2px solid var(--selected-border);
      outline-offset: -2px;
    }
    .file-icon {
      font-size: 19px;
    }
    .file-name {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .muted-text {
      color: var(--muted);
    }
    .empty-state {
      padding: 56px 16px;
      text-align: center;
      color: var(--muted);
    }
    .selection-box {
      position: fixed;
      z-index: 30;
      pointer-events: none;
      border: 1px solid rgb(255, 196, 0);
      background: rgba(255, 196, 0, .18);
    }
    .context-menu {
      position: fixed;
      z-index: 100;
      min-width: 176px;
      display: none;
      padding: 6px;
      border: 1px solid var(--line);
      border-radius: 9px;
      background: var(--surface);
      box-shadow: var(--menu-shadow);
    }
    .context-menu.show {
      display: block;
    }
    .menu-item {
      display: block;
      width: 100%;
      padding: 8px 10px;
      border-radius: 7px;
      cursor: pointer;
      color: var(--text);
    }
    .menu-item:hover {
      background: var(--hover);
    }
    .menu-item.danger {
      color: rgb(255, 84, 0);
    }
    .menu-separator {
      height: 1px;
      margin: 5px 4px;
      background: var(--line);
    }
    .toast {
      position: fixed;
      left: 50%;
      bottom: 22px;
      z-index: 120;
      display: none;
      max-width: min(720px, calc(100vw - 30px));
      padding: 9px 12px;
      border-radius: 999px;
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
      transform: translateX(-50%);
      background: rgba(20, 5, 0, .94);
      color: rgb(255, 218, 0);
    }
    .toast.show {
      display: block;
    }
    .upload-panel {
      position: fixed;
      right: 14px;
      bottom: 14px;
      z-index: 110;
      display: none;
      width: min(460px, calc(100vw - 28px));
      max-height: min(360px, calc(100vh - 80px));
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--surface);
      box-shadow: var(--menu-shadow);
    }
    .upload-panel.show {
      display: block;
    }
    .upload-header {
      position: sticky;
      top: 0;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 9px 10px;
      border-bottom: 1px solid var(--line);
      background: var(--surface);
      font-size: 13px;
      font-weight: 600;
    }
    .upload-task-list {
      padding: 6px 8px 8px;
    }
    .upload-task {
      padding: 7px 4px;
      border-bottom: 1px solid var(--line);
    }
    .upload-task:last-child {
      border-bottom: 0;
    }
    .upload-task-name {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 12px;
    }
    .upload-task-meta {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      margin-top: 3px;
      color: var(--muted);
      font-size: 11px;
    }
    .upload-progress-track {
      height: 4px;
      margin-top: 5px;
      overflow: hidden;
      border-radius: 999px;
      background: rgb(96, 40, 0);
    }
    .upload-progress-bar {
      height: 100%;
      width: 0;
      border-radius: inherit;
      background: rgb(255, 196, 0);
    }
    input[type=file] {
      display: none;
    }
  </style>
</head>
<body>
<div id="app">
  <nav id="breadcrumbBar" class="breadcrumb-bar"></nav>
  <main id="workspace" class="workspace">
    <table id="fileTable" class="file-table">
      <thead>
        <tr>
          <th></th>
          <th>名称</th>
          <th>大小</th>
          <th>修改时间</th>
        </tr>
      </thead>
      <tbody id="fileTableBody"></tbody>
    </table>
  </main>
</div>

<div id="contextMenu" class="context-menu"></div>
<div id="uploadPanel" class="upload-panel">
  <div class="upload-header">
    <span id="uploadPanelTitle">上传队列</span>
    <span id="uploadPanelSummary" class="muted-text"></span>
  </div>
  <div id="uploadTaskList" class="upload-task-list"></div>
</div>
<div id="toast" class="toast"></div>
<input id="fileInput" type="file" multiple />
<input id="folderInput" type="file" webkitdirectory directory multiple />

<script src="https://cdn.jsdelivr.net/npm/hash-wasm@4.12.0"></script>
<script>
(() => {
  // Runtime state.
  let currentPath = "";
  let currentItems = [];
  const selectedPaths = new Set();

  const breadcrumbBar = document.getElementById("breadcrumbBar");
  const workspace = document.getElementById("workspace");
  const fileTableBody = document.getElementById("fileTableBody");
  const contextMenu = document.getElementById("contextMenu");
  const uploadPanel = document.getElementById("uploadPanel");
  const uploadPanelTitle = document.getElementById("uploadPanelTitle");
  const uploadPanelSummary = document.getElementById("uploadPanelSummary");
  const uploadTaskList = document.getElementById("uploadTaskList");
  const toast = document.getElementById("toast");
  const fileInput = document.getElementById("fileInput");
  const folderInput = document.getElementById("folderInput");

  const HASH_CHUNK_SIZE = 4 * 1024 * 1024;
  const MAX_PARALLEL_UPLOADS = 4;
  const SERVER_ERROR_RETRY_LIMIT = 20;
  const RETRY_DELAY_BASE_MS = 700;
  const RETRY_DELAY_MAX_MS = 5000;

  let uploadTaskSequence = 0;
  let activeUploadCount = 0;
  let pendingUploadTasks = [];
  let visibleUploadTasks = [];

  // Small formatting and request helpers.
  function encodePath(path) {
    return encodeURIComponent(path || "");
  }

  function formatFileSize(sizeInBytes) {
    if (sizeInBytes == null) return "—";
    const sizeUnits = ["B", "KB", "MB", "GB", "TB"];
    let displaySize = sizeInBytes;
    let unitIndex = 0;
    while (displaySize >= 1024 && unitIndex < sizeUnits.length - 1) {
      displaySize /= 1024;
      unitIndex += 1;
    }
    return `${displaySize.toFixed(unitIndex ? 1 : 0)} ${sizeUnits[unitIndex]}`;
  }

  function formatModifiedTime(unixSeconds) {
    if (!unixSeconds) return "—";
    return new Date(unixSeconds * 1000).toLocaleString();
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>\"]/g, matchedCharacter => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "\"": "&quot;"
    }[matchedCharacter]));
  }

  function showToast(message) {
    toast.textContent = message;
    toast.classList.add("show");
    clearTimeout(showToast.timerId);
    showToast.timerId = setTimeout(() => toast.classList.remove("show"), 2200);
  }

  async function requestJsonOrThrow(url, options = {}) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let errorMessage = response.statusText;
      try {
        const errorPayload = await response.json();
        errorMessage = errorPayload.error || errorMessage;
      } catch (ignoredError) {}
      throw new Error(errorMessage);
    }

    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      return response.json();
    }
    return response;
  }

  // Directory rendering.
  async function loadDirectory(path = currentPath) {
    try {
      const directoryPayload = await requestJsonOrThrow(`/api/list?path=${encodePath(path)}`);
      currentPath = directoryPayload.path || "";
      currentItems = directoryPayload.items || [];
      selectedPaths.clear();
      renderBreadcrumbs(directoryPayload.breadcrumbs || []);
      renderFileTable();
    } catch (error) {
      showToast(error.message);
    }
  }

  function renderBreadcrumbs(breadcrumbs) {
    breadcrumbBar.innerHTML = "";
    breadcrumbs.forEach((breadcrumb, breadcrumbIndex) => {
      if (breadcrumbIndex > 0) {
        const separatorElement = document.createElement("span");
        separatorElement.className = "breadcrumb-separator";
        separatorElement.textContent = "/";
        breadcrumbBar.appendChild(separatorElement);
      }

      const breadcrumbElement = document.createElement("span");
      breadcrumbElement.className = "breadcrumb-item";
      breadcrumbElement.textContent = breadcrumb.name;
      breadcrumbElement.dataset.path = breadcrumb.path;
      breadcrumbElement.addEventListener("click", () => loadDirectory(breadcrumb.path));
      breadcrumbElement.addEventListener("dragover", handleDestinationDragOver);
      breadcrumbElement.addEventListener("dragleave", () => breadcrumbElement.classList.remove("drop-target"));
      breadcrumbElement.addEventListener("drop", async dropEvent => {
        breadcrumbElement.classList.remove("drop-target");
        await handleDrop(dropEvent, breadcrumb.path);
      });
      breadcrumbBar.appendChild(breadcrumbElement);
    });
  }

  function renderFileTable() {
    fileTableBody.innerHTML = "";

    if (!currentItems.length) {
      const emptyRow = document.createElement("tr");
      const emptyCell = document.createElement("td");
      emptyCell.colSpan = 4;
      emptyCell.className = "empty-state";
      emptyCell.textContent = "空目录。可以拖拽文件到这里上传，或右键空白区域操作。";
      emptyRow.appendChild(emptyCell);
      fileTableBody.appendChild(emptyRow);
      return;
    }

    for (const itemRecord of currentItems) {
      const rowElement = document.createElement("tr");
      rowElement.className = "file-row";
      rowElement.draggable = true;
      rowElement.dataset.path = itemRecord.path;
      rowElement.dataset.type = itemRecord.type;
      rowElement.innerHTML = `
        <td class="file-icon">${itemRecord.type === "dir" ? "📁" : "📄"}</td>
        <td class="file-name" title="${escapeHtml(itemRecord.name)}">${escapeHtml(itemRecord.name)}</td>
        <td class="muted-text">${formatFileSize(itemRecord.size)}</td>
        <td class="muted-text">${formatModifiedTime(itemRecord.mtime)}</td>`;

      rowElement.addEventListener("click", clickEvent => handleRowClick(clickEvent, itemRecord));
      rowElement.addEventListener("contextmenu", contextEvent => handleRowContextMenu(contextEvent, itemRecord));
      rowElement.addEventListener("dragstart", dragEvent => handleDragStart(dragEvent, itemRecord));

      if (itemRecord.type === "dir") {
        rowElement.addEventListener("dragover", handleDestinationDragOver);
        rowElement.addEventListener("dragleave", () => rowElement.classList.remove("drop-target"));
        rowElement.addEventListener("drop", async dropEvent => {
          rowElement.classList.remove("drop-target");
          await handleDrop(dropEvent, itemRecord.path);
        });
      }

      fileTableBody.appendChild(rowElement);
    }

    syncSelectionView();
  }

  function syncSelectionView() {
    fileTableBody.querySelectorAll(".file-row").forEach(rowElement => {
      rowElement.classList.toggle("selected", selectedPaths.has(rowElement.dataset.path));
    });
  }

  function getSelectedPathList() {
    return Array.from(selectedPaths);
  }

  function getSelectedItemList() {
    return currentItems.filter(itemRecord => selectedPaths.has(itemRecord.path));
  }

  function handleRowClick(clickEvent, itemRecord) {
    closeContextMenu();

    if (clickEvent.ctrlKey || clickEvent.metaKey) {
      if (selectedPaths.has(itemRecord.path)) {
        selectedPaths.delete(itemRecord.path);
      } else {
        selectedPaths.add(itemRecord.path);
      }
      syncSelectionView();
      return;
    }

    if (itemRecord.type === "dir") {
      loadDirectory(itemRecord.path);
      return;
    }
    downloadSingleFile(itemRecord.path);
  }

  function handleRowContextMenu(contextEvent, itemRecord) {
    contextEvent.preventDefault();

    if (!selectedPaths.has(itemRecord.path)) {
      selectedPaths.clear();
      selectedPaths.add(itemRecord.path);
      syncSelectionView();
    }

    showContextMenu(contextEvent.clientX, contextEvent.clientY, buildSelectionMenuActions());
  }

  // Context menu actions.
  function buildSelectionMenuActions() {
    const selectedItems = getSelectedItemList();
    const singleItem = selectedItems.length === 1 ? selectedItems[0] : null;
    const menuActions = [];

    if (singleItem && singleItem.type === "dir") {
      menuActions.push({ label: "打开", handler: () => loadDirectory(singleItem.path) });
    }

    menuActions.push({
      label: selectedItems.length > 1 || (singleItem && singleItem.type === "dir") ? "下载为 7z" : "下载",
      handler: () => downloadSelectedItems()
    });

    if (singleItem) {
      menuActions.push({ label: "重命名", handler: () => renameItem(singleItem.path) });
    }

    menuActions.push({ separator: true });
    menuActions.push({
      label: `删除${selectedItems.length > 1 ? "选中项" : ""}`,
      handler: () => deleteSelectedItems(),
      danger: true
    });

    return menuActions;
  }

  function buildBlankMenuActions() {
    return [
      { label: "上传文件", handler: () => chooseFilesForUpload(currentPath) },
      { label: "上传文件夹", handler: () => chooseFolderForUpload(currentPath) },
      { label: "新建文件夹", handler: () => createDirectory() },
      { label: "刷新", handler: () => loadDirectory(currentPath) }
    ];
  }

  function showContextMenu(clientX, clientY, menuActions) {
    contextMenu.innerHTML = "";

    for (const menuAction of menuActions) {
      if (menuAction.separator) {
        const separatorElement = document.createElement("div");
        separatorElement.className = "menu-separator";
        contextMenu.appendChild(separatorElement);
        continue;
      }

      const menuItemElement = document.createElement("div");
      menuItemElement.className = menuAction.danger ? "menu-item danger" : "menu-item";
      menuItemElement.textContent = menuAction.label;
      menuItemElement.addEventListener("click", async () => {
        closeContextMenu();
        try {
          await menuAction.handler();
        } catch (error) {
          showToast(error.message);
        }
      });
      contextMenu.appendChild(menuItemElement);
    }

    contextMenu.classList.add("show");
    const menuRectangle = contextMenu.getBoundingClientRect();
    const menuLeft = Math.min(clientX, window.innerWidth - menuRectangle.width - 8);
    const menuTop = Math.min(clientY, window.innerHeight - menuRectangle.height - 8);
    contextMenu.style.left = `${menuLeft}px`;
    contextMenu.style.top = `${menuTop}px`;
  }

  function closeContextMenu() {
    contextMenu.classList.remove("show");
  }

  workspace.addEventListener("contextmenu", contextEvent => {
    if (contextEvent.target.closest(".file-row")) return;
    contextEvent.preventDefault();
    selectedPaths.clear();
    syncSelectionView();
    showContextMenu(contextEvent.clientX, contextEvent.clientY, buildBlankMenuActions());
  });

  document.addEventListener("click", clickEvent => {
    if (!clickEvent.target.closest(".context-menu")) {
      closeContextMenu();
    }
  });

  // Upload and file operations.
  function chooseFilesForUpload(destinationPath) {
    fileInput.value = "";
    fileInput.onchange = () => uploadFileList(fileInput.files, destinationPath, false).catch(error => showToast(error.message));
    fileInput.click();
  }

  function chooseFolderForUpload(destinationPath) {
    folderInput.value = "";
    folderInput.onchange = () => uploadFileList(folderInput.files, destinationPath, true).catch(error => showToast(error.message));
    folderInput.click();
  }

  async function uploadFileList(fileList, destinationPath, keepRelativePath) {
    const uploadFiles = Array.from(fileList);
    if (!uploadFiles.length) return;

    if (!window.hashwasm || !window.hashwasm.createSHA256) {
      throw new Error("哈希计算库没有加载成功");
    }

    visibleUploadTasks = uploadFiles.map(uploadFile => {
      uploadTaskSequence += 1;
      return {
        id: uploadTaskSequence,
        file: uploadFile,
        relativePath: keepRelativePath && uploadFile.webkitRelativePath ? uploadFile.webkitRelativePath : uploadFile.name,
        expectedSha256: "",
        uploadedItemPath: "",
        status: "pending",
        attemptCount: 0,
        hashProgress: 0,
        uploadProgress: 0,
        errorMessage: "",
      };
    });
    pendingUploadTasks = [...visibleUploadTasks];
    activeUploadCount = 0;

    renderUploadPanel();
    showToast(`准备上传 ${visibleUploadTasks.length} 个文件...`);
    await runUploadQueue(destinationPath);

    const failedTasks = visibleUploadTasks.filter(uploadTask => uploadTask.status === "failed");
    await loadDirectory(currentPath);
    if (failedTasks.length) {
      showToast(`${failedTasks.length} 个文件上传失败`);
      return;
    }
    showToast("上传完成，全部文件已通过哈希校验");
  }

  async function runUploadQueue(destinationPath) {
    return new Promise(resolveUploadQueue => {
      function launchMoreUploadTasks() {
        while (activeUploadCount < MAX_PARALLEL_UPLOADS && pendingUploadTasks.length) {
          const uploadTask = pendingUploadTasks.shift();
          activeUploadCount += 1;
          runUploadTask(uploadTask, destinationPath)
            .catch(error => {
              uploadTask.status = "failed";
              uploadTask.errorMessage = error.message;
              renderUploadPanel();
            })
            .finally(() => {
              activeUploadCount -= 1;
              if (!pendingUploadTasks.length && activeUploadCount === 0) {
                renderUploadPanel();
                resolveUploadQueue();
                return;
              }
              launchMoreUploadTasks();
            });
        }
      }

      launchMoreUploadTasks();
    });
  }

  async function runUploadTask(uploadTask, destinationPath) {
    uploadTask.status = "hashing";
    uploadTask.errorMessage = "";
    renderUploadPanel();

    uploadTask.expectedSha256 = await calculateFileSha256(uploadTask.file, hashProgress => {
      uploadTask.hashProgress = hashProgress;
      renderUploadPanel();
    });

    while (true) {
      uploadTask.attemptCount += 1;
      uploadTask.status = "uploading";
      uploadTask.uploadProgress = 0;
      uploadTask.errorMessage = "";
      renderUploadPanel();

      try {
        const uploadPayload = await uploadFileWithChecksum(uploadTask, destinationPath);
        uploadTask.uploadedItemPath = uploadPayload.item ? uploadPayload.item.path : "";
        uploadTask.uploadProgress = 100;
        uploadTask.status = "done";
        renderUploadPanel();
        return;
      } catch (error) {
        uploadTask.errorMessage = error.message;
        if (!shouldRetryUpload(error, uploadTask.attemptCount)) {
          uploadTask.status = "failed";
          renderUploadPanel();
          return;
        }

        uploadTask.status = "retrying";
        renderUploadPanel();
        await sleep(getUploadRetryDelay(uploadTask.attemptCount));
      }
    }
  }

  async function calculateFileSha256(file, progressHandler) {
    const hashInstance = await window.hashwasm.createSHA256();
    hashInstance.init();

    let processedBytes = 0;
    while (processedBytes < file.size) {
      const nextChunkEnd = Math.min(processedBytes + HASH_CHUNK_SIZE, file.size);
      const fileChunk = file.slice(processedBytes, nextChunkEnd);
      const chunkBuffer = await fileChunk.arrayBuffer();
      hashInstance.update(new Uint8Array(chunkBuffer));
      processedBytes = nextChunkEnd;
      progressHandler(file.size ? Math.round((processedBytes / file.size) * 100) : 100);
    }

    if (file.size === 0) {
      progressHandler(100);
    }
    return hashInstance.digest();
  }

  function uploadFileWithChecksum(uploadTask, destinationPath) {
    return new Promise((resolveUpload, rejectUpload) => {
      const formData = new FormData();
      formData.append("file", uploadTask.file, uploadTask.relativePath);
      formData.append("relativePath", uploadTask.relativePath);
      formData.append("sha256", uploadTask.expectedSha256);
      formData.append("size", String(uploadTask.file.size));

      const uploadRequest = new XMLHttpRequest();
      uploadRequest.open("POST", `/api/upload?path=${encodePath(destinationPath)}`);

      uploadRequest.upload.onprogress = progressEvent => {
        if (!progressEvent.lengthComputable) return;
        uploadTask.uploadProgress = Math.round((progressEvent.loaded / progressEvent.total) * 100);
        renderUploadPanel();
      };

      uploadRequest.onload = () => {
        let responsePayload = {};
        try {
          responsePayload = JSON.parse(uploadRequest.responseText || "{}");
        } catch (ignoredError) {}

        if (uploadRequest.status >= 200 && uploadRequest.status < 300 && responsePayload.ok) {
          resolveUpload(responsePayload);
          return;
        }

        const uploadError = new Error(responsePayload.error || uploadRequest.statusText || "上传失败");
        uploadError.code = responsePayload.code || "upload_failed";
        uploadError.status = uploadRequest.status;
        uploadError.expectedSha256 = responsePayload.expectedSha256 || uploadTask.expectedSha256;
        uploadError.actualSha256 = responsePayload.actualSha256 || "";
        rejectUpload(uploadError);
      };

      uploadRequest.onerror = () => {
        const uploadError = new Error("网络错误，上传中断");
        uploadError.code = "network_error";
        uploadError.status = 0;
        rejectUpload(uploadError);
      };

      uploadRequest.onabort = () => {
        const uploadError = new Error("上传已中止");
        uploadError.code = "upload_aborted";
        uploadError.status = 0;
        rejectUpload(uploadError);
      };

      uploadRequest.send(formData);
    });
  }

  function shouldRetryUpload(uploadError, attemptCount) {
    if (uploadError.code === "hash_mismatch") {
      return attemptCount < SERVER_ERROR_RETRY_LIMIT;
    }
    if (uploadError.status === 0 || uploadError.status >= 500) {
      return attemptCount < SERVER_ERROR_RETRY_LIMIT;
    }
    return false;
  }

  function getUploadRetryDelay(attemptCount) {
    return Math.min(RETRY_DELAY_BASE_MS * attemptCount, RETRY_DELAY_MAX_MS);
  }

  function sleep(delayMilliseconds) {
    return new Promise(resolveSleep => setTimeout(resolveSleep, delayMilliseconds));
  }

  function renderUploadPanel() {
    if (!visibleUploadTasks.length) {
      uploadPanel.classList.remove("show");
      return;
    }

    const completedTaskCount = visibleUploadTasks.filter(uploadTask => uploadTask.status === "done").length;
    const failedTaskCount = visibleUploadTasks.filter(uploadTask => uploadTask.status === "failed").length;
    const runningTaskCount = visibleUploadTasks.filter(uploadTask => ["hashing", "uploading", "retrying"].includes(uploadTask.status)).length;

    uploadPanel.classList.add("show");
    uploadPanelTitle.textContent = "上传队列";
    uploadPanelSummary.textContent = `${completedTaskCount}/${visibleUploadTasks.length} 完成，${runningTaskCount} 运行，${failedTaskCount} 失败`;
    uploadTaskList.innerHTML = "";

    for (const uploadTask of visibleUploadTasks) {
      const taskElement = document.createElement("div");
      taskElement.className = "upload-task";
      const statusLabel = getUploadStatusLabel(uploadTask);
      const progressPercent = uploadTask.status === "hashing" ? uploadTask.hashProgress : uploadTask.uploadProgress;
      taskElement.innerHTML = `
        <div class="upload-task-name" title="${escapeHtml(uploadTask.relativePath)}">${escapeHtml(uploadTask.relativePath)}</div>
        <div class="upload-task-meta">
          <span>${statusLabel}</span>
          <span>${formatFileSize(uploadTask.file.size)}</span>
        </div>
        <div class="upload-progress-track">
          <div class="upload-progress-bar" style="width: ${Math.max(0, Math.min(100, progressPercent))}%"></div>
        </div>`;
      uploadTaskList.appendChild(taskElement);
    }
  }

  function getUploadStatusLabel(uploadTask) {
    if (uploadTask.status === "pending") return "等待中";
    if (uploadTask.status === "hashing") return `计算 SHA-256 ${uploadTask.hashProgress}%`;
    if (uploadTask.status === "uploading") return `上传中 ${uploadTask.uploadProgress}% · 第 ${uploadTask.attemptCount} 次`;
    if (uploadTask.status === "retrying") return `校验失败/传输失败，准备重试 · 第 ${uploadTask.attemptCount} 次`;
    if (uploadTask.status === "done") return "已保存，哈希校验通过";
    if (uploadTask.status === "failed") return uploadTask.errorMessage || "上传失败";
    return uploadTask.status;
  }

  async function createDirectory() {
    const directoryName = prompt("文件夹名称");
    if (!directoryName) return;

    await requestJsonOrThrow("/api/mkdir", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: currentPath, name: directoryName })
    });
    await loadDirectory(currentPath);
  }

  async function renameItem(itemPath) {
    const currentName = itemPath.split("/").pop();
    const newName = prompt("新名称", currentName);
    if (!newName || newName === currentName) return;

    await requestJsonOrThrow("/api/rename", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: itemPath, name: newName })
    });
    await loadDirectory(currentPath);
  }

  async function deleteSelectedItems() {
    const itemPaths = getSelectedPathList();
    if (!itemPaths.length) return;
    if (!confirm(`确认删除 ${itemPaths.length} 项？`)) return;

    await requestJsonOrThrow("/api/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: itemPaths })
    });
    await loadDirectory(currentPath);
    showToast("删除完成");
  }

  function downloadSingleFile(itemPath) {
    window.location.href = `/api/download?path=${encodePath(itemPath)}`;
  }

  async function downloadSelectedItems() {
    const itemPaths = getSelectedPathList();
    if (!itemPaths.length) return;

    if (itemPaths.length === 1) {
      const matchingItem = currentItems.find(itemRecord => itemRecord.path === itemPaths[0]);
      if (matchingItem && matchingItem.type === "file") {
        downloadSingleFile(itemPaths[0]);
        return;
      }
    }

    showToast("正在生成 7z 压缩包...");
    const archivePayload = await requestJsonOrThrow("/api/archive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: itemPaths })
    });
    window.location.href = archivePayload.downloadUrl;
    showToast("下载已开始");
  }

  // Drag moving and drag uploading.
  function handleDragStart(dragEvent, itemRecord) {
    if (!selectedPaths.has(itemRecord.path)) {
      selectedPaths.clear();
      selectedPaths.add(itemRecord.path);
      syncSelectionView();
    }

    dragEvent.dataTransfer.effectAllowed = "move";
    dragEvent.dataTransfer.setData("application/x-file-manager-items", JSON.stringify(getSelectedPathList()));
    dragEvent.dataTransfer.setData("text/plain", getSelectedPathList().join("\n"));
  }

  function handleDestinationDragOver(dragEvent) {
    const hasInternalItems = dragEvent.dataTransfer.types.includes("application/x-file-manager-items");
    const hasExternalFiles = dragEvent.dataTransfer.files.length > 0;
    if (!hasInternalItems && !hasExternalFiles) return;

    dragEvent.preventDefault();
    dragEvent.currentTarget.classList.add("drop-target");
    dragEvent.dataTransfer.dropEffect = hasExternalFiles ? "copy" : "move";
  }

  async function handleDrop(dropEvent, destinationPath) {
    dropEvent.preventDefault();
    closeContextMenu();

    const internalPayload = dropEvent.dataTransfer.getData("application/x-file-manager-items");
    try {
      if (internalPayload) {
        const itemPaths = JSON.parse(internalPayload);
        await moveItemsToDirectory(itemPaths, destinationPath);
        return;
      }

      if (dropEvent.dataTransfer.files && dropEvent.dataTransfer.files.length) {
        await uploadFileList(dropEvent.dataTransfer.files, destinationPath, false);
      }
    } catch (error) {
      showToast(error.message);
    }
  }

  async function moveItemsToDirectory(itemPaths, destinationPath) {
    await requestJsonOrThrow("/api/move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: itemPaths, dest: destinationPath })
    });
    await loadDirectory(currentPath);
    showToast("移动完成");
  }

  workspace.addEventListener("dragover", dragEvent => {
    if (!dragEvent.dataTransfer.files || !dragEvent.dataTransfer.files.length) return;
    dragEvent.preventDefault();
    dragEvent.dataTransfer.dropEffect = "copy";
  });

  workspace.addEventListener("drop", async dropEvent => {
    if (dropEvent.target.closest(".file-row") || dropEvent.target.closest(".breadcrumb-item")) return;
    if (!dropEvent.dataTransfer.files || !dropEvent.dataTransfer.files.length) return;

    dropEvent.preventDefault();
    await uploadFileList(dropEvent.dataTransfer.files, currentPath, false);
  });

  // Rectangle selection works in any drag direction.
  let selectionBoxElement = null;
  let selectionStartPoint = null;

  workspace.addEventListener("pointerdown", pointerEvent => {
    const clickedFileRow = pointerEvent.target.closest(".file-row");
    const clickedMenu = pointerEvent.target.closest(".context-menu");
    if (pointerEvent.button !== 0 || clickedFileRow || clickedMenu) return;

    closeContextMenu();
    selectionStartPoint = { x: pointerEvent.clientX, y: pointerEvent.clientY };
    selectionBoxElement = document.createElement("div");
    selectionBoxElement.className = "selection-box";
    document.body.appendChild(selectionBoxElement);
    selectedPaths.clear();
    syncSelectionView();
    workspace.setPointerCapture(pointerEvent.pointerId);
    pointerEvent.preventDefault();
  });

  workspace.addEventListener("pointermove", pointerEvent => {
    if (!selectionBoxElement || !selectionStartPoint) return;

    const left = Math.min(selectionStartPoint.x, pointerEvent.clientX);
    const top = Math.min(selectionStartPoint.y, pointerEvent.clientY);
    const right = Math.max(selectionStartPoint.x, pointerEvent.clientX);
    const bottom = Math.max(selectionStartPoint.y, pointerEvent.clientY);

    selectionBoxElement.style.left = `${left}px`;
    selectionBoxElement.style.top = `${top}px`;
    selectionBoxElement.style.width = `${right - left}px`;
    selectionBoxElement.style.height = `${bottom - top}px`;

    selectedPaths.clear();
    fileTableBody.querySelectorAll(".file-row").forEach(rowElement => {
      const rowRectangle = rowElement.getBoundingClientRect();
      const intersectsSelection = !(
        rowRectangle.right < left ||
        rowRectangle.left > right ||
        rowRectangle.bottom < top ||
        rowRectangle.top > bottom
      );
      if (intersectsSelection) {
        selectedPaths.add(rowElement.dataset.path);
      }
    });
    syncSelectionView();
  });

  workspace.addEventListener("pointerup", pointerEvent => {
    if (!selectionBoxElement) return;

    selectionBoxElement.remove();
    selectionBoxElement = null;
    selectionStartPoint = null;
    try {
      workspace.releasePointerCapture(pointerEvent.pointerId);
    } catch (ignoredError) {}
  });

  loadDirectory("");
})();
</script>
</body>
</html>
'''

app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
