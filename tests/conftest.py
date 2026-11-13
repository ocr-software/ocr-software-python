"""Fake local HTTP service for the tests. No network, no key, never the production API.
Run: python -m pytest integrations/python -p no:cacheprovider"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ocr_software import client as client_module  # noqa: E402

PDF = b"%PDF-1.4 fake"
ENV_NAMES = ("OCR_KEY", "OCR_BASE_URL", "OCR_ALLOWED_DIR", "OCR_SOFTWARE_ALLOWED_DIR", "OCR_SOFTWARE_BASE_URL",
             "OCR_SOFTWARE_API_KEY")


class Fake(BaseHTTPRequestHandler):
    """Records every request; scripted[(method, path)] is a list of (status, headers, body) used in order, the last one repeats; status 0 cuts the connection."""
    log = []
    scripted = {}

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        path = self.path.split("?")[0]
        Fake.log.append({"method": self.command, "path": path, "query": self.path.partition("?")[2],
                         "auth": self.headers.get("Authorization"), "idem": self.headers.get("Idempotency-Key"), "body": body})
        queue = Fake.scripted.get((self.command, path), [])
        status, headers, payload = queue.pop(0) if len(queue) > 1 else queue[0]
        if status == 0:  # the service cuts the connection without any answer
            self.close_connection = True
            return
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = _handle

    def log_message(self, *args):
        pass


def err(status, code, message="m", headers=None):
    return (status, headers or {}, {"error": {"code": code, "message": message}})


def job_flow(fake, job_id="j1", job=None, result=b"hello"):
    """Script the three calls of one finished job: submit, status, result."""
    state = dict({"job_id": job_id, "state": "done", "pages_failed": 0, "pages_not_run": 0}, **(job or {}))
    fake.scripted[("POST", "/v1/jobs")] = [(202, {}, {"job_id": job_id, "state": "running"})]
    fake.scripted[("GET", "/v1/jobs/" + job_id)] = [(200, {}, state)]
    fake.scripted[("GET", "/v1/jobs/%s/result" % job_id)] = [(200, {}, result)]


@pytest.fixture
def fake(monkeypatch):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=lambda: httpd.serve_forever(0.02), daemon=True).start()
    Fake.log, Fake.scripted = [], {}
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OCR_SOFTWARE_BASE_URL", "http://127.0.0.1:%d" % httpd.server_address[1])
    monkeypatch.setenv("OCR_SOFTWARE_API_KEY", "ocr_testkey")
    monkeypatch.setattr(client_module, "BACKOFF_BASE_S", 0)
    yield Fake
    httpd.shutdown()


@pytest.fixture
def doc(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(PDF)
    return str(p)


@pytest.fixture
def refused(monkeypatch):
    """Every connection is refused (no real socket: a closed port takes seconds per try on Windows). Returns the attempt counter."""
    import urllib.error
    attempts = []

    def open_refused(*args, **kwargs):
        attempts.append(1)
        raise urllib.error.URLError(ConnectionRefusedError("refused"))
    monkeypatch.setattr(client_module._OPENER, "open", open_refused)
    monkeypatch.setattr(client_module, "BACKOFF_BASE_S", 0)
    return attempts
