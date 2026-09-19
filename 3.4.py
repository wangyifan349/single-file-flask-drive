"""
publish v3.4 - 一个使用 FastAPI 编写的简单多用户 Web 文件浏览器。
支持登录注册、独立用户目录、文件与目录分享、上传下载、移动、7Z 打包下载，
以及文本、图片、视频和音频在线查看。分享页面为只读。SQLite3 为 Python 标准库，无需额外安装。
依赖：pip install fastapi uvicorn python-multipart py7zr charset-normalizer
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import lzma
import mimetypes
import os
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from charset_normalizer import from_bytes

try:
    import py7zr
    from py7zr import FILTER_LZMA2
except ImportError:
    py7zr = None
    FILTER_LZMA2 = None

APP_TITLE = "FastAPI File Manager publish v3.4"                                  # 页面和 API 标题
DATA_ROOT = Path(os.environ.get("FILE_MANAGER_ROOT", "./storage")).resolve()      # v3 数据根目录
USERS_STORAGE_ROOT = DATA_ROOT / "users"                                           # 每个用户使用独立子目录
DEFAULT_TEMP_ROOT = Path(__file__).resolve().parent / "temp"                       # 默认 7Z 临时目录
TEMP_ROOT = Path(os.environ.get("FILE_MANAGER_TEMP", str(DEFAULT_TEMP_ROOT))).resolve()  # 7Z 临时文件目录
DEFAULT_DATABASE_PATH = Path(__file__).resolve().parent / "file_manager.db"        # 默认 SQLite3 数据库
DATABASE_PATH = Path(os.environ.get("FILE_MANAGER_DB", str(DEFAULT_DATABASE_PATH))).resolve()  # 用户数据库
DEFAULT_SHARE_DATABASE_PATH = Path(__file__).resolve().parent / "share.db"                 # 默认分享数据库
SHARE_DATABASE_PATH = Path(os.environ.get("FILE_MANAGER_SHARE_DB", str(DEFAULT_SHARE_DATABASE_PATH))).resolve()  # 分享数据库
SESSION_COOKIE_NAME = "file_manager_session"                                      # 登录会话 Cookie
SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60                                        # 默认登录有效期 30 天
PASSWORD_ITERATIONS = 310_000                                                       # PBKDF2 迭代次数
TEMP_DELETE_DELAY_SECONDS = 10 * 60                                                 # 下载完成 10 分钟后删除
TEMP_STALE_SECONDS = 24 * 60 * 60                                                   # 异常残留超过 24 小时清理
CURRENT_USER_ID: ContextVar[int | None] = ContextVar("current_user_id", default=None)  # 当前请求用户
DATA_ROOT.mkdir(parents=True, exist_ok=True)
USERS_STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
TEMP_ROOT.mkdir(parents=True, exist_ok=True)
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
SHARE_DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(title=APP_TITLE)

TEXT_EDITOR_MAX_BYTES = 10 * 1024 * 1024                                     # 在线文本编辑最大 10 MB
ENCODING_DETECT_BYTES = 1024 * 1024                                          # 编码检测最多读取前 1 MB
TEXT_EXTENSIONS = {                                                          # 可在线编辑的文本扩展名
    ".txt", ".md", ".markdown", ".log", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties", ".env",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".htm",
    ".css", ".scss", ".less", ".xml", ".svg", ".sql", ".sh", ".bash", ".zsh",
    ".bat", ".cmd", ".ps1", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go",
    ".rs", ".php", ".rb", ".vue", ".svelte"
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".avif"}  # 在线图片
VIDEO_EXTENSIONS = {".mp4", ".webm", ".m4v", ".mov", ".ogv", ".mkv"}                       # 在线视频
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".opus"}   # 在线音频

MEDIA_ARCHIVE_IMAGE_EXTENSIONS = {                                            # 归档：图片扩展名，仅按后缀分类
    ".jpg", ".jpeg", ".jfif", ".png", ".gif", ".webp", ".bmp", ".dib",
    ".ico", ".avif", ".tif", ".tiff", ".heic", ".heif", ".svg"
}
MEDIA_ARCHIVE_VIDEO_EXTENSIONS = {                                            # 归档：视频扩展名，仅按后缀分类
    ".mp4", ".webm", ".m4v", ".mov", ".ogv", ".mkv", ".avi", ".wmv",
    ".flv", ".mpg", ".mpeg", ".3gp", ".3g2", ".mts", ".m2ts", ".ts",
    ".vob", ".asf", ".rm", ".rmvb"
}
MEDIA_ARCHIVE_AUDIO_EXTENSIONS = {                                            # 归档：音频扩展名，仅按后缀分类
    ".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".opus",
    ".wma", ".aiff", ".aif", ".ape", ".amr", ".mka", ".ac3", ".dts",
    ".mid", ".midi"
}
MEDIA_ARCHIVE_FOLDER_NAMES = {"image": "图片", "video": "视频", "audio": "音频"}

def get_database_connection() -> sqlite3.Connection:
    """创建独立 SQLite3 连接；每个请求/操作使用自己的连接。"""
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection

def initialize_database() -> None:
    """初始化用户和登录会话表。"""
    with get_database_connection() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at)")

def get_share_database_connection() -> sqlite3.Connection:
    """创建 share.db 的独立 SQLite3 连接。"""
    connection = sqlite3.connect(SHARE_DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection

def initialize_share_database() -> None:
    """初始化分享记录表；share.db 与用户数据库保持分离。"""
    with get_share_database_connection() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS shares (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                relative_path TEXT NOT NULL,
                item_type TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_shares_user_id ON shares(user_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_shares_token_hash ON shares(token_hash)")

def share_token_hash(share_token: str) -> str:
    """share.db 只保存分享令牌的 SHA-256，不保存公开 URL 中的原始令牌。"""
    return hashlib.sha256(share_token.encode("utf-8")).hexdigest()

def get_user_storage_root_by_id(user_id: int) -> Path:
    """根据用户 ID 返回其私人文件根目录，不依赖登录请求上下文。"""
    user_root = (USERS_STORAGE_ROOT / str(user_id)).resolve()
    user_root.mkdir(parents=True, exist_ok=True)
    return user_root

def create_share_record(user_id: int, source_relative_path: str, item_type: str) -> str:
    """创建公开分享令牌并把哈希写入 share.db。"""
    share_token = secrets.token_urlsafe(32)
    token_hash = share_token_hash(share_token)
    current_time = int(time.time())
    with get_share_database_connection() as connection:
        connection.execute(
            "INSERT INTO shares(token_hash, user_id, relative_path, item_type, created_at) VALUES (?, ?, ?, ?, ?)",
            (token_hash, user_id, source_relative_path, item_type, current_time),
        )
    return share_token

def get_share_record(share_token: str) -> dict:
    """根据公开令牌读取分享记录；管理页可使用记录哈希生成可复制的等效分享链接。"""
    is_hash_token = len(share_token) == 64 and all(character in "0123456789abcdefABCDEF" for character in share_token)
    token_hash = share_token.lower() if is_hash_token else share_token_hash(share_token)
    with get_share_database_connection() as connection:
        row = connection.execute(
            "SELECT id, token_hash, user_id, relative_path, item_type, created_at FROM shares WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="分享不存在")
    return {
        "id": int(row["id"]),
        "token_hash": str(row["token_hash"]),
        "user_id": int(row["user_id"]),
        "relative_path": str(row["relative_path"]),
        "item_type": str(row["item_type"]),
        "created_at": int(row["created_at"]),
    }

def list_user_share_records(user_id: int, base_url: str) -> list[dict]:
    """列出当前用户的全部分享，供独立分享管理页使用。"""
    with get_share_database_connection() as connection:
        rows = connection.execute(
            "SELECT id, token_hash, relative_path, item_type, created_at FROM shares WHERE user_id = ? ORDER BY created_at DESC, id DESC",
            (user_id,),
        ).fetchall()
    normalized_base_url = base_url.rstrip("/")
    result: list[dict] = []
    user_root = get_user_storage_root_by_id(user_id)
    for row in rows:
        relative_path_value = str(row["relative_path"])
        source_path = (user_root / relative_path_value).resolve()
        try:
            source_path.relative_to(user_root)
            source_exists = source_path.exists()
        except ValueError:
            source_exists = False
        result.append({
            "id": int(row["id"]),
            "path": relative_path_value,
            "type": str(row["item_type"]),
            "created_at": int(row["created_at"]),
            "created_text": datetime.fromtimestamp(int(row["created_at"])).strftime("%Y-%m-%d %H:%M:%S"),
            "exists": source_exists,
            "url": normalized_base_url + "/s/" + str(row["token_hash"]),
        })
    return result

def cancel_user_share_record(user_id: int, share_id: int) -> None:
    """仅允许分享创建者取消自己的分享。"""
    with get_share_database_connection() as connection:
        cursor = connection.execute("DELETE FROM shares WHERE id = ? AND user_id = ?", (share_id, user_id))
        if cursor.rowcount != 1:
            raise HTTPException(status_code=404, detail="分享不存在")

def get_share_root_path(share_record: dict) -> Path:
    """解析分享根路径，并阻止 share.db 中的路径越过用户私人目录。"""
    user_root = get_user_storage_root_by_id(share_record["user_id"])
    normalized_path = share_record["relative_path"].replace("\\", "/").strip("/")
    root_path = (user_root / normalized_path).resolve()
    try:
        root_path.relative_to(user_root)
    except ValueError:
        raise HTTPException(status_code=403, detail="分享路径无效")
    if not root_path.exists():
        raise HTTPException(status_code=404, detail="分享内容已不存在")
    return root_path

def safe_shared_path(share_record: dict, relative_path: str = "") -> Path:
    """把分享页的虚拟相对路径限制在被分享的文件或目录内部。"""
    share_root = get_share_root_path(share_record)
    normalized_path = (relative_path or "").replace("\\", "/").strip("/")
    if share_root.is_file():
        if normalized_path in {"", share_root.name}:
            return share_root
        raise HTTPException(status_code=404, detail="分享路径不存在")
    absolute_path = (share_root / normalized_path).resolve()
    try:
        absolute_path.relative_to(share_root)
    except ValueError:
        raise HTTPException(status_code=403, detail="不能访问分享目录之外的内容")
    return absolute_path

def shared_relative_path(share_record: dict, absolute_path: Path) -> str:
    """把分享内容的真实路径转换成只暴露分享根以下层级的虚拟路径。"""
    share_root = get_share_root_path(share_record)
    resolved_path = absolute_path.resolve()
    if share_root.is_file():
        return share_root.name
    if resolved_path == share_root:
        return ""
    return resolved_path.relative_to(share_root).as_posix()

def get_shared_item_info(share_record: dict, item_path: Path) -> dict:
    """生成公开分享页使用的只读文件信息。"""
    item_stat = item_path.stat()
    item_type = "folder" if item_path.is_dir() else "file"
    item_size = None if item_path.is_dir() else item_stat.st_size
    size_text = "文件夹" if item_path.is_dir() else format_file_size(item_stat.st_size)
    return {
        "name": item_path.name,
        "path": shared_relative_path(share_record, item_path),
        "type": item_type,
        "size": item_size,
        "size_text": size_text,
        "mtime": item_stat.st_mtime,
        "time_text": datetime.fromtimestamp(item_stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "preview_type": get_preview_type(item_path),
    }

def encode_password_part(raw_bytes: bytes) -> str:
    """把密码哈希和 salt 编码为可存入 SQLite TEXT 的字符串。"""
    return base64.urlsafe_b64encode(raw_bytes).decode("ascii")

def decode_password_part(encoded_text: str) -> bytes:
    """把 SQLite 中保存的 Base64 文本恢复成字节。"""
    return base64.urlsafe_b64decode(encoded_text.encode("ascii"))

def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """使用 PBKDF2-HMAC-SHA256 哈希密码；不对密码字符串设置长度上限。"""
    password_salt = salt or secrets.token_bytes(32)
    password_bytes = password.encode("utf-8")
    password_hash = hashlib.pbkdf2_hmac("sha256", password_bytes, password_salt, PASSWORD_ITERATIONS)
    return encode_password_part(password_salt), encode_password_part(password_hash)

def verify_password(password: str, encoded_salt: str, encoded_hash: str) -> bool:
    """使用恒定时间比较验证密码。"""
    password_salt = decode_password_part(encoded_salt)
    expected_hash = decode_password_part(encoded_hash)
    password_bytes = password.encode("utf-8")
    actual_hash = hashlib.pbkdf2_hmac("sha256", password_bytes, password_salt, PASSWORD_ITERATIONS)
    return hmac.compare_digest(actual_hash, expected_hash)

def session_token_hash(session_token: str) -> str:
    """数据库只保存会话令牌的 SHA-256，不保存浏览器里的原始令牌。"""
    return hashlib.sha256(session_token.encode("utf-8")).hexdigest()

def create_session(user_id: int) -> str:
    """创建一个随机登录会话并返回原始 Cookie 令牌。"""
    current_time = int(time.time())
    expires_at = current_time + SESSION_MAX_AGE_SECONDS
    session_token = secrets.token_urlsafe(48)
    token_hash = session_token_hash(session_token)
    with get_database_connection() as connection:
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (current_time,))
        connection.execute(
            "INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (token_hash, user_id, expires_at, current_time),
        )
    return session_token

def get_authenticated_user(request: Request) -> dict | None:
    """从 Cookie 中读取当前登录用户。"""
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_token:
        return None
    token_hash = session_token_hash(session_token)
    current_time = int(time.time())
    with get_database_connection() as connection:
        row = connection.execute(
            """
            SELECT users.id, users.username
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ? AND sessions.expires_at > ?
            """,
            (token_hash, current_time),
        ).fetchone()
    if row is None:
        return None
    return {"id": int(row["id"]), "username": str(row["username"])}

def get_current_user_id() -> int:
    """取得当前请求的用户 ID。"""
    user_id = CURRENT_USER_ID.get()
    if user_id is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return user_id

def get_current_storage_root() -> Path:
    """返回当前用户的独立文件根目录。"""
    user_id = get_current_user_id()
    user_root = (USERS_STORAGE_ROOT / str(user_id)).resolve()
    user_root.mkdir(parents=True, exist_ok=True)
    return user_root

def safe_path(relative_path: str = "") -> Path:
    """把当前用户的前端相对路径转换成安全绝对路径，并阻止越过用户根目录。"""
    user_root = get_current_storage_root()
    normalized_path = (relative_path or "").replace("\\", "/").strip("/")
    absolute_path = (user_root / normalized_path).resolve()
    try:
        absolute_path.relative_to(user_root)
    except ValueError:
        raise HTTPException(status_code=403, detail="非法路径")
    return absolute_path

def relative_path(absolute_path: Path) -> str:
    """把当前用户目录中的绝对路径转换成前端相对路径。"""
    user_root = get_current_storage_root()
    resolved_path = absolute_path.resolve()
    if resolved_path == user_root:
        return ""
    return resolved_path.relative_to(user_root).as_posix()

def format_file_size(file_size: int) -> str:
    """把字节数转换成易读大小。"""
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(file_size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{value:.0f} {unit}"
            return f"{value:.1f} {unit}"
        value = value / 1024
    return f"{file_size} B"

def get_preview_type(file_path: Path) -> str:
    """判断文件是否支持文本编辑、图片、视频或音频预览。"""
    if not file_path.is_file():
        return "none"
    extension = file_path.suffix.lower()
    if extension in TEXT_EXTENSIONS:
        return "text"
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    guessed_type = mimetypes.guess_type(file_path.name)[0] or ""
    if guessed_type.startswith("text/"):
        return "text"
    if guessed_type.startswith("image/") and extension != ".svg":
        return "image"
    if guessed_type.startswith("video/"):
        return "video"
    if guessed_type.startswith("audio/"):
        return "audio"
    return "none"

def detect_text_encoding(raw_bytes: bytes) -> str:
    """完全交给 charset-normalizer 检测文本编码，不再维护手写猜测规则。"""
    detection_sample = raw_bytes[:ENCODING_DETECT_BYTES]                                # 限制检测样本，避免大文本浪费内存
    detection_result = from_bytes(detection_sample).best()                              # charset-normalizer 自动识别
    if detection_result is None or not detection_result.encoding:
        raise HTTPException(status_code=400, detail="charset-normalizer 无法识别该文本编码")
    return detection_result.encoding

def read_text_file(file_path: Path) -> tuple[str, str]:
    """读取文本文件，编码完全由 charset-normalizer 自动检测。"""
    file_size = file_path.stat().st_size
    if file_size > TEXT_EDITOR_MAX_BYTES:
        raise HTTPException(status_code=413, detail="在线编辑仅支持 10 MB 以内的文本文件")
    raw_bytes = file_path.read_bytes()
    encoding = detect_text_encoding(raw_bytes)
    try:
        content = raw_bytes.decode(encoding)
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail=f"charset-normalizer 检测为 {encoding}，但文件解码失败")
    return content, encoding

def parse_range_header(range_header: str, file_size: int) -> tuple[int, int]:
    """解析单段 HTTP Range，供视频和音频拖动进度使用。"""
    if not range_header.startswith("bytes=") or "," in range_header:
        raise HTTPException(status_code=416, detail="不支持的 Range 请求")
    range_value = range_header[6:].strip()
    start_text, separator, end_text = range_value.partition("-")
    if not separator:
        raise HTTPException(status_code=416, detail="Range 格式无效")
    try:
        if start_text:
            start_byte = int(start_text)
            end_byte = int(end_text) if end_text else file_size - 1
        else:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise ValueError
            start_byte = max(0, file_size - suffix_length)
            end_byte = file_size - 1
    except ValueError:
        raise HTTPException(status_code=416, detail="Range 格式无效")
    if start_byte < 0 or start_byte >= file_size or end_byte < start_byte:
        raise HTTPException(status_code=416, detail="Range 超出文件范围")
    end_byte = min(end_byte, file_size - 1)
    return start_byte, end_byte

def stream_file_range(file_path: Path, start_byte: int, end_byte: int):
    """按块输出指定字节范围，避免一次把大媒体文件读入内存。"""
    remaining_bytes = end_byte - start_byte + 1
    with file_path.open("rb") as input_file:
        input_file.seek(start_byte)
        while remaining_bytes > 0:
            chunk_size = min(1024 * 1024, remaining_bytes)
            chunk = input_file.read(chunk_size)
            if not chunk:
                break
            remaining_bytes = remaining_bytes - len(chunk)
            yield chunk

def get_item_info(item_path: Path) -> dict:
    """读取单个文件或文件夹的信息。"""
    item_stat = item_path.stat()
    is_folder = item_path.is_dir()
    item_type = "file"
    item_size = item_stat.st_size
    size_text = format_file_size(item_stat.st_size)
    if is_folder:
        item_type = "folder"
        item_size = None
        size_text = "文件夹"
    return {
        "name": item_path.name,
        "path": relative_path(item_path),
        "type": item_type,
        "size": item_size,
        "size_text": size_text,
        "mtime": item_stat.st_mtime,
        "time_text": datetime.fromtimestamp(item_stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "preview_type": get_preview_type(item_path),
    }

DEDUP_HASH_CHUNK_BYTES = 4 * 1024 * 1024                                      # 去重哈希分块大小 4 MB

def calculate_file_sha256(file_path: Path) -> str:
    """流式计算完整文件 SHA-256，避免大文件一次性读入内存。"""
    digest = hashlib.sha256()
    with file_path.open("rb") as input_file:
        while True:
            chunk = input_file.read(DEDUP_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()

def deduplicate_folder_tree(folder_path: Path) -> dict:
    """递归按完整 SHA-256 删除重复文件，并在完成后删除所有空目录。"""
    file_paths: list[Path] = []
    skipped_symlinks = 0
    for current_root, directory_names, file_names in os.walk(folder_path, topdown=True, followlinks=False):
        current_root_path = Path(current_root)
        kept_directory_names: list[str] = []
        for directory_name in sorted(directory_names, key=lambda value: (value.casefold(), value)):
            directory_path = current_root_path / directory_name
            if directory_path.is_symlink():
                skipped_symlinks = skipped_symlinks + 1
                continue
            kept_directory_names.append(directory_name)
        directory_names[:] = kept_directory_names
        for file_name in sorted(file_names, key=lambda value: (value.casefold(), value)):
            file_path = current_root_path / file_name
            if file_path.is_symlink():
                skipped_symlinks = skipped_symlinks + 1
                continue
            if file_path.is_file():
                file_paths.append(file_path)

    file_paths.sort(key=lambda path: (path.relative_to(folder_path).as_posix().casefold(), path.relative_to(folder_path).as_posix()))
    seen_hashes: dict[tuple[int, str], Path] = {}
    scanned_files = 0
    duplicate_files_deleted = 0
    duplicate_bytes_deleted = 0

    for file_path in file_paths:
        try:
            file_size_before = file_path.stat().st_size
            file_hash = calculate_file_sha256(file_path)
            file_size_after = file_path.stat().st_size
        except FileNotFoundError:
            continue
        if file_size_before != file_size_after:
            raise HTTPException(status_code=409, detail=f"扫描期间文件发生变化，请重试: {file_path.name}")
        scanned_files = scanned_files + 1
        duplicate_key = (file_size_after, file_hash)
        kept_path = seen_hashes.get(duplicate_key)
        if kept_path is None or not kept_path.exists():
            seen_hashes[duplicate_key] = file_path
            continue
        try:
            file_path.unlink()
        except FileNotFoundError:
            continue
        duplicate_files_deleted = duplicate_files_deleted + 1
        duplicate_bytes_deleted = duplicate_bytes_deleted + file_size_after

    empty_folders_deleted = 0
    for current_root, directory_names, file_names in os.walk(folder_path, topdown=False, followlinks=False):
        current_root_path = Path(current_root)
        if current_root_path == folder_path or current_root_path.is_symlink():
            continue
        try:
            next(current_root_path.iterdir())
        except StopIteration:
            current_root_path.rmdir()
            empty_folders_deleted = empty_folders_deleted + 1
        except FileNotFoundError:
            continue

    return {
        "ok": True,
        "scanned_files": scanned_files,
        "duplicate_files_deleted": duplicate_files_deleted,
        "duplicate_bytes_deleted": duplicate_bytes_deleted,
        "empty_folders_deleted": empty_folders_deleted,
        "skipped_symlinks": skipped_symlinks,
    }

def get_unique_destination(destination_path: Path) -> Path:
    """上传重名文件时自动生成不冲突的目标名称。"""
    if not destination_path.exists():
        return destination_path
    file_stem = destination_path.stem
    file_suffix = destination_path.suffix
    parent_folder = destination_path.parent
    counter = 1
    while True:
        candidate_path = parent_folder / f"{file_stem} ({counter}){file_suffix}"
        if not candidate_path.exists():
            return candidate_path
        counter = counter + 1

def get_media_archive_category(file_path: Path) -> str | None:
    """仅根据文件扩展名判断归档类别。"""
    extension = file_path.suffix.lower()
    if extension in MEDIA_ARCHIVE_IMAGE_EXTENSIONS:
        return "image"
    if extension in MEDIA_ARCHIVE_VIDEO_EXTENSIONS:
        return "video"
    if extension in MEDIA_ARCHIVE_AUDIO_EXTENSIONS:
        return "audio"
    return None

def archive_media_folder_tree(folder_path: Path) -> dict:
    """先完整扫描，再把全部图片、视频、音频移动到所选文件夹根目录的分类目录。"""
    planned_files: list[tuple[Path, str]] = []
    scanned_files = 0
    skipped_symlinks = 0

    # 必须先完成扫描，再创建分类目录和开始移动，避免扫描过程被自身修改干扰。
    for current_root, directory_names, file_names in os.walk(folder_path, topdown=True, followlinks=False):
        current_root_path = Path(current_root)
        kept_directory_names: list[str] = []
        for directory_name in sorted(directory_names, key=lambda value: (value.casefold(), value)):
            directory_path = current_root_path / directory_name
            if directory_path.is_symlink():
                skipped_symlinks = skipped_symlinks + 1
                continue
            kept_directory_names.append(directory_name)
        directory_names[:] = kept_directory_names
        for file_name in sorted(file_names, key=lambda value: (value.casefold(), value)):
            file_path = current_root_path / file_name
            if file_path.is_symlink():
                skipped_symlinks = skipped_symlinks + 1
                continue
            if not file_path.is_file():
                continue
            scanned_files = scanned_files + 1
            category = get_media_archive_category(file_path)
            if category is not None:
                planned_files.append((file_path, category))

    planned_files.sort(
        key=lambda entry: (
            entry[0].relative_to(folder_path).as_posix().casefold(),
            entry[0].relative_to(folder_path).as_posix(),
        )
    )

    # 在任何移动发生前检查三个保留名称，避免进行到一半才因目录名冲突失败。
    destination_folders: dict[str, Path] = {}
    for category, folder_name in MEDIA_ARCHIVE_FOLDER_NAMES.items():
        destination_folder = folder_path / folder_name
        if destination_folder.exists() and not destination_folder.is_dir():
            raise HTTPException(status_code=409, detail=f"无法归档：{folder_name} 已存在但不是文件夹")
        destination_folders[category] = destination_folder

    for destination_folder in destination_folders.values():
        destination_folder.mkdir(exist_ok=True)

    moved_counts = {"image": 0, "video": 0, "audio": 0}
    renamed_files = 0

    for source_path, category in planned_files:
        if not source_path.exists() or not source_path.is_file() or source_path.is_symlink():
            continue
        destination_folder = destination_folders[category]
        direct_destination = destination_folder / source_path.name

        # 已经位于正确分类目录根层且名称不变时无需移动。
        if source_path == direct_destination:
            continue

        destination_path = get_unique_destination(direct_destination)
        if destination_path.name != source_path.name:
            renamed_files = renamed_files + 1
        shutil.move(str(source_path), str(destination_path))
        moved_counts[category] = moved_counts[category] + 1

    return {
        "ok": True,
        "scanned_files": scanned_files,
        "matched_files": len(planned_files),
        "moved_files": moved_counts["image"] + moved_counts["video"] + moved_counts["audio"],
        "image_files_moved": moved_counts["image"],
        "video_files_moved": moved_counts["video"],
        "audio_files_moved": moved_counts["audio"],
        "renamed_files": renamed_files,
        "skipped_symlinks": skipped_symlinks,
    }

def get_existing_paths(raw_paths: list[str]) -> list[Path]:
    """验证下载目标并去除重复路径。"""
    valid_paths: list[Path] = []
    seen_paths: set[Path] = set()
    for raw_path in raw_paths:
        item_path = safe_path(raw_path)
        if not item_path.exists():
            raise HTTPException(status_code=404, detail=f"不存在: {raw_path}")
        if item_path in seen_paths:
            continue
        seen_paths.add(item_path)
        valid_paths.append(item_path)
    return valid_paths

def cleanup_stale_temporary_archives() -> None:
    """清理异常中断后遗留的过期 7Z 临时文件。"""
    current_time = time.time()
    for temporary_path in TEMP_ROOT.glob("file-manager-*.7z"):
        try:
            file_age = current_time - temporary_path.stat().st_mtime
            if file_age >= TEMP_STALE_SECONDS:
                temporary_path.unlink(missing_ok=True)
        except OSError:
            continue

def delete_temporary_archive(file_path: str) -> None:
    """删除一个临时 7Z 文件。"""
    Path(file_path).unlink(missing_ok=True)

def schedule_temporary_archive_deletion(file_path: str) -> None:
    """文件发送完成后启动延迟删除计时器，避免立即删除影响浏览器收尾。"""
    cleanup_timer = threading.Timer(TEMP_DELETE_DELAY_SECONDS, delete_temporary_archive, args=(file_path,))
    cleanup_timer.daemon = True
    cleanup_timer.start()

def create_7z_archive(source_paths: list[Path]) -> Path:
    """在专用 temp 目录使用 LZMA2 最高压缩级别创建 7Z。"""
    if py7zr is None or FILTER_LZMA2 is None:
        raise HTTPException(status_code=503, detail="7Z 功能需要安装 py7zr：pip install py7zr")
    cleanup_stale_temporary_archives()
    user_id = get_current_user_id()
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f"file-manager-u{user_id}-", suffix=".7z", dir=TEMP_ROOT)
    os.close(file_descriptor)
    archive_path = Path(temporary_name)
    compression_filters = [{"id": FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}]
    try:
        with py7zr.SevenZipFile(archive_path, mode="w", filters=compression_filters) as seven_zip_file:
            for source_path in source_paths:
                if source_path.is_dir():
                    seven_zip_file.writeall(source_path, arcname=source_path.name)
                else:
                    seven_zip_file.write(source_path, arcname=source_path.name)
        return archive_path
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise

def get_temporary_archive(token: str) -> Path:
    """根据临时令牌定位 7Z 文件，并阻止路径穿越。"""
    clean_token = Path(token).name
    expected_prefix = f"file-manager-u{get_current_user_id()}-"
    if clean_token != token or not clean_token.startswith(expected_prefix) or not clean_token.endswith(".7z"):
        raise HTTPException(status_code=400, detail="临时下载令牌无效")
    archive_path = (TEMP_ROOT / clean_token).resolve()
    try:
        archive_path.relative_to(TEMP_ROOT)
    except ValueError:
        raise HTTPException(status_code=403, detail="临时下载路径无效")
    if not archive_path.exists() or not archive_path.is_file():
        raise HTTPException(status_code=404, detail="临时压缩包不存在或已清理")
    return archive_path

def prepare_7z_archive(paths: list[str]) -> dict:
    """先在 temp 中完成压缩，只把很小的下载令牌返回给前端。"""
    source_paths = get_existing_paths(paths)
    archive_path = create_7z_archive(source_paths)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return {"ok": True, "token": archive_path.name, "download_name": f"download-{timestamp}.7z"}

def build_7z_download_response(token: str, download_name: str, background_tasks: BackgroundTasks) -> FileResponse:
    """发送已生成的 7Z；响应完成后再启动延迟清理。"""
    archive_path = get_temporary_archive(token)
    safe_download_name = Path(download_name or "download.7z").name
    if not safe_download_name.lower().endswith(".7z"):
        safe_download_name = safe_download_name + ".7z"
    background_tasks.add_task(schedule_temporary_archive_deletion, str(archive_path))
    return FileResponse(archive_path, filename=safe_download_name, media_type="application/x-7z-compressed")


def create_shared_7z_archive(source_paths: list[Path], share_record: dict) -> Path:
    """为公开分享在 temp 中创建最高压缩级别 7Z。"""
    if py7zr is None or FILTER_LZMA2 is None:
        raise HTTPException(status_code=503, detail="7Z 功能需要安装 py7zr：pip install py7zr")
    cleanup_stale_temporary_archives()
    namespace = share_record["token_hash"][:16]
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f"file-manager-s{namespace}-", suffix=".7z", dir=TEMP_ROOT)
    os.close(file_descriptor)
    archive_path = Path(temporary_name)
    compression_filters = [{"id": FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}]
    try:
        with py7zr.SevenZipFile(archive_path, mode="w", filters=compression_filters) as seven_zip_file:
            for source_path in source_paths:
                if source_path.is_dir():
                    seven_zip_file.writeall(source_path, arcname=source_path.name)
                else:
                    seven_zip_file.write(source_path, arcname=source_path.name)
        return archive_path
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise

def get_shared_temporary_archive(share_record: dict, token: str) -> Path:
    """校验公开分享产生的 7Z 临时文件令牌。"""
    clean_token = Path(token).name
    expected_prefix = f"file-manager-s{share_record['token_hash'][:16]}-"
    if clean_token != token or not clean_token.startswith(expected_prefix) or not clean_token.endswith(".7z"):
        raise HTTPException(status_code=400, detail="临时下载令牌无效")
    archive_path = (TEMP_ROOT / clean_token).resolve()
    try:
        archive_path.relative_to(TEMP_ROOT)
    except ValueError:
        raise HTTPException(status_code=403, detail="临时下载路径无效")
    if not archive_path.exists() or not archive_path.is_file():
        raise HTTPException(status_code=404, detail="临时压缩包不存在或已清理")
    return archive_path

def prepare_shared_7z_archive(share_record: dict, paths: list[str]) -> dict:
    """验证分享范围内的多选路径并生成公开下载用 7Z。"""
    source_paths: list[Path] = []
    seen_paths: set[Path] = set()
    for raw_path in paths:
        source_path = safe_shared_path(share_record, raw_path)
        if not source_path.exists():
            raise HTTPException(status_code=404, detail=f"不存在: {raw_path}")
        if source_path in seen_paths:
            continue
        seen_paths.add(source_path)
        source_paths.append(source_path)
    if not source_paths:
        raise HTTPException(status_code=400, detail="没有可下载的内容")
    archive_path = create_shared_7z_archive(source_paths, share_record)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return {"ok": True, "token": archive_path.name, "download_name": f"share-{timestamp}.7z"}

def build_shared_7z_download_response(share_record: dict, token: str, download_name: str, background_tasks: BackgroundTasks) -> FileResponse:
    """发送公开分享生成的临时 7Z，并在发送完成后延迟清理。"""
    archive_path = get_shared_temporary_archive(share_record, token)
    safe_download_name = Path(download_name or "share.7z").name
    if not safe_download_name.lower().endswith(".7z"):
        safe_download_name = safe_download_name + ".7z"
    background_tasks.add_task(schedule_temporary_archive_deletion, str(archive_path))
    return FileResponse(archive_path, filename=safe_download_name, media_type="application/x-7z-compressed")

cleanup_stale_temporary_archives()

class PathsPayload(BaseModel):
    paths: list[str] = Field(min_length=1)

class MovePayload(BaseModel):
    paths: list[str] = Field(min_length=1)
    destination: str = ""

class TextSavePayload(BaseModel):
    path: str
    content: str

class ShareCreatePayload(BaseModel):
    path: str


initialize_database()
initialize_share_database()

PUBLIC_PATHS = {"/login", "/register", "/api/auth/login", "/api/auth/register"}   # 无需登录的入口

@app.middleware("http")
async def authentication_middleware(request: Request, call_next):
    """统一保护主页面、预览页面和全部文件 API。"""
    request_path = request.url.path
    if request_path in PUBLIC_PATHS or request_path == "/share-viewer" or request_path.startswith("/s/") or request_path.startswith("/api/public-share/"):
        return await call_next(request)
    current_user = get_authenticated_user(request)
    if current_user is None:
        if request_path.startswith("/api/"):
            return JSONResponse({"detail": "请先登录"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    request.state.current_user = current_user
    context_token = CURRENT_USER_ID.set(current_user["id"])
    try:
        return await call_next(request)
    finally:
        CURRENT_USER_ID.reset(context_token)

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if get_authenticated_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    return render_auth_page("login")

@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    if get_authenticated_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    return render_auth_page("register")

@app.post("/api/auth/register")
def register_user(username: str = Form(...), password: str = Form(...)):
    """注册用户；用户名和密码不设置长度上限。"""
    if username == "":
        raise HTTPException(status_code=400, detail="用户名不能为空")
    if password == "":
        raise HTTPException(status_code=400, detail="密码不能为空")
    encoded_salt, encoded_hash = hash_password(password)
    current_time = int(time.time())
    try:
        with get_database_connection() as connection:
            cursor = connection.execute(
                "INSERT INTO users(username, password_salt, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (username, encoded_salt, encoded_hash, current_time),
            )
            user_id = int(cursor.lastrowid)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="用户名已存在")
    (USERS_STORAGE_ROOT / str(user_id)).mkdir(parents=True, exist_ok=True)
    session_token = create_session(user_id)
    response = JSONResponse({"ok": True, "username": username})
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )
    return response

@app.post("/api/auth/login")
def login_user(username: str = Form(...), password: str = Form(...)):
    """验证用户名和密码并创建登录会话。"""
    with get_database_connection() as connection:
        row = connection.execute(
            "SELECT id, username, password_salt, password_hash FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None or not verify_password(password, row["password_salt"], row["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    user_id = int(row["id"])
    session_token = create_session(user_id)
    response = JSONResponse({"ok": True, "username": str(row["username"])})
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )
    return response

@app.post("/api/auth/logout")
def logout_user(request: Request):
    """删除当前会话并清除 Cookie。"""
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if session_token:
        with get_database_connection() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (session_token_hash(session_token),))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response

@app.get("/api/me")
def get_current_user_info(request: Request) -> dict:
    current_user = request.state.current_user
    return {"id": current_user["id"], "username": current_user["username"]}


@app.post("/api/share")
def create_share(payload: ShareCreatePayload, request: Request) -> dict:
    """为当前用户的单个文件或目录创建公开只读分享链接。"""
    source_path = safe_path(payload.path)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="分享目标不存在")
    item_type = "folder" if source_path.is_dir() else "file"
    source_relative_path = relative_path(source_path)
    share_token = create_share_record(get_current_user_id(), source_relative_path, item_type)
    share_url = str(request.base_url).rstrip("/") + "/s/" + share_token
    return {"ok": True, "url": share_url, "token": share_token, "type": item_type}

@app.get("/shares", response_class=HTMLResponse)
def share_management_page() -> str:
    """当前登录用户的独立分享管理页面。"""
    return SHARE_MANAGEMENT_HTML

@app.get("/api/shares")
def get_my_shares(request: Request) -> dict:
    """返回当前用户创建的全部分享。"""
    shares = list_user_share_records(get_current_user_id(), str(request.base_url))
    return {"shares": shares}

@app.delete("/api/shares/{share_id}")
def cancel_my_share(share_id: int) -> dict:
    """取消当前用户指定的分享。"""
    cancel_user_share_record(get_current_user_id(), share_id)
    return {"ok": True}

@app.get("/s/{share_token}", response_class=HTMLResponse)
def shared_browser(share_token: str) -> str:
    """公开只读分享浏览页面。"""
    get_share_record(share_token)
    return SHARE_HTML

@app.get("/share-viewer", response_class=HTMLResponse)
def shared_viewer() -> str:
    """公开分享使用与私人空间相同的媒体查看器，但文本为只读。"""
    return VIEWER_HTML

@app.get("/api/public-share/{share_token}/meta")
def get_shared_metadata(share_token: str) -> dict:
    share_record = get_share_record(share_token)
    share_root = get_share_root_path(share_record)
    root_type = "folder" if share_root.is_dir() else "file"
    return {"name": share_root.name, "type": root_type}

@app.get("/api/public-share/{share_token}/list")
def list_shared_items(share_token: str, path: str = "") -> dict:
    share_record = get_share_record(share_token)
    share_root = get_share_root_path(share_record)
    root_name = share_root.name
    if share_root.is_file():
        if path not in {"", share_root.name}:
            raise HTTPException(status_code=404, detail="分享路径不存在")
        return {"path": "", "root_name": root_name, "root_type": "file", "items": [get_shared_item_info(share_record, share_root)]}
    folder_path = safe_shared_path(share_record, path)
    if not folder_path.exists():
        raise HTTPException(status_code=404, detail="目录不存在")
    if not folder_path.is_dir():
        raise HTTPException(status_code=400, detail="目标不是目录")
    items: list[dict] = []
    for item_path in folder_path.iterdir():
        try:
            resolved_item = item_path.resolve()
            resolved_item.relative_to(share_root)
        except (OSError, ValueError):
            continue
        items.append(get_shared_item_info(share_record, item_path))
    def item_sort_key(item: dict) -> tuple:
        folder_order = item["type"] != "folder"
        name_order = item["name"].casefold()
        return folder_order, name_order
    items.sort(key=item_sort_key)
    return {"path": shared_relative_path(share_record, folder_path), "root_name": root_name, "root_type": "folder", "items": items}

@app.get("/api/public-share/{share_token}/text")
def get_shared_text_file(share_token: str, path: str) -> dict:
    share_record = get_share_record(share_token)
    file_path = safe_shared_path(share_record, path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    if get_preview_type(file_path) != "text":
        raise HTTPException(status_code=400, detail="该文件不是支持的文本格式")
    content, detected_encoding = read_text_file(file_path)
    return {"path": shared_relative_path(share_record, file_path), "name": file_path.name, "content": content, "encoding": detected_encoding, "size": file_path.stat().st_size}

@app.get("/api/public-share/{share_token}/preview")
def preview_shared_file(share_token: str, path: str, request: Request):
    share_record = get_share_record(share_token)
    file_path = safe_shared_path(share_record, path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    preview_type = get_preview_type(file_path)
    if preview_type not in {"image", "video", "audio"}:
        raise HTTPException(status_code=400, detail="该文件不支持媒体预览")
    media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    common_headers = {"Accept-Ranges": "bytes", "X-Content-Type-Options": "nosniff"}
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(file_path, media_type=media_type, headers=common_headers)
    file_size = file_path.stat().st_size
    start_byte, end_byte = parse_range_header(range_header, file_size)
    content_length = end_byte - start_byte + 1
    range_headers = dict(common_headers)
    range_headers["Content-Range"] = f"bytes {start_byte}-{end_byte}/{file_size}"
    range_headers["Content-Length"] = str(content_length)
    return StreamingResponse(stream_file_range(file_path, start_byte, end_byte), status_code=206, media_type=media_type, headers=range_headers)

@app.get("/api/public-share/{share_token}/download")
def download_shared_file(share_token: str, path: str) -> FileResponse:
    share_record = get_share_record(share_token)
    file_path = safe_shared_path(share_record, path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(file_path, filename=file_path.name, media_type="application/octet-stream")

@app.post("/api/public-share/{share_token}/prepare-7z")
def prepare_shared_7z(share_token: str, payload: PathsPayload) -> dict:
    share_record = get_share_record(share_token)
    return prepare_shared_7z_archive(share_record, payload.paths)

@app.get("/api/public-share/{share_token}/download-7z")
def download_shared_7z(share_token: str, token: str, background_tasks: BackgroundTasks, name: str = "share.7z") -> FileResponse:
    share_record = get_share_record(share_token)
    return build_shared_7z_download_response(share_record, token, name, background_tasks)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML

@app.get("/viewer", response_class=HTMLResponse)
def viewer() -> str:
    return VIEWER_HTML

@app.get("/api/text")
def get_text_file(path: str) -> dict:
    file_path = safe_path(path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    if get_preview_type(file_path) != "text":
        raise HTTPException(status_code=400, detail="该文件不是支持的文本格式")
    content, detected_encoding = read_text_file(file_path)
    return {"path": relative_path(file_path), "name": file_path.name, "content": content, "encoding": detected_encoding, "size": file_path.stat().st_size}

@app.post("/api/text")
def save_text_file(payload: TextSavePayload) -> dict:
    file_path = safe_path(payload.path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    if get_preview_type(file_path) != "text":
        raise HTTPException(status_code=400, detail="该文件不是支持的文本格式")
    original_bytes = file_path.read_bytes()
    detected_encoding = detect_text_encoding(original_bytes)
    try:
        encoded_content = payload.content.encode(detected_encoding)
    except UnicodeEncodeError:
        raise HTTPException(status_code=400, detail=f"当前内容无法使用自动检测到的 {detected_encoding} 编码保存")
    if len(encoded_content) > TEXT_EDITOR_MAX_BYTES:
        raise HTTPException(status_code=413, detail="在线编辑仅支持 10 MB 以内的文本文件")
    temporary_path = file_path.with_name(file_path.name + ".editing.tmp")
    try:
        temporary_path.write_bytes(encoded_content)
        os.replace(temporary_path, file_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return {"ok": True, "encoding": detected_encoding, "size": len(encoded_content)}

@app.get("/api/preview")
def preview_file(path: str, request: Request):
    file_path = safe_path(path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    preview_type = get_preview_type(file_path)
    if preview_type not in {"image", "video", "audio"}:
        raise HTTPException(status_code=400, detail="该文件不支持媒体预览")
    media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    common_headers = {"Accept-Ranges": "bytes", "X-Content-Type-Options": "nosniff"}
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(file_path, media_type=media_type, headers=common_headers)
    file_size = file_path.stat().st_size
    start_byte, end_byte = parse_range_header(range_header, file_size)
    content_length = end_byte - start_byte + 1
    range_headers = dict(common_headers)
    range_headers["Content-Range"] = f"bytes {start_byte}-{end_byte}/{file_size}"
    range_headers["Content-Length"] = str(content_length)
    return StreamingResponse(stream_file_range(file_path, start_byte, end_byte), status_code=206, media_type=media_type, headers=range_headers)

@app.get("/api/list")
def list_items(path: str = "") -> dict:
    folder_path = safe_path(path)
    if not folder_path.exists():
        raise HTTPException(status_code=404, detail="目录不存在")
    if not folder_path.is_dir():
        raise HTTPException(status_code=400, detail="目标不是目录")
    items: list[dict] = []
    for item_path in folder_path.iterdir():
        items.append(get_item_info(item_path))
    def item_sort_key(item: dict) -> tuple:
        folder_order = item["type"] != "folder"
        name_order = item["name"].casefold()
        return folder_order, name_order
    items.sort(key=item_sort_key)
    return {"path": relative_path(folder_path), "items": items}

@app.post("/api/upload")
async def upload_files(path: str = Form(""), files: list[UploadFile] = File(...)) -> dict:
    target_folder = safe_path(path)
    if not target_folder.exists() or not target_folder.is_dir():
        raise HTTPException(status_code=404, detail="上传目标目录不存在")
    saved_paths: list[str] = []
    for upload_file in files:
        original_name = Path(upload_file.filename or "unnamed").name
        destination_path = get_unique_destination(target_folder / original_name)
        with destination_path.open("wb") as output_file:
            shutil.copyfileobj(upload_file.file, output_file, length=1024 * 1024)
        await upload_file.close()
        saved_paths.append(relative_path(destination_path))
    return {"ok": True, "saved": saved_paths}

@app.get("/api/download")
def download_file(path: str) -> FileResponse:
    file_path = safe_path(path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    if file_path.is_dir():
        raise HTTPException(status_code=400, detail="文件夹请使用 7Z 下载接口")
    return FileResponse(file_path, filename=file_path.name, media_type="application/octet-stream")

@app.post("/api/prepare-7z")
def prepare_7z(payload: PathsPayload) -> dict:
    return prepare_7z_archive(payload.paths)

@app.get("/api/download-7z")
def download_7z(token: str, background_tasks: BackgroundTasks, name: str = "download.7z") -> FileResponse:
    return build_7z_download_response(token, name, background_tasks)

@app.post("/api/mkdir")
def create_folder(path: str = Form(""), name: str = Form(...)) -> dict:
    parent_folder = safe_path(path)
    if not parent_folder.exists() or not parent_folder.is_dir():
        raise HTTPException(status_code=404, detail="父目录不存在")
    folder_name = Path(name).name.strip()
    if not folder_name or folder_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="文件夹名称无效")
    target_folder = parent_folder / folder_name
    if target_folder.exists():
        raise HTTPException(status_code=409, detail="同名文件或文件夹已存在")
    target_folder.mkdir()
    return {"ok": True, "path": relative_path(target_folder)}

@app.post("/api/rename")
def rename_item(path: str = Form(...), new_name: str = Form(...)) -> dict:
    source_path = safe_path(path)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="目标不存在")
    clean_name = Path(new_name).name.strip()
    if not clean_name or clean_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="名称无效")
    destination_path = source_path.parent / clean_name
    if destination_path.exists() and destination_path.resolve() != source_path.resolve():
        raise HTTPException(status_code=409, detail="同名文件或文件夹已存在")
    source_path.rename(destination_path)
    return {"ok": True, "path": relative_path(destination_path)}

@app.post("/api/move")
def move_items(payload: MovePayload) -> dict:
    destination_folder = safe_path(payload.destination)
    if not destination_folder.exists() or not destination_folder.is_dir():
        raise HTTPException(status_code=404, detail="目标文件夹不存在")

    source_paths: list[Path] = []
    for raw_path in payload.paths:
        source_path = safe_path(raw_path)
        if not source_path.exists():
            raise HTTPException(status_code=404, detail=f"不存在: {raw_path}")
        if source_path == get_current_storage_root():
            raise HTTPException(status_code=400, detail="不能移动根目录")
        source_paths.append(source_path)

    normalized_paths: list[Path] = []
    for source_path in source_paths:
        is_child_of_selected_folder = False
        for possible_parent in source_paths:
            if possible_parent == source_path:
                continue
            if possible_parent in source_path.parents:
                is_child_of_selected_folder = True
                break
        if not is_child_of_selected_folder:
            normalized_paths.append(source_path)

    moved_paths: list[str] = []
    for source_path in normalized_paths:
        if source_path.is_dir():
            try:
                destination_folder.relative_to(source_path)
                raise HTTPException(status_code=400, detail=f"不能把 {source_path.name} 移动到它自己的子目录")
            except ValueError:
                pass
        if source_path.parent == destination_folder:
            continue
        destination_path = destination_folder / source_path.name
        if destination_path.exists():
            raise HTTPException(status_code=409, detail=f"目标已存在同名项目: {source_path.name}")
        shutil.move(str(source_path), str(destination_path))
        moved_paths.append(relative_path(destination_path))
    return {"ok": True, "moved": moved_paths}

@app.post("/api/delete")
def delete_items(payload: PathsPayload) -> dict:
    for raw_path in payload.paths:
        item_path = safe_path(raw_path)
        if item_path == get_current_storage_root():
            raise HTTPException(status_code=400, detail="不能删除根目录")
        if not item_path.exists():
            continue
        if item_path.is_dir():
            shutil.rmtree(item_path)
        else:
            item_path.unlink()
    return {"ok": True}

@app.post("/api/deduplicate-folder")
def deduplicate_folder(payload: ShareCreatePayload) -> dict:
    folder_path = safe_path(payload.path)
    if not folder_path.exists():
        raise HTTPException(status_code=404, detail="文件夹不存在")
    if not folder_path.is_dir():
        raise HTTPException(status_code=400, detail="目标不是文件夹")
    return deduplicate_folder_tree(folder_path)

@app.post("/api/archive-media-folder")
def archive_media_folder(payload: ShareCreatePayload) -> dict:
    folder_path = safe_path(payload.path)
    if not folder_path.exists():
        raise HTTPException(status_code=404, detail="文件夹不存在")
    if not folder_path.is_dir():
        raise HTTPException(status_code=400, detail="目标不是文件夹")
    return archive_media_folder_tree(folder_path)

AUTH_HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>__PAGE_TITLE__</title>
<style>
:root{--accent:#ff3b00;--gold:#ffcc00;--text:#ff7600;--muted:#cc6600;--page:#090000;--danger:#ff1800}
*{box-sizing:border-box;accent-color:var(--accent)}
html,body{width:100%;min-height:100%;margin:0;background:var(--page);color:var(--text);font-family:Inter,system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color-scheme:dark}
body{min-height:100dvh;overflow:auto}
#authLayout{min-height:100dvh;display:flex;align-items:center;padding:clamp(28px,7vh,76px) clamp(22px,7vw,96px)}
#authContent{width:min(520px,100%)}
#pageTitle{font-size:clamp(26px,3vw,34px);line-height:1.15;font-weight:750;color:var(--gold);margin:0 0 clamp(28px,6vh,52px)}
.field-label{display:block;font-size:12px;color:var(--muted);margin:15px 0 6px}
.auth-input{display:block;width:100%;height:44px;border:0;border-bottom:1px solid #661900;border-radius:0;outline:0;padding:0 2px;background:transparent;color:var(--text);font-size:14px}
.auth-input:focus{border-bottom-color:var(--gold);box-shadow:0 1px 0 var(--gold)}
.auth-input::selection{background:var(--gold);color:#000}
#submitButton{min-width:160px;height:39px;border:0;border-radius:7px;margin-top:24px;padding:0 24px;background:var(--accent);color:#000;font-weight:800;cursor:pointer}
#submitButton:hover{background:var(--gold)}
#submitButton:disabled{opacity:.55;cursor:default}
#message{min-height:20px;margin-top:12px;font-size:12px;color:var(--danger)}
#authFooter{margin-top:20px;font-size:12px}
#switchLink{color:var(--gold);text-decoration:none}
#switchLink:hover{text-decoration:underline}
@media(max-width:600px){#authLayout{align-items:flex-start;padding-top:16vh}#authContent{width:100%}#submitButton{width:100%}}
</style>
</head>
<body>
<main id="authLayout">
<section id="authContent">
<h1 id="pageTitle">__PAGE_TITLE__</h1>
<form id="authForm">
<label class="field-label" for="usernameInput">用户名</label>
<input id="usernameInput" class="auth-input" name="username" autocomplete="username" required autofocus>
<label class="field-label" for="passwordInput">密码</label>
<input id="passwordInput" class="auth-input" name="password" type="password" autocomplete="__PASSWORD_AUTOCOMPLETE__" required>
<button id="submitButton" type="submit">__SUBMIT_TEXT__</button>
<div id="message"></div>
</form>
<div id="authFooter"><a id="switchLink" href="__SWITCH_URL__">__SWITCH_TEXT__</a></div>
</section>
</main>
<script>
const authMode = "__AUTH_MODE__";                                             // 当前认证模式
const authForm = document.getElementById("authForm");                         // 登录或注册表单
const submitButton = document.getElementById("submitButton");                 // 提交按钮
const message = document.getElementById("message");                           // 错误提示

authForm.addEventListener("submit", async function (event) {
    event.preventDefault();
    message.textContent = "";
    submitButton.disabled = true;
    const formData = new FormData(authForm);
    try {
        const response = await fetch("/api/auth/" + authMode, {method: "POST", body: formData});
        const result = await response.json().catch(function () { return {}; });
        if (!response.ok) throw new Error(result.detail || "操作失败");
        window.location.href = "/";
    } catch (error) {
        message.textContent = error.message;
    } finally {
        submitButton.disabled = false;
    }
});
</script>
</body>
</html>"""

