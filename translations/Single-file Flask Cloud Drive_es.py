#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unidad en la nube Flask en un solo archivo
=============================================
Resumen:
    Esta es una versión de despliegue en un solo archivo. Las rutas del backend,
    las plantillas HTML, el CSS y el JavaScript están incrustados en app.py. Para
    desplegarla, copia este archivo e instala las dependencias necesarias.
Funciones principales:
    - Inicio de sesión, registro, cierre de sesión y cambio de contraseña.
    - Resúmenes de contraseña almacenados con una sal aleatoria y SHA3-256.
    - Carpetas jerárquicas, carga de archivos/carpetas, listado, descarga, cambio de nombre,
      eliminación y soporte para mover mediante arrastrar y soltar.
    - Uso compartido de archivos/carpetas, gestión de compartidos y cancelación del uso compartido.
    - Las carpetas compartidas se pueden navegar; las descargas de carpetas generan archivos
      .7z temporales sin cargar todo el archivo comprimido en memoria.
    - Reproducción en línea de audio/vídeo.
    - Edición en línea de texto plano; las páginas compartidas admiten vista de texto de solo lectura.
    - La lectura de texto detecta BOM y codificaciones comunes; al guardar intenta conservar
      la codificación original y el estilo de salto de línea.
Dependencias:
    - flask: servidor web, rutas, sesiones y renderizado de plantillas.
    - py7zr: crea archivos .7z.
    - charset-normalizer: ayuda a detectar codificaciones de archivos de texto.
Instalar:
    pip install flask py7zr charset-normalizer
Ejecutar:
    python app.py
Variables de entorno opcionales:
    SECRET_KEY  # Secreto fijo de sesión; generar con: python -c 'import secrets; print(secrets.token_hex())'
    DRIVE_DATA_DIR
    MAX_CONTENT_LENGTH
    ARCHIVE_RETENTION_SECONDS
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
except ImportError:  # pragma: no cover - alternativa en tiempo de ejecución si falta la dependencia
    detect_character_sets = None


BASE_DIR = Path(__file__).resolve().parent  # Directorio que contiene app.py
DATA_DIR = Path(os.environ.get("DRIVE_DATA_DIR", BASE_DIR / "data"))  # Directorio raíz de datos; puede sobrescribirse con una variable de entorno
UPLOAD_DIR = DATA_DIR / "uploads"  # Directorio para archivos fuente subidos
TMP_ARCHIVE_DIR = DATA_DIR / "tmp_archives"  # Directorio temporal de archivos .7z
DB_PATH = DATA_DIR / "drive.db"  # Archivo de base de datos SQLite
ARCHIVE_RETENTION_SECONDS = int(os.environ.get("ARCHIVE_RETENTION_SECONDS", 24 * 60 * 60))  # Tiempo de retención de archivos temporales

DATA_DIR.mkdir(parents=True, exist_ok=True)  # Crea el directorio de datos en el primer inicio
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)  # Crea el directorio de subidas en el primer inicio
TMP_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)  # Crea el directorio temporal de archivos en el primer inicio

def load_secret_key() -> str:
    """Carga una clave secreta estable de Flask desde el entorno o desde un archivo de datos local.

    Nota de despliegue:
        Puedes generar una clave fija una vez y configurarla como SECRET_KEY:
        python -c 'import secrets; print(secrets.token_hex())'

    Mantener SECRET_KEY estable evita invalidar sesiones firmadas existentes después de reiniciar.
    """
    # Prefiere una clave proporcionada por el operador para que los despliegues de producción permanezcan fijos.
    configured_secret_key = os.environ.get("SECRET_KEY")
    if configured_secret_key:
        return configured_secret_key

    secret_key_path = DATA_DIR / ".secret_key"
    if secret_key_path.exists():
        return secret_key_path.read_text(encoding="utf-8").strip()

    # Alternativa local: crea una clave estable en la primera ejecución y la reutiliza después.
    generated_secret_key = os.urandom(32).hex()
    secret_key_path.write_text(generated_secret_key, encoding="utf-8")
    try:
        # Solo mejor esfuerzo: chmod puede no estar soportado en todas las plataformas.
        secret_key_path.chmod(0o600)
    except OSError:
        pass
    return generated_secret_key


app = Flask(__name__, static_folder=None)  # Los recursos estáticos se sirven mediante rutas incrustadas
app.secret_key = load_secret_key()  # Se usa para firmar las sesiones de Flask
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_CONTENT_LENGTH", 1024 * 1024 * 1024))  # Tamaño máximo de subida predeterminado: 1 GB
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE") == "1",
)

# -----------------------------
# Plantillas incrustadas y recursos estáticos
# -----------------------------

