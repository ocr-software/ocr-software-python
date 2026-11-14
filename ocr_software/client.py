"""Client for the ocr-software.com API (standard library only, Python 3.9 or newer).

Settings (the OCR_SOFTWARE_ name wins if both are set): OCR_SOFTWARE_API_KEY (or OCR_KEY, the name used in the API docs), OCR_SOFTWARE_BASE_URL (or OCR_BASE_URL),
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

from .advice import default_pipeline

DEFAULT_BASE_URL = "https://api.ocr-software.com"
MAX_RETRIES = 5  # retry rules of agents.md: 429, 500, 502, 503, 504 and network errors, waits 1, 2, 4, 8, 16 s
RETRY_STATUS = (429, 500, 502, 503, 504)  # 502/504: proxy in front of a restarting service (seen 13.11.2026)
BACKOFF_BASE_S = 1.0  # tests set it to 0
RETRY_AFTER_LIMIT_S = 60  # longer: stop (locked_out has 900)
HTTP_TIMEOUT_S = 90  # a status call waits up to 60 s on the server
CONNECT_TIMEOUT_S = 10  # a wrong or unreachable address must fail fast, not after the read timeout
PROBE_MIN_BODY = 64 * 1024  # an upload cut after this much may have been refused early (wrong key, pipeline, address): ask
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


_CUT_OFF = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)  # Windows 10053/10054, Linux EPIPE/ECONNRESET


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
    message = err.get("message", "")
    extra = {k: err[k] for k in ("details", "missing", "pipelines") if k in err}
    details = extra.get("details")
    if isinstance(details, list) and all(isinstance(d, str) and d in message for d in details):
        del extra["details"]  # the service joins the details into the message: show them once
    return err.get("code", "http_error"), message + (" " + json.dumps(extra) if extra else "")


def _refusal(url, head):
    """The real answer to an upload that was cut. The service (behind its proxy) answers a wrong key, pipeline or address
    before it reads the body and closes; a client that is still sending gets a reset and never sees the answer (Windows
    10053/10054, found 13.11.2026). The same request with an empty body is refused for the same reason at once, and
    otherwise answers empty_document: then the key and the pipeline are fine and the cut was load. Returns an OcrError
    to raise, or None if the cut is to be retried."""
    probe_head = {k: v for k, v in head.items() if k != "Idempotency-Key"}  # the probe is not the upload
    try:
        with _OPENER.open(urllib.request.Request(url, data=b"", method="POST", headers=probe_head), timeout=CONNECT_TIMEOUT_S):
            return None
    except urllib.error.HTTPError as e:
        code, message = _error_of(e)
        if code == "empty_document" or (e.code in RETRY_STATUS and code != "locked_out"):
            return None
        return OcrError("%d %s: %s" % (e.code, code, message), code, e.code)
    except (OSError, http.client.HTTPException):  # no answer to the probe either: the normal retry rules decide
        return None


_PAGE_HEADING = re.compile(r"^## Page (\d+)\n\n", re.M)


def split_pages(text, format="text"):
    """The pages of a result as [(number, text)]. format "text": pages are separated by a form feed (as pdftotext does), "md":
    every page starts with the heading "## Page N" (the heading is not part of the page text). A result that does not look
    like either format is one page, never an error."""
    if format == "md":
        starts, want = [], 1
        for m in _PAGE_HEADING.finditer(text):  # a heading the model wrote inside a page does not count: numbers run 1, 2, 3 ...
            if int(m.group(1)) == want:
                starts.append(m)
                want += 1
        if starts:
            ends = [m.start() for m in starts[1:]] + [len(text)]
            return [(i + 1, text[m.end():end].rstrip("\n") + "\n") for i, (m, end) in enumerate(zip(starts, ends))]
        return [(1, text)]
    return [(i + 1, page) for i, page in enumerate(text.split("\f"))]


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
                raise OcrError("API key missing: set OCR_SOFTWARE_API_KEY or OCR_KEY (the key starts with ocr_)", "no_key")
            if not KEY_CHARS.fullmatch(self._key):  # fullmatch: "$" would let a trailing newline pass  # a bad character would make http.client echo the key
                raise OcrError("API key contains characters that are not allowed in a header", "no_key")
            head["Authorization"] = "Bearer " + self._key
        probed = False
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
                if e.code not in RETRY_STATUS or attempt == MAX_RETRIES:
                    raise OcrError("%d %s: %s" % (e.code, code, message), code, e.code)
                delay = max(retry_after, BACKOFF_BASE_S * 2 ** attempt)
            except urllib.error.URLError as e:  # urllib raises URLError only if the request could not be sent
                if isinstance(e.reason, _CUT_OFF) and body and len(body) >= PROBE_MIN_BODY and not probed:
                    probed = True
                    refusal = _refusal(url, head)
                    if refusal:
                        raise refusal
                if isinstance(e.reason, _CUT_OFF):  # connected, then cut: the service drops an upload beyond 2 per key instead of 429
                    if attempt == MAX_RETRIES:
                        raise OcrError("the service cut the connection %d times (more than 2 uploads at once with this key?): %s" % (attempt + 1, e.reason), "network")
                    delay = BACKOFF_BASE_S * 2 ** attempt
                elif attempt >= CONNECT_RETRIES:
                    raise OcrError("cannot connect to %s: %s (check base_url, OCR_SOFTWARE_BASE_URL or --base-url)" % (self.base_url, e.reason), "network")
                else:
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

    def default_pipeline(self):
        """name@version used when the caller names none: the fastest and cheapest available one (advice.default_pipeline). Say
        to the user which one it was."""
        name = default_pipeline(self.catalog().get("pipelines", []))
        if not name:
            raise OcrError("no pipeline is available right now: try again later or see %s/v1/catalog" % self.base_url, "unavailable")
        return name

    def quote(self, path, pipeline, dpi=None):
        """Dry run: nothing runs, nothing is billed. Returns the page count, price_per_page_millicents, price_millicents (100,000
        millicents = 1 EUR) and warnings. ocr_software.advice.price_sentence(q) turns it into one sentence in euros."""
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

    def ocr(self, path, pipeline, format="text", timeout=DEFAULT_WAIT_TIMEOUT_S, on_accepted=None, **options):
        """Submit, wait, fetch: the result as bytes, also for text, md, json and hocr (use .decode("utf-8") for a string). options go to submit (dpi, client_ref, job_budget, formats, features).
        Raises PagesUnread (with .data, the result anyway) if a page failed or was not run: never silent.
        on_accepted(answer) is called with the answer to the submit; its "warnings" (absent from an older service) say why fewer pages run."""
        if format not in FORMATS:
            raise OcrError("format must be one of %s" % ", ".join(FORMATS), "bad_input")
        source = read_document(path, self.allowed_dir)  # read once: PDF output needs the same bytes again
        accepted = self._submit(source, pipeline, **options)
        if on_accepted:
            on_accepted(accepted)
        job_id = accepted.get("job_id")
        if not job_id:
            raise OcrError("the answer of %s has no job_id: check OCR_SOFTWARE_BASE_URL" % self.base_url, "bad_response")
        done = self.wait(job_id, timeout)
        data = self._result(job_id, format, source)
        problem = unread_message(done)
        if problem:
            raise PagesUnread(problem, done, data)
        return data
