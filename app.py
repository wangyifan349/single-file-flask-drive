#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
淡橘云盘 Flask 单文件版
========================

这是一个轻量级私有云盘 MVP，所有后端代码、HTML 模板、CSS 和 JavaScript 都合并在本文件中，
方便直接部署和后续维护。界面采用淡橘色 Bootstrap 风格，核心交互尽量使用右键菜单。

主要功能：
- 用户注册、登录、退出；密码使用随机盐 + SHA3-256 摘要存储。
- 层级目录、文件/文件夹上传、列出、下载、重命名、删除、拖拽移动。
- 文件和文件夹分享；分享管理页面可复制链接和取消分享。
- 分享文件夹支持浏览子目录，文件左键下载，文件夹左键进入。
- 音频/视频支持在线播放；分享页面也支持右键播放。
- 纯文本文件支持在线编辑；分享页面支持只读查看。
- 文本读取会尽量识别 BOM 和常见编码，保存时默认沿用检测到的编码与换行风格。
- 文件夹下载会生成 .7z，使用 py7zr + LZMA2 高压缩，并写入临时文件，不把压缩包整体加载进内存。
- 临时压缩包默认保留 24 小时，后续请求会自动清理过期文件。

运行方式：
    pip install flask py7zr charset-normalizer
    python app.py

打开：
    http://127.0.0.1:5000

可选环境变量：
    SECRET_KEY                 Flask session 密钥，生产环境务必设置。
    DRIVE_DATA_DIR             数据目录，默认是当前 app.py 同级目录下的 data。
    MAX_CONTENT_LENGTH         单次请求最大上传大小，默认 1GB。
    ARCHIVE_RETENTION_SECONDS  临时 7z 保留秒数，默认 86400 秒。