TEMPLATE_FILES = {
    'base.html': r"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Unidad en la nube{% endblock %}</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet" integrity="sha384-sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB" crossorigin="anonymous">
  <link rel="stylesheet" href="{{ url_for('static', filename='app.css') }}">
</head>
<body>
  <main class="page-shell">
    {% block body %}{% endblock %}
  </main>
  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/js/bootstrap.bundle.min.js" integrity="sha384-FKyoEForCGlyvwx9Hj09JcYn3nv7wiPVlz7YYwJrWVcXK/BmnVDxM+D2scQbITxI" crossorigin="anonymous"></script>
  {% block scripts %}{% endblock %}
</body>
</html>
""",
    'login.html': r"""{% extends "base.html" %}
{% block title %}Iniciar sesión{% endblock %}
{% block body %}
<section class="auth-layout">
  <div class="auth-panel">
    <h1 class="auth-title">Iniciar sesión</h1>
    {% for msg in get_flashed_messages() %}<div class="alert alert-warning py-2">{{ msg }}</div>{% endfor %}
    <form method="post" class="auth-form">
      <label class="form-label">Nombre de usuario</label>
      <input class="form-control form-control-lg" name="username" autocomplete="username" required>
      <label class="form-label mt-4">Contraseña</label>
      <input class="form-control form-control-lg" name="password" type="password" autocomplete="current-password" required>
      <button class="btn btn-orange btn-lg w-100 mt-4" type="submit">Iniciar sesión</button>
    </form>
    <div class="auth-links"><span></span><a href="{{ url_for('register') }}">Registrarse</a></div>
  </div>
</section>
{% endblock %}
""",
    'register.html': r"""{% extends "base.html" %}
{% block title %}Registrarse{% endblock %}
{% block body %}
<section class="auth-layout">
  <div class="auth-panel">
    <h1 class="auth-title">Registrarse</h1>
    {% for msg in get_flashed_messages() %}<div class="alert alert-warning py-2">{{ msg }}</div>{% endfor %}
    <form method="post" class="auth-form">
      <label class="form-label">Nombre de usuario</label>
      <input class="form-control form-control-lg" name="username" autocomplete="username" required>
      <label class="form-label mt-4">Contraseña</label>
      <input class="form-control form-control-lg" name="password" type="password" autocomplete="new-password" required>
      <button class="btn btn-orange btn-lg w-100 mt-4" type="submit">Registrarse</button>
    </form>
    <div class="auth-links"><a href="{{ url_for('login') }}">Iniciar sesión</a><span></span></div>
  </div>
</section>
{% endblock %}
""",
    'change_password.html': r"""{% extends "base.html" %}
{% block title %}Cambiar contraseña{% endblock %}
{% block body %}
<section class="auth-layout">
  <div class="auth-panel">
    <h1 class="auth-title">Cambiar contraseña</h1>
    {% for msg in get_flashed_messages() %}<div class="alert alert-warning py-2">{{ msg }}</div>{% endfor %}
    <form method="post" class="auth-form">
      <label class="form-label">Contraseña actual</label>
      <input class="form-control form-control-lg" name="current_password" type="password" autocomplete="current-password" required>
      <label class="form-label mt-4">Nueva contraseña</label>
      <input class="form-control form-control-lg" name="new_password" type="password" autocomplete="new-password" required>
      <label class="form-label mt-4">Confirmar nueva contraseña</label>
      <input class="form-control form-control-lg" name="confirm_password" type="password" autocomplete="new-password" required>
      <button class="btn btn-orange btn-lg w-100 mt-4" type="submit">Guardar</button>
    </form>
    <div class="auth-links"><a href="{{ url_for('drive') }}">Volver</a><a href="{{ url_for('logout') }}">Cerrar sesión</a></div>
  </div>
</section>
{% endblock %}
""",
    'drive.html': r"""{% extends "base.html" %}
{% block title %}Archivos{% endblock %}
{% block body %}
<div class="drive-shell" data-current-folder-id="{{ current_folder_id or '' }}">
  <header class="drive-header">
    <div class="drive-title-area">
      <nav aria-label="breadcrumb">
        <ol class="breadcrumb mb-0">
          {% for c in crumbs %}
            <li class="breadcrumb-item {% if loop.last %}active{% endif %}">
              {% if not loop.last %}
                <a class="breadcrumb-target" href="{{ url_for('drive', folder_id=c.id) if c.id else url_for('drive') }}" data-folder-id="{{ c.id or '' }}">{{ c.name }}</a>
              {% else %}
                <span class="breadcrumb-target" data-folder-id="{{ c.id or '' }}">{{ c.name }}</span>
              {% endif %}
            </li>
          {% endfor %}
        </ol>
      </nav>
    </div>
  </header>

  <section id="dropArea" class="file-board" aria-label="Lista de archivos">
    {% for e in entries %}
      <div class="entry-card {{ e.kind }}-entry" draggable="true" data-id="{{ e.id }}" data-kind="{{ e.kind }}" data-name="{{ e.name }}" data-media-kind="{{ media_kind(e.name) or '' }}" data-is-text="{{ '1' if is_text_file(e.name) else '0' }}">
        <div class="entry-icon {{ e.kind }} {{ media_kind(e.name) or ('text' if is_text_file(e.name) else '') }}">{{ entry_icon(e.kind, e.name) }}</div>
        <div class="entry-main">
          <div class="entry-name" title="{{ e.name }}">{{ e.name }}</div>
          <div class="entry-meta">
            {% if e.kind == 'folder' %}
              <span class="kind-badge folder-badge">Carpeta</span>
            {% else %}
              <span class="kind-badge file-badge">{{ kind_label(e.kind, e.name) }}</span><span>{{ file_size_label(e.size) }}</span>
            {% endif %}
          </div>
        </div>
      </div>
    {% else %}
      <div class="empty-hint">Haz clic derecho en el espacio vacío.</div>
    {% endfor %}
  </section>

  <input id="fileInput" type="file" multiple hidden>
  <input id="folderInput" type="file" webkitdirectory directory multiple hidden>

  <div id="contextMenu" class="context-menu shadow-sm" role="menu"></div>
  <div id="toastHost" class="toast-host"></div>
</div>
{% endblock %}
{% block scripts %}
<script src="{{ url_for('static', filename='app.js') }}"></script>
{% endblock %}
""",
    'media.html': r"""{% extends "base.html" %}
{% block title %}Reproducir en línea - {{ item.name }}{% endblock %}
{% block body %}
<section class="media-shell">
  <header class="media-header">
    <div>
      <h1 class="media-title">{{ item.name }}</h1>
      <p class="media-meta">{{ kind_label(item.kind, item.name) }} · {{ file_size_label(item.size) }}</p>
    </div>
    <a class="mini-download" href="{{ download_url }}">Descargar</a>
  </header>

  <div class="player-panel {% if media_kind(item.name) == 'audio' %}audio-panel{% endif %}">
    {% if media_kind(item.name) == 'audio' %}
      <audio controls preload="metadata" src="{{ stream_url }}"></audio>
    {% else %}
      <video controls preload="metadata" src="{{ stream_url }}"></video>
    {% endif %}
  </div>
</section>
{% endblock %}
""",
    'share_file.html': r"""{% extends "base.html" %}
{% block title %}{{ item.name }}{% endblock %}
{% block body %}
<section class="media-shell share-single-file" data-share-token="{{ token }}">
  <header class="media-header">
    <div>
      <h1 class="media-title">{{ item.name }}</h1>
      <p class="media-meta">{{ kind_label(item.kind, item.name) }} · {{ file_size_label(item.size) }}</p>
    </div>
  </header>

  <section id="sharedFileBoard" class="file-board single-shared-file-board" aria-label="Archivo compartido">
    <div class="entry-card shared-entry file-entry"
         data-id="{{ item.id }}"
         data-kind="file"
         data-name="{{ item.name }}"
         data-download-url="{{ url_for('share_download', token=token, item_id=item.id) }}"
         data-play-url="{{ url_for('share_preview', token=token, item_id=item.id) if media_kind(item.name) else '' }}"
         data-text-url="{{ url_for('share_text_viewer', token=token, item_id=item.id) if is_text_file(item.name) else '' }}"
         data-media-kind="{{ media_kind(item.name) or '' }}"
         data-is-text="{{ '1' if is_text_file(item.name) else '0' }}">
      <div class="entry-icon file {{ media_kind(item.name) or ('text' if is_text_file(item.name) else '') }}">{{ entry_icon(item.kind, item.name) }}</div>
      <div class="entry-main">
        <div class="entry-name" title="{{ item.name }}">{{ item.name }}</div>
        <div class="entry-meta"><span class="kind-badge file-badge">{{ kind_label(item.kind, item.name) }}</span><span>{{ file_size_label(item.size) }}</span></div>
      </div>
    </div>
  </section>

  <div id="sharedContextMenu" class="context-menu shadow-sm" role="menu"></div>
</section>
{% endblock %}
{% block scripts %}
<script src="{{ url_for('static', filename='share_page.js') }}"></script>
{% endblock %}
""",
    'share_folder.html': r"""{% extends "base.html" %}
{% block title %}{{ crumbs[0].name }}{% endblock %}
{% block body %}
<div class="drive-shell shared" data-share-token="{{ token }}">
  <header class="drive-header">
    <div class="drive-title-area">
      <nav aria-label="breadcrumb">
        <ol class="breadcrumb mb-0">
          {% for c in crumbs %}
            <li class="breadcrumb-item {% if loop.last %}active{% endif %}">
              {% if not loop.last %}
                <a href="{{ url_for('share_view', token=token, folder_id=c.id) }}">{{ c.name }}</a>
              {% else %}
                <span>{{ c.name }}</span>
              {% endif %}
            </li>
          {% endfor %}
        </ol>
      </nav>
    </div>
    <div class="drive-nav">
      <a class="mini-download" data-shared-archive-link="1" data-prepare-url="{{ url_for('share_prepare_archive_download', token=token, folder_id=current_folder_id) }}" href="{{ url_for('share_archive_download', token=token, folder_id=current_folder_id) }}">Descargar todo en .7z</a>
    </div>
  </header>

  <section id="sharedFileBoard" class="file-board shared-board" aria-label="Lista de archivos compartidos">
    {% for e in entries %}
      <div class="entry-card shared-entry {{ e.kind }}-entry"
           data-id="{{ e.id }}"
           data-kind="{{ e.kind }}"
           data-name="{{ e.name }}"
           data-open-url="{{ url_for('share_view', token=token, folder_id=e.id) if e.kind == 'folder' else '' }}"
           data-download-url="{{ url_for('share_download', token=token, item_id=e.id) }}"
           data-play-url="{{ url_for('share_preview', token=token, item_id=e.id) if media_kind(e.name) else '' }}"
           data-text-url="{{ url_for('share_text_viewer', token=token, item_id=e.id) if is_text_file(e.name) else '' }}"
           data-media-kind="{{ media_kind(e.name) or '' }}"
           data-is-text="{{ '1' if is_text_file(e.name) else '0' }}">
        <div class="entry-icon {{ e.kind }} {{ media_kind(e.name) or ('text' if is_text_file(e.name) else '') }}">{{ entry_icon(e.kind, e.name) }}</div>
        <div class="entry-main">
          <div class="entry-name" title="{{ e.name }}">{{ e.name }}</div>
          <div class="entry-meta">
            {% if e.kind == 'folder' %}
              <span class="kind-badge folder-badge">Carpeta</span>
            {% else %}
              <span class="kind-badge file-badge">{{ kind_label(e.kind, e.name) }}</span><span>{{ file_size_label(e.size) }}</span>
            {% endif %}
          </div>
        </div>
      </div>
    {% else %}
      <div class="empty-hint">Vacío.</div>
    {% endfor %}
  </section>

  <div id="sharedContextMenu" class="context-menu shadow-sm" role="menu"></div>
</div>
{% endblock %}
{% block scripts %}
<script src="{{ url_for('static', filename='share_page.js') }}"></script>
{% endblock %}
""",
    'shares.html': r"""{% extends "base.html" %}
{% block title %}Gestión de compartidos{% endblock %}
{% block body %}
<div class="shares-shell">
  <header class="drive-header shares-header">
    <div class="drive-title-area">
      <nav aria-label="breadcrumb">
        <ol class="breadcrumb mb-0">
          <li class="breadcrumb-item"><a href="{{ url_for('drive') }}">Archivos</a></li>
          <li class="breadcrumb-item active"><span>Gestión de compartidos</span></li>
        </ol>
      </nav>
    </div>
    <div class="drive-nav">
      <a class="nav-pill" href="{{ url_for('change_password') }}">Cambiar contraseña</a>
      <a class="nav-pill" href="{{ url_for('logout') }}">Cerrar sesión</a>
    </div>
  </header>

  <section class="share-list">
    {% for s in shares %}
      {% set share_url = url_for('share_view', token=s.token, _external=True) %}
      <article class="share-row" data-share-id="{{ s.id }}" data-share-url="{{ share_url }}">
        <div class="share-row-main">
          <div class="entry-icon {{ s.kind }} {{ media_kind(s.name) or '' }}">{{ entry_icon(s.kind, s.name) }}</div>
          <div class="share-info">
            <div class="entry-name" title="{{ s.name }}">{{ s.name }}</div>
            <div class="entry-meta">
              <span class="kind-badge {{ 'folder-badge' if s.kind == 'folder' else 'file-badge' }}">{{ 'Carpeta' if s.kind == 'folder' else kind_label(s.kind, s.name) }}</span>
              <span>{{ s.created_at }}</span>
            </div>
            <input class="share-url-input" value="{{ share_url }}" readonly>
          </div>
        </div>
        <div class="share-actions">
          <a class="ghost-action" href="{{ share_url }}" target="_blank" rel="noopener">Abrir</a>
          <button class="ghost-action copy-share" type="button">Copiar</button>
          <button class="ghost-action danger cancel-share" type="button">Cancelar compartido</button>
        </div>
      </article>
    {% else %}
      <div class="empty-hint share-empty">No hay elementos compartidos.</div>
    {% endfor %}
  </section>
  <div id="toastHost" class="toast-host"></div>
</div>
{% endblock %}
{% block scripts %}
<script src="{{ url_for('static', filename='shares.js') }}"></script>
{% endblock %}
""",
    'text_editor.html': r"""{% extends "base.html" %}
{% block title %}{% if read_only %}Ver{% else %}Editar{% endif %} - {{ item.name }}{% endblock %}
{% block body %}
<section class="text-editor-shell" data-save-url="{{ save_url or '' }}" data-read-only="{{ '1' if read_only else '0' }}">
  <header class="text-editor-header">
    <div class="text-editor-title-area">
      <span class="text-editor-title" title="{{ item.name }}">{{ item.name }}</span>
      <span class="text-editor-meta">{{ text_document.encoding }} · {{ newline_style_label(text_document.newline_style) }} · {{ file_size_label(item.size) }}</span>
    </div>
    <div class="text-editor-actions">
      <span id="textEditorStatus" class="text-editor-status">{% if read_only %}Solo lectura{% else %}Cargado{% endif %}</span>
      <a class="text-editor-link" href="{{ download_url }}">Descargar</a>
      {% if not read_only %}
        <button id="saveTextButton" class="text-editor-button" type="button">Guardar</button>
      {% endif %}
    </div>
  </header>

  <textarea id="textContentEditor"
            class="text-content-editor"
            spellcheck="false"
            data-encoding="{{ text_document.encoding }}"
            data-newline-code="{{ newline_style_code(text_document.newline_style) }}"
            {% if read_only %}readonly{% endif %}>{{ text_document.content }}</textarea>
</section>
{% endblock %}
{% block scripts %}
<script src="{{ url_for('static', filename='text_editor.js') }}"></script>
{% endblock %}
""",
}

STATIC_FILES = {
    'app.css': r""":root {
  --orange-25: #fffaf6;
  --orange-50: #fff2e6;
  --orange-100: #ffe0c4;
  --orange-200: #ffc38f;
  --orange-500: #ea8439;
  --orange-600: #c96622;
  --red-orange: #ff6844;
  --text-main: #24170f;
  --muted: #7e6856;
  --line: rgba(183, 113, 54, .16);
  --surface: rgba(255, 255, 255, .68);
}

html,
body {
  width: 100%;
  min-height: 100%;
}

body {
  margin: 0;
  color: var(--text-main);
  background:
    radial-gradient(circle at 0% 0%, rgba(255, 195, 143, .38), transparent 30rem),
    radial-gradient(circle at 100% 10%, rgba(255, 224, 196, .54), transparent 34rem),
    linear-gradient(135deg, #fffaf6 0%, #fff1e5 48%, #fff7ee 100%);
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

a {
  color: var(--orange-600);
  text-decoration: none;
}

a:hover {
  color: #98410c;
}

.page-shell {
  min-height: 100vh;
  padding: 0;
}

.auth-layout {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 22px;
}

.auth-panel {
  width: min(440px, calc(100vw - 36px));
  padding: clamp(24px, 4vw, 42px);
  border-radius: 28px;
  background: rgba(255, 255, 255, .58);
  box-shadow: 0 30px 90px rgba(117, 72, 31, .12);
  backdrop-filter: blur(18px);
}

.auth-title {
  margin: 0 0 26px;
  font-size: clamp(2rem, 5vw, 3.2rem);
  font-weight: 800;
  letter-spacing: -.05em;
}

.form-label {
  font-weight: 700;
  color: #533322;
}

.form-control {
  border: 1px solid rgba(201, 102, 34, .18);
  border-radius: 18px;
  background: rgba(255, 255, 255, .82);
  box-shadow: none !important;
}

.form-control:focus {
  border-color: rgba(234, 132, 57, .62);
}

.btn-orange,
.btn-orange:focus {
  border: 0;
  border-radius: 18px;
  background: linear-gradient(135deg, #f39b53, #db6e2b);
  color: #fff;
  font-weight: 800;
  box-shadow: 0 16px 30px rgba(219, 110, 43, .24);
}

.btn-orange:hover {
  color: #fff;
  background: linear-gradient(135deg, #ec8a3d, #c95f1e);
}

.auth-links {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-top: 18px;
  color: var(--muted);
}

.alert {
  border: 0;
  border-radius: 16px;
}

.drive-shell,
.shares-shell,
.media-shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px clamp(10px, 1.4vw, 18px) 12px;
}

.drive-header,
.media-header {
  min-height: 38px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 3px 0;
}

.drive-title-area {
  flex: 1;
  min-width: 0;
}

.breadcrumb {
  --bs-breadcrumb-divider-color: rgba(68, 38, 17, .38);
  --bs-breadcrumb-item-active-color: var(--text-main);
  display: flex;
  align-items: center;
  flex-wrap: nowrap;
  gap: 2px;
  overflow-x: auto;
  scrollbar-width: thin;
  padding: 6px 10px;
  min-height: 32px;
  border-radius: 999px;
  background: rgba(255, 255, 255, .52);
  box-shadow: inset 0 0 0 1px rgba(185, 116, 55, .11);
}

.breadcrumb-item {
  display: inline-flex;
  align-items: center;
  white-space: nowrap;
  font-size: .92rem;
  font-weight: 700;
}

.breadcrumb-item a,
.breadcrumb-item span {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  padding: 0 8px;
  border-radius: 999px;
}

.breadcrumb-target.drop-over,
.breadcrumb-item a:hover {
  background: rgba(255, 219, 185, .86);
}

.drive-nav {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  white-space: nowrap;
}

.nav-pill,
.mini-download,
.shared-pill,
.ghost-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 32px;
  border: 0;
  border-radius: 999px;
  padding: 6px 12px;
  background: rgba(255, 255, 255, .62);
  color: #5d3521;
  font-size: .88rem;
  font-weight: 700;
}

.nav-pill:hover,
.mini-download:hover,
.ghost-action:hover {
  background: rgba(255, 230, 205, .88);
  color: #9c4310;
}

.file-board {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
  align-content: start;
  gap: 12px;
  overflow: auto;
  padding: 8px 2px 18px;
}

.file-board.drop-over {
  outline: 2px dashed rgba(234, 132, 57, .42);
  outline-offset: -4px;
  border-radius: 22px;
  background: rgba(255, 232, 210, .28);
}

.entry-card {
  min-height: 148px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 10px;
  align-items: center;
  padding: 14px 10px;
  border-radius: 22px;
  background: rgba(255, 255, 255, .56);
  box-shadow: 0 14px 32px rgba(118, 77, 40, .07);
  cursor: pointer;
  user-select: none;
  text-align: center;
  transition: transform .12s ease, background .12s ease, box-shadow .12s ease;
}

.entry-card:hover,
.entry-card.selected,
.entry-card.drop-over {
  transform: translateY(-1px);
  background: rgba(255, 244, 232, .95);
  box-shadow: 0 18px 42px rgba(118, 77, 40, .12);
}

.entry-card.dragging {
  opacity: .45;
}

/* Iconos cuadrados compactos para archivos y carpetas. */
.entry-icon {
  flex: 0 0 auto;
  width: 58px;
  height: 58px;
  display: grid;
  place-items: center;
  border-radius: 10px;
  font-size: 1.72rem;
  background: linear-gradient(135deg, rgba(255, 224, 196, .95), rgba(255, 194, 142, .72));
}

.entry-icon.folder {
  background: linear-gradient(135deg, #ffd696, #f4a35c);
}

.entry-icon.audio,
.entry-icon.video,
.entry-icon.text {
  background: linear-gradient(135deg, #ffe5d7, #ffae8a);
}

.entry-main {
  width: 100%;
  min-width: 0;
}

.entry-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 800;
  letter-spacing: -.01em;
}

.entry-meta {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 5px;
  color: var(--muted);
  font-size: .82rem;
}

.kind-badge {
  display: inline-flex;
  align-items: center;
  min-height: 20px;
  padding: 2px 7px;
  border-radius: 999px;
  background: rgba(255, 236, 218, .9);
  color: #763a13;
  font-weight: 800;
}

.folder-badge {
  background: rgba(255, 220, 156, .72);
}

.file-badge {
  background: rgba(255, 230, 218, .86);
}

.empty-hint {
  grid-column: 1 / -1;
  align-self: start;
  color: var(--muted);
  padding: 18px 20px;
  border-radius: 24px;
  background: rgba(255, 255, 255, .46);
}

.context-menu {
  position: fixed;
  z-index: 2000;
  display: none;
  min-width: 204px;
  padding: 7px;
  border: 1px solid rgba(176, 105, 45, .13);
  border-radius: 18px;
  background: rgba(255, 253, 249, .97);
  box-shadow: 0 24px 70px rgba(73, 44, 20, .18);
  backdrop-filter: blur(16px);
}

.context-menu.show {
  display: block;
}

.context-menu button {
  display: block;
  width: 100%;
  border: 0;
  border-radius: 13px;
  padding: 9px 12px;
  background: transparent;
  color: var(--text-main);
  text-align: left;
  font: inherit;
  font-size: .94rem;
}

.context-menu button:hover {
  background: var(--orange-50);
  color: var(--orange-600);
}

.context-menu .danger:hover,
.ghost-action.danger:hover {
  background: #fff0ed;
  color: #b42318;
}

.toast-host {
  position: fixed;
  right: 18px;
  bottom: 18px;
  z-index: 2500;
  display: grid;
  gap: 10px;
}

.app-toast {
  max-width: min(560px, calc(100vw - 36px));
  padding: 11px 14px;
  border-radius: 15px;
  background: rgba(44, 27, 16, .92);
  color: #fffaf5;
  box-shadow: 0 16px 40px rgba(54, 39, 25, .18);
  word-break: break-all;
}

.download-waiting-overlay {
  position: fixed;
  inset: 0;
  z-index: 3200;
  display: grid;
  place-items: center;
  background: rgba(255, 250, 246, .58);
  backdrop-filter: blur(3px);
}

.download-waiting-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 18px;
  border-radius: 18px;
  background: rgba(44, 27, 16, .92);
  color: #fffaf5;
  box-shadow: 0 22px 50px rgba(54, 39, 25, .2);
  font-size: 14px;
}

.download-waiting-spinner {
  width: 18px;
  height: 18px;
  border: 2px solid rgba(255, 250, 245, .45);
  border-top-color: #fffaf5;
  border-radius: 999px;
  animation: archive-spin .8s linear infinite;
}

@keyframes archive-spin {
  to { transform: rotate(360deg); }
}

.share-list {
  flex: 1;
  min-height: 0;
  display: grid;
  align-content: start;
  gap: 8px;
  overflow: auto;
  padding: 8px 2px 18px;
}

.share-row {
  display: flex;
  justify-content: space-between;
  gap: 14px;
  align-items: center;
  padding: 12px;
  border-radius: 24px;
  background: rgba(255, 255, 255, .54);
  box-shadow: 0 14px 32px rgba(118, 77, 40, .06);
}

.share-row-main {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
  flex: 1;
  text-align: left;
}

.share-info {
  min-width: 0;
  flex: 1;
}

.share-url-input {
  width: min(760px, 100%);
  margin-top: 7px;
  border: 0;
  border-radius: 13px;
  padding: 8px 10px;
  color: var(--muted);
  background: rgba(255, 250, 245, .86);
  font-size: .86rem;
}

.share-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.media-header {
  min-height: 42px;
}

.media-title {
  max-width: min(1000px, 72vw);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin: 0;
  font-size: 1rem;
  font-weight: 800;
}

.media-meta {
  margin: 2px 0 0;
  color: var(--muted);
  font-size: .82rem;
}

.player-panel {
  flex: 1;
  min-height: 0;
  display: grid;
  place-items: center;
  border-radius: 26px;
  background: rgba(255, 255, 255, .44);
  padding: clamp(10px, 1.6vw, 22px);
}

.player-panel video {
  width: min(100%, 1440px);
  max-height: calc(100vh - 86px);
  border-radius: 18px;
  background: #000;
}

.player-panel audio {
  width: min(820px, 100%);
}

.audio-panel {
  min-height: 34vh;
}

.single-shared-file-board {
  flex: initial;
  min-height: 170px;
  grid-template-columns: minmax(160px, 220px);
}

.text-editor-shell {
  width: 100%;
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--red-orange);
  color: #111;
}

.text-editor-header {
  flex: 0 0 auto;
  min-height: 36px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 4px 8px 4px 12px;
  background: rgba(255, 120, 80, .96);
  color: #111;
}

.text-editor-title-area {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 9px;
}

.text-editor-title {
  min-width: 0;
  max-width: 46vw;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 800;
  font-size: .92rem;
}

.text-editor-meta {
  color: #351006;
  font-size: .78rem;
  white-space: nowrap;
}

.text-editor-actions {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  white-space: nowrap;
}

.text-editor-button,
.text-editor-link {
  border: 0;
  border-radius: 999px;
  background: rgba(255, 238, 226, .86);
  color: #111;
  padding: 4px 10px;
  font: inherit;
  font-size: .84rem;
  font-weight: 800;
}

.text-editor-button:hover,
.text-editor-link:hover {
  background: rgba(255, 250, 246, .96);
  color: #111;
}

.text-content-editor {
  flex: 1;
  width: 100%;
  min-height: 0;
  border: 0;
  outline: 0;
  resize: none;
  padding: 14px clamp(14px, 2vw, 28px);
  background: var(--red-orange);
  color: #111;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, ui-monospace, monospace;
  font-size: clamp(15px, .95vw, 18px);
  line-height: 1.62;
  tab-size: 4;
  white-space: pre;
  overflow: auto;
}

.text-content-editor::selection {
  background: rgba(255, 245, 238, .75);
}

.text-content-editor[readonly] {
  cursor: pointer;
}

.text-editor-status {
  flex: 0 0 auto;
  min-width: 80px;
  color: #351006;
  font-size: .8rem;
  text-align: right;
}

@media (max-width: 820px) {
  .drive-shell,
  .shares-shell,
  .media-shell {
    padding: 8px 10px 12px;
  }

  .drive-header,
  .media-header,
  .share-row {
    align-items: stretch;
    flex-direction: column;
  }

  .drive-nav,
  .share-actions {
    justify-content: flex-start;
  }

  .file-board {
    grid-template-columns: repeat(auto-fill, minmax(118px, 1fr));
  }

  .text-editor-header {
    min-height: 40px;
  }

  .text-editor-meta {
    display: none;
  }

  .text-editor-title {
    max-width: 48vw;
  }
}
""",
    'app.js': r"""const shell = document.querySelector('.drive-shell');
const currentFolderId = shell?.dataset.currentFolderId || '';
const board = document.getElementById('dropArea');
const menu = document.getElementById('contextMenu');
const fileInput = document.getElementById('fileInput');
const folderInput = document.getElementById('folderInput');
const toastHost = document.getElementById('toastHost');
let activeEntry = null;
let dragEntryId = null;
let suppressNextClick = false;

// Las acciones de vista previa/edición del menú contextual no deben reemplazar la página de lista de archivos.
function openInNewTab(url) {
  const openedWindow = window.open(url, '_blank', 'noopener');
  if (openedWindow) {
    openedWindow.opener = null;
    return;
  }
  showToast('El navegador bloqueó la nueva pestaña. Permite las ventanas emergentes e inténtalo de nuevo.', 4200);
}

function api(url, body) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  }).then(async res => {
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) throw new Error(data.error || 'La operación falló');
    return data;
  });
}

