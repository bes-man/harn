"""Studio-level attachment wiring: upload_attachment/delete_attachment +
board_payload surfacing each task's files, and the raw file-serving HTTP
route (harn/studio.py, backed by harn/attachments.py)."""
from __future__ import annotations

import base64
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from harn import attachments, studio, tasks, ENV_DIRNAME


def _env(tmp_path):
    env = tmp_path / ENV_DIRNAME
    (env / "tasks").mkdir(parents=True)
    return env


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_upload_attachment_saves_file(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    r = studio.upload_attachment(env, {"task_id": t.id, "filename": "ref.png",
                                       "content_b64": _b64(b"pngbytes")})
    assert r["ok"] is True and r["name"] == "ref.png"
    assert attachments.read_bytes(env, t.id, "ref.png") == b"pngbytes"


def test_upload_attachment_unknown_task(tmp_path):
    env = _env(tmp_path)
    r = studio.upload_attachment(env, {"task_id": "NOPE", "filename": "a.png",
                                       "content_b64": _b64(b"x")})
    assert r["ok"] is False


def test_upload_attachment_missing_filename(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    r = studio.upload_attachment(env, {"task_id": t.id, "filename": "",
                                       "content_b64": _b64(b"x")})
    assert r["ok"] is False


def test_upload_attachment_bad_base64(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    r = studio.upload_attachment(env, {"task_id": t.id, "filename": "a.png",
                                       "content_b64": "!!!not valid!!!"})
    assert r["ok"] is False


def test_upload_attachment_too_large_rejected(tmp_path, monkeypatch):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    monkeypatch.setattr(studio, "_MAX_ATTACHMENT_BYTES", 10)
    r = studio.upload_attachment(env, {"task_id": t.id, "filename": "a.png",
                                       "content_b64": _b64(b"x" * 100)})
    assert r["ok"] is False and "large" in r["error"]
    assert attachments.list_files(env, t.id) == []


def test_delete_attachment(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    attachments.save(env, t.id, "a.png", b"x")
    r = studio.delete_attachment(env, {"task_id": t.id, "filename": "a.png"})
    assert r["ok"] is True
    assert attachments.list_files(env, t.id) == []


def test_delete_attachment_missing(tmp_path):
    env = _env(tmp_path)
    r = studio.delete_attachment(env, {"task_id": "PRJ-001", "filename": "nope.png"})
    assert r["ok"] is False


def test_board_payload_includes_attachments(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    attachments.save(env, t.id, "ref.png", b"x")
    payload = studio.board_payload(env)
    row = next(r for r in payload["tasks"] if r["id"] == t.id)
    assert row["attachments"][0]["name"] == "ref.png"
    assert row["attachments"][0]["kind"] == "image"


def test_board_payload_empty_attachments(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    payload = studio.board_payload(env)
    row = next(r for r in payload["tasks"] if r["id"] == t.id)
    assert row["attachments"] == []


# --------------------------------------------------------------------------- #
# raw HTTP layer — file serving + upload/delete round trip over the wire
# --------------------------------------------------------------------------- #
def _serve(env_dir: Path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), studio._make_handler(env_dir))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def test_http_serves_attachment_bytes_with_content_type(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    attachments.save(env, t.id, "ref.png", b"\x89PNGdata")
    srv, port = _serve(env)
    try:
        url = f"http://127.0.0.1:{port}/api/attachments/file?task={t.id}&name=ref.png&env={env}"
        resp = urllib.request.urlopen(url)
        assert resp.read() == b"\x89PNGdata"
        assert resp.headers.get("Content-Type") == "image/png"
    finally:
        srv.shutdown()


def test_http_missing_attachment_404s(tmp_path):
    env = _env(tmp_path)
    tasks.create_task(env, "Add auth", task_id="PRJ-001")
    srv, port = _serve(env)
    try:
        url = f"http://127.0.0.1:{port}/api/attachments/file?task=PRJ-001&name=nope.png&env={env}"
        try:
            urllib.request.urlopen(url)
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()


def test_http_upload_then_list_then_delete_round_trip(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    srv, port = _serve(env)
    try:
        def post(path, body):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}?env={env}",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            return json.loads(urllib.request.urlopen(req).read())

        r = post("/api/attachments/upload",
                 {"task_id": t.id, "filename": "shot.png", "content_b64": _b64(b"data")})
        assert r["ok"] is True

        board = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/board?env={env}").read())
        row = next(x for x in board["tasks"] if x["id"] == t.id)
        assert row["attachments"][0]["name"] == "shot.png"

        r = post("/api/attachments/delete", {"task_id": t.id, "filename": "shot.png"})
        assert r["ok"] is True
        assert attachments.list_files(env, t.id) == []
    finally:
        srv.shutdown()
