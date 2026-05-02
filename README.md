# Single File Flask Drive ☁️

**Single File Flask Drive** is a compact, self-hosted cloud drive written as one deployable Python file: `app.py`.

Repository:

```text
https://github.com/wangyifan349/single-file-flask-drive
```

This project is designed for people who want a practical file manager that is easy to deploy, easy to inspect, and easy to maintain. The backend routes, SQLite database logic, embedded HTML templates, CSS, and JavaScript are all contained inside `app.py`, so there is no separate frontend build step and no complex project tree.

## ✨ What it does

`app.py` starts a complete web-based cloud drive service.

It supports user accounts, hierarchical folders, file and folder upload, file and folder download, file sharing, folder sharing, share management, online audio playback, online video playback, online plain-text editing, and read-only text viewing from shared pages.

Metadata is stored in SQLite, while uploaded file content is stored on disk. Folder downloads are generated as temporary `.7z` archives on disk instead of being loaded as one large in-memory archive. This makes large folder downloads more stable and more suitable for real-world usage.

## ✅ Project status

This package is intended to be directly usable.

The current packaged version has passed practical smoke testing, including application startup, registration, login, authenticated drive access, folder creation, file upload, file download, folder archive generation, sharing, shared folder browsing, shared file access, and embedded static asset loading.

For production use, you should still deploy it behind HTTPS, set a stable `SECRET_KEY`, and back up the data directory regularly.

## 🚀 Main features

### 👤 Account management

- User registration.
- User login.
- User logout.
- Password change.
- Passwords are stored as salted SHA3-256 digests.
- Session cookies use safer defaults such as `HttpOnly` and `SameSite=Lax`.

### 📁 File and folder management

- Hierarchical folder structure.
- Breadcrumb navigation.
- File upload.
- Folder upload.
- File listing.
- Folder listing.
- Single-click file download.
- Single-click folder navigation.
- File rename.
- Folder rename.
- File deletion.
- Folder deletion.
- Drag-and-drop movement into folders.
- Drag-and-drop movement into breadcrumb destinations.
- Right-click context menus for file, folder, and blank-area actions.
- Visually distinct square icons for folders, files, text files, audio files, and video files.

### 🔗 Sharing

Sharing is one of the main features of this project.

- Share individual files.
- Share entire folders.
- Copy share links from the browser.
- Manage all created shares from the share management page.
- Cancel existing shares.
- Browse shared folders.
- Download shared files.
- Download shared folders as `.7z` archives.
- Shared folder pages keep the same simple browsing behavior: click folders to enter and click files to download.

### 🎵 Online audio playback

Audio files can be played directly in the browser.

- Signed-in users can right-click supported audio files and open playback in a new tab.
- Shared audio files can also be played online from the share page.
- Shared audio files can still be downloaded normally.
- Playback uses browser-native media controls.

### 🎬 Online video playback

Video files can be played directly in the browser.

- Signed-in users can right-click supported video files and open playback in a new tab.
- Shared video files can also be played online from the share page.
- Shared video files can still be downloaded normally.
- Playback uses browser-native media controls.

### 📝 Online plain-text editing

Signed-in users can edit plain-text files online.

- Text editing opens in a separate browser tab.
- The editor is large and focused on screen space.
- The editor preserves indentation and line breaks.
- `Tab` inserts indentation while editing.
- `Ctrl + S` / `Command + S` saves text.
- The app detects BOM and common text encodings.
- The app tries to preserve the original encoding and newline style when saving.

### 👀 Shared plain-text viewing

Shared text files can be viewed online in read-only mode.

- Shared text viewing opens in a separate browser tab.
- The interface is similar to the editor.
- Shared users can view and download the text file.
- Shared users cannot modify or save the file.
- This is useful for sharing logs, notes, Markdown documents, code snippets, configuration files, and plain-text articles.

### 🗜️ Archive download behavior

Folder downloads are generated as `.7z` files.

- The app uses `py7zr` for high-compression archive generation.
- Archive files are written to a temporary archive directory on disk.
- The browser waits while the server prepares the archive.
- After the archive is ready, the browser starts the download.
- Temporary archives are automatically cleaned up after the configured retention period.
- The default archive retention time is one day.
- The archive workflow avoids loading the whole archive into memory at once.

### 🖥️ Interface behavior

- Bootstrap-based interface.
- No separate frontend build system.
- Embedded templates and static assets.
- Right-click menus are used for most drive actions.
- File and folder icons are visually distinct.
- The main file browser uses the screen space directly and avoids heavy container borders.
- Media preview, text editing, and shared text viewing open in separate tabs so the current file list is not replaced.

## 📦 Repository contents

```text
single-file-flask-drive/
  app.py             # Complete single-file Flask application
  README.md          # Project documentation
  LICENSE            # GNU General Public License v3.0
  requirements.txt   # pip dependency list
```

## 🧩 Dependencies

