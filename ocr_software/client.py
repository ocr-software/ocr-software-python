"""Client for the ocr-software.com API (standard library only, Python 3.9 or newer).

Settings: OCR_SOFTWARE_API_KEY (or OCR_KEY, the name used in the API docs), OCR_SOFTWARE_BASE_URL (or OCR_BASE_URL),
OCR_SOFTWARE_ALLOWED_DIR (optional). The key is never printed, never in an error message, never in a log.
Spec of every path, field and error code: <base>/openapi.json.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_BASE_URL = "https://api.ocr-software.com"
MAX_RETRIES = 5  # retry rules of agents.md: 429, 503, 500 and network errors, waits 1, 2, 4, 8, 16 s
BACKOFF_BASE_S = 1.0  # tests set it to 0
RETRY_AFTER_LIMIT_S = 60  # longer: stop (locked_out has 900)
HTTP_TIMEOUT_S = 90  # a status call waits up to 60 s on the server
CONNECT_TIMEOUT_S = 10  # a wrong or unreachable address must fail fast, not after the read timeout
CONNECT_RETRIES = 2  # no connection at all (refused, unknown host, no route): 2 retries, not 5
WAIT_SLICE_S = 30  # one long poll of wait()
DEFAULT_WAIT_TIMEOUT_S = 3600
FORMATS = ("json", "text", "md", "hocr", "pdf")
MAX_DOCUMENT_BYTES = 100 * 1024 * 1024  # same as max_body_bytes of the service
MAX_RESPONSE_BYTES = 256 * 1024 * 1024  # a larger result is refused, never held in memory
DOCUMENT_MAGIC = (b"%PDF-", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"II*\x00", b"MM\x00*")  # PDF, PNG, JPEG, TIFF
JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
KEY_CHARS = re.compile(r"^[\x21-\x7e]+$")


class OcrError(Exception):
    """Any failure. code is the API error code (unauthorized, busy, ...) or one of: no_key, bad_input, network, timeout."""

    def __init__(self, message, code="error", http_status=0):
        super().__init__(message)
        self.code = code
        self.http_status = http_status  # 0 if the failure was not an HTTP answer


class PagesUnread(OcrError):
    """The job is done but not every page was read. data is the result anyway, job the final job state."""

    def __init__(self, message, job, data):
        super().__init__(message, code="pages_unread")
        self.job = job
        self.data = data


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The key must never follow a redirect to another host."""

    def redirect_request(self, *args, **kwargs):
        return None


class _ConnectTimeoutMixin:
    """Connect with CONNECT_TIMEOUT_S, then read with the normal timeout (urllib has one timeout for both)."""

    def connect(self):
        read_timeout, self.timeout = self.timeout, CONNECT_TIMEOUT_S
        try:
            super().connect()
        finally:
            self.timeout = read_timeout
        self.sock.settimeout(read_timeout)


class _HTTPConnection(_ConnectTimeoutMixin, http.client.HTTPConnection):
    pass


class _HTTPSConnection(_ConnectTimeoutMixin, http.client.HTTPSConnection):
    pass


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req)


_OPENER = urllib.request.build_opener(_NoRedirect, _HTTPHandler, _HTTPSHandler)