function showToast(message, timeout = 2600) {
  if (!toastHost) { alert(message); return; }
  const el = document.createElement('div');
  el.className = 'app-toast';
  el.textContent = message;
  toastHost.appendChild(el);
  setTimeout(() => el.remove(), timeout);
}

// Los archivos comprimidos de carpetas pueden tardar; la página espera hasta que Flask devuelva la URL lista.
function showArchiveWaitingOverlay(message) {
  const overlay = document.createElement('div');
  overlay.className = 'download-waiting-overlay';
  overlay.setAttribute('role', 'status');
  overlay.setAttribute('aria-live', 'polite');
  overlay.innerHTML = `<div class="download-waiting-card"><span class="download-waiting-spinner"></span><span>${message}</span></div>`;
  document.body.appendChild(overlay);
  return overlay;
}

function hideArchiveWaitingOverlay(overlay) {
  if (overlay) overlay.remove();
}

function triggerBrowserDownload(downloadUrl) {
  const link = document.createElement('a');
  link.href = downloadUrl;
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  link.remove();
}

async function prepareArchiveDownload(prepareUrl) {
  const overlay = showArchiveWaitingOverlay('Comprimiendo, espera...');
  try {
    const response = await fetch(prepareUrl, {
      headers: { 'Accept': 'application/json' },
      credentials: 'same-origin'
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false || !data.url) {
      throw new Error(data.error || 'La compresión falló. Inténtalo de nuevo más tarde.');
    }
    showToast('Compresión completada. Iniciando la descarga.');
    triggerBrowserDownload(data.url);
  } finally {
    hideArchiveWaitingOverlay(overlay);
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_err) {
    const temp = document.createElement('textarea');
    temp.value = text;
    temp.setAttribute('readonly', '');
    temp.style.position = 'fixed';
    temp.style.opacity = '0';
    document.body.appendChild(temp);
    temp.select();
    const ok = document.execCommand('copy');
    temp.remove();
    return ok;
  }
}

function closeMenu() {
  menu.classList.remove('show');
  menu.innerHTML = '';
  document.querySelectorAll('.entry-card.selected').forEach(el => el.classList.remove('selected'));
}

function placeMenu(x, y) {
  menu.classList.add('show');
  const rect = menu.getBoundingClientRect();
  const pad = 10;
  const left = Math.min(x, window.innerWidth - rect.width - pad);
  const top = Math.min(y, window.innerHeight - rect.height - pad);
  menu.style.left = `${Math.max(pad, left)}px`;
  menu.style.top = `${Math.max(pad, top)}px`;
}

function addMenuItem(label, action, danger = false) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.textContent = label;
  if (danger) btn.classList.add('danger');
  btn.addEventListener('click', async () => {
    closeMenu();
    try { await action(); } catch (e) { showToast(e.message || String(e), 4200); }
  });
  menu.appendChild(btn);
}