生产环境建议：
- 使用 gunicorn/uwsgi 等 WSGI 服务运行，不要直接使用 Flask debug server。
- 把 SECRET_KEY 设置为强随机字符串。
- 如果面向公网，建议将密码算法升级为 Argon2id 或 bcrypt，并加 HTTPS。
"""

import datetime as dt
import hashlib
import mimetypes
import os
import sqlite3
import uuid
from pathlib import PurePosixPath, Path

import py7zr
from flask import (
    Flask, Response, abort, flash, g, jsonify, redirect, render_template,
    request, send_file, session, url_for
)
from jinja2 import DictLoader

try:
    from charset_normalizer import from_bytes as detect_character_sets
except ImportError:  # pragma: no cover - runtime fallback if dependency is missing
    detect_character_sets = None


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DRIVE_DATA_DIR", BASE_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
TMP_ARCHIVE_DIR = DATA_DIR / "tmp_archives"
DB_PATH = DATA_DIR / "drive.db"
ARCHIVE_RETENTION_SECONDS = int(os.environ.get("ARCHIVE_RETENTION_SECONDS", 24 * 60 * 60))

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
TMP_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_CONTENT_LENGTH", 1024 * 1024 * 1024))

# -----------------------------
# Embedded templates and static assets
# -----------------------------

TEMPLATE_FILES = {'base.html': '<!doctype html>\n'
              '<html lang="zh-CN">\n'
              '<head>\n'
              '  <meta charset="utf-8">\n'
              '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
              '  <title>{% block title %}淡橘云盘{% endblock %}</title>\n'
              '  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" '
              'rel="stylesheet">\n'
              '  <link rel="stylesheet" href="{{ url_for(\'static\', filename=\'app.css\') }}">\n'
              '</head>\n'
              '<body>\n'
              '  <main class="page-shell">\n'
              '    {% block body %}{% endblock %}\n'
              '  </main>\n'
              '  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>\n'
              '  {% block scripts %}{% endblock %}\n'
              '</body>\n'
              '</html>\n',
 'drive.html': '{% extends "base.html" %}\n'
               '{% block title %}我的云盘 - 淡橘云盘{% endblock %}\n'
               '{% block body %}\n'
               '<div class="drive-shell" data-current-folder-id="{{ current_folder_id or \'\' }}">\n'
               '  <header class="drive-header">\n'
               '    <div class="drive-title-area">\n'
               '      <div class="small text-muted">淡橘云盘</div>\n'
               '      <nav aria-label="breadcrumb">\n'
               '        <ol class="breadcrumb mb-0">\n'
               '          {% for c in crumbs %}\n'
               '            <li class="breadcrumb-item {% if loop.last %}active{% endif %}">\n'
               '              {% if not loop.last %}\n'
               '                <a class="breadcrumb-target" href="{{ url_for(\'drive\', folder_id=c.id) if c.id else '
               'url_for(\'drive\') }}" data-folder-id="{{ c.id or \'\' }}">{{ c.name }}</a>\n'
               '              {% else %}\n'
               '                <span class="breadcrumb-target" data-folder-id="{{ c.id or \'\' }}">{{ c.name '
               '}}</span>\n'
               '              {% endif %}\n'
               '            </li>\n'
               '          {% endfor %}\n'
               '        </ol>\n'
               '      </nav>\n'
               '    </div>\n'
               '    <div class="drive-nav">\n'
               '      <a class="nav-pill" href="{{ url_for(\'shares_page\') }}">分享管理</a>\n'
               '      <a class="nav-pill" href="{{ url_for(\'logout\') }}">退出</a>\n'
               '    </div>\n'
               '  </header>\n'
               '\n'
               '  <section id="dropArea" class="file-board" aria-label="文件列表">\n'
               '    {% for e in entries %}\n'
               '      <div class="entry-card {{ e.kind }}-entry" draggable="true" data-id="{{ e.id }}" data-kind="{{ '
               'e.kind }}" data-name="{{ e.name }}" data-media-kind="{{ media_kind(e.name) or \'\' }}" '
               'data-is-text="{{ \'1\' if is_text_file(e.name) else \'0\' }}">\n'
               '        <div class="entry-icon {{ e.kind }} {{ media_kind(e.name) or (\'text\' if is_text_file(e.name) '
               'else \'\') }}">{{ entry_icon(e.kind, e.name) }}</div>\n'
               '        <div class="entry-main">\n'
               '          <div class="entry-name" title="{{ e.name }}">{{ e.name }}</div>\n'
               '          <div class="entry-meta">\n'
               "            {% if e.kind == 'folder' %}\n"
               '              <span class="kind-badge folder-badge">文件夹</span><span>左键进入</span>\n'
               '            {% else %}\n'
               '              <span class="kind-badge file-badge">{{ kind_label(e.kind, e.name) }}</span><span>{{ '
               'file_size_label(e.size) }} · 左键下载</span>\n'
               '            {% endif %}\n'
               '          </div>\n'
               '        </div>\n'
               '      </div>\n'
               '    {% else %}\n'
               '      <div class="empty-hint">空白处右键：上传文件、上传文件夹或新建文件夹。</div>\n'
               '    {% endfor %}\n'
               '  </section>\n'
               '\n'
               '  <input id="fileInput" type="file" multiple hidden>\n'
               '  <input id="folderInput" type="file" webkitdirectory directory multiple hidden>\n'
               '\n'
               '  <div id="contextMenu" class="context-menu shadow-sm" role="menu"></div>\n'
               '  <div id="toastHost" class="toast-host"></div>\n'
               '</div>\n'
               '{% endblock %}\n'
               '{% block scripts %}\n'
               '<script src="{{ url_for(\'static\', filename=\'app.js\') }}"></script>\n'
               '{% endblock %}\n',
 'login.html': '{% extends "base.html" %}\n'
               '{% block title %}登录 - 淡橘云盘{% endblock %}\n'
               '{% block body %}\n'
               '<section class="auth-layout">\n'
               '  <div class="auth-hero">\n'
               '    <div class="brand-dot mb-4"></div>\n'
               '    <div class="eyebrow">Flask Cloud Drive</div>\n'
               '    <h1>登录淡橘云盘</h1>\n'
               '    <p>用一个尽量轻量的界面访问文件、目录和分享链接。页面留白更大，减少盒子边框感。</p>\n'
               '  </div>\n'
               '  <div class="auth-panel">\n'
               '    {% for msg in get_flashed_messages() %}<div class="alert alert-warning py-2">{{ msg }}</div>{% '
               'endfor %}\n'
               '    <form method="post" class="auth-form">\n'
               '      <label class="form-label">用户名</label>\n'
               '      <input class="form-control form-control-lg" name="username" autocomplete="username" required>\n'
               '      <label class="form-label mt-4">密码</label>\n'
               '      <input class="form-control form-control-lg" name="password" type="password" '
               'autocomplete="current-password" required>\n'
               '      <button class="btn btn-orange btn-lg w-100 mt-4" type="submit">登录</button>\n'
               '    </form>\n'
               '    <p class="mt-4 mb-0 text-muted">还没有账号？<a href="{{ url_for(\'register\') }}">注册</a></p>\n'
               '  </div>\n'
               '</section>\n'
               '{% endblock %}\n',
 'media.html': '{% extends "base.html" %}\n'
               '{% block title %}在线播放 - {{ item.name }}{% endblock %}\n'
               '{% block body %}\n'
               '<section class="media-shell">\n'
               '  <header class="media-header">\n'
               '    <div>\n'
               '      <div class="small text-muted">在线播放</div>\n'
               '      <h1>{{ item.name }}</h1>\n'
               '      <p>{{ kind_label(item.kind, item.name) }} · {{ file_size_label(item.size) }}</p>\n'
               '    </div>\n'
               '    <a class="mini-download" href="{{ download_url }}">下载文件</a>\n'
               '  </header>\n'
               '\n'
               '  <div class="player-panel {% if media_kind(item.name) == \'audio\' %}audio-panel{% endif %}">\n'
               "    {% if media_kind(item.name) == 'audio' %}\n"
               '      <audio controls preload="metadata" src="{{ stream_url }}"></audio>\n'
               '    {% else %}\n'
               '      <video controls preload="metadata" src="{{ stream_url }}"></video>\n'
               '    {% endif %}\n'
               '  </div>\n'
               '</section>\n'
               '{% endblock %}\n',
 'register.html': '{% extends "base.html" %}\n'
                  '{% block title %}注册 - 淡橘云盘{% endblock %}\n'
                  '{% block body %}\n'
                  '<section class="auth-layout">\n'
                  '  <div class="auth-hero">\n'
                  '    <div class="brand-dot mb-4"></div>\n'
                  '    <div class="eyebrow">Flask Cloud Drive</div>\n'
                  '    <h1>注册淡橘云盘</h1>\n'
                  '    <p>密码会以随机盐 + SHA3-256 摘要形式保存。生产环境建议改成 Argon2id 或 bcrypt。</p>\n'
                  '  </div>\n'
                  '  <div class="auth-panel">\n'
                  '    {% for msg in get_flashed_messages() %}<div class="alert alert-warning py-2">{{ msg }}</div>{% '
                  'endfor %}\n'
                  '    <form method="post" class="auth-form">\n'
                  '      <label class="form-label">用户名</label>\n'
                  '      <input class="form-control form-control-lg" name="username" autocomplete="username" '
                  'required>\n'
                  '      <label class="form-label mt-4">密码</label>\n'
                  '      <input class="form-control form-control-lg" name="password" type="password" '
                  'autocomplete="new-password" minlength="6" required>\n'
                  '      <button class="btn btn-orange btn-lg w-100 mt-4" type="submit">注册</button>\n'
                  '    </form>\n'
                  '    <p class="mt-4 mb-0 text-muted">已有账号？<a href="{{ url_for(\'login\') }}">登录</a></p>\n'
                  '  </div>\n'
                  '</section>\n'
                  '{% endblock %}\n',
 'share_file.html': '{% extends "base.html" %}\n'
                    '{% block title %}分享文件 - {{ item.name }}{% endblock %}\n'
                    '{% block body %}\n'
                    '<section class="media-shell share-single-file" data-share-token="{{ token }}">\n'
                    '  <header class="media-header">\n'
                    '    <div>\n'
                    '      <div class="small text-muted">分享文件</div>\n'
                    '      <h1>{{ item.name }}</h1>\n'
                    '      <p>{{ kind_label(item.kind, item.name) }} · {{ file_size_label(item.size) }}</p>\n'
                    '    </div>\n'
                    '    <span class="shared-pill">左键下载，右键更多操作</span>\n'
                    '  </header>\n'
                    '\n'
                    '  <section id="sharedFileBoard" class="file-board single-shared-file-board" aria-label="分享文件">\n'
                    '    <div class="entry-card shared-entry file-entry"\n'
                    '         data-id="{{ item.id }}"\n'
                    '         data-kind="file"\n'
                    '         data-name="{{ item.name }}"\n'
                    '         data-download-url="{{ url_for(\'share_download\', token=token, item_id=item.id) }}"\n'
                    '         data-play-url="{{ url_for(\'share_preview\', token=token, item_id=item.id) if '
                    'media_kind(item.name) else \'\' }}"\n'
                    '         data-text-url="{{ url_for(\'share_text_viewer\', token=token, item_id=item.id) if '
                    'is_text_file(item.name) else \'\' }}"\n'
                    '         data-media-kind="{{ media_kind(item.name) or \'\' }}"\n'
                    '         data-is-text="{{ \'1\' if is_text_file(item.name) else \'0\' }}">\n'
                    '      <div class="entry-icon file {{ media_kind(item.name) or (\'text\' if '
                    'is_text_file(item.name) else \'\') }}">{{ entry_icon(item.kind, item.name) }}</div>\n'
                    '      <div class="entry-main">\n'
                    '        <div class="entry-name" title="{{ item.name }}">{{ item.name }}</div>\n'
                    '        <div class="entry-meta"><span class="kind-badge file-badge">{{ kind_label(item.kind, '
                    'item.name) }}</span><span>{{ file_size_label(item.size) }} · 左键下载</span></div>\n'
                    '      </div>\n'
                    '    </div>\n'
                    '  </section>\n'
                    '\n'
                    '  <div id="sharedContextMenu" class="context-menu shadow-sm" role="menu"></div>\n'
                    '</section>\n'
                    '{% endblock %}\n'
                    '{% block scripts %}\n'
                    '<script src="{{ url_for(\'static\', filename=\'share_page.js\') }}"></script>\n'
                    '{% endblock %}\n',
 'share_folder.html': '{% extends "base.html" %}\n'
                      '{% block title %}分享文件夹 - {{ crumbs[0].name }}{% endblock %}\n'
                      '{% block body %}\n'
                      '<div class="drive-shell shared" data-share-token="{{ token }}">\n'
                      '  <header class="drive-header">\n'
                      '    <div class="drive-title-area">\n'
                      '      <div class="small text-muted">分享文件夹</div>\n'
                      '      <nav aria-label="breadcrumb">\n'
                      '        <ol class="breadcrumb mb-0">\n'
                      '          {% for c in crumbs %}\n'
                      '            <li class="breadcrumb-item {% if loop.last %}active{% endif %}">\n'
                      '              {% if not loop.last %}\n'
                      '                <a href="{{ url_for(\'share_view\', token=token, folder_id=c.id) }}">{{ c.name '
                      '}}</a>\n'
                      '              {% else %}\n'
                      '                <span>{{ c.name }}</span>\n'
                      '              {% endif %}\n'
                      '            </li>\n'
                      '          {% endfor %}\n'
                      '        </ol>\n'
                      '      </nav>\n'
                      '    </div>\n'
                      '    <div class="drive-nav">\n'
                      '      <span class="shared-pill">只读分享</span>\n'
                      '      <a class="mini-download" href="{{ url_for(\'share_archive_download\', token=token, '
                      'folder_id=current_folder_id) }}">下载全部 .7z</a>\n'
                      '    </div>\n'
                      '  </header>\n'
                      '\n'
                      '  <section id="sharedFileBoard" class="file-board shared-board" aria-label="分享文件列表">\n'
                      '    {% for e in entries %}\n'
                      '      <div class="entry-card shared-entry {{ e.kind }}-entry"\n'
                      '           data-id="{{ e.id }}"\n'
                      '           data-kind="{{ e.kind }}"\n'
                      '           data-name="{{ e.name }}"\n'
                      '           data-open-url="{{ url_for(\'share_view\', token=token, folder_id=e.id) if e.kind == '
                      '\'folder\' else \'\' }}"\n'
                      '           data-download-url="{{ url_for(\'share_download\', token=token, item_id=e.id) }}"\n'
                      '           data-play-url="{{ url_for(\'share_preview\', token=token, item_id=e.id) if '
                      'media_kind(e.name) else \'\' }}"\n'
                      '           data-text-url="{{ url_for(\'share_text_viewer\', token=token, item_id=e.id) if '
                      'is_text_file(e.name) else \'\' }}"\n'
                      '           data-media-kind="{{ media_kind(e.name) or \'\' }}"\n'
                      '           data-is-text="{{ \'1\' if is_text_file(e.name) else \'0\' }}">\n'
                      '        <div class="entry-icon {{ e.kind }} {{ media_kind(e.name) or (\'text\' if '
                      'is_text_file(e.name) else \'\') }}">{{ entry_icon(e.kind, e.name) }}</div>\n'
                      '        <div class="entry-main">\n'
                      '          <div class="entry-name" title="{{ e.name }}">{{ e.name }}</div>\n'
                      '          <div class="entry-meta">\n'
                      "            {% if e.kind == 'folder' %}\n"
                      '              <span class="kind-badge folder-badge">文件夹</span><span>左键进入</span>\n'
                      '            {% else %}\n'
                      '              <span class="kind-badge file-badge">{{ kind_label(e.kind, e.name) '
                      '}}</span><span>{{ file_size_label(e.size) }} · 左键下载</span>\n'
                      '            {% endif %}\n'
                      '          </div>\n'
                      '        </div>\n'
                      '      </div>\n'
                      '    {% else %}\n'
                      '      <div class="empty-hint">这个分享文件夹为空。</div>\n'
                      '    {% endfor %}\n'
                      '  </section>\n'
                      '\n'
                      '  <div id="sharedContextMenu" class="context-menu shadow-sm" role="menu"></div>\n'
                      '</div>\n'
                      '{% endblock %}\n'
                      '{% block scripts %}\n'
                      '<script src="{{ url_for(\'static\', filename=\'share_page.js\') }}"></script>\n'
                      '{% endblock %}\n',
 'shares.html': '{% extends "base.html" %}\n'
                '{% block title %}分享管理 - 淡橘云盘{% endblock %}\n'
                '{% block body %}\n'
                '<div class="shares-shell">\n'
                '  <header class="drive-header shares-header">\n'
                '    <div>\n'
                '      <div class="small text-muted">淡橘云盘</div>\n'
                '      <h1 class="shares-title">分享管理</h1>\n'
                '    </div>\n'
                '    <div class="drive-nav">\n'
                '      <a class="nav-pill" href="{{ url_for(\'drive\') }}">返回云盘</a>\n'
                '      <a class="nav-pill" href="{{ url_for(\'logout\') }}">退出</a>\n'
                '    </div>\n'
                '  </header>\n'
                '\n'
                '  <section class="share-list">\n'
                '    {% for s in shares %}\n'
                "      {% set share_url = url_for('share_view', token=s.token, _external=True) %}\n"
                '      <article class="share-row" data-share-id="{{ s.id }}" data-share-url="{{ share_url }}">\n'
                '        <div class="share-row-main">\n'
                '          <div class="entry-icon {{ s.kind }} {{ media_kind(s.name) or \'\' }}">{{ entry_icon(s.kind, '
                's.name) }}</div>\n'
                '          <div class="share-info">\n'
                '            <div class="entry-name" title="{{ s.name }}">{{ s.name }}</div>\n'
                '            <div class="entry-meta">\n'
                '              <span class="kind-badge {% if s.kind == \'folder\' %}folder-badge{% else %}file-badge{% '
                'endif %}">{{ kind_label(s.kind, s.name) }}</span>\n'
                '              <span>创建于 {{ s.created_at }}</span>\n'
                '            </div>\n'
                '            <input class="share-url-input" readonly value="{{ share_url }}">\n'
                '          </div>\n'
                '        </div>\n'
                '        <div class="share-actions">\n'
                '          <button class="ghost-action copy-share" type="button">复制链接</button>\n'
                '          <button class="ghost-action danger cancel-share" type="button">取消分享</button>\n'
                '        </div>\n'
                '      </article>\n'
                '    {% else %}\n'
                '      <div class="empty-hint share-empty">还没有分享。回到云盘后，右键文件或文件夹选择“分享”。</div>\n'
                '    {% endfor %}\n'
                '  </section>\n'
                '  <div id="toastHost" class="toast-host"></div>\n'
                '</div>\n'
                '{% endblock %}\n'
                '{% block scripts %}\n'
                '<script src="{{ url_for(\'static\', filename=\'shares.js\') }}"></script>\n'
                '{% endblock %}\n',
 'text_editor.html': '{% extends "base.html" %}\n'
                     '{% block title %}{% if read_only %}在线查看{% else %}在线编辑{% endif %} - {{ item.name }}{% endblock '
                     '%}\n'
                     '{% block body %}\n'
                     '<section class="text-editor-shell" data-save-url="{{ save_url or \'\' }}" data-read-only="{{ '
                     '\'1\' if read_only else \'0\' }}">\n'
                     '  <header class="text-editor-header">\n'
                     '    <div class="text-editor-title-area">\n'
                     '      <div class="small text-editor-kicker">{{ \'只读文本查看\' if read_only else \'文本在线编辑\' }}</div>\n'
                     '      <h1>{{ item.name }}</h1>\n'
                     '      <p>编码：<span id="detectedEncoding">{{ text_document.encoding }}</span> · 换行：<span '
                     'id="newlineStyleLabel">{{ text_document.newline_style | replace(\'\\r\', \'CR\') | '
                     "replace('\\n', 'LF') }}</span> · {{ file_size_label(item.size) }}</p>\n"
                     '    </div>\n'
                     '    <div class="text-editor-actions">\n'
                     '      <a class="text-editor-link" href="{{ download_url }}">下载</a>\n'
                     '      {% if not read_only %}\n'
                     '        <button id="saveTextButton" class="text-editor-button" type="button">保存</button>\n'
                     '      {% endif %}\n'
                     '    </div>\n'
                     '  </header>\n'
                     '\n'
                     '  <textarea id="textContentEditor" class="text-content-editor" spellcheck="false" '
                     'autocomplete="off" autocorrect="off" autocapitalize="off" wrap="off" data-encoding="{{ '
                     'text_document.encoding }}" data-newline-style="{{ text_document.newline_style }}" {% if '
                     'read_only %}readonly{% endif %}>{{ text_document.content }}</textarea>\n'
                     '\n'
                     '  <div id="textEditorStatus" class="text-editor-status">{% if read_only %}只读模式：分享页面不能保存修改。{% '
                     'else %}Ctrl/⌘ + S 保存。Tab 会插入缩进。{% endif %}</div>\n'
                     '</section>\n'
                     '{% endblock %}\n'
                     '{% block scripts %}\n'
                     '<script src="{{ url_for(\'static\', filename=\'text_editor.js\') }}"></script>\n'
                     '{% endblock %}\n'}

STATIC_FILES = {'app.css': ':root {\n'
            '  --orange-25: #fffaf5;\n'
            '  --orange-50: #fff4e8;\n'
            '  --orange-100: #ffe8cc;\n'
            '  --orange-200: #ffd6a3;\n'
            '  --orange-300: #f8bd79;\n'
            '  --orange-500: #e8893c;\n'
            '  --orange-600: #ce6d22;\n'
            '  --text-main: #362719;\n'
            '  --muted: #8a7461;\n'
            '  --line: #f0dcc7;\n'
            '  --soft-white: rgba(255, 255, 255, .72);\n'
            '}\n'
            '\n'
            'html,\n'
            'body {\n'
            '  min-height: 100%;\n'
            '}\n'
            '\n'
            'body {\n'
            '  margin: 0;\n'
            '  color: var(--text-main);\n'
            '  background:\n'
            '    radial-gradient(circle at 10% 12%, rgba(255, 214, 163, .72), transparent 34rem),\n'
            '    radial-gradient(circle at 90% 0%, rgba(255, 244, 232, .95), transparent 36rem),\n'
            '    linear-gradient(135deg, #fffaf5 0%, #fff4e8 52%, #fff8f0 100%);\n'
            '  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;\n'
            '}\n'
            '\n'
            'a {\n'
            '  color: var(--orange-600);\n'
            '  text-decoration: none;\n'
            '}\n'
            '\n'
            'a:hover {\n'
            '  color: #a94e0f;\n'
            '}\n'
            '\n'
            '.page-shell {\n'
            '  min-height: 100vh;\n'
            '  padding: clamp(10px, 1.6vw, 22px);\n'
            '}\n'
            '\n'
            '.brand-dot {\n'
            '  width: 48px;\n'
            '  height: 48px;\n'
            '  border-radius: 16px;\n'
            '  background: linear-gradient(135deg, var(--orange-300), var(--orange-600));\n'
            '  box-shadow: 0 16px 32px rgba(206, 109, 34, .22);\n'
            '}\n'
            '\n'
            '.eyebrow {\n'
            '  color: var(--orange-600);\n'
            '  font-weight: 700;\n'
            '  letter-spacing: .06em;\n'
            '  text-transform: uppercase;\n'
            '  font-size: .82rem;\n'
            '  margin-bottom: 12px;\n'
            '}\n'
            '\n'
            '.auth-layout {\n'
            '  min-height: calc(100vh - clamp(20px, 3.2vw, 44px));\n'
            '  display: grid;\n'
            '  grid-template-columns: minmax(0, 1.15fr) minmax(340px, 460px);\n'
            '  gap: clamp(24px, 6vw, 88px);\n'
            '  align-items: center;\n'
            '  padding: clamp(18px, 5vw, 76px);\n'
            '}\n'
            '\n'
            '.auth-hero h1 {\n'
            '  font-size: clamp(2.5rem, 6vw, 5.8rem);\n'
            '  line-height: .95;\n'
            '  letter-spacing: -.05em;\n'
            '  margin-bottom: 24px;\n'
            '}\n'
            '\n'
            '.auth-hero p {\n'
            '  max-width: 680px;\n'
            '  color: var(--muted);\n'
            '  font-size: clamp(1rem, 1.5vw, 1.22rem);\n'
            '}\n'
            '\n'
            '.auth-panel {\n'
            '  padding: clamp(22px, 3.5vw, 42px);\n'
            '  background: rgba(255, 255, 255, .58);\n'
            '  backdrop-filter: blur(16px);\n'
            '  border-radius: 28px;\n'
            '  box-shadow: 0 24px 70px rgba(206, 109, 34, .08);\n'
            '}\n'
            '\n'
            '.form-control {\n'
            '  border: 0;\n'
            '  background-color: rgba(255, 253, 249, .96);\n'
            '  box-shadow: inset 0 0 0 1px rgba(240, 220, 199, .82);\n'
            '}\n'
            '\n'
            '.form-control:focus {\n'
            '  border-color: var(--orange-300);\n'
            '  box-shadow: inset 0 0 0 1px var(--orange-300), 0 0 0 .25rem rgba(232, 137, 60, .14);\n'
            '}\n'
            '\n'
            '.btn-orange {\n'
            '  --bs-btn-color: #fff;\n'
            '  --bs-btn-bg: var(--orange-500);\n'
            '  --bs-btn-border-color: var(--orange-500);\n'
            '  --bs-btn-hover-color: #fff;\n'
            '  --bs-btn-hover-bg: var(--orange-600);\n'
            '  --bs-btn-hover-border-color: var(--orange-600);\n'
            '  --bs-btn-active-color: #fff;\n'
            '  --bs-btn-active-bg: #b85a18;\n'
            '  --bs-btn-active-border-color: #b85a18;\n'
            '  border-radius: 14px;\n'
            '}\n'
            '\n'
            '.drive-shell,\n'
            '.shares-shell,\n'
            '.media-shell {\n'
            '  width: 100%;\n'
            '  min-height: calc(100vh - clamp(20px, 3.2vw, 44px));\n'
            '  display: flex;\n'
            '  flex-direction: column;\n'
            '}\n'
            '\n'
            '.drive-header,\n'
            '.media-header {\n'
            '  display: flex;\n'
            '  align-items: center;\n'
            '  justify-content: space-between;\n'
            '  gap: 24px;\n'
            '  padding: 12px clamp(10px, 1vw, 18px);\n'
            '  border-radius: 22px;\n'
            '  background: rgba(255, 255, 255, .52);\n'
            '  backdrop-filter: blur(14px);\n'
            '}\n'
            '\n'
            '.drive-title-area {\n'
            '  min-width: 0;\n'
            '}\n'
            '\n'
            '.breadcrumb {\n'
            '  --bs-breadcrumb-divider-color: #b9987b;\n'
            '  --bs-breadcrumb-item-active-color: var(--text-main);\n'
            '  font-size: clamp(1rem, 1.2vw, 1.14rem);\n'
            '  flex-wrap: wrap;\n'
            '}\n'
            '\n'
            '.breadcrumb-target {\n'
            '  border-radius: 10px;\n'
            '  padding: 2px 7px;\n'
            '}\n'
            '\n'
            '.breadcrumb-target.drop-over {\n'
            '  background: var(--orange-100);\n'
            '  outline: 2px dashed var(--orange-500);\n'
            '}\n'
            '\n'
            '.drive-nav {\n'
            '  display: flex;\n'
            '  align-items: center;\n'
            '  gap: 10px;\n'
            '  white-space: nowrap;\n'
            '}\n'
            '\n'
            '.nav-pill,\n'
            '.shared-pill,\n'
            '.mini-download,\n'
            '.ghost-action {\n'
            '  white-space: nowrap;\n'
            '  padding: 8px 13px;\n'
            '  border-radius: 999px;\n'
            '  background: rgba(255, 244, 232, .85);\n'
            '  color: var(--orange-600);\n'
            '  border: 0;\n'
            '  font: inherit;\n'
            '}\n'
            '\n'
            '.nav-pill:hover,\n'
            '.mini-download:hover,\n'
            '.ghost-action:hover {\n'
            '  background: var(--orange-100);\n'
            '  color: #a94e0f;\n'
            '}\n'
            '\n'
            '.file-board {\n'
            '  flex: 1;\n'
            '  min-height: 0;\n'
            '  margin-top: 14px;\n'
            '  padding: clamp(10px, 1.5vw, 20px);\n'
            '  border-radius: 28px;\n'
            '  background: rgba(255, 255, 255, .36);\n'
            '  display: grid;\n'
            '  grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));\n'
            '  align-content: start;\n'
            '  gap: 12px;\n'
            '  overflow: auto;\n'
            '}\n'
            '\n'
            '.file-board.drop-over {\n'
            '  outline: 3px dashed var(--orange-300);\n'
            '  outline-offset: -12px;\n'
            '  background: rgba(255, 244, 232, .72);\n'
            '}\n'
            '\n'
            '.entry-card {\n'
            '  display: flex;\n'
            '  align-items: center;\n'
            '  gap: 13px;\n'
            '  min-height: 78px;\n'
            '  padding: 13px;\n'
            '  border-radius: 20px;\n'
            '  background: rgba(255, 253, 249, .82);\n'
            '  cursor: default;\n'
            '  user-select: none;\n'
            '  color: var(--text-main);\n'
            '  transition: transform .12s ease, box-shadow .12s ease, background-color .12s ease;\n'
            '}\n'
            '\n'
            '.folder-entry {\n'
            '  background: rgba(255, 239, 215, .88);\n'
            '}\n'
            '\n'
            '.file-entry {\n'
            '  background: rgba(255, 253, 249, .88);\n'
            '}\n'
            '\n'
            '.entry-card:hover,\n'
            '.entry-card.selected {\n'
            '  background: var(--orange-50);\n'
            '  box-shadow: 0 16px 34px rgba(206, 109, 34, .12);\n'
            '}\n'
            '\n'
            '.entry-card:active {\n'
            '  transform: scale(.992);\n'
            '}\n'
            '\n'
            '.entry-card.dragging {\n'
            '  opacity: .55;\n'
            '}\n'
            '\n'
            '.entry-card.drop-over {\n'
            '  outline: 2px dashed var(--orange-500);\n'
            '  outline-offset: -5px;\n'
            '}\n'
            '\n'
            '.shared-entry {\n'
            '  cursor: pointer;\n'
            '}\n'
            '\n'
            '.entry-icon {\n'
            '  width: 50px;\n'
            '  height: 50px;\n'
            '  flex: 0 0 50px;\n'
            '  display: grid;\n'
            '  place-items: center;\n'
            '  border-radius: 17px;\n'
            '  font-size: 27px;\n'
            '  background: var(--orange-50);\n'
            '}\n'
            '\n'
            '.entry-icon.folder {\n'
            '  background: #ffe4bd;\n'
            '}\n'
            '\n'
            '.entry-icon.file {\n'
            '  background: #fff7ed;\n'
            '}\n'
            '\n'
            '.entry-icon.audio {\n'
            '  background: #fff0d9;\n'
            '}\n'
            '\n'
            '.entry-icon.video {\n'
            '  background: #ffe6d6;\n'
            '}\n'
            '\n'
            '.entry-icon.text {\n'
            '  background: #ffe9dc;\n'
            '}\n'
            '\n'
            '.entry-main,\n'
            '.share-info {\n'
            '  min-width: 0;\n'
            '}\n'
            '\n'
            '.entry-name {\n'
            '  overflow: hidden;\n'
            '  white-space: nowrap;\n'
            '  text-overflow: ellipsis;\n'
            '  font-weight: 700;\n'
            '}\n'
            '\n'
            '.entry-meta {\n'
            '  margin-top: 6px;\n'
            '  color: var(--muted);\n'
            '  font-size: .86rem;\n'
            '  display: flex;\n'
            '  align-items: center;\n'
            '  gap: 7px;\n'
            '  flex-wrap: wrap;\n'
            '}\n'
            '\n'
            '.kind-badge {\n'
            '  display: inline-flex;\n'
            '  align-items: center;\n'
            '  padding: 2px 7px;\n'
            '  border-radius: 999px;\n'
            '  font-size: .76rem;\n'
            '  font-weight: 700;\n'
            '}\n'
            '\n'
            '.folder-badge {\n'
            '  background: #ffdcae;\n'
            '  color: #90500f;\n'
            '}\n'
            '\n'
            '.file-badge {\n'
            '  background: #fff0df;\n'
            '  color: #8b6040;\n'
            '}\n'
            '\n'
            '.empty-hint {\n'
            '  grid-column: 1 / -1;\n'
            '  align-self: start;\n'
            '  justify-self: center;\n'
            '  margin-top: 14vh;\n'
            '  color: var(--muted);\n'
            '  padding: 14px 18px;\n'
            '  border-radius: 999px;\n'
            '  background: rgba(255, 244, 232, .78);\n'
            '}\n'
            '\n'
            '.context-menu {\n'
            '  position: fixed;\n'
            '  z-index: 2000;\n'
            '  display: none;\n'
            '  min-width: 200px;\n'
            '  padding: 7px;\n'
            '  border: 1px solid var(--line);\n'
            '  border-radius: 16px;\n'
            '  background: rgba(255, 253, 249, .98);\n'
            '  backdrop-filter: blur(12px);\n'
            '}\n'
            '\n'
            '.context-menu.show {\n'
            '  display: block;\n'
            '}\n'
            '\n'
            '.context-menu button {\n'
            '  display: block;\n'
            '  width: 100%;\n'
            '  border: 0;\n'
            '  background: transparent;\n'
            '  text-align: left;\n'
            '  color: var(--text-main);\n'
            '  padding: 9px 11px;\n'
            '  border-radius: 11px;\n'
            '  font-size: .95rem;\n'
            '}\n'
            '\n'
            '.context-menu button:hover {\n'
            '  background: var(--orange-50);\n'
            '  color: var(--orange-600);\n'
            '}\n'
            '\n'
            '.context-menu .danger:hover,\n'
            '.ghost-action.danger:hover {\n'
            '  background: #fff1f0;\n'
            '  color: #b42318;\n'
            '}\n'
            '\n'
            '.toast-host {\n'
            '  position: fixed;\n'
            '  right: 18px;\n'
            '  bottom: 18px;\n'
            '  z-index: 2500;\n'
            '  display: grid;\n'
            '  gap: 10px;\n'
            '}\n'
            '\n'
            '.app-toast {\n'
            '  max-width: min(560px, calc(100vw - 36px));\n'
            '  padding: 11px 14px;\n'
            '  border-radius: 14px;\n'
            '  background: rgba(54, 39, 25, .92);\n'
            '  color: #fffaf5;\n'
            '  box-shadow: 0 16px 40px rgba(54, 39, 25, .18);\n'
            '  word-break: break-all;\n'
            '}\n'
            '\n'
            '.shares-title,\n'
            '.media-header h1 {\n'
            '  margin: 0;\n'
            '  font-size: clamp(1.4rem, 2.4vw, 2.2rem);\n'
            '  letter-spacing: -.03em;\n'
            '}\n'
            '\n'
            '.share-list {\n'
            '  margin-top: 14px;\n'
            '  display: grid;\n'
            '  gap: 10px;\n'
            '  overflow: auto;\n'
            '  padding-bottom: 12px;\n'
            '}\n'
            '\n'
            '.share-row {\n'
            '  display: flex;\n'
            '  justify-content: space-between;\n'
            '  gap: 18px;\n'
            '  align-items: center;\n'
            '  padding: 14px;\n'
            '  border-radius: 22px;\n'
            '  background: rgba(255, 255, 255, .56);\n'
            '}\n'
            '\n'
            '.share-row-main {\n'
            '  display: flex;\n'
            '  align-items: center;\n'
            '  gap: 13px;\n'
            '  min-width: 0;\n'
            '  flex: 1;\n'
            '}\n'
            '\n'
            '.share-url-input {\n'
            '  width: min(720px, 100%);\n'
            '  margin-top: 8px;\n'
            '  border: 0;\n'
            '  border-radius: 12px;\n'
            '  padding: 8px 10px;\n'
            '  color: var(--muted);\n'
            '  background: rgba(255, 250, 245, .9);\n'
            '  font-size: .88rem;\n'
            '}\n'
            '\n'
            '.share-actions {\n'
            '  display: flex;\n'
            '  gap: 8px;\n'
            '  flex-wrap: wrap;\n'
            '  justify-content: flex-end;\n'
            '}\n'
            '\n'
            '.player-panel {\n'
            '  flex: 1;\n'
            '  min-height: 0;\n'
            '  margin-top: 14px;\n'
            '  border-radius: 28px;\n'
            '  background: rgba(255, 255, 255, .42);\n'
            '  display: grid;\n'
            '  place-items: center;\n'
            '  padding: clamp(12px, 2vw, 28px);\n'
            '}\n'
            '\n'
            '.player-panel video {\n'
            '  width: min(100%, 1280px);\n'
            '  max-height: calc(100vh - 170px);\n'
            '  border-radius: 22px;\n'
            '  background: #000;\n'
            '}\n'
            '\n'
            '.player-panel audio {\n'
            '  width: min(820px, 100%);\n'
            '}\n'
            '\n'
            '.audio-panel {\n'
            '  min-height: 34vh;\n'
            '}\n'
            '\n'
            '.media-header p {\n'
            '  margin: 4px 0 0;\n'
            '  color: var(--muted);\n'
            '}\n'
            '\n'
            '.single-file-hint {\n'
            '  display: inline-block;\n'
            '}\n'
            '\n'
            '.single-shared-file-board {\n'
            '  flex: initial;\n'
            '  min-height: 160px;\n'
            '  grid-template-columns: minmax(260px, 520px);\n'
            '}\n'
            '\n'
            '.text-editor-shell {\n'
            '  width: 100%;\n'
            '  height: calc(100vh - clamp(20px, 3.2vw, 44px));\n'
            '  display: flex;\n'
            '  flex-direction: column;\n'
            '  overflow: hidden;\n'
            '  border-radius: 22px;\n'
            '  background: #ff6a45;\n'
            '  color: #111;\n'
            '}\n'
            '\n'
            '.text-editor-header {\n'
            '  display: flex;\n'
            '  justify-content: space-between;\n'
            '  align-items: center;\n'
            '  gap: 16px;\n'
            '  padding: 13px clamp(14px, 2vw, 28px);\n'
            '  background: rgba(255, 128, 82, .96);\n'
            '  color: #111;\n'
            '}\n'
            '\n'
            '.text-editor-title-area {\n'
            '  min-width: 0;\n'
            '}\n'
            '\n'
            '.text-editor-kicker {\n'
            '  color: #4d1608;\n'
            '  font-weight: 700;\n'
            '}\n'
            '\n'
            '.text-editor-header h1 {\n'
            '  margin: 0;\n'
            '  max-width: min(64vw, 980px);\n'
            '  overflow: hidden;\n'
            '  white-space: nowrap;\n'
            '  text-overflow: ellipsis;\n'
            '  font-size: clamp(1.2rem, 2vw, 2rem);\n'
            '  letter-spacing: -.03em;\n'
            '  color: #111;\n'
            '}\n'
            '\n'
            '.text-editor-header p {\n'
            '  margin: 4px 0 0;\n'
            '  color: #301006;\n'
            '}\n'
            '\n'
            '.text-editor-actions {\n'
            '  display: flex;\n'
            '  align-items: center;\n'
            '  gap: 10px;\n'
            '  white-space: nowrap;\n'
            '}\n'
            '\n'
            '.text-editor-button,\n'
            '.text-editor-link {\n'
            '  border: 0;\n'
            '  border-radius: 999px;\n'
            '  background: rgba(255, 238, 226, .85);\n'
            '  color: #111;\n'
            '  padding: 8px 15px;\n'
            '  font: inherit;\n'
            '}\n'
            '\n'
            '.text-editor-button:hover,\n'
            '.text-editor-link:hover {\n'
            '  background: rgba(255, 250, 246, .96);\n'
            '  color: #111;\n'
            '}\n'
            '\n'
            '.text-content-editor {\n'
            '  flex: 1;\n'
            '  width: 100%;\n'
            '  min-height: 0;\n'
            '  border: 0;\n'
            '  outline: 0;\n'
            '  resize: none;\n'
            '  padding: clamp(16px, 2.2vw, 34px);\n'
            '  background: #ff6a45;\n'
            '  color: #111;\n'
            '  font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, ui-monospace, monospace;\n'
            '  font-size: clamp(15px, 1vw, 18px);\n'
            '  line-height: 1.62;\n'
            '  tab-size: 4;\n'
            '  white-space: pre;\n'
            '  overflow: auto;\n'
            '}\n'
            '\n'
            '.text-content-editor::selection {\n'
            '  background: rgba(255, 245, 238, .75);\n'
            '}\n'
            '\n'
            '.text-content-editor[readonly] {\n'
            '  cursor: default;\n'
            '}\n'
            '\n'
            '.text-editor-status {\n'
            '  padding: 8px clamp(14px, 2vw, 28px);\n'
            '  background: rgba(255, 128, 82, .96);\n'
            '  color: #111;\n'
            '  font-size: .92rem;\n'
            '}\n'
            '\n'
            '@media (max-width: 820px) {\n'
            '  .auth-layout {\n'
            '    grid-template-columns: 1fr;\n'
            '    padding: 18px;\n'
            '  }\n'
            '\n'
            '  .auth-hero h1 {\n'
            '    font-size: clamp(2.2rem, 14vw, 4.4rem);\n'
            '  }\n'
            '\n'
            '  .drive-header,\n'
            '  .media-header,\n'
            '  .text-editor-header,\n'
            '  .share-row {\n'
            '    align-items: flex-start;\n'
            '    flex-direction: column;\n'
            '  }\n'
            '\n'
            '  .drive-nav,\n'
            '  .share-actions {\n'
            '    width: 100%;\n'
            '    justify-content: flex-start;\n'
            '  }\n'
            '\n'
            '  .file-board {\n'
            '    grid-template-columns: 1fr;\n'
            '  }\n'
            '\n'
            '  .text-editor-header h1 {\n'
            '    max-width: calc(100vw - 56px);\n'
            '  }\n'
            '}\n',
 'app.js': "const shell = document.querySelector('.drive-shell');\n"
           "const currentFolderId = shell?.dataset.currentFolderId || '';\n"
           "const board = document.getElementById('dropArea');\n"
           "const menu = document.getElementById('contextMenu');\n"
           "const fileInput = document.getElementById('fileInput');\n"
           "const folderInput = document.getElementById('folderInput');\n"
           "const toastHost = document.getElementById('toastHost');\n"
           'let activeEntry = null;\n'
           'let dragEntryId = null;\n'
           'let suppressNextClick = false;\n'
           '\n'
           'function openLargePopup(url) {\n'
           '  const popupWidth = Math.max(900, Math.floor(window.screen.availWidth * 0.92));\n'
           '  const popupHeight = Math.max(700, Math.floor(window.screen.availHeight * 0.9));\n'
           '  const popupLeft = Math.max(0, Math.floor((window.screen.availWidth - popupWidth) / 2));\n'
           '  const popupTop = Math.max(0, Math.floor((window.screen.availHeight - popupHeight) / 2));\n'
           '  const openedWindow = window.open(\n'
           '    url,\n'
           "    '_blank',\n"
           '    '
           '`popup=yes,width=${popupWidth},height=${popupHeight},left=${popupLeft},top=${popupTop},resizable=yes,scrollbars=yes`\n'
           '  );\n'
           '  if (!openedWindow) window.location.href = url;\n'
           '}\n'
           '\n'
           'function api(url, body) {\n'
           '  return fetch(url, {\n'
           "    method: 'POST',\n"
           "    headers: { 'Content-Type': 'application/json' },\n"
           '    body: JSON.stringify(body || {})\n'
           '  }).then(async res => {\n'
           '    const data = await res.json().catch(() => ({}));\n'
           "    if (!res.ok || data.ok === false) throw new Error(data.error || '操作失败');\n"
           '    return data;\n'
           '  });\n'
           '}\n'
           '\n'
           'function showToast(message, timeout = 2600) {\n'
           "  const el = document.createElement('div');\n"
           "  el.className = 'app-toast';\n"
           '  el.textContent = message;\n'
           '  toastHost.appendChild(el);\n'
           '  setTimeout(() => el.remove(), timeout);\n'
           '}\n'
           '\n'
           'async function copyText(text) {\n'
           '  try {\n'
           '    await navigator.clipboard.writeText(text);\n'
           '    return true;\n'
           '  } catch (_err) {\n'
           "    const temp = document.createElement('textarea');\n"
           '    temp.value = text;\n'
           "    temp.setAttribute('readonly', '');\n"
           "    temp.style.position = 'fixed';\n"
           "    temp.style.opacity = '0';\n"
           '    document.body.appendChild(temp);\n'
           '    temp.select();\n'
           "    const ok = document.execCommand('copy');\n"
           '    temp.remove();\n'
           '    return ok;\n'
           '  }\n'
           '}\n'
           '\n'
           'function closeMenu() {\n'
           "  menu.classList.remove('show');\n"
           "  menu.innerHTML = '';\n"
           "  document.querySelectorAll('.entry-card.selected').forEach(el => el.classList.remove('selected'));\n"
           '}\n'
           '\n'
           'function placeMenu(x, y) {\n'
           "  menu.classList.add('show');\n"
           '  const rect = menu.getBoundingClientRect();\n'
           '  const pad = 10;\n'
           '  const left = Math.min(x, window.innerWidth - rect.width - pad);\n'
           '  const top = Math.min(y, window.innerHeight - rect.height - pad);\n'
           '  menu.style.left = `${Math.max(pad, left)}px`;\n'
           '  menu.style.top = `${Math.max(pad, top)}px`;\n'
           '}\n'
           '\n'
           'function addMenuItem(label, action, danger = false) {\n'
           "  const btn = document.createElement('button');\n"
           "  btn.type = 'button';\n"
           '  btn.textContent = label;\n'
           "  if (danger) btn.classList.add('danger');\n"
           "  btn.addEventListener('click', async () => {\n"
           '    closeMenu();\n'
           '    try { await action(); } catch (e) { showToast(e.message || String(e), 4200); }\n'
           '  });\n'
           '  menu.appendChild(btn);\n'
           '}\n'
           '\n'
           'function entryMainAction(entry) {\n'
           '  const id = entry.dataset.id;\n'
           "  if (entry.dataset.kind === 'folder') {\n"
           '    window.location.href = `/drive/${id}`;\n'
           '  } else {\n'
           '    window.location.href = `/download/${id}`;\n'
           '  }\n'
           '}\n'
           '\n'
           'function openEntryMenu(entry, x, y) {\n'
           '  closeMenu();\n'
           '  activeEntry = entry;\n'
           "  entry.classList.add('selected');\n"
           '  const id = entry.dataset.id;\n'
           '  const kind = entry.dataset.kind;\n'
           '  const name = entry.dataset.name;\n'
           '  const mediaKind = entry.dataset.mediaKind;\n'
           "  const isTextFile = entry.dataset.isText === '1';\n"
           '\n'
           "  if (kind === 'folder') {\n"
           "    addMenuItem('打开', () => { window.location.href = `/drive/${id}`; });\n"
           "    addMenuItem('下载为 7z', () => { window.location.href = `/download/${id}`; });\n"
           '  } else {\n'
           "    if (mediaKind === 'audio' || mediaKind === 'video') {\n"
           "      addMenuItem('在线播放', () => { window.location.href = `/preview/${id}`; });\n"
           '    }\n'
           '    if (isTextFile) {\n'
           "      addMenuItem('在线编辑文本', () => { openLargePopup(`/text/${id}`); });\n"
           '    }\n'
           "    addMenuItem('下载', () => { window.location.href = `/download/${id}`; });\n"
           '  }\n'
           '\n'
           "  addMenuItem('重命名', async () => {\n"
           "    const next = prompt('输入新名称：', name);\n"
           '    if (!next || next.trim() === name) return;\n'
           "    await api('/api/rename', { item_id: id, name: next.trim() });\n"
           '    location.reload();\n'
           '  });\n'
           "  addMenuItem('分享', async () => {\n"
           "    const data = await api('/api/share', { item_id: id });\n"
           '    const copied = await copyText(data.url);\n'
           '    if (copied) {\n'
           "      showToast('分享链接已复制到剪切板。');\n"
           '    } else {\n'
           '      showToast(`复制失败，请手动复制：${data.url}`, 8000);\n'
           "      prompt('复制分享链接：', data.url);\n"
           '    }\n'
           '  });\n'
           "  addMenuItem('删除', async () => {\n"
           '    if (!confirm(`确认删除「${name}」？`)) return;\n'
           "    await api('/api/delete', { item_id: id });\n"
           '    location.reload();\n'
           '  }, true);\n'
           '\n'
           '  placeMenu(x, y);\n'
           '}\n'
           '\n'
           'function openBlankMenu(x, y) {\n'
           '  closeMenu();\n'
           '  activeEntry = null;\n'
           "  addMenuItem('上传文件', () => fileInput.click());\n"
           "  addMenuItem('上传文件夹', () => folderInput.click());\n"
           "  addMenuItem('新建文件夹', async () => {\n"
           "    const name = prompt('文件夹名称：', '新建文件夹');\n"
           '    if (!name) return;\n'
           "    await api('/api/folders', { parent_id: currentFolderId, name: name.trim() });\n"
           '    location.reload();\n'
           '  });\n'
           '  placeMenu(x, y);\n'
           '}\n'
           '\n'
           'function shouldOpenBlankMenu(target) {\n'
           "  return !target.closest('.entry-card') && !target.closest('.context-menu') && "
           "target.closest('#dropArea');\n"
           '}\n'
           '\n'
           'if (board) {\n'
           "  document.addEventListener('contextmenu', (e) => {\n"
           "    const entry = e.target.closest('.entry-card');\n"
           '    if (entry && board.contains(entry)) {\n'
           '      e.preventDefault();\n'
           '      openEntryMenu(entry, e.clientX, e.clientY);\n'
           '      return;\n'
           '    }\n'
           '    if (shouldOpenBlankMenu(e.target)) {\n'
           '      e.preventDefault();\n'
           '      openBlankMenu(e.clientX, e.clientY);\n'
           '    }\n'
           '  });\n'
           '\n'
           "  document.addEventListener('click', (e) => {\n"
           "    if (!e.target.closest('.context-menu')) closeMenu();\n"
           '  });\n'
           '\n'
           "  document.addEventListener('keydown', (e) => {\n"
           "    if (e.key === 'Escape') closeMenu();\n"
           '  });\n'
           '\n'
           "  board.addEventListener('click', (e) => {\n"
           "    const entry = e.target.closest('.entry-card');\n"
           '    if (!entry || suppressNextClick) return;\n'
           '    entryMainAction(entry);\n'
           '  });\n'
           '\n'
           '  async function uploadFiles(fileList, isFolder = false) {\n'
           '    const files = Array.from(fileList || []);\n'
           '    if (!files.length) return;\n'
           '    const fd = new FormData();\n'
           "    fd.append('parent_id', currentFolderId);\n"
           '    for (const file of files) {\n'
           '      const relative = isFolder ? (file.webkitRelativePath || file.name) : file.name;\n'
           "      fd.append('files', file, relative);\n"
           '    }\n'
           "    const res = await fetch('/api/upload', { method: 'POST', body: fd });\n"
           '    const data = await res.json().catch(() => ({}));\n'
           "    if (!res.ok || data.ok === false) throw new Error(data.error || '上传失败');\n"
           '    location.reload();\n'
           '  }\n'
           '\n'
           "  fileInput.addEventListener('change', async () => {\n"
           '    try { await uploadFiles(fileInput.files, false); }\n'
           '    catch (e) { showToast(e.message || String(e)); }\n'
           "    finally { fileInput.value = ''; }\n"
           '  });\n'
           '\n'
           "  folderInput.addEventListener('change', async () => {\n"
           '    try { await uploadFiles(folderInput.files, true); }\n'
           '    catch (e) { showToast(e.message || String(e)); }\n'
           "    finally { folderInput.value = ''; }\n"
           '  });\n'
           '\n'
           '  document.querySelectorAll(\'.entry-card[draggable="true"]\').forEach(entry => {\n'
           "    entry.addEventListener('dragstart', (e) => {\n"
           '      dragEntryId = entry.dataset.id;\n'
           "      entry.classList.add('dragging');\n"
           "      e.dataTransfer.effectAllowed = 'move';\n"
           "      e.dataTransfer.setData('text/plain', dragEntryId);\n"
           '    });\n'
           "    entry.addEventListener('dragend', () => {\n"
           '      dragEntryId = null;\n'
           '      suppressNextClick = true;\n'
           '      setTimeout(() => { suppressNextClick = false; }, 120);\n'
           "      entry.classList.remove('dragging');\n"
           "      document.querySelectorAll('.drop-over').forEach(el => el.classList.remove('drop-over'));\n"
           '    });\n'
           '\n'
           "    if (entry.dataset.kind === 'folder') {\n"
           "      entry.addEventListener('dragover', (e) => {\n"
           '        if (!dragEntryId || dragEntryId === entry.dataset.id) return;\n'
           '        e.preventDefault();\n'
           "        entry.classList.add('drop-over');\n"
           '      });\n'
           "      entry.addEventListener('dragleave', () => entry.classList.remove('drop-over'));\n"
           "      entry.addEventListener('drop', async (e) => {\n"
           '        e.preventDefault();\n'
           "        entry.classList.remove('drop-over');\n"
           "        const itemId = e.dataTransfer.getData('text/plain') || dragEntryId;\n"
           '        if (!itemId || itemId === entry.dataset.id) return;\n'
           '        try {\n'
           "          await api('/api/move', { item_id: itemId, target_parent_id: entry.dataset.id });\n"
           '          location.reload();\n'
           '        } catch (err) { showToast(err.message || String(err)); }\n'
           '      });\n'
           '    }\n'
           '  });\n'
           '\n'
           "  document.querySelectorAll('.breadcrumb-target').forEach(crumb => {\n"
           "    crumb.addEventListener('dragover', (e) => {\n"
           '      if (!dragEntryId) return;\n'
           '      e.preventDefault();\n'
           "      crumb.classList.add('drop-over');\n"
           '    });\n'
           "    crumb.addEventListener('dragleave', () => crumb.classList.remove('drop-over'));\n"
           "    crumb.addEventListener('drop', async (e) => {\n"
           '      e.preventDefault();\n'
           "      crumb.classList.remove('drop-over');\n"
           "      const itemId = e.dataTransfer.getData('text/plain') || dragEntryId;\n"
           '      try {\n'
           "        await api('/api/move', { item_id: itemId, target_parent_id: crumb.dataset.folderId || '' });\n"
           '        location.reload();\n'
           '      } catch (err) { showToast(err.message || String(err)); }\n'
           '    });\n'
           '  });\n'
           '\n'
           "  board.addEventListener('dragover', (e) => {\n"
           '    if (!dragEntryId) return;\n'
           "    if (e.target.closest('.entry-card')) return;\n"
           '    e.preventDefault();\n'
           "    board.classList.add('drop-over');\n"
           '  });\n'
           '\n'
           "  board.addEventListener('dragleave', (e) => {\n"
           "    if (!board.contains(e.relatedTarget)) board.classList.remove('drop-over');\n"
           '  });\n'
           '\n'
           "  board.addEventListener('drop', async (e) => {\n"
           '    if (!dragEntryId) return;\n'
           "    if (e.target.closest('.entry-card')) return;\n"
           '    e.preventDefault();\n'
           "    board.classList.remove('drop-over');\n"
           "    const itemId = e.dataTransfer.getData('text/plain') || dragEntryId;\n"
           '    try {\n'
           "      await api('/api/move', { item_id: itemId, target_parent_id: currentFolderId });\n"
           '      location.reload();\n'
           '    } catch (err) { showToast(err.message || String(err)); }\n'
           '  });\n'
           '}\n',
 'share_page.js': "const sharedFileBoard = document.getElementById('sharedFileBoard');\n"
                  "const sharedContextMenu = document.getElementById('sharedContextMenu');\n"
                  'let selectedSharedEntry = null;\n'
                  '\n'
                  'function openLargePopup(url) {\n'
                  '  const popupWidth = Math.max(900, Math.floor(window.screen.availWidth * 0.92));\n'
                  '  const popupHeight = Math.max(700, Math.floor(window.screen.availHeight * 0.9));\n'
                  '  const popupLeft = Math.max(0, Math.floor((window.screen.availWidth - popupWidth) / 2));\n'
                  '  const popupTop = Math.max(0, Math.floor((window.screen.availHeight - popupHeight) / 2));\n'
                  '  const openedWindow = window.open(\n'
                  '    url,\n'
                  "    '_blank',\n"
                  '    '
                  '`popup=yes,width=${popupWidth},height=${popupHeight},left=${popupLeft},top=${popupTop},resizable=yes,scrollbars=yes`\n'
                  '  );\n'
                  '  if (!openedWindow) window.location.href = url;\n'
                  '}\n'
                  '\n'
                  'function closeSharedContextMenu() {\n'
                  "  sharedContextMenu?.classList.remove('show');\n"
                  "  if (sharedContextMenu) sharedContextMenu.innerHTML = '';\n"
                  "  document.querySelectorAll('.shared-entry.selected').forEach((entryElement) => {\n"
                  "    entryElement.classList.remove('selected');\n"
                  '  });\n'
                  '}\n'
                  '\n'
                  'function positionSharedContextMenu(clientX, clientY) {\n'
                  "  sharedContextMenu.classList.add('show');\n"
                  '  const menuRectangle = sharedContextMenu.getBoundingClientRect();\n'
                  '  const viewportPadding = 10;\n'
                  '  const left = Math.min(clientX, window.innerWidth - menuRectangle.width - viewportPadding);\n'
                  '  const top = Math.min(clientY, window.innerHeight - menuRectangle.height - viewportPadding);\n'
                  '  sharedContextMenu.style.left = `${Math.max(viewportPadding, left)}px`;\n'
                  '  sharedContextMenu.style.top = `${Math.max(viewportPadding, top)}px`;\n'
                  '}\n'
                  '\n'
                  'function addSharedMenuItem(label, action, isDangerous = false) {\n'
                  "  const menuButton = document.createElement('button');\n"
                  "  menuButton.type = 'button';\n"
                  '  menuButton.textContent = label;\n'
                  "  if (isDangerous) menuButton.classList.add('danger');\n"
                  "  menuButton.addEventListener('click', () => {\n"
                  '    closeSharedContextMenu();\n'
                  '    action();\n'
                  '  });\n'
                  '  sharedContextMenu.appendChild(menuButton);\n'
                  '}\n'
                  '\n'
                  'function openSharedEntry(entryElement) {\n'
                  "  if (entryElement.dataset.kind === 'folder') {\n"
                  '    window.location.href = entryElement.dataset.openUrl;\n'
                  '    return;\n'
                  '  }\n'
                  '  window.location.href = entryElement.dataset.downloadUrl;\n'
                  '}\n'
                  '\n'
                  'function openSharedEntryMenu(entryElement, clientX, clientY) {\n'
                  '  closeSharedContextMenu();\n'
                  '  selectedSharedEntry = entryElement;\n'
                  "  selectedSharedEntry.classList.add('selected');\n"
                  '\n'
                  "  if (entryElement.dataset.kind === 'folder') {\n"
                  "    addSharedMenuItem('打开', () => { window.location.href = entryElement.dataset.openUrl; });\n"
                  "    addSharedMenuItem('下载为 7z', () => { window.location.href = entryElement.dataset.downloadUrl; "
                  '});\n'
                  '  } else {\n'
                  "    addSharedMenuItem('下载', () => { window.location.href = entryElement.dataset.downloadUrl; });\n"
                  '    if (entryElement.dataset.playUrl) {\n'
                  "      addSharedMenuItem('在线播放', () => { window.location.href = entryElement.dataset.playUrl; });\n"
                  '    }\n'
                  '    if (entryElement.dataset.textUrl) {\n'
                  "      addSharedMenuItem('在线查看文本', () => { openLargePopup(entryElement.dataset.textUrl); });\n"
                  '    }\n'
                  '  }\n'
                  '\n'
                  '  positionSharedContextMenu(clientX, clientY);\n'
                  '}\n'
                  '\n'
                  'if (sharedFileBoard && sharedContextMenu) {\n'
                  "  sharedFileBoard.addEventListener('click', (event) => {\n"
                  "    const entryElement = event.target.closest('.shared-entry');\n"
                  '    if (!entryElement || !sharedFileBoard.contains(entryElement)) return;\n'
                  '    openSharedEntry(entryElement);\n'
                  '  });\n'
                  '\n'
                  "  document.addEventListener('contextmenu', (event) => {\n"
                  "    const entryElement = event.target.closest('.shared-entry');\n"
                  '    if (!entryElement || !sharedFileBoard.contains(entryElement)) return;\n'
                  '    event.preventDefault();\n'
                  '    openSharedEntryMenu(entryElement, event.clientX, event.clientY);\n'
                  '  });\n'
                  '\n'
                  "  document.addEventListener('click', (event) => {\n"
                  "    if (!event.target.closest('.context-menu')) closeSharedContextMenu();\n"
                  '  });\n'
                  '\n'
                  "  document.addEventListener('keydown', (event) => {\n"
                  "    if (event.key === 'Escape') closeSharedContextMenu();\n"
                  '  });\n'
                  '}\n',
 'shares.js': "const toastHost = document.getElementById('toastHost');\n"
              '\n'
              'function showToast(message, timeout = 2600) {\n'
              "  const el = document.createElement('div');\n"
              "  el.className = 'app-toast';\n"
              '  el.textContent = message;\n'
              '  toastHost.appendChild(el);\n'
              '  setTimeout(() => el.remove(), timeout);\n'
              '}\n'
              '\n'
              'async function copyText(text) {\n'
              '  try {\n'
              '    await navigator.clipboard.writeText(text);\n'
              '    return true;\n'
              '  } catch (_err) {\n'
              "    const temp = document.createElement('textarea');\n"
              '    temp.value = text;\n'
              "    temp.setAttribute('readonly', '');\n"
              "    temp.style.position = 'fixed';\n"
              "    temp.style.opacity = '0';\n"
              '    document.body.appendChild(temp);\n'
              '    temp.select();\n'
              "    const ok = document.execCommand('copy');\n"
              '    temp.remove();\n'
              '    return ok;\n'
              '  }\n'
              '}\n'
              '\n'
              "document.querySelectorAll('.share-row').forEach(row => {\n"
              "  row.querySelector('.copy-share')?.addEventListener('click', async () => {\n"
              '    const url = row.dataset.shareUrl;\n'
              '    const ok = await copyText(url);\n'
              "    if (ok) showToast('分享链接已复制到剪切板。');\n"
              "    else prompt('复制分享链接：', url);\n"
              '  });\n'
              '\n'
              "  row.querySelector('.cancel-share')?.addEventListener('click', async () => {\n"
              "    const name = row.querySelector('.entry-name')?.textContent || '这个分享';\n"
              '    if (!confirm(`确认取消「${name}」的分享？`)) return;\n'
              "    const res = await fetch(`/api/shares/${row.dataset.shareId}/cancel`, { method: 'POST' });\n"
              '    const data = await res.json().catch(() => ({}));\n'
              '    if (!res.ok || data.ok === false) {\n'
              "      showToast(data.error || '取消分享失败。', 4200);\n"
              '      return;\n'
              '    }\n'
              '    row.remove();\n'
              "    showToast('分享已取消。');\n"
              '  });\n'
              '});\n',
 'text_editor.js': "const textEditorShell = document.querySelector('.text-editor-shell');\n"
                   "const textContentEditor = document.getElementById('textContentEditor');\n"
                   "const saveTextButton = document.getElementById('saveTextButton');\n"
                   "const textEditorStatus = document.getElementById('textEditorStatus');\n"
                   "const isReadOnlyTextView = textEditorShell?.dataset.readOnly === '1';\n"
                   "const saveTextUrl = textEditorShell?.dataset.saveUrl || '';\n"
                   'let hasUnsavedTextChanges = false;\n'
                   '\n'
                   'function setEditorStatus(message) {\n'
                   '  if (textEditorStatus) textEditorStatus.textContent = message;\n'
                   '}\n'
                   '\n'
                   'async function saveTextDocument() {\n'
                   '  if (isReadOnlyTextView || !saveTextUrl) return;\n'
                   '  saveTextButton.disabled = true;\n'
                   "  setEditorStatus('正在保存…');\n"
                   '  try {\n'
                   '    const response = await fetch(saveTextUrl, {\n'
                   "      method: 'POST',\n"
                   "      headers: { 'Content-Type': 'application/json' },\n"
                   '      body: JSON.stringify({\n'
                   '        content: textContentEditor.value,\n'
                   "        encoding: textContentEditor.dataset.encoding || 'utf-8',\n"
                   "        newline_style: textContentEditor.dataset.newlineStyle || '\\n'\n"
                   '      })\n'
                   '    });\n'
                   '    const responseData = await response.json().catch(() => ({}));\n'
                   "    if (!response.ok || responseData.ok === false) throw new Error(responseData.error || '保存失败');\n"
                   '    hasUnsavedTextChanges = false;\n'
                   '    setEditorStatus(`已保存：${new Date().toLocaleString()}`);\n'
                   '  } catch (error) {\n'
                   '    setEditorStatus(error.message || String(error));\n'
                   '  } finally {\n'
                   '    saveTextButton.disabled = false;\n'
                   '  }\n'
                   '}\n'
                   '\n'
                   'if (textContentEditor) {\n'
                   "  textContentEditor.addEventListener('input', () => {\n"
                   '    if (isReadOnlyTextView) return;\n'
                   '    hasUnsavedTextChanges = true;\n'
                   "    setEditorStatus('有未保存修改。Ctrl/⌘ + S 保存。');\n"
                   '  });\n'
                   '\n'
                   "  textContentEditor.addEventListener('keydown', (event) => {\n"
                   "    if (event.key === 'Tab' && !isReadOnlyTextView) {\n"
                   '      event.preventDefault();\n'
                   '      const selectionStart = textContentEditor.selectionStart;\n'
                   '      const selectionEnd = textContentEditor.selectionEnd;\n'
                   "      textContentEditor.setRangeText('    ', selectionStart, selectionEnd, 'end');\n"
                   '      hasUnsavedTextChanges = true;\n'
                   "      setEditorStatus('有未保存修改。Ctrl/⌘ + S 保存。');\n"
                   '    }\n'
                   '  });\n'
                   '}\n'
                   '\n'
                   "saveTextButton?.addEventListener('click', saveTextDocument);\n"
                   '\n'
                   "document.addEventListener('keydown', (event) => {\n"
                   "  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {\n"
                   '    event.preventDefault();\n'
                   '    saveTextDocument();\n'
                   '  }\n'
                   '});\n'
                   '\n'
                   "window.addEventListener('beforeunload', (event) => {\n"
                   '  if (!hasUnsavedTextChanges) return;\n'
                   '  event.preventDefault();\n'
                   "  event.returnValue = '';\n"
                   '});\n'}

app.jinja_loader = DictLoader(TEMPLATE_FILES)


@app.route("/static/<path:filename>", endpoint="static")
def embedded_static(filename: str):
    content = STATIC_FILES.get(filename)
    if content is None:
        abort(404)
    mimetype = mimetypes.guess_type(filename)[0] or "text/plain"
    return Response(content, mimetype=mimetype)


AUDIO_EXTS = {
    ".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".webm", ".opus"
}
VIDEO_EXTS = {
    ".mp4", ".webm", ".ogv", ".mov", ".m4v", ".mkv", ".avi"
}
TEXT_EXTS = {
    ".txt", ".text", ".md", ".markdown", ".log", ".csv", ".tsv",
    ".json", ".jsonl", ".xml", ".html", ".htm", ".css", ".js", ".mjs",
    ".cjs", ".py", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go",
    ".rs", ".php", ".rb", ".swift", ".kt", ".kts", ".sh", ".bash",
    ".zsh", ".fish", ".bat", ".cmd", ".ps1", ".sql", ".ini", ".cfg",
    ".conf", ".yaml", ".yml", ".toml", ".env", ".gitignore", ".dockerignore"
}
TEXT_FALLBACK_ENCODINGS = [
    "utf-8", "utf-8-sig", "gb18030", "gbk", "big5", "shift_jis",
    "euc_jp", "cp949", "cp1252", "latin-1"
]


# -----------------------------
# SQLite helpers
# -----------------------------

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            parent_id INTEGER NULL,
            kind TEXT NOT NULL CHECK(kind IN ('file', 'folder')),
            name TEXT NOT NULL,
            storage_name TEXT NULL,
            size INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(owner_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(parent_id) REFERENCES entries(id) ON DELETE CASCADE
        );

        CREATE UNIQUE INDEX IF NOT EXISTS entries_unique_name_root
            ON entries(owner_id, name)
            WHERE parent_id IS NULL;

        CREATE UNIQUE INDEX IF NOT EXISTS entries_unique_name_folder
            ON entries(owner_id, parent_id, name)
            WHERE parent_id IS NOT NULL;

        CREATE TABLE IF NOT EXISTS shares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            item_id INTEGER NOT NULL,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NULL,
            FOREIGN KEY(item_id) REFERENCES entries(id) ON DELETE CASCADE,
            FOREIGN KEY(owner_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """
    )
    db.commit()


