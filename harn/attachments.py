"""Per-task file attachments — ``harn_env/storage/<task_id>/<filename>``.

For anything a design.py-style single HTML mockup doesn't fit: a reference
screenshot the human pastes in, a diagram the agent generates, an exported
asset. Files live directly on disk (no DB, no task-JSON field to keep in sync —
the directory listing IS the source of truth, same spirit as design.py). The
studio UI's board renders images as thumbnails and offers any file for
download; the MCP `read_attachment` tool hands an image straight back to the
agent as real image content, so it can actually SEE a design reference instead
of just knowing a file exists.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

STORAGE_DIRNAME = "storage"

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}


def attachments_dir(env_dir: Path, task_id: str) -> Path:
    return env_dir / STORAGE_DIRNAME / task_id


def is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in _IMAGE_EXTS


def _safe_name(filename: str) -> str:
    """Basename only (blocks path traversal via `../` or absolute paths)."""
    name = Path(filename).name.strip()
    return re.sub(r"[\x00-\x1f]", "", name)   # strip control chars; keep unicode


def _unique_path(d: Path, name: str) -> Path:
    """Never silently overwrite a differently-intentioned file: `design.png`,
    `design (1).png`, `design (2).png`, … Delete first if you want to replace."""
    p = d / name
    if not p.exists():
        return p
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 1
    while True:
        p = d / f"{stem} ({i}){suffix}"
        if not p.exists():
            return p
        i += 1


def save(env_dir: Path, task_id: str, filename: str, data: bytes) -> Path:
    name = _safe_name(filename)
    if not name:
        raise ValueError("empty filename")
    d = attachments_dir(env_dir, task_id)
    d.mkdir(parents=True, exist_ok=True)
    p = _unique_path(d, name)
    p.write_bytes(data)
    return p


def list_files(env_dir: Path, task_id: str) -> list[dict]:
    d = attachments_dir(env_dir, task_id)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_file():
            continue
        st = p.stat()
        out.append({
            "name": p.name,
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kind": "image" if is_image(p.name) else "file",
        })
    return out


def read_bytes(env_dir: Path, task_id: str, filename: str) -> bytes | None:
    p = attachments_dir(env_dir, task_id) / _safe_name(filename)
    if not p.is_file():
        return None
    try:
        return p.read_bytes()
    except OSError:
        return None


def path_for(env_dir: Path, task_id: str, filename: str) -> Path | None:
    """Resolved on-disk path for an existing attachment, or None."""
    p = attachments_dir(env_dir, task_id) / _safe_name(filename)
    return p if p.is_file() else None


def delete(env_dir: Path, task_id: str, filename: str) -> bool:
    p = attachments_dir(env_dir, task_id) / _safe_name(filename)
    if not p.is_file():
        return False
    p.unlink()
    return True