function entryMainAction(entry) {
  const id = entry.dataset.id;
  if (entry.dataset.kind === 'folder') {
    window.location.href = `/drive/${id}`;
  } else {
    window.location.href = `/download/${id}`;
  }
}

function openEntryMenu(entry, x, y) {
  closeMenu();
  activeEntry = entry;
  entry.classList.add('selected');
  const id = entry.dataset.id;
  const kind = entry.dataset.kind;
  const name = entry.dataset.name;
  const mediaKind = entry.dataset.mediaKind;
  const isTextFile = entry.dataset.isText === '1';

  if (kind === 'folder') {
    addMenuItem('Abrir', () => { window.location.href = `/drive/${id}`; });
    addMenuItem('Descargar como 7z', () => prepareArchiveDownload(`/api/prepare_download/${id}`));
  } else {
    if (mediaKind === 'audio' || mediaKind === 'video') {
      addMenuItem('Reproducir en línea', () => { openInNewTab(`/preview/${id}`); });
    }
    if (isTextFile) {
      addMenuItem('Editar texto en línea', () => { openInNewTab(`/text/${id}`); });
    }
    addMenuItem('Descargar', () => { window.location.href = `/download/${id}`; });
  }

  addMenuItem('Cambiar nombre', async () => {
    const next = prompt('Introduce un nombre nuevo:', name);
    if (!next || next.trim() === name) return;
    await api('/api/rename', { item_id: id, name: next.trim() });
    location.reload();
  });
  addMenuItem('Compartir', async () => {
    const data = await api('/api/share', { item_id: id });
    const copied = await copyText(data.url);
    if (copied) {
      showToast('Enlace compartido copiado al portapapeles.');
    } else {
      showToast(`No se pudo copiar. Copia manualmente: ${data.url}`, 8000);
      prompt('Copiar enlace compartido:', data.url);
    }
  });
  addMenuItem('Eliminar', async () => {
    if (!confirm(`¿Eliminar "${name}"?`)) return;
    await api('/api/delete', { item_id: id });
    location.reload();
  }, true);

  placeMenu(x, y);
}

