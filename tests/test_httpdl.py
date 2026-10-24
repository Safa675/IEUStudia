"""The resumable downloader against a small local server that answers Range requests."""

import http.server
import os
import threading

import pytest
import requests

from studia import httpdl


class _Ranged(http.server.BaseHTTPRequestHandler):
    data = b""

    def do_GET(self):  # noqa: N802 - http.server's naming
        rng = self.headers.get("Range")
        if rng:
            a, b = rng.split("=")[1].split("-")
            a, b = int(a), int(b or len(self.data) - 1)
            body = self.data[a:b + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(self.data)}")
        else:
            body = self.data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    _Ranged.data = os.urandom(20 * 1024 * 1024)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Ranged)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/model.bin"
    srv.shutdown()


def test_a_download_in_parts_resumes_and_leaves_nothing_behind(server, tmp_path):
    dest = tmp_path / "model.bin"
    first = dest.with_name("model.bin.part0")
    first.write_bytes(_Ranged.data[:1000])                      # an earlier attempt got this far
    seen = []
    httpdl.download(server, dest, session=requests.Session(), parts=4, progress=lambda d, t: seen.append((d, t)))
    assert dest.read_bytes() == _Ranged.data and seen[-1] == (len(_Ranged.data), len(_Ranged.data))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["model.bin"]


def test_joining_frees_each_part_and_without_room_touches_nothing(tmp_path, monkeypatch):
    import errno
    import shutil
    import types

    parts = [tmp_path / f"f.part{i}" for i in range(3)]
    for i, p in enumerate(parts):
        p.write_bytes(bytes([i]) * 10)
    httpdl._join(parts, tmp_path / "f.part")
    assert (tmp_path / "f.part").read_bytes() == b"\0" * 10 + b"\1" * 10 + b"\2" * 10
    assert not any(p.exists() for p in parts)
    for i, p in enumerate(parts):
        p.write_bytes(bytes([i]) * 10)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: types.SimpleNamespace(free=1000))     # a nearly full disk
    with pytest.raises(OSError) as err:
        httpdl._join(parts, tmp_path / "g.part")
    assert err.value.errno == errno.ENOSPC and all(p.exists() for p in parts)          # the next try only joins


def test_parts_of_a_file_that_changed_since_are_not_reused(server, tmp_path):
    dest = tmp_path / "a.m4a"
    dest.with_name("a.m4a.part0").write_bytes(b"\xff" * 5000)        # parts of an earlier version of the recording
    dest.with_name("a.m4a.parts.json").write_text('{"total": 123, "version": ""}', encoding="utf-8")
    httpdl.download(server, dest, session=requests.Session(), parts=4)
    assert dest.read_bytes() == _Ranged.data and sorted(p.name for p in tmp_path.iterdir()) == ["a.m4a"]