def _int(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return 0


def _error_of(e):
    """Code and message of an API error body {"error":{"code","message","details","missing","pipelines"}}."""
    try:
        err = json.loads(e.read(65536))["error"]  # bounded: an error body is short
    except (ValueError, KeyError, TypeError, OSError, http.client.HTTPException):
        return "http_error", str(e.reason)
    extra = {k: err[k] for k in ("details", "missing", "pipelines") if k in err}
    return err.get("code", "http_error"), err.get("message", "") + (" " + json.dumps(extra) if extra else "")


def check_job_id(job_id):
    if not isinstance(job_id, str) or not JOB_ID.match(job_id):
        raise OcrError("invalid job_id", "bad_input")


def read_document(path, allowed_dir=None):
    """Read a local document: inside allowed_dir if set (realpath, so no escape by symlink or ..), a regular file of
    at most 100 MiB, starting with the magic bytes of PDF, PNG, JPEG or TIFF."""
    real = os.path.realpath(path)
    if allowed_dir:
        allowed = os.path.realpath(allowed_dir)
        try:
            inside = os.path.commonpath([real, allowed]) == allowed
        except ValueError:  # other drive on Windows
            inside = False
        if not inside:
            raise OcrError("file is outside the allowed folder: %s" % path, "bad_input")
    try:
        with open(real, "rb") as f:  # one open: the file that is checked is the file that is sent
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise OcrError("not a file: %s" % path, "bad_input")
            if st.st_size > MAX_DOCUMENT_BYTES:
                raise OcrError("file is larger than %d MiB: %s" % (MAX_DOCUMENT_BYTES >> 20, path), "bad_input")
            data = f.read(MAX_DOCUMENT_BYTES + 1)  # bounded even if the file grows meanwhile
    except OSError:
        raise OcrError("not a file: %s" % path, "bad_input")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise OcrError("file is larger than %d MiB: %s" % (MAX_DOCUMENT_BYTES >> 20, path), "bad_input")
    # like the service: a PDF may start with junk, "%PDF-" anywhere in the first 1024 bytes
    if not data:
        raise OcrError("file is empty: %s" % path, "bad_input")
    if not (data.startswith(DOCUMENT_MAGIC) or b"%PDF-" in data[:1024]):
        raise OcrError("not a PDF, PNG, JPEG or TIFF file: %s" % path, "bad_input")
    return data


def unread_message(job):
    """None if every page was read, else one sentence saying what is missing."""
    failed, not_run, stop = job.get("pages_failed", 0), job.get("pages_not_run", 0), job.get("stop")
    if not (failed or not_run or stop):
        return None
    parts = []
    if failed:
        parts.append("%d page(s) failed" % failed)
    if not_run:
        parts.append("%d page(s) not run" % not_run)
    if stop:
        parts.append("stopped: %s" % stop)
    return "job %s is done but not every page was read: %s" % (job.get("job_id", "?"), ", ".join(parts))


class Client:
    def __init__(self, api_key=None, base_url=None, allowed_dir=None):
        env = os.environ
        key = api_key if api_key is not None else env.get("OCR_SOFTWARE_API_KEY") or env.get("OCR_KEY", "")
        self._key = key.strip()  # a key copied from a file or $(cat key) often ends in a newline
        self.base_url = (base_url or env.get("OCR_SOFTWARE_BASE_URL") or env.get("OCR_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
        self.allowed_dir = allowed_dir if allowed_dir is not None else env.get("OCR_SOFTWARE_ALLOWED_DIR") or env.get("OCR_ALLOWED_DIR")
        if urllib.parse.urlsplit(self.base_url).scheme not in ("http", "https"):
            raise OcrError("base URL must start with http:// or https://", "bad_input")

    def __repr__(self):
        return "Client(base_url=%r)" % self.base_url  # never the key

    def _call(self, method, path, query=None, body=None, key_needed=True, headers=None):
        """One API call with the retry rules. Returns (bytes, headers). Raises OcrError carrying the API error code."""
        url = self.base_url + path
        query = {k: v for k, v in (query or {}).items() if v is not None}
        if query:
            url += "?" + urllib.parse.urlencode(query, safe=",@")
        head = dict(headers or {})
        if key_needed:
            if not self._key:
                raise OcrError("API key missing: set OCR_SOFTWARE_API_KEY (the key starts with ocr_)", "no_key")
            if not KEY_CHARS.fullmatch(self._key):  # fullmatch: "$" would let a trailing newline pass  # a bad character would make http.client echo the key
                raise OcrError("API key contains characters that are not allowed in a header", "no_key")
            head["Authorization"] = "Bearer " + self._key
        for attempt in range(MAX_RETRIES + 1):
            req = urllib.request.Request(url, data=body, method=method, headers=head)
            try:
                with _OPENER.open(req, timeout=HTTP_TIMEOUT_S) as resp:
                    data = resp.read(MAX_RESPONSE_BYTES + 1)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise OcrError("response larger than %d bytes, refused" % MAX_RESPONSE_BYTES, "too_large")
                    return data, resp.headers
            except urllib.error.HTTPError as e:
                code, message = _error_of(e)
                retry_after = _int(e.headers.get("Retry-After"))
                if retry_after > RETRY_AFTER_LIMIT_S:
                    raise OcrError("%d %s: %s (Retry-After %d s: not retried, ask the human)" % (e.code, code, message, retry_after), code, e.code)
                if e.code not in (429, 500, 503) or attempt == MAX_RETRIES:
                    raise OcrError("%d %s: %s" % (e.code, code, message), code, e.code)
                delay = max(retry_after, BACKOFF_BASE_S * 2 ** attempt)
            except urllib.error.URLError as e:  # urllib raises URLError only if the request could not be sent: no connection
                if attempt >= CONNECT_RETRIES:
                    raise OcrError("cannot connect to %s: %s (check OCR_SOFTWARE_BASE_URL or --base-url)" % (self.base_url, e.reason), "network")
                delay = BACKOFF_BASE_S * 2 ** attempt
            except (OSError, http.client.HTTPException) as e:  # timeouts, timeouts (socket.timeout is no TimeoutError before 3.10), resets, cut bodies
                if attempt == MAX_RETRIES:
                    raise OcrError("network error: %s" % e, "network")
                delay = BACKOFF_BASE_S * 2 ** attempt
            time.sleep(delay)

    def _json(self, method, path, **kw):
        data, _ = self._call(method, path, **kw)
        try:
            answer = json.loads(data)
        except ValueError:  # a proxy or captive portal answering 200 with a web page
            answer = None
        if not isinstance(answer, dict):
            raise OcrError("%s did not answer like the OCR API (not JSON): check OCR_SOFTWARE_BASE_URL" % self.base_url, "bad_response")
        return answer

    def catalog(self):
        """What the service offers (no key needed): pipelines, formats, features."""
        return self._json("GET", "/v1/catalog", key_needed=False)

    def quote(self, path, pipeline, dpi=None):
        """Dry run: nothing runs, nothing is billed. Returns the page count and warnings."""
        body = read_document(path, self.allowed_dir)
        return self._json("POST", "/v1/jobs", query={"pipeline": pipeline, "dry_run": 1, "dpi": dpi}, body=body)

    def submit(self, path, pipeline, formats=None, features=None, dpi=None, client_ref=None, job_budget=None):
        """Submit one document. Returns the job state (job_id, state, pages ...); the job may still run."""
        return self._submit(read_document(path, self.allowed_dir), pipeline, formats, features, dpi, client_ref, job_budget)

    def _submit(self, data, pipeline, formats=None, features=None, dpi=None, client_ref=None, job_budget=None):
        if not pipeline:
            raise OcrError("pipeline is required (name@version, see catalog())", "bad_input")
        query = {"pipeline": pipeline, "formats": formats, "features": features, "dpi": dpi, "client_ref": client_ref,
                 "job_budget": job_budget}  # no wait here: POST /v1/jobs rejects it (400); status() long-polls
        # The idempotency key makes the retries of the retry rules safe: no second job, no second charge.
        return self._json("POST", "/v1/jobs", query=query, body=data, headers={"Idempotency-Key": str(uuid.uuid4())})

    def status(self, job_id, wait=0):
        """State and counts of a job. wait (0 to 60 s): hold until the job changes."""
        check_job_id(job_id)
        return self._json("GET", "/v1/jobs/" + job_id, query={"wait": max(0, min(60, int(wait)))})

    def wait(self, job_id, timeout=DEFAULT_WAIT_TIMEOUT_S):
        """Poll until state is done, return the last state. done does not mean every page worked: see unread_message()."""
        deadline = time.monotonic() + timeout
        while True:
            job = self.status(job_id, wait=WAIT_SLICE_S)
            if job.get("state") == "done":
                return job
            if time.monotonic() >= deadline:
                raise OcrError("job %s still running after %d s" % (job_id, timeout), "timeout")

    def result(self, job_id, format="json", source_path=None):
        """Result bytes of a finished job. format pdf (searchable PDF) needs source_path, the original document."""
        return self._result(job_id, format, read_document(source_path, self.allowed_dir) if source_path else None)

    def _result(self, job_id, format, source):
        check_job_id(job_id)
        if format not in FORMATS:
            raise OcrError("format must be one of %s" % ", ".join(FORMATS), "bad_input")
        if format == "pdf":
            if source is None:
                raise OcrError("format pdf needs the original document (source_path)", "bad_input")
            data, _ = self._call("POST", "/v1/jobs/%s/result" % job_id, query={"format": "pdf"}, body=source)
        else:
            data, _ = self._call("GET", "/v1/jobs/%s/result" % job_id, query={"format": format})
        return data

    def ocr(self, path, pipeline, format="text", timeout=DEFAULT_WAIT_TIMEOUT_S, **options):
        """Submit, wait, fetch: the result bytes. options go to submit (dpi, client_ref, job_budget, formats, features).
        Raises PagesUnread (with .data, the result anyway) if a page failed or was not run: never silent."""
        if format not in FORMATS:
            raise OcrError("format must be one of %s" % ", ".join(FORMATS), "bad_input")
        source = read_document(path, self.allowed_dir)  # read once: PDF output needs the same bytes again
        job_id = self._submit(source, pipeline, **options).get("job_id")
        if not job_id:
            raise OcrError("the answer of %s has no job_id: check OCR_SOFTWARE_BASE_URL" % self.base_url, "bad_response")
        done = self.wait(job_id, timeout)
        data = self._result(job_id, format, source)
        problem = unread_message(done)
        if problem:
            raise PagesUnread(problem, done, data)
        return data