function openBlankMenu(x, y) {
  closeMenu();
  activeEntry = null;
  addMenuItem('Subir archivos', () => fileInput.click());
  addMenuItem('Subir carpeta', () => folderInput.click());
  addMenuItem('Nueva carpeta', async () => {
    const name = prompt('Nombre de carpeta:', 'Nueva carpeta');
    if (!name) return;
    await api('/api/folders', { parent_id: currentFolderId, name: name.trim() });
    location.reload();
  });
  addMenuItem('Gestión de compartidos', () => { window.location.href = '/shares'; });
  addMenuItem('Cambiar contraseña', () => { window.location.href = '/change-password'; });
  addMenuItem('Cerrar sesión', () => { window.location.href = '/logout'; });
  placeMenu(x, y);
}

function shouldOpenBlankMenu(target) {
  return !target.closest('.entry-card') && !target.closest('.context-menu') && target.closest('#dropArea');
}

if (board) {
  document.addEventListener('contextmenu', (e) => {
    const entry = e.target.closest('.entry-card');
    if (entry && board.contains(entry)) {
      e.preventDefault();
      openEntryMenu(entry, e.clientX, e.clientY);
      return;
    }
    if (shouldOpenBlankMenu(e.target)) {
      e.preventDefault();
      openBlankMenu(e.clientX, e.clientY);
    }
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.context-menu')) closeMenu();
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeMenu();
  });

  board.addEventListener('click', (e) => {
    const entry = e.target.closest('.entry-card');
    if (!entry || suppressNextClick) return;
    entryMainAction(entry);
  });

  async function uploadFiles(fileList, isFolder = false) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const fd = new FormData();
    fd.append('parent_id', currentFolderId);
    for (const file of files) {
      const relative = isFolder ? (file.webkitRelativePath || file.name) : file.name;
      fd.append('files', file, relative);
    }
    const res = await fetch('/api/upload', { method: 'POST', body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) throw new Error(data.error || 'La subida falló');
    location.reload();
  }

  fileInput.addEventListener('change', async () => {
    try { await uploadFiles(fileInput.files, false); }
    catch (e) { showToast(e.message || String(e)); }
    finally { fileInput.value = ''; }
  });

  folderInput.addEventListener('change', async () => {
    try { await uploadFiles(folderInput.files, true); }
    catch (e) { showToast(e.message || String(e)); }
    finally { folderInput.value = ''; }
  });

  document.querySelectorAll('.entry-card[draggable="true"]').forEach(entry => {
    entry.addEventListener('dragstart', (e) => {
      dragEntryId = entry.dataset.id;
      entry.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', dragEntryId);
    });
    entry.addEventListener('dragend', () => {
      dragEntryId = null;
      suppressNextClick = true;
      setTimeout(() => { suppressNextClick = false; }, 120);
      entry.classList.remove('dragging');
      document.querySelectorAll('.drop-over').forEach(el => el.classList.remove('drop-over'));
    });

    if (entry.dataset.kind === 'folder') {
      entry.addEventListener('dragover', (e) => {
        if (!dragEntryId || dragEntryId === entry.dataset.id) return;
        e.preventDefault();
        entry.classList.add('drop-over');
      });
      entry.addEventListener('dragleave', () => entry.classList.remove('drop-over'));
      entry.addEventListener('drop', async (e) => {
        e.preventDefault();
        entry.classList.remove('drop-over');
        const itemId = e.dataTransfer.getData('text/plain') || dragEntryId;
        if (!itemId || itemId === entry.dataset.id) return;
        try {
          await api('/api/move', { item_id: itemId, target_parent_id: entry.dataset.id });
          location.reload();
        } catch (err) { showToast(err.message || String(err)); }
      });
    }
  });

  document.querySelectorAll('.breadcrumb-target').forEach(crumb => {
    crumb.addEventListener('dragover', (e) => {
      if (!dragEntryId) return;
      e.preventDefault();
      crumb.classList.add('drop-over');
    });
    crumb.addEventListener('dragleave', () => crumb.classList.remove('drop-over'));
    crumb.addEventListener('drop', async (e) => {
      e.preventDefault();
      crumb.classList.remove('drop-over');
      const itemId = e.dataTransfer.getData('text/plain') || dragEntryId;
      try {
        await api('/api/move', { item_id: itemId, target_parent_id: crumb.dataset.folderId || '' });
        location.reload();
      } catch (err) { showToast(err.message || String(err)); }
    });
  });

  board.addEventListener('dragover', (e) => {
    if (!dragEntryId) return;
    if (e.target.closest('.entry-card')) return;
    e.preventDefault();
    board.classList.add('drop-over');
  });

  board.addEventListener('dragleave', (e) => {
    if (!board.contains(e.relatedTarget)) board.classList.remove('drop-over');
  });

  board.addEventListener('drop', async (e) => {
    if (!dragEntryId) return;
    if (e.target.closest('.entry-card')) return;
    e.preventDefault();
    board.classList.remove('drop-over');
    const itemId = e.dataTransfer.getData('text/plain') || dragEntryId;
    try {
      await api('/api/move', { item_id: itemId, target_parent_id: currentFolderId });
      location.reload();
    } catch (err) { showToast(err.message || String(err)); }
  });
}
""",
    'share_page.js': r"""const sharedFileBoard = document.getElementById('sharedFileBoard');
