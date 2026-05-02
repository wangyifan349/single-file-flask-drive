# Single File Flask Drive ☁️

**Single File Flask Drive** is a compact, self-hosted cloud drive written as one deployable Python file: `app.py`.

The suggested GitHub repository name is:

```text
single-file-flask-drive
```

Suggested repository URL:

```text
https://github.com/wangyifan349/single-file-flask-drive
```

This project is designed for people who want a practical file manager that is easy to deploy, easy to inspect, and easy to maintain. The backend routes, SQLite database logic, embedded HTML templates, CSS, and JavaScript are all contained inside `app.py`, so there is no separate frontend build step and no complex project tree.

## ✨ What it does

`app.py` starts a complete web-based cloud drive service. It supports account management, hierarchical folders, uploads, downloads, sharing, share cancellation, online media playback, and online plain-text editing.

It stores metadata in SQLite and stores uploaded file content on disk. Folder downloads are generated as temporary `.7z` archives on disk, instead of being loaded as one large in-memory archive. This makes large folder downloads more stable and more suitable for real-world use.

## ✅ Project status

This package is intended to be directly usable.

The project has passed practical smoke testing, including application startup, registration, authenticated drive access, folder creation, file upload, file download, folder archive generation, sharing, shared browsing, and embedded static asset loading.

For production use, you should still deploy it behind HTTPS, set a stable `SECRET_KEY`, and back up the data directory regularly.

## 🚀 Main features

### 👤 Account features

- User registration.
- User login.
- User logout.
- Password change.
- Passwords are stored as salted SHA3-256 digests.
- Session cookies use safer defaults such as `HttpOnly` and `SameSite=Lax`.

### 📁 File and folder management

- Hierarchical folders.
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

### 🔗 Sharing features

- Share individual files.
- Share entire folders.
- Copy share links from the browser.
- Share management page.
- Cancel existing shares.
- Browse shared folders.
- Download shared files.
- Download shared folders as `.7z` archives.
- Shared folder pages preserve the same basic browsing behavior: click folders to enter and click files to download.

### 🎵 Audio and video playback

- Online audio playback.
- Online video playback.
- Playback opens in a separate browser tab.
- Shared audio and video files can also be played online.
- Shared media files can still be downloaded.

### 📝 Online text editing and viewing

- Signed-in users can edit plain-text files online.
- Shared plain-text files can be viewed online in read-only mode.
- Text editing opens in a separate browser tab.
- The editor is large and screen-focused.
- The editor supports preserving indentation and line breaks.
- `Tab` inserts indentation while editing.
- `Ctrl + S` / `Command + S` saves text.
- The app detects BOM and common text encodings.
- The app tries to preserve the original encoding and newline style when saving.

### 🗜️ Archive download behavior

- Folder downloads are generated as `.7z` files.
- The app uses `py7zr` for high-compression archive generation.
- Archive files are written to a temporary archive directory on disk.
- The browser waits while the server prepares the archive.
- After the archive is ready, the browser starts the download.
- Temporary archives are automatically cleaned up after the configured retention period.
- The default archive retention time is one day.

### 🖥️ Interface behavior

- Bootstrap-based interface.
- No separate frontend build system.
- Embedded templates and static assets.
- Right-click menus are used for most drive actions.
- File and folder icons are visually distinct.
- The main file browser uses the screen space directly and avoids heavy container borders.

## 📦 Repository contents

```text
single-file-flask-drive/
  app.py             # Complete single-file Flask application
  README.md          # Project documentation
  LICENSE            # GNU General Public License v3.0
  requirements.txt   # pip dependency list
  DEPENDENCIES.md    # Human-readable dependency notes
```

## 🧩 Dependency list

The runtime dependencies are intentionally small:

```text
flask
py7zr
charset-normalizer
```

Install them with:

```bash
pip install flask py7zr charset-normalizer
```

Or install from the included dependency file:

```bash
pip install -r requirements.txt
```

## 🛠️ Create the GitHub repository

Create a new repository under your GitHub account with this name:

```text
single-file-flask-drive
```

Your repository URL will be:

```text
https://github.com/wangyifan349/single-file-flask-drive
```

## 📥 Clone and run

After the repository is created and pushed to GitHub, deploy from `git clone` like this:

```bash
git clone https://github.com/wangyifan349/single-file-flask-drive.git
cd single-file-flask-drive
pip install -r requirements.txt
python app.py
```

Then open:

```text
http://127.0.0.1:5000
```

## 🧪 Recommended local setup

A virtual environment is recommended:

```bash
git clone https://github.com/wangyifan349/single-file-flask-drive.git
cd single-file-flask-drive
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

On Windows PowerShell:

```powershell
git clone https://github.com/wangyifan349/single-file-flask-drive.git
cd single-file-flask-drive
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

## 🌐 Simple deployment notes

The simplest deployment is just:

```bash
pip install -r requirements.txt
python app.py
```

For a long-running server, run the app behind a process manager or WSGI server of your choice. The application itself does not require Node.js, npm, Vite, React, Vue, a template directory, or a static directory.

For public deployment, use HTTPS and set a stable `SECRET_KEY`.

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