def render_auth_page(auth_mode: str) -> str:
    """渲染独立的登录或注册页面。"""
    if auth_mode == "register":
        page_values = {
            "__PAGE_TITLE__": "注册",
            "__AUTH_MODE__": "register",
            "__PASSWORD_AUTOCOMPLETE__": "new-password",
            "__SUBMIT_TEXT__": "注册并进入",
            "__SWITCH_URL__": "/login",
            "__SWITCH_TEXT__": "已有账号？登录",
        }
    else:
        page_values = {
            "__PAGE_TITLE__": "登录",
            "__AUTH_MODE__": "login",
            "__PASSWORD_AUTOCOMPLETE__": "current-password",
            "__SUBMIT_TEXT__": "登录",
            "__SWITCH_URL__": "/register",
            "__SWITCH_TEXT__": "没有账号？注册",
        }
    page_html = AUTH_HTML_TEMPLATE
    for placeholder, value in page_values.items():
        page_html = page_html.replace(placeholder, value)
    return page_html


VIEWER_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>在线查看</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
:root{--toolbar-height:40px;--accent:#ff3b00;--accent-dark:#cc2200;--gold:#ffcc00;--gold-dark:#cc9900;--surface:#210000;--surface-hover:#360000;--text:#ff7600;--muted:#cc6600;--bs-primary:#ff3b00;--bs-link-color:#ff3b00;--bs-focus-ring-color:rgba(255,59,0,.28)}
*{box-sizing:border-box;accent-color:var(--accent)}html,body{width:100%;height:100%;margin:0;overflow:hidden;background:#000;color:var(--text);font-family:Inter,system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color-scheme:dark}
#viewerBar{height:var(--toolbar-height);display:flex;align-items:center;gap:6px;padding:0 9px;background:#180000;box-shadow:0 1px 8px rgba(120,0,0,.38);position:relative;z-index:10}
#viewerName{flex:1;min-width:0;font-size:13px;font-weight:600;color:#ff7600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#viewerInfo{font-size:11px;color:var(--muted);white-space:nowrap}
.viewer-button{height:27px;border:0;border-radius:7px;padding:0 9px;background:transparent;font-size:12px;color:#ff7600;white-space:nowrap}.viewer-button:hover{background:var(--surface-hover);color:var(--gold)}.viewer-button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}.viewer-button.primary{background:var(--accent);color:#000}.viewer-button.primary:hover{background:var(--gold);color:#000}
#zoomControls{display:none;align-items:center;height:27px;background:#280000;border-radius:8px;padding:0 2px}.zoom-button{width:27px;height:23px;border:0;background:transparent;border-radius:6px;color:#ff7600;font-size:15px;line-height:1}.zoom-button:hover{background:#3c0000;color:var(--gold)}#zoomResetButton{width:48px;font-size:10px;font-weight:600;color:#ff7600}
#viewerStage{position:absolute;inset:var(--toolbar-height) 0 0;overflow:auto;background:#000;scrollbar-color:#cc3300 #180000}
#mediaCanvas{display:none;place-items:center;min-width:100%;min-height:100%;padding:12px;background:#000}
#imageViewer,#videoViewer{display:none;max-width:none;max-height:none;width:auto;height:auto;object-fit:contain;flex:none}
#videoViewer{background:#000}
#audioStage{display:none;width:100%;height:100%;align-items:center;justify-content:center;padding:20px;background:#000}
#audioPanel{width:min(720px,calc(100% - 10px));padding:26px;border-radius:14px;background:#210000;color:#ff7600;box-shadow:0 18px 60px rgba(120,0,0,.32)}#audioTitle{margin-bottom:18px;font-size:15px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}#audioViewer{width:100%;accent-color:var(--accent)}
#textEditor{display:none;width:100%;height:100%;resize:none;border:0;outline:0;padding:16px 18px;background:#000;color:#ff5a00;caret-color:#ffcc00;font:13px/1.55 ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;tab-size:4;white-space:pre;overflow:auto;scrollbar-color:#cc3300 #000}
#textEditor::selection{background:#ffcc00;color:#000}#textEditor::-moz-selection{background:#ffcc00;color:#000}
#viewerMessage{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#ff7600;font-size:14px;pointer-events:none}.text-mode #viewerStage{background:#000}.text-mode #viewerInfo{display:block}
@media(max-width:700px){#viewerBar{padding:0 5px;gap:2px}#viewerInfo{display:none}.viewer-button{padding:0 6px}#zoomResetButton{width:40px}#textEditor{padding:12px;font-size:12px}}
</style>
</head>
<body>
<header id="viewerBar"><button id="closeButton" class="viewer-button">关闭</button><div id="viewerName">在线查看</div><div id="viewerInfo"></div><div id="zoomControls"><button id="zoomOutButton" class="zoom-button" title="缩小">−</button><button id="zoomResetButton" class="zoom-button" title="适应窗口">100%</button><button id="zoomInButton" class="zoom-button" title="放大">＋</button></div><button id="downloadButton" class="viewer-button">下载</button><button id="saveButton" class="viewer-button primary" style="display:none">保存</button></header>
<main id="viewerStage"><div id="viewerMessage">正在载入…</div><div id="mediaCanvas"><img id="imageViewer" alt="图片预览"><video id="videoViewer" controls preload="metadata"></video></div><div id="audioStage"><div id="audioPanel"><div id="audioTitle"></div><audio id="audioViewer" controls preload="metadata"></audio></div></div><textarea id="textEditor" spellcheck="false"></textarea></main>
<script>
const viewerParameters = new URLSearchParams(window.location.search);                 // 预览参数
const viewerPath = viewerParameters.get("path") || "";                              // 文件路径
const viewerType = viewerParameters.get("type") || "none";                          // 预览类型
const viewerShareToken = viewerParameters.get("share") || "";                        // 公开分享令牌
const sharedViewerMode = viewerShareToken !== "";                                      // 分享页面只读模式
const viewerName = document.getElementById("viewerName");                            // 文件名
const viewerInfo = document.getElementById("viewerInfo");                            // 编码和状态
const viewerStage = document.getElementById("viewerStage");                          // 主显示区域
const viewerMessage = document.getElementById("viewerMessage");                      // 加载提示
const mediaCanvas = document.getElementById("mediaCanvas");                          // 图片和视频缩放画布
const imageViewer = document.getElementById("imageViewer");                          // 图片查看器
const videoViewer = document.getElementById("videoViewer");                          // 视频播放器
const audioStage = document.getElementById("audioStage");                            // 音频显示区域
const audioViewer = document.getElementById("audioViewer");                          // 音频播放器
const audioTitle = document.getElementById("audioTitle");                            // 音频名称
const textEditor = document.getElementById("textEditor");                            // 文本编辑器
const saveButton = document.getElementById("saveButton");                            // 保存按钮
const downloadButton = document.getElementById("downloadButton");                    // 下载按钮
const closeButton = document.getElementById("closeButton");                          // 关闭按钮
const zoomControls = document.getElementById("zoomControls");                        // 媒体缩放按钮组
const zoomOutButton = document.getElementById("zoomOutButton");                      // 缩小
const zoomResetButton = document.getElementById("zoomResetButton");                  // 恢复适应窗口
const zoomInButton = document.getElementById("zoomInButton");                        // 放大
let textEncoding = "";                                                               // charset-normalizer 自动检测到的编码
let originalTextContent = "";                                                        // 用于判断是否修改
let mediaNaturalWidth = 0;                                                            // 图片或视频原始宽度
let mediaNaturalHeight = 0;                                                           // 图片或视频原始高度
let mediaZoom = 1;                                                                    // 相对于自动适应尺寸的缩放比例
const minimumMediaZoom = 0.25;                                                        // 最小缩放 25%
const maximumMediaZoom = 4;                                                           // 最大缩放 400%
function getFileName(path) {
    const pathParts = path.split("/");
    return pathParts[pathParts.length - 1] || path;
}
function setViewerError(message) {
    viewerMessage.textContent = message;
    viewerMessage.style.display = "flex";
}
function formatBytes(byteCount) {
    if (!byteCount) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    let value = byteCount;
    let unitIndex = 0;
    while (value >= 1024 && unitIndex < units.length - 1) {
        value = value / 1024;
        unitIndex = unitIndex + 1;
    }
    const formattedValue = unitIndex === 0 ? value.toFixed(0) : value.toFixed(1);
    return formattedValue + " " + units[unitIndex];
}
async function loadTextEditor() {
    document.body.classList.add("text-mode");
    try {
        let requestUrl = "/api/text?path=" + encodeURIComponent(viewerPath);
        if (sharedViewerMode) requestUrl = "/api/public-share/" + encodeURIComponent(viewerShareToken) + "/text?path=" + encodeURIComponent(viewerPath);
        const response = await fetch(requestUrl);
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "文本读取失败");
        textEncoding = data.encoding || "";
        originalTextContent = data.content;
        textEditor.value = data.content;
        textEditor.style.display = "block";
        textEditor.readOnly = sharedViewerMode;
        saveButton.style.display = sharedViewerMode ? "none" : "inline-block";
        viewerInfo.textContent = (sharedViewerMode ? "只读 · " : "") + "编码: " + (textEncoding || "未知") + " · " + formatBytes(data.size || 0);
        viewerMessage.style.display = "none";
        textEditor.focus();
    } catch (error) {
        setViewerError(error.message);
    }
}
function getActiveMediaElement() {
    if (viewerType === "image") return imageViewer;
    if (viewerType === "video") return videoViewer;
    return null;
}
function updateMediaLayout() {
    const mediaElement = getActiveMediaElement();
    if (!mediaElement || !mediaNaturalWidth || !mediaNaturalHeight) return;
    const availableWidth = Math.max(1, viewerStage.clientWidth - 24);
    const availableHeight = Math.max(1, viewerStage.clientHeight - 24);
    const widthScale = availableWidth / mediaNaturalWidth;
    const heightScale = availableHeight / mediaNaturalHeight;
    const fitScale = Math.min(widthScale, heightScale);
    const displayWidth = Math.max(1, Math.round(mediaNaturalWidth * fitScale * mediaZoom));
    const displayHeight = Math.max(1, Math.round(mediaNaturalHeight * fitScale * mediaZoom));
    const canvasWidth = Math.max(viewerStage.clientWidth, displayWidth + 24);
    const canvasHeight = Math.max(viewerStage.clientHeight, displayHeight + 24);
    mediaCanvas.style.width = canvasWidth + "px";
    mediaCanvas.style.height = canvasHeight + "px";
    mediaElement.style.width = displayWidth + "px";
    mediaElement.style.height = displayHeight + "px";
    zoomResetButton.textContent = Math.round(mediaZoom * 100) + "%";
}
function changeMediaZoom(delta) {
    const nextZoom = mediaZoom + delta;
    mediaZoom = Math.min(maximumMediaZoom, Math.max(minimumMediaZoom, nextZoom));
    mediaZoom = Math.round(mediaZoom * 100) / 100;
    updateMediaLayout();
}
function resetMediaZoom() {
    mediaZoom = 1;
    updateMediaLayout();
    viewerStage.scrollLeft = 0;
    viewerStage.scrollTop = 0;
}
function loadMediaViewer() {
    let mediaUrl = "/api/preview?path=" + encodeURIComponent(viewerPath);
    if (sharedViewerMode) mediaUrl = "/api/public-share/" + encodeURIComponent(viewerShareToken) + "/preview?path=" + encodeURIComponent(viewerPath);
    viewerMessage.style.display = "none";
    if (viewerType === "image") {
        mediaCanvas.style.display = "grid";
        zoomControls.style.display = "flex";
        imageViewer.style.display = "block";
        imageViewer.addEventListener("load", function () {
            mediaNaturalWidth = imageViewer.naturalWidth;
            mediaNaturalHeight = imageViewer.naturalHeight;
            resetMediaZoom();
        }, {once:true});
        imageViewer.src = mediaUrl;
        return;
    }
    if (viewerType === "video") {
        mediaCanvas.style.display = "grid";
        zoomControls.style.display = "flex";
        videoViewer.style.display = "block";
        videoViewer.addEventListener("loadedmetadata", function () {
            mediaNaturalWidth = videoViewer.videoWidth;
            mediaNaturalHeight = videoViewer.videoHeight;
            resetMediaZoom();
        }, {once:true});
        videoViewer.src = mediaUrl;
        return;
    }
    if (viewerType === "audio") {
        audioTitle.textContent = getFileName(viewerPath);
        audioViewer.src = mediaUrl;
        audioStage.style.display = "flex";
        return;
    }
    setViewerError("该格式暂不支持在线查看");
}
async function saveText() {
    if (sharedViewerMode) return;
    saveButton.disabled = true;
    saveButton.textContent = "保存中…";
    try {
        const requestBody = {path:viewerPath, content:textEditor.value};
        const response = await fetch("/api/text", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(requestBody)});
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "保存失败");
        originalTextContent = textEditor.value;
        textEncoding = data.encoding || textEncoding;
        viewerInfo.textContent = "已保存 · 编码: " + textEncoding + " · " + formatBytes(data.size || 0);
    } catch (error) {
        viewerInfo.textContent = error.message;
    } finally {
        saveButton.disabled = false;
        saveButton.textContent = "保存";
    }
}
viewerName.textContent = getFileName(viewerPath) || "在线查看";
document.title = viewerName.textContent + " - 在线查看";
if (viewerType === "text") loadTextEditor();
else loadMediaViewer();
saveButton.addEventListener("click", saveText);
zoomOutButton.addEventListener("click", function () { changeMediaZoom(-0.1); });
zoomInButton.addEventListener("click", function () { changeMediaZoom(0.1); });
zoomResetButton.addEventListener("click", resetMediaZoom);
window.addEventListener("resize", function () {
    if (viewerType === "image" || viewerType === "video") updateMediaLayout();
});
downloadButton.addEventListener("click", function () {
    if (sharedViewerMode) {
        window.location.href = "/api/public-share/" + encodeURIComponent(viewerShareToken) + "/download?path=" + encodeURIComponent(viewerPath);
        return;
    }
    window.location.href = "/api/download?path=" + encodeURIComponent(viewerPath);
});
closeButton.addEventListener("click", function () { window.close(); });
window.addEventListener("keydown", function (event) {
    if (!sharedViewerMode && (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s" && viewerType === "text") {
        event.preventDefault();
        saveText();
    }
    if ((viewerType === "image" || viewerType === "video") && (event.key === "+" || event.key === "=")) changeMediaZoom(0.1);
    if ((viewerType === "image" || viewerType === "video") && event.key === "-") changeMediaZoom(-0.1);
    if ((viewerType === "image" || viewerType === "video") && event.key === "0") resetMediaZoom();
});
window.addEventListener("beforeunload", function (event) {
    if (!sharedViewerMode && viewerType === "text" && textEditor.value !== originalTextContent) {
        event.preventDefault();
        event.returnValue = "";
    }
});
</script>
</body>
</html>"""
HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>FastAPI 文件管理器</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet" integrity="sha384-sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB" crossorigin="anonymous">
<style>
:root{--toolbar-height:44px;--accent:#ff3b00;--accent-dark:#cc2200;--accent-soft:#330000;--accent-soft-2:#4d0d00;--gold:#ffcc00;--folder:#ff7200;--text:#ff6a00;--muted:#cc6600;--page:#120000;--surface:#1f0000;--surface-hover:#320000;--danger:#ff1800;--bs-primary:#ff3b00;--bs-link-color:#ff3b00;--bs-focus-ring-color:rgba(255,59,0,.28)}
*{box-sizing:border-box;accent-color:var(--accent)}
html,body{width:100%;height:100%;margin:0;overflow:hidden;background:var(--page);color:var(--text);font-family:Inter,system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color-scheme:dark}
button{font:inherit}
#topBar{height:var(--toolbar-height);display:flex;align-items:center;gap:5px;padding:0 10px;background:rgba(30,0,0,.98);box-shadow:0 1px 8px rgba(120,0,0,.28);position:relative;z-index:40;user-select:none}
.toolbar-button{height:30px;padding:0 9px!important;border:0!important;border-radius:7px!important;background:transparent!important;color:#ff6a00!important;font-size:13px!important;white-space:nowrap}
.toolbar-button:hover{background:var(--surface-hover)!important;color:var(--gold)!important}
.toolbar-button:focus-visible{outline:2px solid var(--accent)!important;outline-offset:1px!important;box-shadow:none!important}
.toolbar-icon{width:16px;height:16px;margin-right:4px;vertical-align:-3px}
#breadcrumbs{display:flex;align-items:center;gap:0;min-width:0;flex:1;overflow:auto;scrollbar-width:none}
#breadcrumbs::-webkit-scrollbar{display:none}
.breadcrumb-item-button{height:29px;border:0;background:transparent;border-radius:7px;padding:0 7px;color:#ff7600;font-size:13px;white-space:nowrap}
.breadcrumb-item-button:hover,.breadcrumb-item-button.drop-target{background:var(--accent-soft);color:var(--gold)}
.breadcrumb-separator{padding:0 1px;color:#cc5500;font-size:16px;line-height:1}
#selectionCount{font-size:12px;color:var(--gold);background:var(--accent-soft);border-radius:999px;padding:4px 8px;white-space:nowrap;display:none}
#userControls{display:flex;align-items:center;gap:2px;min-width:0;margin-left:2px}
#currentUsername{max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;color:var(--gold);padding:0 4px}
#logoutButton{height:28px!important;padding:0 7px!important}
#fileViewport{position:absolute;inset:var(--toolbar-height) 0 0 0;overflow:auto;padding:14px 16px 32px;outline:none;background:var(--page);scrollbar-color:#cc3300 #180000}
#fileGrid{min-height:100%;display:grid;grid-template-columns:repeat(auto-fill,minmax(126px,1fr));grid-auto-rows:142px;gap:4px 8px;align-content:start;position:relative}
.file-item{min-width:0;border:0;border-radius:10px;padding:9px 6px 7px;display:flex;flex-direction:column;align-items:center;justify-content:flex-start;user-select:none;cursor:default;position:relative;transition:background .12s ease,transform .12s ease}
.file-item:hover{background:#260000}
.file-item.selected{background:var(--accent-soft)}
.file-item.dragging{opacity:.42}
.file-item.drop-target{background:var(--accent-soft-2);transform:translateY(-1px)}
.file-icon-wrap{height:72px;width:82px;display:flex;align-items:center;justify-content:center;pointer-events:none;position:relative}
.file-svg{width:66px;height:66px;display:block;filter:drop-shadow(0 3px 3px rgba(100,0,0,.26))}
.file-extension{position:absolute;left:50%;bottom:12px;transform:translateX(-50%);max-width:48px;padding:2px 5px;border-radius:4px;background:var(--accent);color:#000;font-size:9px;font-weight:700;line-height:1.1;letter-spacing:.3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.file-name{width:100%;min-height:35px;margin-top:2px;text-align:center;font-size:13px;font-weight:500;color:#ff7600;line-height:1.32;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;word-break:break-word;pointer-events:none}
.file-meta{width:100%;margin-top:3px;text-align:center;font-size:10.5px;line-height:1.2;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;pointer-events:none}
#emptyState{display:none;position:absolute;left:50%;top:44%;transform:translate(-50%,-50%);text-align:center;color:#cc6600;pointer-events:none}
#emptyState svg{width:70px;height:70px;margin-bottom:8px;opacity:.7}
#selectionMarquee{position:fixed;display:none;z-index:25;pointer-events:none;background:rgba(255,59,0,.16);outline:1px solid rgba(255,204,0,.72)}
#uploadOverlay{position:fixed;inset:var(--toolbar-height) 0 0;display:none;place-items:center;z-index:35;background:rgba(80,0,0,.88);color:var(--gold);font-size:18px;font-weight:600;pointer-events:none}
#uploadOverlay .upload-message{padding:18px 28px;border-radius:16px;background:#210000;color:#ff7600;box-shadow:0 12px 30px rgba(120,0,0,.32)}
#contextMenu{position:fixed;display:none;z-index:120;min-width:190px;padding:6px;background:#210000;border-radius:10px;box-shadow:0 12px 34px rgba(120,0,0,.36);user-select:none}
.context-menu-item{height:34px;display:flex;align-items:center;gap:9px;padding:0 10px;border-radius:7px;font-size:13px;cursor:pointer;color:#ff7600}
.context-menu-item:hover{background:var(--accent-soft);color:var(--gold)}
.context-menu-item.danger{color:var(--danger)}
.context-menu-separator{height:1px;margin:5px 4px;background:#660000}
.context-menu-icon{width:17px;height:17px;flex:0 0 auto}
#toastMessage{position:fixed;left:50%;bottom:24px;z-index:200;transform:translateX(-50%) translateY(10px);padding:9px 14px;border-radius:9px;background:#330000;color:#ffcc00;font-size:13px;opacity:0;pointer-events:none;transition:.18s ease;box-shadow:0 8px 24px rgba(120,0,0,.30)}
#toastMessage.show{opacity:1;transform:translateX(-50%) translateY(0)}
@media(max-width:720px){#topBar{padding:0 6px}.toolbar-button span{display:none}.toolbar-icon{margin-right:0}#selectionCount{display:none!important}#fileViewport{padding:10px 8px 24px}#fileGrid{grid-template-columns:repeat(auto-fill,minmax(100px,1fr));grid-auto-rows:132px;gap:2px}.file-svg{width:58px;height:58px}.file-icon-wrap{height:64px;width:70px}.file-name{font-size:12px}}
</style>
</head>
<body>
<header id="topBar">
<button id="homeButton" class="btn btn-sm toolbar-button" title="根目录">
<svg class="toolbar-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 10.5 12 3l9 7.5"/><path d="M5.5 9.5V21h13V9.5"/><path d="M9.5 21v-6h5v6"/></svg><span>主页</span>
</button>
<div id="breadcrumbs"></div>
<span id="selectionCount"></span>
<div id="userControls"><span id="currentUsername"></span><button id="logoutButton" class="btn btn-sm toolbar-button" title="退出登录"><span>退出</span></button></div>
<input id="fileInput" type="file" multiple hidden>
</header>
<main id="fileViewport" tabindex="0"><div id="fileGrid"></div><div id="emptyState"></div></main>
<div id="selectionMarquee"></div>
<div id="uploadOverlay"><div class="upload-message">松开鼠标，上传到当前目录</div></div>
<div id="contextMenu"></div>
<div id="toastMessage"></div>
<iframe name="archiveDownloadFrame" title="7Z 下载" hidden></iframe>
<script>
const applicationState = {currentPath: "", items: [], selectedPaths: new Set(), selectionAnchor: null, draggedPaths: [], marquee: null, suppressNextClick: false}; // 页面状态
const fileGrid = document.getElementById("fileGrid");                       // 文件网格
const fileViewport = document.getElementById("fileViewport");               // 主文件区域
const breadcrumbs = document.getElementById("breadcrumbs");                 // 面包屑导航
const selectionMarquee = document.getElementById("selectionMarquee");       // 框选区域
const contextMenu = document.getElementById("contextMenu");                 // 右键菜单
const toastMessage = document.getElementById("toastMessage");               // 底部提示
const uploadOverlay = document.getElementById("uploadOverlay");             // 外部拖拽上传提示
const fileInput = document.getElementById("fileInput");                     // 文件选择器
const emptyState = document.getElementById("emptyState");                   // 空目录提示
const homeButton = document.getElementById("homeButton");                   // 根目录按钮
const selectionCount = document.getElementById("selectionCount");           // 已选数量
const currentUsername = document.getElementById("currentUsername");       // 当前用户名
const logoutButton = document.getElementById("logoutButton");                   // 退出登录按钮
function joinPaths(firstPath, secondPath) {
    const pathParts = [];
    if (firstPath) pathParts.push(firstPath);
    if (secondPath) pathParts.push(secondPath);
    return pathParts.join("/").replace(/\/+/g, "/");
}
function getParentPath(path) {
    const pathParts = path.split("/").filter(Boolean);
    pathParts.pop();
    return pathParts.join("/");
}
function showToast(message) {
    toastMessage.textContent = message;
    toastMessage.classList.add("show");
    clearTimeout(showToast.timeoutId);
    showToast.timeoutId = setTimeout(function () {
        toastMessage.classList.remove("show");
    }, 1900);
}
async function fetchJson(url, options) {
    const response = await fetch(url, options || {});
    if (response.status === 401) {
        window.location.href = "/login";
        throw new Error("登录已失效");
    }
    if (!response.ok) {
        let errorMessage = response.statusText || "请求失败";
        try {
            const errorBody = await response.json();
            if (errorBody.detail) errorMessage = errorBody.detail;
        } catch (error) {}
        throw new Error(errorMessage);
    }
    return response.json();
}
async function loadCurrentUser() {
    try {
        const userInfo = await fetchJson("/api/me");
        currentUsername.textContent = userInfo.username;
        currentUsername.title = userInfo.username;
    } catch (error) {}
}
function postJson(url, body) {
    return fetch(url, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)
    });
}
async function loadDirectory(path) {
    const requestedPath = path === undefined ? applicationState.currentPath : path;
    try {
        const data = await fetchJson("/api/list?path=" + encodeURIComponent(requestedPath));
        applicationState.currentPath = data.path;
        applicationState.items = data.items;
        applicationState.selectedPaths.clear();
        applicationState.selectionAnchor = null;
        renderBreadcrumbs();
        renderItems();
        updateSelectionCount();
    } catch (error) {
        showToast(error.message);
    }
}
function renderBreadcrumbs() {
    breadcrumbs.innerHTML = "";
    const rootButton = document.createElement("button");
    rootButton.className = "breadcrumb-item-button";
    rootButton.textContent = "文件";
    rootButton.dataset.path = "";
    breadcrumbs.append(rootButton);
    let accumulatedPath = "";
    const pathParts = applicationState.currentPath.split("/").filter(Boolean);
    for (const pathPart of pathParts) {
        const separator = document.createElement("span");
        separator.className = "breadcrumb-separator";
        separator.textContent = "›";
        breadcrumbs.append(separator);
        accumulatedPath = joinPaths(accumulatedPath, pathPart);
        const pathButton = document.createElement("button");
        pathButton.className = "breadcrumb-item-button";
        pathButton.textContent = pathPart;
        pathButton.dataset.path = accumulatedPath;
        breadcrumbs.append(pathButton);
    }
    breadcrumbs.scrollLeft = breadcrumbs.scrollWidth;
}
function getFileExtension(fileName) {
    const lastDotIndex = fileName.lastIndexOf(".");
    if (lastDotIndex <= 0 || lastDotIndex === fileName.length - 1) return "FILE";
    const extension = fileName.slice(lastDotIndex + 1).toUpperCase();
    return extension.length > 5 ? extension.slice(0, 5) : extension;
}
function createFolderIcon() {
    return `<svg class="file-svg" viewBox="0 0 80 68" aria-hidden="true"><path d="M5 15a7 7 0 0 1 7-7h21l8 9h27a7 7 0 0 1 7 7v31a8 8 0 0 1-8 8H13a8 8 0 0 1-8-8z" fill="#cc3300"/><path d="M5 25h70v30a8 8 0 0 1-8 8H13a8 8 0 0 1-8-8z" fill="#ff6a00"/><path d="M9 28h62" stroke="#ffcc00" stroke-width="2" opacity=".8"/></svg>`;
}
function createFileIcon(fileName) {
    const extension = getFileExtension(fileName);
    return `<svg class="file-svg" viewBox="0 0 72 78" aria-hidden="true"><path d="M13 4h31l15 15v50a5 5 0 0 1-5 5H13a5 5 0 0 1-5-5V9a5 5 0 0 1 5-5z" fill="#ffcc00" stroke="#cc5500" stroke-width="1.5"/><path d="M44 4v13a3 3 0 0 0 3 3h12" fill="#ff8a00"/><path d="M44 4l15 16H47a3 3 0 0 1-3-3z" fill="#ff5a00"/><path d="M17 33h33M17 41h25M17 49h29" stroke="#cc6600" stroke-width="3" stroke-linecap="round"/></svg><span class="file-extension">${extension}</span>`;
}
function renderItems() {
    fileGrid.innerHTML = "";
    emptyState.style.display = applicationState.items.length ? "none" : "block";
    if (!applicationState.items.length) {
        emptyState.innerHTML = `<svg viewBox="0 0 80 68"><path d="M5 15a7 7 0 0 1 7-7h21l8 9h27a7 7 0 0 1 7 7v31a8 8 0 0 1-8 8H13a8 8 0 0 1-8-8z" fill="#cc3300"/><path d="M5 25h70v30a8 8 0 0 1-8 8H13a8 8 0 0 1-8-8z" fill="#ff6a00"/></svg><div>这个文件夹是空的</div>`;
        return;
    }
    for (const item of applicationState.items) {
        const itemElement = document.createElement("div");
        itemElement.className = "file-item";
        itemElement.dataset.path = item.path;
        itemElement.dataset.type = item.type;
        itemElement.draggable = true;
        const iconHtml = item.type === "folder" ? createFolderIcon() : createFileIcon(item.name);
        itemElement.innerHTML = `<div class="file-icon-wrap">${iconHtml}</div><div class="file-name"></div><div class="file-meta"></div>`;
        itemElement.querySelector(".file-name").textContent = item.name;
        itemElement.querySelector(".file-meta").textContent = item.size_text;
        if (applicationState.selectedPaths.has(item.path)) itemElement.classList.add("selected");
        fileGrid.append(itemElement);
    }
}
function updateSelectionCount() {
    const selectedCountValue = applicationState.selectedPaths.size;
    if (!selectedCountValue) {
        selectionCount.style.display = "none";
        selectionCount.textContent = "";
        return;
    }
    selectionCount.style.display = "inline-block";
    selectionCount.textContent = `已选择 ${selectedCountValue} 项`;
}
function refreshSelectionStyles() {
    const itemElements = document.querySelectorAll(".file-item");
    for (const itemElement of itemElements) {
        const isSelected = applicationState.selectedPaths.has(itemElement.dataset.path);
        itemElement.classList.toggle("selected", isSelected);
    }
    updateSelectionCount();
}
function selectOnly(path) {
    applicationState.selectedPaths.clear();
    applicationState.selectedPaths.add(path);
    applicationState.selectionAnchor = path;
    refreshSelectionStyles();
}
function toggleSelection(path) {
    if (applicationState.selectedPaths.has(path)) applicationState.selectedPaths.delete(path);
    else applicationState.selectedPaths.add(path);
    applicationState.selectionAnchor = path;
    refreshSelectionStyles();
}
function selectRange(path) {
    const orderedPaths = [];
    for (const item of applicationState.items) orderedPaths.push(item.path);
    const anchorIndex = orderedPaths.indexOf(applicationState.selectionAnchor);
    const targetIndex = orderedPaths.indexOf(path);
    if (anchorIndex < 0 || targetIndex < 0) {
        selectOnly(path);
        return;
    }
    applicationState.selectedPaths.clear();
    const startIndex = Math.min(anchorIndex, targetIndex);
    const endIndex = Math.max(anchorIndex, targetIndex);
    for (let index = startIndex; index <= endIndex; index = index + 1) applicationState.selectedPaths.add(orderedPaths[index]);
    refreshSelectionStyles();
}
fileGrid.addEventListener("click", function (event) {
    const itemElement = event.target.closest(".file-item");
    if (!itemElement) return;
    const itemPath = itemElement.dataset.path;
    if (event.ctrlKey || event.metaKey) {
        toggleSelection(itemPath);
        return;
    }
    if (event.shiftKey) {
        selectRange(itemPath);
        return;
    }
    let clickedItem = null;
    for (const item of applicationState.items) {
        if (item.path === itemPath) {
            clickedItem = item;
            break;
        }
    }
    if (!clickedItem) return;
    if (clickedItem.type === "folder") {
        loadDirectory(itemPath);
        return;
    }
    downloadSingleFile(itemPath);
});
fileViewport.addEventListener("click", function (event) {
    if (event.target !== fileViewport && event.target !== fileGrid) return;
    applicationState.selectedPaths.clear();
    refreshSelectionStyles();
});
fileViewport.addEventListener("pointerdown", function (event) {
    if (event.button !== 0) return;
    if (event.target.closest(".file-item") || event.target.closest("#contextMenu")) return;
    hideContextMenu();
    const baseSelection = new Set();
    if (event.ctrlKey || event.metaKey) {
        for (const selectedPath of applicationState.selectedPaths) baseSelection.add(selectedPath);
    }
    applicationState.marquee = {startX: event.clientX, startY: event.clientY, baseSelection: baseSelection, moved: false};
    selectionMarquee.style.display = "block";
    selectionMarquee.style.left = event.clientX + "px";
    selectionMarquee.style.top = event.clientY + "px";
    selectionMarquee.style.width = "0px";
    selectionMarquee.style.height = "0px";
    if (fileViewport.setPointerCapture) fileViewport.setPointerCapture(event.pointerId);
    event.preventDefault();
});
fileViewport.addEventListener("pointermove", function (event) {
    const marqueeState = applicationState.marquee;
    if (!marqueeState) return;
    const left = Math.min(marqueeState.startX, event.clientX);
    const top = Math.min(marqueeState.startY, event.clientY);
    const right = Math.max(marqueeState.startX, event.clientX);
    const bottom = Math.max(marqueeState.startY, event.clientY);
    if (Math.abs(event.clientX - marqueeState.startX) > 3 || Math.abs(event.clientY - marqueeState.startY) > 3) marqueeState.moved = true;
    selectionMarquee.style.left = left + "px";
    selectionMarquee.style.top = top + "px";
    selectionMarquee.style.width = right - left + "px";
    selectionMarquee.style.height = bottom - top + "px";
    const newSelection = new Set();
    for (const selectedPath of marqueeState.baseSelection) newSelection.add(selectedPath);
    const itemElements = document.querySelectorAll(".file-item");
    for (const itemElement of itemElements) {
        const itemRectangle = itemElement.getBoundingClientRect();
        const intersects = itemRectangle.right >= left && itemRectangle.left <= right && itemRectangle.bottom >= top && itemRectangle.top <= bottom;
        if (intersects) newSelection.add(itemElement.dataset.path);
    }
    applicationState.selectedPaths = newSelection;
    refreshSelectionStyles();
});
fileViewport.addEventListener("pointerup", function () {
    if (!applicationState.marquee) return;
    const marqueeMoved = applicationState.marquee.moved;
    applicationState.marquee = null;
    selectionMarquee.style.display = "none";
    if (marqueeMoved) applicationState.suppressNextClick = true;
});
fileViewport.addEventListener("click", function (event) {
    if (!applicationState.suppressNextClick) return;
    applicationState.suppressNextClick = false;
    event.preventDefault();
    event.stopPropagation();
}, true);
fileViewport.addEventListener("pointercancel", function () {
    if (!applicationState.marquee) return;
    applicationState.marquee = null;
    selectionMarquee.style.display = "none";
});
function getDraggedPaths(clickedPath) {
    const paths = [];
    if (applicationState.selectedPaths.has(clickedPath)) {
        for (const selectedPath of applicationState.selectedPaths) paths.push(selectedPath);
        return paths;
    }
    paths.push(clickedPath);
    return paths;
}
fileGrid.addEventListener("dragstart", function (event) {
    const itemElement = event.target.closest(".file-item");
    if (!itemElement) return;
    const itemPath = itemElement.dataset.path;
    if (!applicationState.selectedPaths.has(itemPath)) selectOnly(itemPath);
    applicationState.draggedPaths = getDraggedPaths(itemPath);
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("application/x-fastapi-file-manager", JSON.stringify(applicationState.draggedPaths));
    requestAnimationFrame(function () {
        for (const draggedPath of applicationState.draggedPaths) {
            const escapedPath = CSS.escape(draggedPath);
            const draggedElement = document.querySelector(`.file-item[data-path="${escapedPath}"]`);
            if (draggedElement) draggedElement.classList.add("dragging");
        }
    });
});
fileGrid.addEventListener("dragend", function () {
    const styledElements = document.querySelectorAll(".dragging,.drop-target");
    for (const styledElement of styledElements) styledElement.classList.remove("dragging", "drop-target");
    applicationState.draggedPaths = [];
});
fileGrid.addEventListener("dragover", function (event) {
    const folderElement = event.target.closest('.file-item[data-type="folder"]');
    if (!folderElement) return;
    const internalDrag = Array.from(event.dataTransfer.types).includes("application/x-fastapi-file-manager");
    if (!internalDrag) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    const previousTargets = document.querySelectorAll(".file-item.drop-target");
    for (const previousTarget of previousTargets) previousTarget.classList.remove("drop-target");
    folderElement.classList.add("drop-target");
});
fileGrid.addEventListener("dragleave", function (event) {
    const itemElement = event.target.closest(".file-item");
    if (itemElement) itemElement.classList.remove("drop-target");
});
fileGrid.addEventListener("drop", async function (event) {
    const folderElement = event.target.closest('.file-item[data-type="folder"]');
    if (!folderElement) return;
    const rawDragData = event.dataTransfer.getData("application/x-fastapi-file-manager");
    if (!rawDragData) return;
    event.preventDefault();
    folderElement.classList.remove("drop-target");
    try {
        const draggedPaths = JSON.parse(rawDragData);
        await moveSelectedItems(draggedPaths, folderElement.dataset.path);
    } catch (error) {
        showToast(error.message);
    }
});
breadcrumbs.addEventListener("click", function (event) {
    const breadcrumbButton = event.target.closest(".breadcrumb-item-button");
    if (breadcrumbButton) loadDirectory(breadcrumbButton.dataset.path);
});
breadcrumbs.addEventListener("dragover", function (event) {
    const breadcrumbButton = event.target.closest(".breadcrumb-item-button");
    if (!breadcrumbButton) return;
    const internalDrag = Array.from(event.dataTransfer.types).includes("application/x-fastapi-file-manager");
    if (!internalDrag) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    const previousTargets = document.querySelectorAll(".breadcrumb-item-button.drop-target");
    for (const previousTarget of previousTargets) previousTarget.classList.remove("drop-target");
    breadcrumbButton.classList.add("drop-target");
});
breadcrumbs.addEventListener("dragleave", function (event) {
    const breadcrumbButton = event.target.closest(".breadcrumb-item-button");
    if (breadcrumbButton) breadcrumbButton.classList.remove("drop-target");
});
breadcrumbs.addEventListener("drop", async function (event) {
    const breadcrumbButton = event.target.closest(".breadcrumb-item-button");
    if (!breadcrumbButton) return;
    const rawDragData = event.dataTransfer.getData("application/x-fastapi-file-manager");
    if (!rawDragData) return;
    event.preventDefault();
    breadcrumbButton.classList.remove("drop-target");
    try {
        const draggedPaths = JSON.parse(rawDragData);
        await moveSelectedItems(draggedPaths, breadcrumbButton.dataset.path);
    } catch (error) {
        showToast(error.message);
    }
});
async function moveSelectedItems(paths, destination) {
    const response = await postJson("/api/move", {paths: paths, destination: destination});
    if (!response.ok) {
        const errorBody = await response.json().catch(function () { return {}; });
        throw new Error(errorBody.detail || "移动失败");
    }
    showToast("移动完成");
    await loadDirectory(applicationState.currentPath);
}
let externalDragDepth = 0;
document.addEventListener("dragenter", function (event) {
    const containsFiles = Array.from(event.dataTransfer.types).includes("Files");
    const isInternalDrag = Array.from(event.dataTransfer.types).includes("application/x-fastapi-file-manager");
    if (!containsFiles || isInternalDrag) return;
    externalDragDepth = externalDragDepth + 1;
    uploadOverlay.style.display = "grid";
});
document.addEventListener("dragleave", function (event) {
    if (!Array.from(event.dataTransfer.types).includes("Files")) return;
    externalDragDepth = Math.max(0, externalDragDepth - 1);
    if (externalDragDepth === 0) uploadOverlay.style.display = "none";
});
document.addEventListener("dragover", function (event) {
    if (Array.from(event.dataTransfer.types).includes("Files")) event.preventDefault();
});
document.addEventListener("drop", async function (event) {
    const isInternalDrag = Boolean(event.dataTransfer.getData("application/x-fastapi-file-manager"));
    if (!Array.from(event.dataTransfer.types).includes("Files") || isInternalDrag) return;
    event.preventDefault();
    externalDragDepth = 0;
    uploadOverlay.style.display = "none";
    await uploadFiles(event.dataTransfer.files);
});
async function uploadFiles(files) {
    if (!files || !files.length) return;
    const formData = new FormData();
    formData.append("path", applicationState.currentPath);
    for (const file of files) formData.append("files", file);
    try {
        const response = await fetch("/api/upload", {method: "POST", body: formData});
        if (!response.ok) {
            const errorBody = await response.json().catch(function () { return {}; });
            throw new Error(errorBody.detail || "上传失败");
        }
        showToast(`已上传 ${files.length} 个文件`);
        await loadDirectory();
    } catch (error) {
        showToast(error.message);
    }
}
function menuIcon(iconName) {
    const icons = {
        preview: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6z"/><circle cx="12" cy="12" r="2.7"/></svg>',
        download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3v12"/><path d="m7.5 10.5 4.5 4.5 4.5-4.5"/><path d="M4 20h16"/></svg>',
        archive: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M5 3h14v18H5z"/><path d="M9 3v4h6V3M10 11h4M10 15h4"/></svg>',
        rename: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="m4 20 4.5-1 10-10-3.5-3.5-10 10z"/><path d="m13.5 7 3.5 3.5"/></svg>',
        delete: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7h16M9 3h6l1 4H8zM7 7l1 14h8l1-14"/></svg>',
        folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 6.5h6l2 2h10v10.5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 12v6M9 15h6"/></svg>',
        upload: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 16V4"/><path d="m7.5 8.5 4.5-4.5 4.5 4.5"/><path d="M4 14.5V20h16v-5.5"/></svg>',
        share: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="18" cy="5" r="2.5"/><circle cx="6" cy="12" r="2.5"/><circle cx="18" cy="19" r="2.5"/><path d="m8.2 10.8 7.5-4.5M8.2 13.2l7.5 4.5"/></svg>',
        deduplicate: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M5 5h10v10H5z"/><path d="M9 9h10v10H9z"/><path d="M12 12h4M14 10v4"/></svg>',
        organize: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 6.5h7l2 2h9v10.5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M8 12h8M8 15h8M8 18h5"/></svg>'
    };
    return icons[iconName] || "";
}
function addContextMenuItem(label, iconName, clickHandler, className) {
    const menuItem = document.createElement("div");
    menuItem.className = "context-menu-item";
    if (className) menuItem.classList.add(className);
    menuItem.innerHTML = `<span class="context-menu-icon">${menuIcon(iconName)}</span><span>${label}</span>`;
    menuItem.addEventListener("click", async function () {
        hideContextMenu();
        await clickHandler();
    });
    contextMenu.append(menuItem);
}
async function createFolder() {
    const folderName = window.prompt("文件夹名称");
    if (!folderName) return;
    const formData = new FormData();
    formData.append("path", applicationState.currentPath);
    formData.append("name", folderName);
    try {
        const response = await fetch("/api/mkdir", {method: "POST", body: formData});
        if (!response.ok) {
            const errorBody = await response.json().catch(function () { return {}; });
            throw new Error(errorBody.detail || "创建失败");
        }
        await loadDirectory();
    } catch (error) {
        showToast(error.message);
    }
}
function showBlankContextMenu(x, y) {
    contextMenu.innerHTML = "";
    addContextMenuItem("新建文件夹", "folder", createFolder);
    addContextMenuItem("上传文件", "upload", function () { fileInput.click(); });
    contextMenu.style.display = "block";
    const menuWidth = contextMenu.offsetWidth;
    const menuHeight = contextMenu.offsetHeight;
    contextMenu.style.left = Math.min(x, window.innerWidth - menuWidth - 8) + "px";
    contextMenu.style.top = Math.min(y, window.innerHeight - menuHeight - 8) + "px";
}
function showContextMenu(x, y, paths) {
    const selectedItems = [];
    for (const selectedPath of paths) {
        for (const item of applicationState.items) {
            if (item.path === selectedPath) {
                selectedItems.push(item);
                break;
            }
        }
    }
    const isSingleSelection = selectedItems.length === 1;
    const singleItem = isSingleSelection ? selectedItems[0] : null;
    contextMenu.innerHTML = "";
    if (isSingleSelection && singleItem.type === "file" && singleItem.preview_type !== "none") {
        addContextMenuItem(getPreviewMenuLabel(singleItem.preview_type), "preview", function () { openOnlineViewer(singleItem); });
    }
    if (isSingleSelection && singleItem.type === "file") addContextMenuItem("下载原文件", "download", function () { downloadSingleFile(singleItem.path); });
    if (!isSingleSelection || singleItem.type === "folder") {
        const archiveLabel = isSingleSelection ? "下载为 7Z" : "多选下载为 7Z";
        addContextMenuItem(archiveLabel, "archive", function () { downloadSevenZipArchive(paths); });
    }
    if (isSingleSelection && singleItem.type === "folder") addContextMenuItem("去重清理", "deduplicate", function () { return deduplicateFolder(singleItem); });
    if (isSingleSelection && singleItem.type === "folder") addContextMenuItem("归档", "organize", function () { return archiveMediaFolder(singleItem); });
    if (isSingleSelection) addContextMenuItem("分享", "share", function () { return createShareLink(singleItem); });
    if (isSingleSelection) addContextMenuItem("重命名", "rename", function () { return renameItem(singleItem); });
    const separator = document.createElement("div");
    separator.className = "context-menu-separator";
    contextMenu.append(separator);
    addContextMenuItem("删除", "delete", function () { return deleteSelectedItems(paths); }, "danger");
    contextMenu.style.display = "block";
    const menuWidth = contextMenu.offsetWidth;
    const menuHeight = contextMenu.offsetHeight;
    contextMenu.style.left = Math.min(x, window.innerWidth - menuWidth - 8) + "px";
    contextMenu.style.top = Math.min(y, window.innerHeight - menuHeight - 8) + "px";
}
function hideContextMenu() {
    contextMenu.style.display = "none";
}
fileViewport.addEventListener("contextmenu", function (event) {
    event.preventDefault();
    const itemElement = event.target.closest(".file-item");
    if (!itemElement) {
        applicationState.selectedPaths.clear();
        refreshSelectionStyles();
        showBlankContextMenu(event.clientX, event.clientY);
        return;
    }
    const itemPath = itemElement.dataset.path;
    if (!applicationState.selectedPaths.has(itemPath)) selectOnly(itemPath);
    const selectedPaths = [];
    for (const selectedPath of applicationState.selectedPaths) selectedPaths.push(selectedPath);
    showContextMenu(event.clientX, event.clientY, selectedPaths);
});
document.addEventListener("pointerdown", function (event) {
    if (!event.target.closest("#contextMenu")) hideContextMenu();
});
function getPreviewMenuLabel(previewType) {
    if (previewType === "text") return "在线编辑文本";
    if (previewType === "image") return "在线查看图片";
    if (previewType === "video") return "在线播放视频";
    if (previewType === "audio") return "在线播放音频";
    return "在线打开";
}
function openOnlineViewer(item) {
    if (!item || item.preview_type === "none") {
        showToast("该格式暂不支持在线打开");
        return;
    }
    const viewerUrl = "/viewer?path=" + encodeURIComponent(item.path) + "&type=" + encodeURIComponent(item.preview_type);
    window.open(viewerUrl, "_blank", "noopener");
}
function downloadSingleFile(path) {
    window.location.href = "/api/download?path=" + encodeURIComponent(path);
}
async function downloadSevenZipArchive(paths) {
    if (!paths || paths.length === 0) return;
    try {
        showToast("正在使用最高压缩级别生成 7Z…");
        const response = await postJson("/api/prepare-7z", {paths: paths});
        const responseBody = await response.json().catch(function () { return {}; });
        if (!response.ok) throw new Error(responseBody.detail || "7Z 压缩失败");
        const token = responseBody.token || "";
        const downloadName = responseBody.download_name || "download.7z";
        if (!token) throw new Error("服务器没有返回临时下载令牌");
        const downloadUrl = "/api/download-7z?token=" + encodeURIComponent(token) + "&name=" + encodeURIComponent(downloadName);
        document.querySelector('iframe[name="archiveDownloadFrame"]').src = downloadUrl;
        showToast("7Z 已生成，开始下载");
    } catch (error) {
        showToast(error.message);
    }
}
async function deduplicateFolder(item) {
    if (!item || item.type !== "folder") return;
    const confirmed = window.confirm(
        "将递归扫描这个文件夹内的全部文件并计算完整 SHA-256。\n\n" +
        "内容重复的文件只保留路径排序后的第一份，其余会永久删除；去重后所有没有任何文件的空文件夹（包括多层空文件夹）也会删除。\n\n" +
        "此操作无法撤销。确定继续？"
    );
    if (!confirmed) return;
    try {
        showToast("正在计算哈希并清理，文件较多时请等待…");
        const response = await postJson("/api/deduplicate-folder", {path: item.path});
        const responseBody = await response.json().catch(function () { return {}; });
        if (!response.ok) throw new Error(responseBody.detail || "去重清理失败");
        const deletedFiles = Number(responseBody.duplicate_files_deleted || 0);
        const deletedFolders = Number(responseBody.empty_folders_deleted || 0);
        const scannedFiles = Number(responseBody.scanned_files || 0);
        const freedBytes = Number(responseBody.duplicate_bytes_deleted || 0);
        showToast(`去重完成：扫描 ${scannedFiles} 个文件，删除 ${deletedFiles} 个重复文件、${deletedFolders} 个空文件夹，释放 ${formatBytes(freedBytes)}`);
        await loadDirectory(applicationState.currentPath);
    } catch (error) {
        showToast(error.message);
    }
}

async function archiveMediaFolder(item) {
    if (!item || item.type !== "folder") return;
    const confirmed = window.confirm(
        "将先递归扫描这个文件夹内的全部文件，再仅按扩展名把图片、视频、音频移动到该文件夹根目录下的 图片 / 视频 / 音频 三个目录。\n\n" +
        "同名文件不会覆盖，会自动重命名；其他类型文件和原有文件夹结构不处理。确定继续？"
    );
    if (!confirmed) return;
    try {
        showToast("正在扫描并归档媒体文件…");
        const response = await postJson("/api/archive-media-folder", {path: item.path});
        const responseBody = await response.json().catch(function () { return {}; });
        if (!response.ok) throw new Error(responseBody.detail || "归档失败");
        const scannedFiles = Number(responseBody.scanned_files || 0);
        const movedFiles = Number(responseBody.moved_files || 0);
        const imageFiles = Number(responseBody.image_files_moved || 0);
        const videoFiles = Number(responseBody.video_files_moved || 0);
        const audioFiles = Number(responseBody.audio_files_moved || 0);
        const renamedFiles = Number(responseBody.renamed_files || 0);
        showToast(`归档完成：扫描 ${scannedFiles} 个文件，移动 ${movedFiles} 个（图片 ${imageFiles}、视频 ${videoFiles}、音频 ${audioFiles}），重名自动改名 ${renamedFiles} 个`);
        await loadDirectory(applicationState.currentPath);
    } catch (error) {
        showToast(error.message);
    }
}

async function createShareLink(item) {
    try {
        const response = await postJson("/api/share", {path: item.path});
        const responseBody = await response.json().catch(function () { return {}; });
        if (!response.ok) throw new Error(responseBody.detail || "创建分享失败");
        const shareUrl = responseBody.url || "";
        if (!shareUrl) throw new Error("服务器没有返回分享链接");
        let copied = false;
        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(shareUrl);
                copied = true;
            }
        } catch (error) {}
        if (copied) showToast("分享链接已复制");
        else window.prompt("分享链接", shareUrl);
    } catch (error) {
        showToast(error.message);
    }
}
async function renameItem(item) {
    const newName = window.prompt("新名称", item.name);
    if (!newName || newName === item.name) return;
    const formData = new FormData();
    formData.append("path", item.path);
    formData.append("new_name", newName);
    try {
        const response = await fetch("/api/rename", {method: "POST", body: formData});
        if (!response.ok) {
            const errorBody = await response.json().catch(function () { return {}; });
            throw new Error(errorBody.detail || "重命名失败");
        }
        await loadDirectory();
    } catch (error) {
        showToast(error.message);
    }
}
async function deleteSelectedItems(paths) {
    try {
        const response = await postJson("/api/delete", {paths: paths});
        if (!response.ok) {
            const errorBody = await response.json().catch(function () { return {}; });
            throw new Error(errorBody.detail || "删除失败");
        }
        showToast("已删除");
        await loadDirectory();
    } catch (error) {
        showToast(error.message);
    }
}
homeButton.addEventListener("click", function () {
    loadDirectory("");
});
logoutButton.addEventListener("click", async function () {
    try {
        await fetch("/api/auth/logout", {method: "POST"});
    } finally {
        window.location.href = "/login";
    }
});
fileInput.addEventListener("change", async function () {
    await uploadFiles(fileInput.files);
    fileInput.value = "";
});
fileViewport.addEventListener("keydown", function (event) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
        event.preventDefault();
        applicationState.selectedPaths.clear();
        for (const item of applicationState.items) applicationState.selectedPaths.add(item.path);
        refreshSelectionStyles();
    }
    if (event.key === "Delete" && applicationState.selectedPaths.size) {
        const selectedPaths = [];
        for (const selectedPath of applicationState.selectedPaths) selectedPaths.push(selectedPath);
        deleteSelectedItems(selectedPaths);
    }
    if (event.key === "Escape") {
        applicationState.selectedPaths.clear();
        refreshSelectionStyles();
        hideContextMenu();
    }
    if (event.key === "Backspace" && !event.ctrlKey && !event.metaKey && applicationState.currentPath) {
        event.preventDefault();
        loadDirectory(getParentPath(applicationState.currentPath));
    }
});
loadCurrentUser();
loadDirectory("");
</script>
</body>
</html>"""

SHARE_HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>分享</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet" integrity="sha384-sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB" crossorigin="anonymous">
<style>
__MAIN_STYLE__
#readonlyBadge{font-size:11px;color:var(--gold);padding:3px 7px;border-radius:999px;background:var(--accent-soft);white-space:nowrap}
</style>
</head>
<body>
<header id="topBar">
<button id="homeButton" class="btn btn-sm toolbar-button" title="分享根目录"><svg class="toolbar-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 10.5 12 3l9 7.5"/><path d="M5.5 9.5V21h13V9.5"/><path d="M9.5 21v-6h5v6"/></svg><span>主页</span></button>
<div id="breadcrumbs"></div><span id="selectionCount"></span><span id="readonlyBadge">只读</span>
</header>
<main id="fileViewport" tabindex="0"><div id="fileGrid"></div><div id="emptyState"></div></main>
<div id="selectionMarquee"></div><div id="contextMenu"></div><div id="toastMessage"></div>
<iframe name="shareArchiveDownloadFrame" title="7Z 下载" hidden></iframe>
<script>
const shareToken = decodeURIComponent(window.location.pathname.split("/").filter(Boolean).pop() || ""); // 分享令牌
const shareApiBase = "/api/public-share/" + encodeURIComponent(shareToken);                              // 公开 API 根路径
const applicationState = {currentPath:"", rootName:"分享", rootType:"folder", items:[], selectedPaths:new Set(), selectionAnchor:null, marquee:null, suppressNextClick:false}; // 页面状态
const fileGrid = document.getElementById("fileGrid");                       // 文件网格
const fileViewport = document.getElementById("fileViewport");               // 主文件区域
const breadcrumbs = document.getElementById("breadcrumbs");                 // 面包屑
const selectionMarquee = document.getElementById("selectionMarquee");       // 框选区域
const contextMenu = document.getElementById("contextMenu");                 // 右键菜单
const toastMessage = document.getElementById("toastMessage");               // 提示
const emptyState = document.getElementById("emptyState");                   // 空目录提示
const homeButton = document.getElementById("homeButton");                   // 根目录
const selectionCount = document.getElementById("selectionCount");           // 选择数量
function joinPaths(firstPath, secondPath) {
    const pathParts = [];
    if (firstPath) pathParts.push(firstPath);
    if (secondPath) pathParts.push(secondPath);
    return pathParts.join("/").replace(/\/+/g, "/");
}
function getParentPath(path) {
    const pathParts = path.split("/").filter(Boolean);
    pathParts.pop();
    return pathParts.join("/");
}
function showToast(message) {
    toastMessage.textContent = message;
    toastMessage.classList.add("show");
    clearTimeout(showToast.timeoutId);
    showToast.timeoutId = setTimeout(function () { toastMessage.classList.remove("show"); }, 1900);
}
async function fetchJson(url, options) {
    const response = await fetch(url, options || {});
    if (!response.ok) {
        let errorMessage = response.statusText || "请求失败";
        try {
            const errorBody = await response.json();
            if (errorBody.detail) errorMessage = errorBody.detail;
        } catch (error) {}
        throw new Error(errorMessage);
    }
    return response.json();
}
function postJson(url, body) {
    return fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
}
async function loadDirectory(path) {
    const requestedPath = path === undefined ? applicationState.currentPath : path;
    try {
        const data = await fetchJson(shareApiBase + "/list?path=" + encodeURIComponent(requestedPath));
        applicationState.currentPath = data.path;
        applicationState.rootName = data.root_name || "分享";
        applicationState.rootType = data.root_type || "folder";
        applicationState.items = data.items || [];
        applicationState.selectedPaths.clear();
        applicationState.selectionAnchor = null;
        document.title = applicationState.rootName;
        renderBreadcrumbs();
        renderItems();
        updateSelectionCount();
    } catch (error) {
        fileGrid.innerHTML = "";
        emptyState.style.display = "block";
        emptyState.innerHTML = "<div>" + escapeHtml(error.message) + "</div>";
        showToast(error.message);
    }
}
function renderBreadcrumbs() {
    breadcrumbs.innerHTML = "";
    const rootButton = document.createElement("button");
    rootButton.className = "breadcrumb-item-button";
    rootButton.textContent = applicationState.rootName;
    rootButton.dataset.path = "";
    breadcrumbs.append(rootButton);
    let accumulatedPath = "";
    const pathParts = applicationState.currentPath.split("/").filter(Boolean);
    for (const pathPart of pathParts) {
        const separator = document.createElement("span");
        separator.className = "breadcrumb-separator";
        separator.textContent = "›";
        breadcrumbs.append(separator);
        accumulatedPath = joinPaths(accumulatedPath, pathPart);
        const pathButton = document.createElement("button");
        pathButton.className = "breadcrumb-item-button";
        pathButton.textContent = pathPart;
        pathButton.dataset.path = accumulatedPath;
        breadcrumbs.append(pathButton);
    }
    breadcrumbs.scrollLeft = breadcrumbs.scrollWidth;
}
function escapeHtml(text) {
    const element = document.createElement("div");
    element.textContent = String(text || "");
    return element.innerHTML;
}
function getFileExtension(fileName) {
    const lastDotIndex = fileName.lastIndexOf(".");
    if (lastDotIndex <= 0 || lastDotIndex === fileName.length - 1) return "FILE";
    const extension = fileName.slice(lastDotIndex + 1).toUpperCase();
    return extension.length > 5 ? extension.slice(0, 5) : extension;
}
function createFolderIcon() {
    return `<svg class="file-svg" viewBox="0 0 80 68" aria-hidden="true"><path d="M5 15a7 7 0 0 1 7-7h21l8 9h27a7 7 0 0 1 7 7v31a8 8 0 0 1-8 8H13a8 8 0 0 1-8-8z" fill="#cc3300"/><path d="M5 25h70v30a8 8 0 0 1-8 8H13a8 8 0 0 1-8-8z" fill="#ff6a00"/><path d="M9 28h62" stroke="#ffcc00" stroke-width="2" opacity=".8"/></svg>`;
}
function createFileIcon(fileName) {
    const extension = getFileExtension(fileName);
    return `<svg class="file-svg" viewBox="0 0 72 78" aria-hidden="true"><path d="M13 4h31l15 15v50a5 5 0 0 1-5 5H13a5 5 0 0 1-5-5V9a5 5 0 0 1 5-5z" fill="#ffcc00" stroke="#cc5500" stroke-width="1.5"/><path d="M44 4v13a3 3 0 0 0 3 3h12" fill="#ff8a00"/><path d="M44 4l15 16H47a3 3 0 0 1-3-3z" fill="#ff5a00"/><path d="M17 33h33M17 41h25M17 49h29" stroke="#cc6600" stroke-width="3" stroke-linecap="round"/></svg><span class="file-extension">${extension}</span>`;
}
function renderItems() {
    fileGrid.innerHTML = "";
    emptyState.style.display = applicationState.items.length ? "none" : "block";
    if (!applicationState.items.length) {
        emptyState.innerHTML = `<svg viewBox="0 0 80 68"><path d="M5 15a7 7 0 0 1 7-7h21l8 9h27a7 7 0 0 1 7 7v31a8 8 0 0 1-8 8H13a8 8 0 0 1-8-8z" fill="#cc3300"/><path d="M5 25h70v30a8 8 0 0 1-8 8H13a8 8 0 0 1-8-8z" fill="#ff6a00"/></svg><div>这个文件夹是空的</div>`;
        return;
    }
    for (const item of applicationState.items) {
        const itemElement = document.createElement("div");
        itemElement.className = "file-item";
        itemElement.dataset.path = item.path;
        itemElement.dataset.type = item.type;
        itemElement.draggable = false;
        const iconHtml = item.type === "folder" ? createFolderIcon() : createFileIcon(item.name);
        itemElement.innerHTML = `<div class="file-icon-wrap">${iconHtml}</div><div class="file-name"></div><div class="file-meta"></div>`;
        itemElement.querySelector(".file-name").textContent = item.name;
        itemElement.querySelector(".file-meta").textContent = item.size_text;
        if (applicationState.selectedPaths.has(item.path)) itemElement.classList.add("selected");
        fileGrid.append(itemElement);
    }
}
function updateSelectionCount() {
    const selectedCountValue = applicationState.selectedPaths.size;
    if (!selectedCountValue) {
        selectionCount.style.display = "none";
        selectionCount.textContent = "";
        return;
    }
    selectionCount.style.display = "inline-block";
    selectionCount.textContent = `已选择 ${selectedCountValue} 项`;
}
function refreshSelectionStyles() {
    const itemElements = document.querySelectorAll(".file-item");
    for (const itemElement of itemElements) itemElement.classList.toggle("selected", applicationState.selectedPaths.has(itemElement.dataset.path));
    updateSelectionCount();
}
function selectOnly(path) {
    applicationState.selectedPaths.clear();
    applicationState.selectedPaths.add(path);
    applicationState.selectionAnchor = path;
    refreshSelectionStyles();
}
function toggleSelection(path) {
    if (applicationState.selectedPaths.has(path)) applicationState.selectedPaths.delete(path);
    else applicationState.selectedPaths.add(path);
    applicationState.selectionAnchor = path;
    refreshSelectionStyles();
}
function selectRange(path) {
    const orderedPaths = [];
    for (const item of applicationState.items) orderedPaths.push(item.path);
    const anchorIndex = orderedPaths.indexOf(applicationState.selectionAnchor);
    const targetIndex = orderedPaths.indexOf(path);
    if (anchorIndex < 0 || targetIndex < 0) {
        selectOnly(path);
        return;
    }
    applicationState.selectedPaths.clear();
    const startIndex = Math.min(anchorIndex, targetIndex);
    const endIndex = Math.max(anchorIndex, targetIndex);
    for (let index = startIndex; index <= endIndex; index = index + 1) applicationState.selectedPaths.add(orderedPaths[index]);
    refreshSelectionStyles();
}
fileGrid.addEventListener("click", function (event) {
    const itemElement = event.target.closest(".file-item");
    if (!itemElement) return;
    const itemPath = itemElement.dataset.path;
    if (event.ctrlKey || event.metaKey) {
        toggleSelection(itemPath);
        return;
    }
    if (event.shiftKey) {
        selectRange(itemPath);
        return;
    }
    let clickedItem = null;
    for (const item of applicationState.items) {
        if (item.path === itemPath) {
            clickedItem = item;
            break;
        }
    }
    if (!clickedItem) return;
    if (clickedItem.type === "folder") {
        loadDirectory(itemPath);
        return;
    }
    downloadSingleFile(itemPath);
});
fileViewport.addEventListener("click", function (event) {
    if (event.target !== fileViewport && event.target !== fileGrid) return;
    applicationState.selectedPaths.clear();
    refreshSelectionStyles();
});
fileViewport.addEventListener("pointerdown", function (event) {
    if (event.button !== 0) return;
    if (event.target.closest(".file-item") || event.target.closest("#contextMenu")) return;
    hideContextMenu();
    const baseSelection = new Set();
    if (event.ctrlKey || event.metaKey) {
        for (const selectedPath of applicationState.selectedPaths) baseSelection.add(selectedPath);
    }
    applicationState.marquee = {startX:event.clientX, startY:event.clientY, baseSelection:baseSelection, moved:false};
    selectionMarquee.style.display = "block";
    selectionMarquee.style.left = event.clientX + "px";
    selectionMarquee.style.top = event.clientY + "px";
    selectionMarquee.style.width = "0px";
    selectionMarquee.style.height = "0px";
    if (fileViewport.setPointerCapture) fileViewport.setPointerCapture(event.pointerId);
    event.preventDefault();
});
fileViewport.addEventListener("pointermove", function (event) {
    const marqueeState = applicationState.marquee;
    if (!marqueeState) return;
    const left = Math.min(marqueeState.startX, event.clientX);
    const top = Math.min(marqueeState.startY, event.clientY);
    const right = Math.max(marqueeState.startX, event.clientX);
    const bottom = Math.max(marqueeState.startY, event.clientY);
    if (Math.abs(event.clientX - marqueeState.startX) > 3 || Math.abs(event.clientY - marqueeState.startY) > 3) marqueeState.moved = true;
    selectionMarquee.style.left = left + "px";
    selectionMarquee.style.top = top + "px";
    selectionMarquee.style.width = right - left + "px";
    selectionMarquee.style.height = bottom - top + "px";
    const newSelection = new Set();
    for (const selectedPath of marqueeState.baseSelection) newSelection.add(selectedPath);
    const itemElements = document.querySelectorAll(".file-item");
    for (const itemElement of itemElements) {
        const itemRectangle = itemElement.getBoundingClientRect();
        const intersects = itemRectangle.right >= left && itemRectangle.left <= right && itemRectangle.bottom >= top && itemRectangle.top <= bottom;
        if (intersects) newSelection.add(itemElement.dataset.path);
    }
    applicationState.selectedPaths = newSelection;
    refreshSelectionStyles();
});
fileViewport.addEventListener("pointerup", function () {
    if (!applicationState.marquee) return;
    const marqueeMoved = applicationState.marquee.moved;
    applicationState.marquee = null;
    selectionMarquee.style.display = "none";
    if (marqueeMoved) applicationState.suppressNextClick = true;
});
fileViewport.addEventListener("click", function (event) {
    if (!applicationState.suppressNextClick) return;
    applicationState.suppressNextClick = false;
    event.preventDefault();
    event.stopPropagation();
}, true);
fileViewport.addEventListener("pointercancel", function () {
    if (!applicationState.marquee) return;
    applicationState.marquee = null;
    selectionMarquee.style.display = "none";
});
breadcrumbs.addEventListener("click", function (event) {
    const breadcrumbButton = event.target.closest(".breadcrumb-item-button");
    if (breadcrumbButton) loadDirectory(breadcrumbButton.dataset.path);
});
function menuIcon(iconName) {
    const icons = {
        preview:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6z"/><circle cx="12" cy="12" r="2.7"/></svg>',
        download:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3v12"/><path d="m7.5 10.5 4.5 4.5 4.5-4.5"/><path d="M4 20h16"/></svg>',
        archive:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M5 3h14v18H5z"/><path d="M9 3v4h6V3M10 11h4M10 15h4"/></svg>'
    };
    return icons[iconName] || "";
}
function addContextMenuItem(label, iconName, clickHandler) {
    const menuItem = document.createElement("div");
    menuItem.className = "context-menu-item";
    menuItem.innerHTML = `<span class="context-menu-icon">${menuIcon(iconName)}</span><span>${label}</span>`;
    menuItem.addEventListener("click", async function () {
        hideContextMenu();
        await clickHandler();
    });
    contextMenu.append(menuItem);
}
function showContextMenu(x, y, paths) {
    const selectedItems = [];
    for (const selectedPath of paths) {
        for (const item of applicationState.items) {
            if (item.path === selectedPath) {
                selectedItems.push(item);
                break;
            }
        }
    }
    const isSingleSelection = selectedItems.length === 1;
    const singleItem = isSingleSelection ? selectedItems[0] : null;
    contextMenu.innerHTML = "";
    if (isSingleSelection && singleItem.type === "file" && singleItem.preview_type !== "none") {
        addContextMenuItem(getPreviewMenuLabel(singleItem.preview_type), "preview", function () { openOnlineViewer(singleItem); });
    }
    if (isSingleSelection && singleItem.type === "file") addContextMenuItem("下载原文件", "download", function () { downloadSingleFile(singleItem.path); });
    if (!isSingleSelection || singleItem.type === "folder") {
        const archiveLabel = isSingleSelection ? "下载为 7Z" : "多选下载为 7Z";
        addContextMenuItem(archiveLabel, "archive", function () { downloadSevenZipArchive(paths); });
    }
    if (!contextMenu.children.length) return;
    contextMenu.style.display = "block";
    const menuWidth = contextMenu.offsetWidth;
    const menuHeight = contextMenu.offsetHeight;
    contextMenu.style.left = Math.min(x, window.innerWidth - menuWidth - 8) + "px";
    contextMenu.style.top = Math.min(y, window.innerHeight - menuHeight - 8) + "px";
}
function hideContextMenu() {
    contextMenu.style.display = "none";
}
fileViewport.addEventListener("contextmenu", function (event) {
    event.preventDefault();
    const itemElement = event.target.closest(".file-item");
    if (!itemElement) {
        hideContextMenu();
        return;
    }
    const itemPath = itemElement.dataset.path;
    if (!applicationState.selectedPaths.has(itemPath)) selectOnly(itemPath);
    const selectedPaths = [];
    for (const selectedPath of applicationState.selectedPaths) selectedPaths.push(selectedPath);
    showContextMenu(event.clientX, event.clientY, selectedPaths);
});
document.addEventListener("pointerdown", function (event) {
    if (!event.target.closest("#contextMenu")) hideContextMenu();
});
function getPreviewMenuLabel(previewType) {
    if (previewType === "text") return "在线查看文本";
    if (previewType === "image") return "在线查看图片";
    if (previewType === "video") return "在线播放视频";
    if (previewType === "audio") return "在线播放音频";
    return "在线打开";
}
function openOnlineViewer(item) {
    if (!item || item.preview_type === "none") {
        showToast("该格式暂不支持在线打开");
        return;
    }
    const viewerUrl = "/share-viewer?share=" + encodeURIComponent(shareToken) + "&path=" + encodeURIComponent(item.path) + "&type=" + encodeURIComponent(item.preview_type);
    window.open(viewerUrl, "_blank", "noopener");
}
function downloadSingleFile(path) {
    window.location.href = shareApiBase + "/download?path=" + encodeURIComponent(path);
}
async function downloadSevenZipArchive(paths) {
    if (!paths || paths.length === 0) return;
    try {
        showToast("正在使用最高压缩级别生成 7Z…");
        const response = await postJson(shareApiBase + "/prepare-7z", {paths:paths});
        const responseBody = await response.json().catch(function () { return {}; });
        if (!response.ok) throw new Error(responseBody.detail || "7Z 压缩失败");
        const token = responseBody.token || "";
        const downloadName = responseBody.download_name || "share.7z";
        if (!token) throw new Error("服务器没有返回临时下载令牌");
        const downloadUrl = shareApiBase + "/download-7z?token=" + encodeURIComponent(token) + "&name=" + encodeURIComponent(downloadName);
        document.querySelector('iframe[name="shareArchiveDownloadFrame"]').src = downloadUrl;
        showToast("7Z 已生成，开始下载");
    } catch (error) {
        showToast(error.message);
    }
}
homeButton.addEventListener("click", function () { loadDirectory(""); });
fileViewport.addEventListener("keydown", function (event) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
        event.preventDefault();
        applicationState.selectedPaths.clear();
        for (const item of applicationState.items) applicationState.selectedPaths.add(item.path);
        refreshSelectionStyles();
    }
    if (event.key === "Escape") {
        applicationState.selectedPaths.clear();
        refreshSelectionStyles();
        hideContextMenu();
    }
    if (event.key === "Backspace" && !event.ctrlKey && !event.metaKey && applicationState.currentPath) {
        event.preventDefault();
        loadDirectory(getParentPath(applicationState.currentPath));
    }
});
loadDirectory("");
</script>
</body>
</html>"""
MAIN_STYLE = HTML.split("<style>", 1)[1].split("</style>", 1)[0]
SHARE_HTML = SHARE_HTML_TEMPLATE.replace("__MAIN_STYLE__", MAIN_STYLE)

SHARE_MANAGEMENT_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>分享管理</title>
<style>
:root{color-scheme:dark;--bg:#180000;--bar:#210000;--line:#660000;--text:#ffcc00;--muted:#cc9900;--accent2:#ffcc00;--danger:#ff3b00}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%;background:var(--bg);color:var(--text);font-family:Arial,"Microsoft YaHei",sans-serif}
body{min-height:100vh}
header{height:46px;display:flex;align-items:center;gap:10px;padding:0 14px;background:var(--bar);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
header strong{font-size:15px;font-weight:700;color:var(--accent2)}
header a{margin-left:auto;color:var(--text);text-decoration:none;font-size:13px;padding:6px 9px;border-radius:6px}
header a:hover{background:#330000;color:var(--accent2)}
main{width:100%;max-width:1100px;margin:0 auto;padding:10px 14px 28px}
ul{list-style:none;margin:0;padding:0}
.share-row{min-height:58px;display:flex;align-items:center;gap:14px;padding:9px 4px;border-bottom:1px solid var(--line)}
.share-info{min-width:0;flex:1}
.share-path{font-size:14px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.share-meta{margin-top:4px;font-size:12px;color:var(--muted);display:flex;gap:12px;flex-wrap:wrap}
.share-meta .missing{color:var(--danger)}
.actions{display:flex;align-items:center;gap:6px;flex:0 0 auto}
button{border:0;background:transparent;color:var(--accent2);padding:7px 9px;border-radius:6px;cursor:pointer;font-size:13px}
button:hover{background:#330000}
button.danger{color:#ff3b00}
button.danger:hover{background:#4d0d00}
#empty{padding:34px 4px;color:var(--muted);font-size:14px}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(8px);opacity:0;pointer-events:none;background:#330000;color:var(--accent2);padding:9px 13px;border-radius:8px;transition:.16s ease;font-size:13px}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
@media(max-width:680px){main{padding-left:10px;padding-right:10px}.share-row{align-items:flex-start;flex-direction:column;gap:5px;padding:10px 2px}.actions{width:100%;justify-content:flex-end}.share-path{white-space:normal;overflow-wrap:anywhere}}
</style>
</head>
<body>
<header><strong>分享管理</strong><a href="/">返回文件管理</a></header>
<main><ul id="shareList"></ul><p id="empty" hidden>暂无分享</p></main>
<p id="toast"></p>
<script>
const shareList = document.getElementById("shareList");
const emptyState = document.getElementById("empty");
const toast = document.getElementById("toast");
function showToast(message){toast.textContent=message;toast.classList.add("show");clearTimeout(showToast.timer);showToast.timer=setTimeout(function(){toast.classList.remove("show")},1800)}
async function fetchJson(url,options){const response=await fetch(url,options||{});if(response.status===401){window.location.href="/login";throw new Error("登录已失效")}let body={};try{body=await response.json()}catch(error){}if(!response.ok)throw new Error(body.detail||response.statusText||"请求失败");return body}
function typeText(type){return type==="folder"?"文件夹":"文件"}
async function copyText(text){try{await navigator.clipboard.writeText(text);return true}catch(error){}const input=document.createElement("textarea");input.value=text;input.style.position="fixed";input.style.opacity="0";document.body.append(input);input.select();let ok=false;try{ok=document.execCommand("copy")}catch(error){}input.remove();return ok}
function renderShares(shares){shareList.innerHTML="";emptyState.hidden=shares.length!==0;for(const item of shares){const row=document.createElement("li");row.className="share-row";const info=document.createElement("section");info.className="share-info";const path=document.createElement("span");path.className="share-path";path.textContent=item.path||"/";path.title=item.path||"/";const meta=document.createElement("small");meta.className="share-meta";const type=document.createElement("span");type.textContent=typeText(item.type);const created=document.createElement("span");created.textContent=item.created_text||"";meta.append(type,created);if(!item.exists){const missing=document.createElement("span");missing.className="missing";missing.textContent="源内容已不存在";meta.append(missing)}info.append(path,meta);const actions=document.createElement("nav");actions.className="actions";const openButton=document.createElement("button");openButton.type="button";openButton.textContent="打开";openButton.addEventListener("click",function(){window.open(item.url,"_blank","noopener")});const copyButton=document.createElement("button");copyButton.type="button";copyButton.textContent="复制";copyButton.addEventListener("click",async function(){const ok=await copyText(item.url);if(ok)showToast("分享链接已复制");else window.prompt("分享链接",item.url)});const cancelButton=document.createElement("button");cancelButton.type="button";cancelButton.className="danger";cancelButton.textContent="取消分享";cancelButton.addEventListener("click",async function(){if(!window.confirm("确定取消这个分享吗？取消后原分享链接将立即失效。"))return;try{await fetchJson("/api/shares/"+encodeURIComponent(item.id),{method:"DELETE"});showToast("分享已取消");await loadShares()}catch(error){showToast(error.message)}});actions.append(openButton,copyButton,cancelButton);row.append(info,actions);shareList.append(row)}}
async function loadShares(){try{const data=await fetchJson("/api/shares");renderShares(data.shares||[])}catch(error){showToast(error.message)}}
loadShares();
</script>
</body>
</html>"""

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
