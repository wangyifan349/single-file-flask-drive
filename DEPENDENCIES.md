# Dependencies 🧩

This project intentionally keeps the runtime dependency list small.

## Python packages

Install everything with:

```bash
pip install -r requirements.txt
```

Or install the packages directly:

```bash
pip install flask py7zr charset-normalizer
```

## Runtime dependency list

```text
flask
py7zr
charset-normalizer
```

## What each dependency does

### Flask

Flask provides the web server framework used by `app.py`, including routing, request handling, response handling, session support, template rendering, redirects, JSON responses, and file downloads.

### py7zr

py7zr is used to generate `.7z` archives for folder downloads. The application writes these archives to a temporary directory on disk and sends them to the browser after generation is complete.

### charset-normalizer

charset-normalizer helps detect text file encodings for online text viewing and editing. The application also has fallback encoding logic for common encodings.

## Standard library modules

The application also uses Python standard library modules such as:

```text
datetime
hashlib
mimetypes
os
pathlib
sqlite3
uuid
```

These do not need to be installed separately.

## Frontend library

The interface loads Bootstrap from a CDN in the embedded HTML template. No npm installation or frontend build step is required.