def cleanup_old_archives():
    """Delete generated 7z files older than ARCHIVE_RETENTION_SECONDS."""
    cutoff = dt.datetime.now().timestamp() - ARCHIVE_RETENTION_SECONDS
    for path in TMP_ARCHIVE_DIR.glob("*.7z"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            pass


@app.before_request
def ensure_db():
    init_db()
    cleanup_old_archives()
    user_id = session.get("user_id")
    g.user = None
    if user_id:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


# -----------------------------
# Presentation helpers
# -----------------------------

def media_kind(name: str | None) -> str | None:
    ext = Path(name or "").suffix.lower()
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def is_text_file(name: str | None) -> bool:
    name = name or ""
    if Path(name).suffix.lower() in TEXT_EXTS:
        return True
    guessed_mime_type = mimetypes.guess_type(name)[0]
    return bool(guessed_mime_type and guessed_mime_type.startswith("text/"))


def entry_icon(kind: str, name: str | None = None) -> str:
    if kind == "folder":
        return "📁"
    m = media_kind(name)
    if m == "audio":
        return "🎵"
    if m == "video":
        return "🎬"
    if is_text_file(name):
        return "📝"
    return "📄"


def kind_label(kind: str, name: str | None = None) -> str:
    if kind == "folder":
        return "文件夹"
    m = media_kind(name)
    if m == "audio":
        return "音频文件"
    if m == "video":
        return "视频文件"
    if is_text_file(name):
        return "文本文件"
    return "文件"


def file_size_label(size: int | None) -> str:
    size = int(size or 0)
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


@app.context_processor
def inject_template_helpers():
    return {
        "media_kind": media_kind,
        "is_text_file": is_text_file,
        "entry_icon": entry_icon,
        "kind_label": kind_label,
        "file_size_label": file_size_label,
    }


# -----------------------------
# Auth and password hashing
# -----------------------------

def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    """Return (salt_hex, sha3_256_hash_hex). Uses random salt + SHA3-256."""
    if salt_hex is None:
        salt_hex = os.urandom(16).hex()
    digest = hashlib.sha3_256((salt_hex + password).encode("utf-8")).hexdigest()
    return salt_hex, digest


def login_required():
    if not g.user:
        abort(401)


def current_user_id() -> int:
    login_required()
    return int(g.user["id"])


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            flash("用户名和密码不能为空。")
            return render_template("register.html")
        if len(password) < 6:
            flash("密码至少 6 位。")
            return render_template("register.html")

        salt, pwd_hash = hash_password(password)
        try:
            cur = get_db().execute(
                "INSERT INTO users(username, password_salt, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (username, salt, pwd_hash, now_iso()),
            )
            get_db().commit()
        except sqlite3.IntegrityError:
            flash("用户名已存在。")
            return render_template("register.html")

        session["user_id"] = cur.lastrowid
        return redirect(url_for("drive"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user:
            _salt, pwd_hash = hash_password(password, user["password_salt"])
            if pwd_hash == user["password_hash"]:
                session.clear()
                session["user_id"] = user["id"]
                return redirect(url_for("drive"))
        flash("用户名或密码错误。")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# -----------------------------
# Entry helpers
# -----------------------------

def row_to_dict(row):
    return dict(row) if row else None


def clean_segment(name: str) -> str:
    """Keep Unicode names, remove path separators/control chars, normalize empty names."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch >= " " and ch not in '<>:"|?*')
    name = name.strip().strip(".")
    return name[:180] or "未命名"


def split_upload_path(filename: str) -> list[str]:
    path = PurePosixPath((filename or "").replace("\\", "/"))
    parts = []
    for p in path.parts:
        if p in ("", ".", ".."):
            continue
        parts.append(clean_segment(p))
    return parts or ["未命名"]


def get_entry(entry_id: int | None, owner_id: int | None = None):
    if entry_id is None:
        return None
    db = get_db()
    if owner_id is None:
        return db.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    return db.execute(
        "SELECT * FROM entries WHERE id = ? AND owner_id = ?", (entry_id, owner_id)
    ).fetchone()


def require_owned_entry(entry_id: int):
    row = get_entry(entry_id, current_user_id())
    if not row:
        abort(404)
    return row


def require_folder_or_root(folder_id):
    owner_id = current_user_id()
    if folder_id in (None, "", "root"):
        return None
    folder_id = int(folder_id)
    row = get_entry(folder_id, owner_id)
    if not row or row["kind"] != "folder":
        abort(404)
    return row


def child_by_name(owner_id: int, parent_id: int | None, name: str):
    db = get_db()
    if parent_id is None:
        return db.execute(
            "SELECT * FROM entries WHERE owner_id = ? AND parent_id IS NULL AND name = ?",
            (owner_id, name),
        ).fetchone()
    return db.execute(
        "SELECT * FROM entries WHERE owner_id = ? AND parent_id = ? AND name = ?",
        (owner_id, parent_id, name),
    ).fetchone()


def unique_name(owner_id: int, parent_id: int | None, desired: str, exclude_id: int | None = None) -> str:
    desired = clean_segment(desired)
    path = Path(desired)
    stem = path.stem if path.suffix else desired
    suffix = path.suffix

    def exists(candidate: str) -> bool:
        db = get_db()
        params = [owner_id]
        if parent_id is None:
            sql = "SELECT id FROM entries WHERE owner_id = ? AND parent_id IS NULL AND name = ?"
            params.append(candidate)
        else:
            sql = "SELECT id FROM entries WHERE owner_id = ? AND parent_id = ? AND name = ?"
            params.extend([parent_id, candidate])
        if exclude_id is not None:
            sql += " AND id <> ?"
            params.append(exclude_id)
        return db.execute(sql, tuple(params)).fetchone() is not None

    if not exists(desired):
        return desired
    for i in range(1, 10000):
        candidate = f"{stem} ({i}){suffix}"
        if not exists(candidate):
            return candidate
    raise RuntimeError("无法生成唯一名称")


def insert_folder(owner_id: int, parent_id: int | None, name: str) -> int:
    name = unique_name(owner_id, parent_id, name)
    cur = get_db().execute(
        """
        INSERT INTO entries(owner_id, parent_id, kind, name, storage_name, size, created_at, updated_at)
        VALUES (?, ?, 'folder', ?, NULL, 0, ?, ?)
        """,
        (owner_id, parent_id, name, now_iso(), now_iso()),
    )
    get_db().commit()
    return int(cur.lastrowid)


def get_or_create_folder(owner_id: int, parent_id: int | None, name: str) -> int:
    name = clean_segment(name)
    existing = child_by_name(owner_id, parent_id, name)
    if existing and existing["kind"] == "folder":
        return int(existing["id"])
    return insert_folder(owner_id, parent_id, name)


def insert_file(owner_id: int, parent_id: int | None, name: str, storage_name: str, size: int) -> int:
    name = unique_name(owner_id, parent_id, name)
    cur = get_db().execute(
        """
        INSERT INTO entries(owner_id, parent_id, kind, name, storage_name, size, created_at, updated_at)
        VALUES (?, ?, 'file', ?, ?, ?, ?, ?)
        """,
        (owner_id, parent_id, name, storage_name, size, now_iso(), now_iso()),
    )
    get_db().commit()
    return int(cur.lastrowid)


def list_children(owner_id: int, parent_id: int | None):
    db = get_db()
    if parent_id is None:
        rows = db.execute(
            """
            SELECT * FROM entries
            WHERE owner_id = ? AND parent_id IS NULL
            ORDER BY kind DESC, lower(name) ASC
            """,
            (owner_id,),
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT * FROM entries
            WHERE owner_id = ? AND parent_id = ?
            ORDER BY kind DESC, lower(name) ASC
            """,
            (owner_id, parent_id),
        ).fetchall()
    return rows


def breadcrumb(owner_id: int, folder_id: int | None):
    crumbs = [{"id": None, "name": "我的云盘"}]
    if folder_id is None:
        return crumbs
    stack = []
    cur = get_entry(folder_id, owner_id)
    while cur:
        stack.append({"id": cur["id"], "name": cur["name"]})
        if cur["parent_id"] is None:
            break
        cur = get_entry(cur["parent_id"], owner_id)
    return crumbs + list(reversed(stack))


def is_descendant(candidate_id: int, possible_ancestor_id: int) -> bool:
    cur = get_entry(candidate_id)
    while cur and cur["parent_id"] is not None:
        if int(cur["parent_id"]) == int(possible_ancestor_id):
            return True
        cur = get_entry(cur["parent_id"])
    return False


def file_path(row) -> Path:
    return UPLOAD_DIR / str(row["owner_id"]) / row["storage_name"]


def iter_file_rows(root_id: int):
    root = get_entry(root_id)
    if not root:
        return
    if root["kind"] == "file":
        yield root, root["name"]
        return

    def walk(folder_id: int, prefix: str):
        for child in list_children(root["owner_id"], folder_id):
            rel = f"{prefix}/{child['name']}" if prefix else child["name"]
            if child["kind"] == "file":
                yield child, rel
            else:
                yield from walk(int(child["id"]), rel)

    yield from walk(root_id, root["name"])


def archive_filters():
    # LZMA2 level 9 with py7zr's extreme bit when the installed version exposes it.
    extreme = getattr(py7zr, "PRESET_EXTREME", 0)
    return [{"id": py7zr.FILTER_LZMA2, "preset": 9 | extreme}]


def create_7z_archive(row) -> Path:
    """Create a high-compression 7z archive on disk and return its path."""
    archive_path = TMP_ARCHIVE_DIR / f"{uuid.uuid4().hex}.7z"
    marker_path = None
    written = 0

    with py7zr.SevenZipFile(archive_path, "w", filters=archive_filters()) as archive:
        for file_row, rel in iter_file_rows(int(row["id"])):
            p = file_path(file_row)
            if not p.exists():
                continue
            archive.write(p, arcname=str(PurePosixPath(rel)))
            written += 1

        if written == 0:
            marker_path = TMP_ARCHIVE_DIR / f"{uuid.uuid4().hex}.txt"
            marker_path.write_text("这个目录当前没有文件。\n", encoding="utf-8")
            archive.write(marker_path, arcname=str(PurePosixPath(row["name"]) / "空目录说明.txt"))

    if marker_path:
        marker_path.unlink(missing_ok=True)
    return archive_path


def send_entry_download(row):
    if row["kind"] == "file":
        p = file_path(row)
        if not p.exists():
            abort(404)
        return send_file(p, as_attachment=True, download_name=row["name"], conditional=True)

    archive_path = create_7z_archive(row)
    return send_file(
        archive_path,
        mimetype="application/x-7z-compressed",
        as_attachment=True,
        download_name=f"{row['name']}.7z",
        conditional=True,
        max_age=0,
    )


def send_media_file(row):
    if row["kind"] != "file" or not media_kind(row["name"]):
        abort(404)
    p = file_path(row)
    if not p.exists():
        abort(404)
    mimetype = mimetypes.guess_type(row["name"])[0] or "application/octet-stream"
    return send_file(p, as_attachment=False, download_name=row["name"], mimetype=mimetype, conditional=True)


def detect_bom_encoding(raw_content: bytes) -> str | None:
    if raw_content.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw_content.startswith(b"\xff\xfe\x00\x00"):
        return "utf-32-le"
    if raw_content.startswith(b"\x00\x00\xfe\xff"):
        return "utf-32-be"
    if raw_content.startswith(b"\xff\xfe"):
        return "utf-16-le"
    if raw_content.startswith(b"\xfe\xff"):
        return "utf-16-be"
    return None


def detect_text_encoding(raw_content: bytes) -> str:
    bom_encoding = detect_bom_encoding(raw_content)
    if bom_encoding:
        return bom_encoding
    if not raw_content:
        return "utf-8"

    if detect_character_sets is not None:
        detection_result = detect_character_sets(raw_content).best()
        if detection_result and detection_result.encoding:
            return detection_result.encoding

    for encoding_name in TEXT_FALLBACK_ENCODINGS:
        try:
            raw_content.decode(encoding_name)
            return encoding_name
        except UnicodeDecodeError:
            continue
    return "utf-8"


def decode_text_content(raw_content: bytes) -> tuple[str, str]:
    preferred_encoding = detect_text_encoding(raw_content)
    attempted_encodings = [preferred_encoding] + [
        encoding_name for encoding_name in TEXT_FALLBACK_ENCODINGS
        if encoding_name.lower() != preferred_encoding.lower()
    ]
    for encoding_name in attempted_encodings:
        try:
            return raw_content.decode(encoding_name), encoding_name
        except (LookupError, UnicodeDecodeError):
            continue
    return raw_content.decode("utf-8", errors="replace"), "utf-8"


def detect_newline_style(text_content: str) -> str:
    crlf_count = text_content.count("\r\n")
    without_crlf = text_content.replace("\r\n", "")
    lf_count = without_crlf.count("\n")
    cr_count = without_crlf.count("\r")
    counts = {"\r\n": crlf_count, "\n": lf_count, "\r": cr_count}
    newline_style = max(counts, key=counts.get)
    return newline_style if counts[newline_style] > 0 else "\n"


def read_text_document(row) -> dict[str, str]:
    if row["kind"] != "file" or not is_text_file(row["name"]):
        abort(404)
    path = file_path(row)
    if not path.exists():
        abort(404)
    raw_content = path.read_bytes()
    text_content, detected_encoding = decode_text_content(raw_content)
    return {
        "content": text_content,
        "encoding": detected_encoding,
        "newline_style": detect_newline_style(text_content),
    }


def normalize_newlines(text_content: str, newline_style: str) -> str:
    if newline_style not in {"\n", "\r\n", "\r"}:
        newline_style = "\n"
    normalized = text_content.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", newline_style)


def save_text_document(row, text_content: str, encoding_name: str, newline_style: str):
    if row["kind"] != "file" or not is_text_file(row["name"]):
        abort(404)
    path = file_path(row)
    if not path.exists():
        abort(404)
    normalized_content = normalize_newlines(text_content, newline_style)
    try:
        encoded_content = normalized_content.encode(encoding_name)
    except (LookupError, UnicodeEncodeError) as exc:
        raise ValueError(f"当前文本无法用 {encoding_name} 编码保存，请移除不兼容字符或另存为 UTF-8。") from exc
    path.write_bytes(encoded_content)
    get_db().execute(
        "UPDATE entries SET size = ?, updated_at = ? WHERE id = ?",
        (path.stat().st_size, now_iso(), row["id"]),
    )
    get_db().commit()


def delete_storage_under(entry_id: int):
    row = get_entry(entry_id)
    if not row:
        return
    if row["kind"] == "file":
        try:
            file_path(row).unlink(missing_ok=True)
        except OSError:
            pass
        return
    for child in list_children(row["owner_id"], entry_id):
        delete_storage_under(int(child["id"]))


def is_shared_descendant(item_id: int, root_id: int) -> bool:
    if int(item_id) == int(root_id):
        return True
    return is_descendant(int(item_id), int(root_id))


# -----------------------------
# Drive pages
# -----------------------------

@app.route("/")
def home():
    if g.user:
        return redirect(url_for("drive"))
    return redirect(url_for("login"))


@app.route("/drive/")
@app.route("/drive/<int:folder_id>")
def drive(folder_id=None):
    login_required()
    owner_id = current_user_id()
    folder = require_folder_or_root(folder_id)
    parent_id = int(folder["id"]) if folder else None
    entries = list_children(owner_id, parent_id)
    return render_template(
        "drive.html",
        entries=entries,
        current_folder_id=parent_id,
        crumbs=breadcrumb(owner_id, parent_id),
    )


@app.route("/preview/<int:item_id>")
def preview(item_id):
    row = require_owned_entry(item_id)
    if not media_kind(row["name"]):
        abort(404)
    return render_template(
        "media.html",
        item=row,
        token=None,
        mode="owner",
        stream_url=url_for("media_stream", item_id=item_id),
        download_url=url_for("download", item_id=item_id),
    )


@app.route("/media/<int:item_id>")
def media_stream(item_id):
    row = require_owned_entry(item_id)
    return send_media_file(row)


@app.route("/text/<int:item_id>")
def text_editor(item_id):
    row = require_owned_entry(item_id)
    text_document = read_text_document(row)
    return render_template(
        "text_editor.html",
        item=row,
        read_only=False,
        text_document=text_document,
        save_url=url_for("api_save_text_document", item_id=item_id),
        download_url=url_for("download", item_id=item_id),
        back_url=url_for("drive", folder_id=row["parent_id"]) if row["parent_id"] else url_for("drive"),
    )


@app.post("/api/text/<int:item_id>")
def api_save_text_document(item_id):
    row = require_owned_entry(item_id)
    payload = request.get_json(force=True)
    text_content = payload.get("content", "")
    encoding_name = payload.get("encoding") or read_text_document(row)["encoding"]
    newline_style = payload.get("newline_style") or "\n"
    try:
        save_text_document(row, text_content, encoding_name, newline_style)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    return jsonify(ok=True, saved_at=now_iso())


@app.route("/shares")
def shares_page():
    owner_id = current_user_id()
    rows = get_db().execute(
        """
        SELECT shares.*, entries.kind, entries.name, entries.size
        FROM shares
        JOIN entries ON entries.id = shares.item_id
        WHERE shares.owner_id = ?
        ORDER BY shares.created_at DESC
        """,
        (owner_id,),
    ).fetchall()
    return render_template("shares.html", shares=rows)


# -----------------------------
# API
# -----------------------------

@app.post("/api/folders")
def api_create_folder():
    owner_id = current_user_id()
    data = request.get_json(force=True)
    parent = require_folder_or_root(data.get("parent_id"))
    parent_id = int(parent["id"]) if parent else None
    name = clean_segment(data.get("name", "新建文件夹"))
    folder_id = insert_folder(owner_id, parent_id, name)
    return jsonify(ok=True, id=folder_id)


@app.post("/api/upload")
def api_upload():
    owner_id = current_user_id()
    parent = require_folder_or_root(request.form.get("parent_id"))
    parent_id = int(parent["id"]) if parent else None
    files = request.files.getlist("files")
    if not files:
        return jsonify(ok=False, error="没有选择文件。"), 400

    user_dir = UPLOAD_DIR / str(owner_id)
    user_dir.mkdir(parents=True, exist_ok=True)

    created = []
    folder_cache: dict[tuple[int | None, str], int] = {}

    for f in files:
        if not f or not f.filename:
            continue
        parts = split_upload_path(f.filename)
        filename = parts[-1]
        cur_parent = parent_id
        for segment in parts[:-1]:
            cache_key = (cur_parent, segment)
            if cache_key not in folder_cache:
                folder_cache[cache_key] = get_or_create_folder(owner_id, cur_parent, segment)
            cur_parent = folder_cache[cache_key]

        storage_name = uuid.uuid4().hex
        target = user_dir / storage_name
        f.save(target)
        created.append(insert_file(owner_id, cur_parent, filename, storage_name, target.stat().st_size))

    return jsonify(ok=True, created=created)


@app.post("/api/rename")
def api_rename():
    owner_id = current_user_id()
    data = request.get_json(force=True)
    row = require_owned_entry(int(data.get("item_id")))
    new_name = unique_name(owner_id, row["parent_id"], data.get("name", row["name"]), exclude_id=int(row["id"]))
    get_db().execute(
        "UPDATE entries SET name = ?, updated_at = ? WHERE id = ?",
        (new_name, now_iso(), row["id"]),
    )
    get_db().commit()
    return jsonify(ok=True, name=new_name)


@app.post("/api/delete")
def api_delete():
    data = request.get_json(force=True)
    row = require_owned_entry(int(data.get("item_id")))
    delete_storage_under(int(row["id"]))
    get_db().execute("DELETE FROM entries WHERE id = ?", (row["id"],))
    get_db().commit()
    return jsonify(ok=True)


@app.post("/api/move")
def api_move():
    owner_id = current_user_id()
    data = request.get_json(force=True)
    item = require_owned_entry(int(data.get("item_id")))
    target = require_folder_or_root(data.get("target_parent_id"))
    target_parent_id = int(target["id"]) if target else None

    if item["kind"] == "folder" and target_parent_id is not None:
        if int(item["id"]) == target_parent_id or is_descendant(target_parent_id, int(item["id"])):
            return jsonify(ok=False, error="不能把文件夹移动到它自己或它的子目录中。"), 400

    if item["parent_id"] == target_parent_id:
        return jsonify(ok=True)

    new_name = unique_name(owner_id, target_parent_id, item["name"], exclude_id=int(item["id"]))
    get_db().execute(
        "UPDATE entries SET parent_id = ?, name = ?, updated_at = ? WHERE id = ?",
        (target_parent_id, new_name, now_iso(), item["id"]),
    )
    get_db().commit()
    return jsonify(ok=True, name=new_name)


@app.post("/api/share")
def api_share():
    owner_id = current_user_id()
    data = request.get_json(force=True)
    row = require_owned_entry(int(data.get("item_id")))
    token = uuid.uuid4().hex
    get_db().execute(
        "INSERT INTO shares(token, item_id, owner_id, created_at, expires_at) VALUES (?, ?, ?, ?, NULL)",
        (token, row["id"], owner_id, now_iso()),
    )
    get_db().commit()
    return jsonify(ok=True, url=url_for("share_view", token=token, _external=True))


@app.post("/api/shares/<int:share_id>/cancel")
def api_cancel_share(share_id):
    owner_id = current_user_id()
    cur = get_db().execute(
        "DELETE FROM shares WHERE id = ? AND owner_id = ?",
        (share_id, owner_id),
    )
    get_db().commit()
    if cur.rowcount == 0:
        return jsonify(ok=False, error="分享不存在或无权限。"), 404
    return jsonify(ok=True)


@app.get("/download/<int:item_id>")
def download(item_id):
    row = require_owned_entry(item_id)
    return send_entry_download(row)


# -----------------------------
# Shared pages
# -----------------------------

def get_share(token: str):
    row = get_db().execute(
        """
        SELECT shares.*, entries.kind, entries.name
        FROM shares
        JOIN entries ON entries.id = shares.item_id
        WHERE shares.token = ?
        """,
        (token,),
    ).fetchone()
    if not row:
        abort(404)
    if row["expires_at"] and row["expires_at"] < now_iso():
        abort(410)
    return row


@app.route("/s/<token>/")
@app.route("/s/<token>/<int:folder_id>")
def share_view(token, folder_id=None):
    share = get_share(token)
    root = get_entry(int(share["item_id"]))
    if not root:
        abort(404)

    if root["kind"] == "file":
        return render_template("share_file.html", share=share, item=root, token=token)

    current_id = int(root["id"]) if folder_id is None else int(folder_id)
    if not is_shared_descendant(current_id, int(root["id"])):
        abort(403)
    current = get_entry(current_id)
    if not current or current["kind"] != "folder":
        abort(404)

    entries = list_children(int(root["owner_id"]), current_id)

    crumbs = [{"id": int(root["id"]), "name": root["name"]}]
    if current_id != int(root["id"]):
        stack = []
        cur = current
        while cur and int(cur["id"]) != int(root["id"]):
            stack.append({"id": int(cur["id"]), "name": cur["name"]})
            cur = get_entry(cur["parent_id"])
        crumbs += list(reversed(stack))

    return render_template(
        "share_folder.html",
        share=share,
        entries=entries,
        crumbs=crumbs,
        token=token,
        current_folder_id=current_id,
    )


@app.get("/s/<token>/download/<int:item_id>")
def share_download(token, item_id):
    share = get_share(token)
    root_id = int(share["item_id"])
    if not is_shared_descendant(item_id, root_id):
        abort(403)
    row = get_entry(item_id)
    if not row:
        abort(404)
    return send_entry_download(row)


@app.get("/s/<token>/archive/<int:folder_id>")
def share_archive_download(token, folder_id):
    share = get_share(token)
    root_id = int(share["item_id"])
    if not is_shared_descendant(folder_id, root_id):
        abort(403)
    row = get_entry(folder_id)
    if not row or row["kind"] != "folder":
        abort(404)
    return send_entry_download(row)


@app.route("/s/<token>/preview/<int:item_id>")
def share_preview(token, item_id):
    share = get_share(token)
    root_id = int(share["item_id"])
    if not is_shared_descendant(item_id, root_id):
        abort(403)
    row = get_entry(item_id)
    if not row or not media_kind(row["name"]):
        abort(404)
    return render_template(
        "media.html",
        item=row,
        token=token,
        mode="share",
        stream_url=url_for("share_media_stream", token=token, item_id=item_id),
        download_url=url_for("share_download", token=token, item_id=item_id),
    )


@app.route("/s/<token>/text/<int:item_id>")
def share_text_viewer(token, item_id):
    share = get_share(token)
    root_id = int(share["item_id"])
    if not is_shared_descendant(item_id, root_id):
        abort(403)
    row = get_entry(item_id)
    if not row:
        abort(404)
    text_document = read_text_document(row)
    return render_template(
        "text_editor.html",
        item=row,
        read_only=True,
        text_document=text_document,
        save_url=None,
        download_url=url_for("share_download", token=token, item_id=item_id),
        back_url=url_for("share_view", token=token, folder_id=row["parent_id"])
        if row["parent_id"] and is_shared_descendant(int(row["parent_id"]), root_id)
        else url_for("share_view", token=token),
    )


@app.route("/s/<token>/media/<int:item_id>")
def share_media_stream(token, item_id):
    share = get_share(token)
    root_id = int(share["item_id"])
    if not is_shared_descendant(item_id, root_id):
        abort(403)
    row = get_entry(item_id)
    if not row:
        abort(404)
    return send_media_file(row)


if __name__ == "__main__":
    app.run(debug=True)