The runtime dependency list is intentionally small.

```text
flask
py7zr
charset-normalizer
```

### Dependency roles

| Dependency | Why it is used |
|---|---|
| `flask` | Web server, routing, sessions, request handling, response handling, and template rendering. |
| `py7zr` | Generates `.7z` archives for folder downloads. |
| `charset-normalizer` | Helps detect text encodings when opening plain-text files for editing or read-only viewing. |

### Install dependencies

Install the dependencies with:

```bash
pip install flask py7zr charset-normalizer
```

Or install from the included dependency file:

```bash
pip install -r requirements.txt
```

### Suggested `requirements.txt`

```text
flask
py7zr
charset-normalizer
```

## 📥 Clone and run

Clone the repository:

```bash
git clone https://github.com/wangyifan349/single-file-flask-drive.git
cd single-file-flask-drive
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the app:

```bash
python app.py
```

Open the app in your browser:

```text
http://127.0.0.1:5000
```

## 🧪 Recommended local setup

A virtual environment is recommended.

### Linux / macOS

```bash
git clone https://github.com/wangyifan349/single-file-flask-drive.git
cd single-file-flask-drive
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

### Windows PowerShell

```powershell
git clone https://github.com/wangyifan349/single-file-flask-drive.git
cd single-file-flask-drive
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Then open:

```text
http://127.0.0.1:5000
```

## 🌐 Simple deployment notes

The simplest deployment is:

```bash
git clone https://github.com/wangyifan349/single-file-flask-drive.git
cd single-file-flask-drive
pip install -r requirements.txt
python app.py
```

The application itself does not require Node.js, npm, Vite, React, Vue, a template directory, or a static directory.

For a long-running server, run the app behind a process manager or WSGI server of your choice. For public deployment, use HTTPS and set a stable `SECRET_KEY`.

## ⚙️ Environment variables

### `SECRET_KEY`

A stable Flask session secret. Set this in production so sessions remain valid across restarts.

Generate one with:

```bash
python -c 'import secrets; print(secrets.token_hex())'
```

Example:

```bash
export SECRET_KEY="replace-this-with-your-generated-secret"
python app.py
```

### `DRIVE_DATA_DIR`

Optional path for application data.

If this is not set, the app creates a local `data/` directory beside `app.py`.

Example:

```bash
export DRIVE_DATA_DIR="/opt/single-file-flask-drive-data"
python app.py
```

### `MAX_CONTENT_LENGTH`

Optional maximum request size in bytes.

Example for roughly 2 GB:

```bash
export MAX_CONTENT_LENGTH="2147483648"
python app.py
```

### `ARCHIVE_RETENTION_SECONDS`

Optional number of seconds to keep temporary `.7z` archives before cleanup.

Default: one day.

Example for two days:

```bash
export ARCHIVE_RETENTION_SECONDS="172800"
python app.py
```

### `SESSION_COOKIE_SECURE`

Set this to `1` when running behind HTTPS so the browser only sends session cookies over secure connections.

```bash
export SESSION_COOKIE_SECURE="1"
python app.py
```

## 🗂️ Data layout

By default, the app creates this local directory structure:

```text
data/
  drive.db
  uploads/
  tmp_archives/
  .secret_key
```

### `drive.db`

SQLite database file. It stores users, file metadata, folder metadata, and share records.

### `uploads/`

Uploaded file content is stored here.

### `tmp_archives/`

Temporary `.7z` files are stored here while users download folder archives.

### `.secret_key`

If `SECRET_KEY` is not provided, the app creates a local stable secret key file on first run.

## 🔐 Security notes

This project includes practical security measures for a small self-hosted drive:

- User ownership checks for private files and folders.
- Share-token checks for shared files and folders.
- `HttpOnly` session cookies.
- `SameSite=Lax` session cookies.
- Basic browser security headers.
- Disk-based archive generation instead of large in-memory archives.
- Random token generation for share links.
- Random salt generation for password hashing.

For stronger production security, consider using a dedicated password hashing algorithm such as Argon2id or bcrypt, running only behind HTTPS, adding rate limiting, adding account lockout rules, and placing the app behind a mature reverse proxy.

## ☕ Support the author

If this project helps you and you would like to say thanks, you can buy me a coffee. I really appreciate it.

Bitcoin:

```text
bc1qxqfhumpqtnxrznkx9r4xsp8m6zsedtgusjns7p
```

## 🧾 License

This project is licensed under the **GNU General Public License v3.0**.

See [`LICENSE`](LICENSE) for the full license text.

```text
Copyright (C) 2026 wangyifan349
```

## 👤 Author

GitHub: [wangyifan349](https://github.com/wangyifan349/)

## 🧭 Quick command summary

```bash
git clone https://github.com/wangyifan349/single-file-flask-drive.git
cd single-file-flask-drive
pip install -r requirements.txt
python app.py
```

Open:

```text
http://127.0.0.1:5000
```
