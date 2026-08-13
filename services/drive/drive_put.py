#!/usr/bin/env python3
"""drive-put — upload a local file into the operator's Google Drive `/agent` tree.

The agents' Drive MCP can create Google *Docs* from text, and nothing else: there was no way to put
a PDF (a clinical letter, a statement, a scan the operator sent over Telegram) into Drive at all.
This is that missing verb, deliberately small:

    drive-put <file> [folder] [--name "Nicer Name.pdf"]

`folder` is a path RELATIVE TO /agent (default `inbox`), created if missing — `drive-put x.pdf
articles` lands in the same /agent/articles the article pipeline uses. Prints the Drive link.

Auth: the service account at ~/.config/claude-dev/google-sa.json with domain-wide delegation,
impersonating the operator, scopes drive.readonly + drive.file — the same pair docindex/copy2reorg
use. drive.file is enough to create files and folders and to write into folders this app made.

Interpreter: googleapiclient is NOT in the system python; the wrapper script picks a venv that has
it (see services/drive/drive-put).
"""
import argparse, mimetypes, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog  # noqa: E402  — no silent swallows; see services/lib/errlog.py

from google.oauth2 import service_account          # noqa: E402
from googleapiclient.discovery import build        # noqa: E402
from googleapiclient.http import MediaFileUpload   # noqa: E402

KEY = os.path.expanduser("~/.config/claude-dev/google-sa.json")
SUBJECT = "mikael@odinlake.net"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly",
          "https://www.googleapis.com/auth/drive.file"]
AGENT_ROOT = "14dCaUKDP-4zkbTixtCC09Jpa6-gOhr-5"    # Drive /agent — the agents' own subtree
FOLDER_MIME = "application/vnd.google-apps.folder"


def service():
    creds = service_account.Credentials.from_service_account_file(KEY, scopes=SCOPES, subject=SUBJECT)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def ensure_folder(svc, path, parent=AGENT_ROOT):
    """Resolve 'a/b' under /agent to a folder id, creating what doesn't exist yet."""
    for part in [p for p in (path or "").split("/") if p]:
        q = "name = '%s' and mimeType = '%s' and '%s' in parents and trashed = false" % (
            part.replace("'", "\\'"), FOLDER_MIME, parent)
        found = svc.files().list(q=q, fields="files(id)", pageSize=1,
                                 supportsAllDrives=True).execute().get("files", [])
        parent = (found[0]["id"] if found else
                  svc.files().create(body={"name": part, "mimeType": FOLDER_MIME, "parents": [parent]},
                                     fields="id").execute()["id"])
    return parent


def put(local, folder="inbox", name=None):
    src = Path(local).expanduser()
    if not src.is_file(): raise SystemExit("no such file: %s" % src)
    svc = service()
    parent = ensure_folder(svc, folder)
    mime = mimetypes.guess_type(src.name)[0] or "application/octet-stream"
    f = svc.files().create(
        body={"name": name or src.name, "parents": [parent]},
        media_body=MediaFileUpload(str(src), mimetype=mime, resumable=src.stat().st_size > 5 * 1024 * 1024),
        fields="id,name,webViewLink,size").execute()
    return f


def main():
    ap = argparse.ArgumentParser(description="upload a file into Google Drive /agent/<folder>")
    ap.add_argument("file")
    ap.add_argument("folder", nargs="?", default="inbox", help="path under /agent (default: inbox)")
    ap.add_argument("--name", help="name to give it in Drive (default: the local filename)")
    a = ap.parse_args()
    try:
        f = put(a.file, a.folder, a.name)
    except SystemExit:
        raise
    except Exception as e:                       # an upload that fails must be loud, never assumed
        errlog.err("drive-put: uploading %s to /agent/%s" % (a.file, a.folder), e)
        raise
    print("uploaded: /agent/%s/%s\n%s" % (a.folder, f["name"], f.get("webViewLink", "(no link)")))


if __name__ == "__main__":
    main()
