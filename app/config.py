import os
from pathlib import Path

ROOT = Path(os.getenv("VLOG_ROOT", "/vlog"))
INBOX = ROOT / "inbox"
AUTO = ROOT / "_automation"
DATA = AUTO / "data"
PROJECTS = AUTO / "projects"
PROXIES = AUTO / "proxies"
AUDIO = AUTO / "audio"
OUTPUTS = AUTO / "outputs"
APPROVED = AUTO / "approved"
LOGS = AUTO / "logs"
MUSIC = AUTO / "music-library"
FACE_MODELS = AUTO / "models" / "opencv-face"
OWNER_IDENTITY = AUTO / "identity" / "owner"
PRIVACY_AVATAR = AUTO / "identity" / "privacy-avatar.jpg"
SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "30"))
STABLE_SECONDS = int(os.getenv("STABLE_SECONDS", "300"))
RAW_RETENTION_DAYS = int(os.getenv("RAW_RETENTION_DAYS", "14"))
FINAL_RETENTION_DAYS = int(os.getenv("FINAL_RETENTION_DAYS", "90"))
MIN_FREE_GIB = int(os.getenv("MIN_FREE_GIB", "80"))
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".mts", ".m2ts", ".webm"}
for path in (INBOX, DATA, PROJECTS, PROXIES, AUDIO, OUTPUTS, APPROVED, LOGS, MUSIC, FACE_MODELS, OWNER_IDENTITY):
    path.mkdir(parents=True, exist_ok=True)
