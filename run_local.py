"""Start a loopback dashboard for viewing and editing local settings."""

import os
from pathlib import Path
from secrets import token_urlsafe

import uvicorn
from fastapi import HTTPException

project_dir = Path(__file__).resolve().parent
password_file = project_dir / ".local-admin"
if not password_file.exists():
    fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write("admin\n")
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASS"] = password_file.read_text().strip()
os.environ["DATA_DIR"] = str(project_dir / "local-data")
os.environ["DB_PATH"] = str(project_dir / "local-data" / "payout_test.sqlite3")
os.environ.setdefault("HEADLESS", "false")
os.environ.setdefault("BROWSER_CHANNEL", "chrome")

from app.main import app


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765)