const sharedContextMenu = document.getElementById('sharedContextMenu');
const sharedArchiveLink = document.querySelector('[data-shared-archive-link]');
const sharedShell = document.querySelector('[data-share-token]');
const shareToken = sharedShell?.dataset.shareToken || '';
let selectedSharedEntry = null;

function openInNewTab(url) {
  const openedWindow = window.open(url, '_blank', 'noopener');
  if (openedWindow) {
    openedWindow.opener = null;
    return;
  }
  alert('El navegador bloqueó la nueva pestaña. Permite las ventanas emergentes e inténtalo de nuevo.');
}

function showSharedArchiveWaitingOverlay(message) {
  const overlay = document.createElement('div');
  overlay.className = 'download-waiting-overlay';
  overlay.setAttribute('role', 'status');
  overlay.setAttribute('aria-live', 'polite');
  overlay.innerHTML = `<div class="download-waiting-card"><span class="download-waiting-spinner"></span><span>${message}</span></div>`;
  document.body.appendChild(overlay);
  return overlay;
}

function triggerSharedBrowserDownload(downloadUrl) {
  const link = document.createElement('a');
  link.href = downloadUrl;
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  link.remove();
}

async function prepareSharedArchiveDownload(prepareUrl) {
  const overlay = showSharedArchiveWaitingOverlay('Comprimiendo, espera...');
  try {
    const response = await fetch(prepareUrl, {
      headers: { 'Accept': 'application/json' },
      credentials: 'same-origin'
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false || !data.url) {
      throw new Error(data.error || 'La compresión falló. Inténtalo de nuevo más tarde.');
    }
    triggerSharedBrowserDownload(data.url);
  } finally {
    overlay.remove();
  }
}

function closeSharedContextMenu() {
  sharedContextMenu?.classList.remove('show');
  if (sharedContextMenu) sharedContextMenu.innerHTML = '';
  document.querySelectorAll('.shared-entry.selected').forEach((entryElement) => {
    entryElement.classList.remove('selected');
  });
}

function positionSharedContextMenu(clientX, clientY) {
  sharedContextMenu.classList.add('show');
  const menuRectangle = sharedContextMenu.getBoundingClientRect();
  const viewportPadding = 10;
  const left = Math.min(clientX, window.innerWidth - menuRectangle.width - viewportPadding);
  const top = Math.min(clientY, window.innerHeight - menuRectangle.height - viewportPadding);
  sharedContextMenu.style.left = `${Math.max(viewportPadding, left)}px`;
  sharedContextMenu.style.top = `${Math.max(viewportPadding, top)}px`;
}

function addSharedMenuItem(label, action, isDangerous = false) {
  const menuButton = document.createElement('button');
  menuButton.type = 'button';
  menuButton.textContent = label;
  if (isDangerous) menuButton.classList.add('danger');
  menuButton.addEventListener('click', async () => {
    closeSharedContextMenu();
    try {
      await action();
    } catch (error) {
      alert(error.message || String(error));
    }
  });
  sharedContextMenu.appendChild(menuButton);
}

function openSharedEntry(entryElement) {
  if (entryElement.dataset.kind === 'folder') {
    window.location.href = entryElement.dataset.openUrl;
    return;
  }
  window.location.href = entryElement.dataset.downloadUrl;
}

function openSharedEntryMenu(entryElement, clientX, clientY) {
  closeSharedContextMenu();
  selectedSharedEntry = entryElement;
  selectedSharedEntry.classList.add('selected');

  if (entryElement.dataset.kind === 'folder') {
    addSharedMenuItem('Abrir', () => { window.location.href = entryElement.dataset.openUrl; });
    addSharedMenuItem('Descargar como 7z', () => prepareSharedArchiveDownload(`/api/s/${shareToken}/prepare_archive/${entryElement.dataset.id}`));
  } else {
    addSharedMenuItem('Descargar', () => { window.location.href = entryElement.dataset.downloadUrl; });
    if (entryElement.dataset.playUrl) {
      addSharedMenuItem('Reproducir en línea', () => { openInNewTab(entryElement.dataset.playUrl); });
    }
    if (entryElement.dataset.textUrl) {
      addSharedMenuItem('Ver texto en línea', () => { openInNewTab(entryElement.dataset.textUrl); });
    }
  }

  positionSharedContextMenu(clientX, clientY);
}

if (sharedArchiveLink) {
  sharedArchiveLink.addEventListener('click', (event) => {
    event.preventDefault();
    prepareSharedArchiveDownload(sharedArchiveLink.dataset.prepareUrl || sharedArchiveLink.href);
  });
}

if (sharedFileBoard && sharedContextMenu) {
  sharedFileBoard.addEventListener('click', (event) => {
    const entryElement = event.target.closest('.shared-entry');
    if (!entryElement || !sharedFileBoard.contains(entryElement)) return;
    openSharedEntry(entryElement);
  });

  document.addEventListener('contextmenu', (event) => {
    const entryElement = event.target.closest('.shared-entry');
    if (!entryElement || !sharedFileBoard.contains(entryElement)) return;
    event.preventDefault();
    openSharedEntryMenu(entryElement, event.clientX, event.clientY);
  });

  document.addEventListener('click', (event) => {
    if (!event.target.closest('.context-menu')) closeSharedContextMenu();
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeSharedContextMenu();
  });
}
""",
    'shares.js': r"""const toastHost = document.getElementById('toastHost');

function showToast(message, timeout = 2600) {
  const el = document.createElement('div');
  el.className = 'app-toast';
  el.textContent = message;
  toastHost.appendChild(el);
  setTimeout(() => el.remove(), timeout);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_err) {
    const temp = document.createElement('textarea');
    temp.value = text;
    temp.setAttribute('readonly', '');
    temp.style.position = 'fixed';
    temp.style.opacity = '0';
    document.body.appendChild(temp);
    temp.select();
    const ok = document.execCommand('copy');
    temp.remove();
    return ok;
  }
}

document.querySelectorAll('.share-row').forEach(row => {
  row.querySelector('.copy-share')?.addEventListener('click', async () => {
    const url = row.dataset.shareUrl;
    const ok = await copyText(url);
    if (ok) showToast('Enlace compartido copiado al portapapeles.');
    else prompt('Copiar enlace compartido:', url);
  });

  row.querySelector('.cancel-share')?.addEventListener('click', async () => {
    const name = row.querySelector('.entry-name')?.textContent || 'este compartido';
    if (!confirm(`¿Cancelar el uso compartido de "${name}"?`)) return;
    const res = await fetch(`/api/shares/${row.dataset.shareId}/cancel`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      showToast(data.error || 'No se pudo cancelar el compartido.', 4200);
      return;
    }
    row.remove();
    showToast('Compartido cancelado.');
  });
});
""",
    'text_editor.js': r"""const textEditorShell = document.querySelector('.text-editor-shell');
const textContentEditor = document.getElementById('textContentEditor');
const saveTextButton = document.getElementById('saveTextButton');
const textEditorStatus = document.getElementById('textEditorStatus');
const isReadOnlyTextView = textEditorShell?.dataset.readOnly === '1';
const saveTextUrl = textEditorShell?.dataset.saveUrl || '';
let hasUnsavedTextChanges = false;

function setEditorStatus(message) {
  if (textEditorStatus) textEditorStatus.textContent = message;
}

function newlineCodeToValue(newlineCode) {
  if (newlineCode === 'crlf') return '\r\n';
  if (newlineCode === 'cr') return '\r';
  return '\n';
}

async function saveTextDocument() {
  if (isReadOnlyTextView || !saveTextUrl) return;
  saveTextButton.disabled = true;
  setEditorStatus('Guardando...');
  try {
    const response = await fetch(saveTextUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        content: textContentEditor.value,
        encoding: textContentEditor.dataset.encoding || 'utf-8',
        newline_style: newlineCodeToValue(textContentEditor.dataset.newlineCode)
      })
    });
    const responseData = await response.json().catch(() => ({}));
    if (!response.ok || responseData.ok === false) throw new Error(responseData.error || 'No se pudo guardar');
    hasUnsavedTextChanges = false;
    setEditorStatus(`Guardado: ${new Date().toLocaleString()}`);
  } catch (error) {
    setEditorStatus(error.message || String(error));
  } finally {
    saveTextButton.disabled = false;
  }
}

if (textContentEditor) {
  textContentEditor.addEventListener('input', () => {
    if (isReadOnlyTextView) return;
    hasUnsavedTextChanges = true;
    setEditorStatus('Hay cambios sin guardar. Pulsa Ctrl/⌘ + S para guardar.');
  });

  textContentEditor.addEventListener('keydown', (event) => {
    if (event.key === 'Tab' && !isReadOnlyTextView) {
      event.preventDefault();
      const selectionStart = textContentEditor.selectionStart;
      const selectionEnd = textContentEditor.selectionEnd;
      textContentEditor.setRangeText('    ', selectionStart, selectionEnd, 'end');
      hasUnsavedTextChanges = true;
      setEditorStatus('Hay cambios sin guardar. Pulsa Ctrl/⌘ + S para guardar.');
    }
  });
}

saveTextButton?.addEventListener('click', saveTextDocument);

document.addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
    event.preventDefault();
    saveTextDocument();
  }
});

window.addEventListener('beforeunload', (event) => {
  if (!hasUnsavedTextChanges) return;
  event.preventDefault();
  event.returnValue = '';
});
""",
}

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



@app.after_request
def add_security_headers(response):
    """Agrega cabeceras conservadoras de seguridad del navegador para la aplicación de un solo archivo."""
    content_security_policy = (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
        "img-src 'self' data:; "
        "media-src 'self'; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    )
    response.headers.setdefault("Content-Security-Policy", content_security_policy)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response

# -----------------------------
# Funciones auxiliares de SQLite
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
        -- Tabla de usuarios: almacena nombre de usuario, sal de contraseña, hash de contraseña y hora de creación.
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, -- Clave primaria de usuario, entero autoincremental.
            username TEXT NOT NULL UNIQUE,        -- Nombre de usuario de inicio de sesión; obligatorio y globalmente único.
            password_salt TEXT NOT NULL,          -- Sal aleatoria por usuario usada en el hash de contraseña.
            password_hash TEXT NOT NULL,          -- Hash SHA3 calculado a partir de sal + contraseña.
            created_at TEXT NOT NULL              -- Hora de creación del usuario almacenada como cadena ISO.
        );

        -- Tabla de entradas: archivos y carpetas comparten una tabla y se distinguen por kind.
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,                 -- Clave primaria de entrada, entero autoincremental.
            owner_id INTEGER NOT NULL,                            -- ID del usuario propietario; referencia users.id.
            parent_id INTEGER NULL,                               -- ID de carpeta padre; NULL significa la carpeta raíz del usuario.
            kind TEXT NOT NULL CHECK(kind IN ('file', 'folder')), -- Tipo de entrada; solo se permite file o folder.
            name TEXT NOT NULL,                                   -- Nombre de archivo o carpeta mostrado al usuario.
            storage_name TEXT NULL,                               -- Nombre de archivo real en disco; las carpetas normalmente son NULL.
            size INTEGER NOT NULL DEFAULT 0,                      -- Tamaño de archivo en bytes; las carpetas tienen 0 por defecto.
            created_at TEXT NOT NULL,                             -- Hora de creación de la entrada almacenada como cadena ISO.
            updated_at TEXT NOT NULL,                             -- Hora de actualización de la entrada; se refresca al cambiar nombre o mover.
            FOREIGN KEY(owner_id) REFERENCES users(id) ON DELETE CASCADE,   -- Elimina entradas en cascada cuando se elimina el usuario.
            FOREIGN KEY(parent_id) REFERENCES entries(id) ON DELETE CASCADE -- Elimina entradas hijas en cascada cuando se elimina la carpeta padre.
        );

        -- Unicidad de nombre raíz: un usuario no puede tener dos entradas raíz con el mismo nombre.
        CREATE UNIQUE INDEX IF NOT EXISTS entries_unique_name_root
            ON entries(owner_id, name)
            WHERE parent_id IS NULL;

        -- Unicidad de nombre en carpeta: las entradas bajo el mismo padre no pueden compartir nombre.
        CREATE UNIQUE INDEX IF NOT EXISTS entries_unique_name_folder
            ON entries(owner_id, parent_id, name)
            WHERE parent_id IS NOT NULL;

        -- Tabla de compartidos: asigna tokens de uso compartido a archivos o carpetas compartidos.
        CREATE TABLE IF NOT EXISTS shares (
            id INTEGER PRIMARY KEY AUTOINCREMENT, -- Clave primaria del registro compartido, entero autoincremental.
            token TEXT NOT NULL UNIQUE,           -- Token aleatorio usado en enlaces públicos compartidos; debe ser único.
            item_id INTEGER NOT NULL,             -- ID de archivo o carpeta compartido; referencia entries.id.
            owner_id INTEGER NOT NULL,            -- ID del creador del compartido; referencia users.id.
            created_at TEXT NOT NULL,             -- Hora de creación del compartido almacenada como cadena ISO.
            expires_at TEXT NULL,                 -- Hora de expiración del compartido; NULL significa que no hay expiración configurada.
            FOREIGN KEY(item_id) REFERENCES entries(id) ON DELETE CASCADE, -- Elimina compartidos en cascada cuando se elimina la entrada.
            FOREIGN KEY(owner_id) REFERENCES users(id) ON DELETE CASCADE  -- Elimina compartidos en cascada cuando se elimina el usuario.
        );
        """
    )
    db.commit()


def cleanup_old_archives():
    """Elimina archivos 7z generados que sean más antiguos que ARCHIVE_RETENTION_SECONDS."""
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
# Funciones auxiliares de presentación
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
        return "Carpeta"
    m = media_kind(name)
    if m == "audio":
        return "Archivo de audio"
    if m == "video":
        return "Archivo de vídeo"
    if is_text_file(name):
        return "Archivo de texto"
    return "Archivo"


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


def newline_style_code(newline_style: str) -> str:
    if newline_style == "\r\n":
        return "crlf"
    if newline_style == "\r":
        return "cr"
    return "lf"


def newline_style_label(newline_style: str) -> str:
    if newline_style == "\r\n":
        return "CRLF"
    if newline_style == "\r":
        return "CR"
    return "LF"


@app.context_processor
def inject_template_helpers():
    return {
        "media_kind": media_kind,
        "is_text_file": is_text_file,
        "entry_icon": entry_icon,
        "kind_label": kind_label,
        "file_size_label": file_size_label,
        "newline_style_code": newline_style_code,
        "newline_style_label": newline_style_label,
    }


# -----------------------------
# Autenticación y hash de contraseñas
# -----------------------------

def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    """Devuelve (salt_hex, sha3_256_hash_hex). Usa sal aleatoria + SHA3-256."""
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
        # La política de contraseñas se mantiene intencionalmente flexible: solo rechaza valores vacíos.
        if not username or not password:
            flash("El nombre de usuario y la contraseña no pueden estar vacíos.")
            return render_template("register.html")

        salt, pwd_hash = hash_password(password)
        try:
            cur = get_db().execute(
                "INSERT INTO users(username, password_salt, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (username, salt, pwd_hash, now_iso()),
            )
            get_db().commit()
        except sqlite3.IntegrityError:
            flash("El nombre de usuario ya existe.")
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
        flash("Nombre de usuario o contraseña no válidos.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/change-password", methods=["GET", "POST"])
def change_password():
    login_required()
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        # Aquí se aplica la misma regla flexible: la nueva contraseña solo debe no estar vacía.
        if not current_password or not new_password or not confirm_password:
            flash("Completa todos los campos.")
            return render_template("change_password.html")
        if new_password != confirm_password:
            flash("Las nuevas contraseñas no coinciden.")
            return render_template("change_password.html")

        user = get_db().execute("SELECT * FROM users WHERE id = ?", (current_user_id(),)).fetchone()
        _salt, current_password_hash = hash_password(current_password, user["password_salt"])
        if current_password_hash != user["password_hash"]:
            flash("La contraseña actual es incorrecta.")
            return render_template("change_password.html")

        new_salt, new_password_hash = hash_password(new_password)
        get_db().execute(
            "UPDATE users SET password_salt = ?, password_hash = ? WHERE id = ?",
            (new_salt, new_password_hash, current_user_id()),
        )
        get_db().commit()
        flash("Contraseña actualizada.")

    return render_template("change_password.html")


# -----------------------------
# Funciones auxiliares de entradas
# -----------------------------

def row_to_dict(row):
    return dict(row) if row else None


def clean_segment(name: str) -> str:
    """Mantiene nombres Unicode, elimina separadores de ruta/caracteres de control y normaliza nombres vacíos."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch >= " " and ch not in '<>:"|?*')
    name = name.strip().strip(".")
    return name[:180] or "Sin título"


def split_upload_path(filename: str) -> list[str]:
    path = PurePosixPath((filename or "").replace("\\", "/"))
    parts = []
    for p in path.parts:
        if p in ("", ".", ".."):
            continue
        parts.append(clean_segment(p))
    return parts or ["Sin título"]


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
    raise RuntimeError("No se puede generar un nombre único")


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
    crumbs = [{"id": None, "name": "Archivos"}]
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
    # LZMA2 nivel 9 con el bit extremo de py7zr cuando la versión instalada lo expone.
    extreme = getattr(py7zr, "PRESET_EXTREME", 0)
    return [{"id": py7zr.FILTER_LZMA2, "preset": 9 | extreme}]


def create_7z_archive(row) -> Path:
    """Crea un archivo 7z de alta compresión en disco y devuelve su ruta."""
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
            marker_path.write_text("Esta carpeta no tiene archivos actualmente.\n", encoding="utf-8")
            archive.write(marker_path, arcname=str(PurePosixPath(row["name"]) / "empty-folder-note.txt"))
    if marker_path:
        marker_path.unlink(missing_ok=True)
    return archive_path

def is_valid_archive_id(archive_id: str) -> bool:
    """Solo permite nombres hexadecimales aleatorios generados por create_7z_archive()."""
    if len(archive_id) != 32:
        return False
    return all(character in "0123456789abcdef" for character in archive_id)

def prepared_archive_json(row):
    """Genera primero un archivo de carpeta y luego devuelve a JavaScript la URL de descarga lista."""
    archive_path = create_7z_archive(row)
    download_name = f"{row['name']}.7z"
    return jsonify(
        ok=True,
        url=url_for("prepared_archive_download", archive_id=archive_path.stem, name=download_name),
    )
@app.get("/tmp-archive/<archive_id>")
def prepared_archive_download(archive_id):
    if not is_valid_archive_id(archive_id):
        abort(404)
    archive_path = TMP_ARCHIVE_DIR / f"{archive_id}.7z"
    if not archive_path.exists():
        abort(404)
    download_name = (request.args.get("name") or "archive.7z").strip()
    download_name = download_name.replace("\r", "").replace("\n", "") or "archive.7z"
    if not download_name.lower().endswith(".7z"):
        download_name = f"{download_name}.7z"
    return send_file(
        archive_path,
        mimetype="application/x-7z-compressed",
        as_attachment=True,
        download_name=download_name,
        conditional=True,
        max_age=0,
    )

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
        raise ValueError(f"The current text cannot be saved with {encoding_name} encoding. Please remove incompatible characters or save as UTF-8.") from exc
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
# Páginas de la unidad
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
    name = clean_segment(data.get("name", "Nueva carpeta"))
    folder_id = insert_folder(owner_id, parent_id, name)
    return jsonify(ok=True, id=folder_id)
@app.post("/api/upload")
def api_upload():
    owner_id = current_user_id()
    parent = require_folder_or_root(request.form.get("parent_id"))
    parent_id = int(parent["id"]) if parent else None
    files = request.files.getlist("files")
    if not files:
        return jsonify(ok=False, error="No se seleccionaron archivos."), 400
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
            return jsonify(ok=False, error="No se puede mover una carpeta dentro de sí misma ni de una de sus subcarpetas."), 400

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
        return jsonify(ok=False, error="El compartido no existe o no tienes permiso."), 404
    return jsonify(ok=True)


@app.get("/download/<int:item_id>")
def download(item_id):
    row = require_owned_entry(item_id)
    return send_entry_download(row)

@app.get("/api/prepare_download/<int:item_id>")
def api_prepare_download(item_id):
    row = require_owned_entry(item_id)
    if row["kind"] != "folder":
        return jsonify(ok=True, url=url_for("download", item_id=item_id))
    return prepared_archive_json(row)

# -----------------------------
# Páginas compartidas
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


@app.get("/api/s/<token>/prepare_archive/<int:folder_id>")
def share_prepare_archive_download(token, folder_id):
    share = get_share(token)
    root_id = int(share["item_id"])
    if not is_shared_descendant(folder_id, root_id):
        abort(403)
    row = get_entry(folder_id)
    if not row or row["kind"] != "folder":
        abort(404)
    return prepared_archive_json(row)

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
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
