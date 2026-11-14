import os
import re
import threading
from http.server import ThreadingHTTPServer

import pytest
from conftest import PDF, Fake, err, job_flow

from ocr_software import Client, OcrError, PagesUnread, client as client_module


def test_catalog_needs_no_key(fake, monkeypatch):
    monkeypatch.delenv("OCR_SOFTWARE_API_KEY")
    fake.scripted[("GET", "/v1/catalog")] = [(200, {}, {"pipelines": [{"pipeline": "scan-ctc@2"}]})]
    assert Client().catalog()["pipelines"][0]["pipeline"] == "scan-ctc@2"
    assert fake.log[0]["auth"] is None


def test_quote_is_dry_run(fake, doc):
    fake.scripted[("POST", "/v1/jobs")] = [(200, {}, {"dry_run": True, "pages": 3})]
    assert Client().quote(doc, "scan-ctc@2", dpi=200)["pages"] == 3
    call = fake.log[0]
    assert "dry_run=1" in call["query"] and "pipeline=scan-ctc@2" in call["query"] and "dpi=200" in call["query"]
    assert call["auth"] == "Bearer ocr_testkey" and call["body"] == PDF


def test_submit_sends_query_and_idempotency_key_but_no_wait(fake, doc):
    fake.scripted[("POST", "/v1/jobs")] = [(202, {}, {"job_id": "j1", "state": "running"})]
    assert Client().submit(doc, "p@1", formats="json,md", job_budget=5000, client_ref="inv-7")["job_id"] == "j1"
    call = fake.log[0]
    assert "formats=json,md" in call["query"] and "job_budget=5000" in call["query"] and "client_ref=inv-7" in call["query"]
    assert "wait" not in call["query"] and "dry_run" not in call["query"] and call["idem"]


def test_two_submits_use_two_idempotency_keys(fake, doc):
    fake.scripted[("POST", "/v1/jobs")] = [(202, {}, {"job_id": "j"})]
    Client().submit(doc, "p@1")
    Client().submit(doc, "p@1")
    assert fake.log[0]["idem"] != fake.log[1]["idem"]


def test_pipeline_is_required_and_has_no_default(fake, doc):
    for empty in (None, ""):
        with pytest.raises(OcrError, match="pipeline is required"):
            Client().submit(doc, empty)
    assert fake.log == []


def test_missing_key_sends_nothing(fake, doc, monkeypatch):
    monkeypatch.delenv("OCR_SOFTWARE_API_KEY")
    with pytest.raises(OcrError, match="OCR_SOFTWARE_API_KEY") as e:
        Client().quote(doc, "p@1")
    assert e.value.code == "no_key" and fake.log == []


def test_key_with_newline_is_refused_and_not_echoed(fake, doc):
    with pytest.raises(OcrError) as e:
        Client(api_key="ocr_secret\nX-Evil: 1").quote(doc, "p@1")
    assert "secret" not in str(e.value) and fake.log == []


def test_key_names_new_wins_old_still_read(fake, doc, monkeypatch):
    fake.scripted[("POST", "/v1/jobs")] = [(200, {}, {"pages": 1})]
    monkeypatch.setenv("OCR_KEY", "ocr_old")
    Client().quote(doc, "p@1")
    monkeypatch.delenv("OCR_SOFTWARE_API_KEY")
    Client().quote(doc, "p@1")
    assert [c["auth"] for c in fake.log] == ["Bearer ocr_testkey", "Bearer ocr_old"]


def test_base_url_names_and_check(fake, monkeypatch):
    assert Client().base_url.startswith("http://127.0.0.1")
    monkeypatch.delenv("OCR_SOFTWARE_BASE_URL")
    assert Client().base_url == "https://api.ocr-software.com"
    monkeypatch.setenv("OCR_BASE_URL", "http://10.0.0.5:8080/")
    assert Client().base_url == "http://10.0.0.5:8080"
    assert Client(base_url="http://192.168.1.2:8080").base_url == "http://192.168.1.2:8080"
    for bad in ("file:///etc/passwd", "ftp://x", "api.ocr-software.com"):
        with pytest.raises(OcrError, match="http"):
            Client(base_url=bad)


def test_repr_hides_key():
    assert "ocr_" not in repr(Client(api_key="ocr_secret"))


def test_unauthorized_not_retried_and_key_not_leaked(fake, doc):
    fake.scripted[("POST", "/v1/jobs")] = [err(401, "unauthorized", "Wrong key")]
    with pytest.raises(OcrError) as e:
        Client().quote(doc, "p@1")
    assert "ocr_testkey" not in str(e.value) and e.value.code == "unauthorized" and e.value.http_status == 401
    assert len(fake.log) == 1


def test_retry_after_over_60_is_not_retried(fake, doc):
    fake.scripted[("POST", "/v1/jobs")] = [err(429, "locked_out", "locked", {"Retry-After": "900"})]
    with pytest.raises(OcrError, match="not retried") as e:
        Client().quote(doc, "p@1")
    assert e.value.code == "locked_out" and len(fake.log) == 1


def test_short_retry_then_success_and_same_idempotency_key(fake, doc):
    fake.scripted[("POST", "/v1/jobs")] = [err(503, "busy"), err(429, "rate_limited", headers={"Retry-After": "0"}),
                                           (202, {}, {"job_id": "j2"})]
    assert Client().submit(doc, "p@1")["job_id"] == "j2"
    assert len(fake.log) == 3 and len({c["idem"] for c in fake.log}) == 1


def test_retry_after_is_waited(fake, monkeypatch):
    waits = []
    monkeypatch.setattr(client_module.time, "sleep", waits.append)
    fake.scripted[("GET", "/v1/jobs/j1")] = [err(429, "rate_limited", headers={"Retry-After": "7"}), (200, {}, {"state": "running"})]
    Client().status("j1")
    assert waits == [7]


def test_backoff_doubles_without_retry_after(fake, monkeypatch):
    monkeypatch.setattr(client_module, "BACKOFF_BASE_S", 1.0)
    waits = []
    monkeypatch.setattr(client_module.time, "sleep", waits.append)
    fake.scripted[("GET", "/v1/jobs/j1")] = [err(503, "busy")]
    with pytest.raises(OcrError, match="503 busy"):
        Client().status("j1")
    assert waits == [1, 2, 4, 8, 16] and len(fake.log) == 6


def test_500_is_retried_other_4xx_never(fake):
    fake.scripted[("GET", "/v1/jobs/j1")] = [err(500, "internal_error"), (200, {}, {"state": "running"})]
    assert Client().status("j1")["state"] == "running" and len(fake.log) == 2
    fake.log.clear()
    for status, code in ((400, "invalid_request"), (402, "payment_required"), (404, "not_found"), (409, "idempotency_conflict"),
                         (422, "budget_too_low"), (413, "too_large")):
        fake.scripted[("GET", "/v1/jobs/j2")] = [err(status, code)]
        with pytest.raises(OcrError) as e:
            Client().status("j2")
        assert e.value.code == code
    assert len(fake.log) == 6


def test_capability_missing_shows_pipelines(fake, doc):
    fake.scripted[("POST", "/v1/jobs")] = [(422, {}, {"error": {"code": "capability_missing", "message": "x", "missing": ["hocr"],
                                                       "pipelines": ["a@1"]}})]
    with pytest.raises(OcrError, match="a@1"):
        Client().submit(doc, "p@1", formats="hocr")


def test_no_connection_fails_fast_with_a_clear_message_without_key(refused, doc):
    """Customer test 12.11.2026: a wrong base URL was silent for 43 s (5 retries). Now 2 retries and the address in the message."""
    with pytest.raises(OcrError, match="cannot connect to http://127.0.0.1:1") as e:
        Client(api_key="ocr_secretkey", base_url="http://127.0.0.1:1").quote(doc, "p@1")
    assert "ocr_secretkey" not in str(e.value) and "base" in str(e.value).lower() and e.value.code == "network"
    assert len(refused) == client_module.CONNECT_RETRIES + 1


def test_connect_uses_the_short_timeout_then_the_read_timeout(fake, monkeypatch):
    import http.client
    seen = []
    real_connect = http.client.HTTPConnection.connect

    def spy(self):
        seen.append(self.timeout)
        return real_connect(self)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", spy)
    port = int(os.environ["OCR_SOFTWARE_BASE_URL"].rsplit(":", 1)[1])
    conn = client_module._HTTPConnection("127.0.0.1", port, timeout=77)
    conn.connect()
    try:
        assert seen == [client_module.CONNECT_TIMEOUT_S] and conn.timeout == 77 and conn.sock.gettimeout() == 77
    finally:
        conn.close()


def test_redirect_is_not_followed_so_the_key_stays_home(fake, doc):
    other = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=lambda: other.serve_forever(0.02), daemon=True).start()
    target = "http://127.0.0.1:%d/v1/jobs" % other.server_address[1]
    fake.scripted[("POST", "/v1/jobs")] = [(307, {"Location": target}, b"")]
    try:
        with pytest.raises(OcrError, match="307"):
            Client().quote(doc, "p@1")
        assert [c["path"] for c in fake.log] == ["/v1/jobs"]  # one call to the first server, none to the second
    finally:
        other.shutdown()


def test_status_waits_at_most_60_and_checks_the_id(fake):
    fake.scripted[("GET", "/v1/jobs/j1")] = [(200, {}, {"state": "done"})]
    assert Client().status("j1", wait=99)["state"] == "done" and "wait=60" in fake.log[0]["query"]
    for bad in ("../x", "a?b", "a/b", "x" * 101, "", None):
        with pytest.raises(OcrError, match="invalid job_id"):
            Client().status(bad)
    assert len(fake.log) == 1


def test_wait_polls_until_done(fake):
    fake.scripted[("GET", "/v1/jobs/j1")] = [(200, {}, {"state": "running"}), (200, {}, {"state": "running"}),
                                            (200, {}, {"state": "done", "pages_done": 2})]
    assert Client().wait("j1")["pages_done"] == 2 and len(fake.log) == 3
    assert all("wait=30" in c["query"] for c in fake.log)


def test_wait_times_out(fake):
    fake.scripted[("GET", "/v1/jobs/j1")] = [(200, {}, {"state": "running"})]
    with pytest.raises(OcrError, match="still running") as e:
        Client().wait("j1", timeout=0)
    assert e.value.code == "timeout"


def test_result_formats(fake):
    fake.scripted[("GET", "/v1/jobs/j1/result")] = [(200, {}, b"## Page 1\nhello")]
    for fmt in ("json", "text", "md", "hocr"):
        assert Client().result("j1", fmt) == b"## Page 1\nhello"
        assert "format=%s" % fmt in fake.log[-1]["query"]
    with pytest.raises(OcrError, match="format must be"):
        Client().result("j1", "docx")
    for bad in ("../x", "a/b", ""):
        with pytest.raises(OcrError, match="invalid job_id"):
            Client().result(bad)


def test_result_not_ready(fake):
    fake.scripted[("GET", "/v1/jobs/j1/result")] = [err(409, "not_ready")]
    with pytest.raises(OcrError, match="409 not_ready"):
        Client().result("j1")


def test_pdf_is_a_post_with_the_original(fake, doc):
    fake.scripted[("POST", "/v1/jobs/j1/result")] = [(200, {"X-Pages-Unread": ""}, b"%PDF-out")]
    assert Client().result("j1", "pdf", source_path=doc) == b"%PDF-out"
    call = fake.log[0]
    assert call["method"] == "POST" and "format=pdf" in call["query"] and call["body"] == PDF
    with pytest.raises(OcrError, match="original document"):
        Client().result("j1", "pdf")
    assert len(fake.log) == 1


def test_document_must_look_like_pdf_png_jpeg_tiff(fake, tmp_path):
    fake.scripted[("POST", "/v1/jobs")] = [(200, {}, {"pages": 1})]
    for head in (b"%PDF-1.7", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff\xe0", b"II*\x00", b"MM\x00*"):
        ok = tmp_path / "ok.bin"
        ok.write_bytes(head + b"x")
        assert Client().quote(str(ok), "p@1")["pages"] == 1
    bad = tmp_path / "secrets.txt"
    bad.write_bytes(b"password=1")
    with pytest.raises(OcrError, match="not a PDF, PNG, JPEG or TIFF"):
        Client().quote(str(bad), "p@1")
    assert len(fake.log) == 5


def test_pdf_with_leading_junk_is_accepted(fake, tmp_path):
    p = tmp_path / "j.pdf"
    p.write_bytes(b"\r\n junk\n" + PDF)
    fake.scripted[("POST", "/v1/jobs")] = [(200, {}, {"pages": 1})]
    assert Client().quote(str(p), "p@1")["pages"] == 1


def test_document_size_limit(fake, doc, monkeypatch):
    monkeypatch.setattr(client_module, "MAX_DOCUMENT_BYTES", 5)
    with pytest.raises(OcrError, match="larger than"):
        Client().quote(doc, "p@1")
    assert fake.log == []


def test_not_a_file_and_missing_file(fake, tmp_path):
    for path in (str(tmp_path), str(tmp_path / "nope.pdf")):
        with pytest.raises(OcrError, match="not a file"):
            Client().quote(path, "p@1")
    assert fake.log == []


def test_allowed_dir(fake, tmp_path, monkeypatch):
    inside = tmp_path / "in"
    inside.mkdir()
    (inside / "a.pdf").write_bytes(PDF)
    (tmp_path / "b.pdf").write_bytes(PDF)
    fake.scripted[("POST", "/v1/jobs")] = [(200, {}, {"pages": 1})]
    client = Client(allowed_dir=str(inside))
    assert client.quote(str(inside / "a.pdf"), "p@1")["pages"] == 1
    for outside in (tmp_path / "b.pdf", inside / ".." / "b.pdf"):
        with pytest.raises(OcrError, match="outside the allowed folder"):
            client.quote(str(outside), "p@1")
    monkeypatch.setenv("OCR_SOFTWARE_ALLOWED_DIR", str(inside))
    with pytest.raises(OcrError, match="outside"):
        Client().quote(str(tmp_path / "b.pdf"), "p@1")
    assert len(fake.log) == 1


def test_allowed_dir_symlink_escape(fake, tmp_path):
    inside = tmp_path / "in"
    inside.mkdir()
    (tmp_path / "b.pdf").write_bytes(PDF)
    try:
        (inside / "link.pdf").symlink_to(tmp_path / "b.pdf")
    except OSError:
        pytest.skip("no symlinks here")
    with pytest.raises(OcrError, match="outside"):
        Client(allowed_dir=str(inside)).quote(str(inside / "link.pdf"), "p@1")
    assert fake.log == []


def test_allowed_dir_on_another_drive_is_outside(fake, doc, monkeypatch):
    def other_drive(paths):
        raise ValueError("Paths don't have the same drive")  # what ntpath.commonpath raises
    monkeypatch.setattr(os.path, "commonpath", other_drive)
    with pytest.raises(OcrError, match="outside"):
        Client(allowed_dir="D:\\scans").quote(doc, "p@1")


def test_names_with_spaces_and_umlauts(fake, tmp_path):
    folder = tmp_path / "Scans M\u00e4rz 2026"
    folder.mkdir()
    p = folder / "Rechnung M\u00fcller (1).pdf"
    p.write_bytes(PDF)
    fake.scripted[("POST", "/v1/jobs")] = [(200, {}, {"pages": 1})]
    assert Client(allowed_dir=str(tmp_path)).quote(str(p), "p@1")["pages"] == 1
    assert fake.log[0]["body"] == PDF


@pytest.mark.skipif(os.name != "nt", reason="Windows path syntax")
def test_windows_path_with_mixed_separators_and_other_case(fake, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.pdf").write_bytes(PDF)
    fake.scripted[("POST", "/v1/jobs")] = [(200, {}, {"pages": 1})]
    mixed = str(tmp_path).replace("\\", "/") + "\\sub/a.pdf"
    assert Client(allowed_dir=str(tmp_path).upper()).quote(mixed, "p@1")["pages"] == 1


def test_response_size_limit(fake, monkeypatch):
    monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", 4)
    fake.scripted[("GET", "/v1/jobs/j1/result")] = [(200, {}, b"12345")]
    with pytest.raises(OcrError, match="response larger than"):
        Client().result("j1")


def test_ocr_submit_wait_fetch(fake, doc):
    job_flow(fake, result=b"hello")
    assert Client().ocr(doc, "p@1", "md", dpi=300) == b"hello"
    assert [c["path"] for c in fake.log] == ["/v1/jobs", "/v1/jobs/j1", "/v1/jobs/j1/result"]
    assert "dpi=300" in fake.log[0]["query"] and "format=md" in fake.log[2]["query"]


def test_ocr_pdf_sends_the_original_again(fake, doc):
    job_flow(fake)
    fake.scripted[("POST", "/v1/jobs/j1/result")] = [(200, {}, b"%PDF-out")]
    assert Client().ocr(doc, "p@1", "pdf") == b"%PDF-out"
    assert fake.log[-1]["method"] == "POST" and fake.log[-1]["body"] == PDF


@pytest.mark.parametrize("job, text", [({"pages_failed": 2}, "2 page(s) failed"), ({"pages_not_run": 1}, "1 page(s) not run"),
                                        ({"stop": "job_budget"}, "stopped: job_budget")])
def test_ocr_never_hides_unread_pages(fake, doc, job, text):
    job_flow(fake, job=job, result=b"partial")
    with pytest.raises(PagesUnread, match=re.escape(text)) as e:
        Client().ocr(doc, "p@1")
    assert e.value.data == b"partial" and e.value.job["job_id"] == "j1" and "j1" in str(e.value)


def test_ocr_rejects_bad_format_before_any_call(fake, doc):
    with pytest.raises(OcrError, match="format must be"):
        Client().ocr(doc, "p@1", "docx")
    assert fake.log == []


@pytest.mark.parametrize("exc", [__import__("socket").timeout("timed out"), __import__("http.client").client.IncompleteRead(b"x")])
def test_read_timeout_and_cut_body_are_retried_then_reported(monkeypatch, doc, exc):
    attempts = []

    def broken(*args, **kwargs):
        attempts.append(1)
        raise exc
    monkeypatch.setattr(client_module._OPENER, "open", broken)
    monkeypatch.setattr(client_module, "BACKOFF_BASE_S", 0)
    with pytest.raises(OcrError) as e:
        Client(api_key="ocr_secretkey", base_url="http://127.0.0.1:1").quote(doc, "p@1")
    assert e.value.code == "network" and len(attempts) == 6 and "ocr_secretkey" not in str(e.value)


def test_key_with_trailing_newline_is_accepted_and_never_echoed(fake, doc):
    """Review 13.11.2026: a key read with $(cat key) ended in a newline; "$" in the key check let it pass and http.client raised a
    ValueError with the key in its text (stack trace)."""
    job_flow(fake)
    Client(api_key="ocr_testkey\n", base_url=" " + client_module.os.environ["OCR_SOFTWARE_BASE_URL"] + "/ ").ocr(doc, "p@1")
    assert fake.log[0]["auth"] == "Bearer ocr_testkey"
    with pytest.raises(OcrError, match="characters") as e:
        Client(api_key="ocr_te st").quote(doc, "p@1")
    assert "ocr_te" not in str(e.value)


@pytest.mark.parametrize("body", [b"<html>login required</html>", b"[]", b"{}"])
def test_answer_that_is_not_the_api_is_a_clear_error_not_a_stack_trace(fake, doc, body):
    """Review 13.11.2026: a proxy answering 200 with a web page made json.loads raise a JSONDecodeError trace."""
    fake.scripted[("POST", "/v1/jobs")] = [(200, {}, body)]
    with pytest.raises(OcrError, match="OCR_SOFTWARE_BASE_URL") as e:
        Client().ocr(doc, "p@1")
    assert e.value.code == "bad_response"


def test_empty_file_is_named_empty(fake, tmp_path):
    p = tmp_path / "leer.pdf"
    p.write_bytes(b"")
    with pytest.raises(OcrError, match="file is empty") as e:
        Client().quote(str(p), "p@1")
    assert e.value.code == "bad_input" and fake.log == []


@pytest.mark.parametrize("status,code", [(502, "bad_gateway"), (504, "gateway_timeout")])
def test_502_and_504_are_retried_like_503(fake, status, code):
    """The production answered 502 while the service restarted (13.11.2026)."""
    fake.scripted[("GET", "/v1/jobs/j1")] = [err(status, code), (200, {}, {"state": "running"})]
    assert Client().status("j1")["state"] == "running" and len(fake.log) == 2
    fake.log.clear()
    fake.scripted[("GET", "/v1/jobs/j1")] = [err(status, code)]
    with pytest.raises(OcrError) as e:
        Client().status("j1")
    assert e.value.code == code and len(fake.log) == 6


def test_upload_cut_by_the_service_is_retried_like_429_with_the_same_idempotency_key(fake, doc):
    """Measured 13.11.2026: beyond 2 uploads at once the service cuts the upload instead of answering 429."""
    fake.scripted[("POST", "/v1/jobs")] = [(0, {}, b""), (0, {}, b""), (202, {}, {"job_id": "j2"})]
    assert Client().submit(doc, "p@1")["job_id"] == "j2"
    assert len(fake.log) == 3 and len({c["idem"] for c in fake.log}) == 1


@pytest.mark.parametrize("reason", [ConnectionResetError(10054, "reset"), ConnectionAbortedError(10053, "aborted"), BrokenPipeError("pipe")])
def test_cut_while_sending_is_retried_five_times_and_says_what_it_is(monkeypatch, doc, reason):
    """urllib wraps a cut during the upload in URLError: that is not 'no connection' (2 tries) but 'busy' (5 retries)."""
    import urllib.error
    attempts = []

    def cut(*args, **kwargs):
        attempts.append(1)
        raise urllib.error.URLError(reason)
    monkeypatch.setattr(client_module._OPENER, "open", cut)
    monkeypatch.setattr(client_module, "BACKOFF_BASE_S", 0)
    with pytest.raises(OcrError, match="cut the connection") as e:
        Client(api_key="ocr_secretkey", base_url="http://127.0.0.1:1").quote(doc, "p@1")
    assert e.value.code == "network" and len(attempts) == 6 and "ocr_secretkey" not in str(e.value)


def test_unknown_pipeline_error_shows_the_problem_once(fake, doc):
    problem = "Unknown pipeline. Use name@version, one of: a@1, b@1."
    fake.scripted[("POST", "/v1/jobs")] = [(400, {}, {"error": {"code": "invalid_request", "message": problem, "details": [problem]}})]
    with pytest.raises(OcrError) as e:
        Client().submit(doc, "x@1")
    assert str(e.value).count("Unknown pipeline") == 1 and "details" not in str(e.value) and "{" not in str(e.value)


def test_details_not_in_the_message_and_capability_lists_are_still_shown(fake, doc):
    fake.scripted[("POST", "/v1/jobs")] = [(400, {}, {"error": {"code": "invalid_request", "message": "bad", "details": ["other"]}})]
    with pytest.raises(OcrError, match="other"):
        Client().submit(doc, "x@1")
    fake.scripted[("POST", "/v1/jobs")] = [(422, {}, {"error": {"code": "capability_missing", "message": "m", "pipelines": ["a@1"]}})]
    with pytest.raises(OcrError, match="a@1"):
        Client().submit(doc, "x@1")


BIG_DOC = b"%PDF-1.4 " + b"x" * (200 * 1024)  # beyond what the proxy discards after an early answer (256 KiB with headers)


@pytest.fixture
def big_doc(tmp_path):
    p = tmp_path / "big.pdf"
    p.write_bytes(BIG_DOC)
    return str(p)


@pytest.fixture
def reset_on_upload(monkeypatch):
    """What a client sees when the service answers early and closes while the upload is still going out: the first request
    with a body fails while sending (Windows 10053), the answer is lost. Every other request really goes to the fake."""
    import urllib.error
    real, state = client_module._OPENER.open, {"cut": 0}

    def open_(req, *args, **kwargs):
        if req.data and len(req.data) > 1000 and not state["cut"]:
            state["cut"] = 1
            raise urllib.error.URLError(ConnectionAbortedError(10053, "aborted"))
        return real(req, *args, **kwargs)
    monkeypatch.setattr(client_module._OPENER, "open", open_)


def test_big_upload_cut_after_a_refusal_shows_the_refusal_not_overload(fake, big_doc, reset_on_upload):
    """13.11.2026 (LangChain test): a wrong key or pipeline with a 700 KB file came back after 33 s as 'cut 6 times, more than
    2 uploads?' because the answer was lost in the reset. The client asks with an empty body and raises that answer at once."""
    fake.scripted[("POST", "/v1/jobs")] = [err(400, "invalid_request", "Unknown pipeline.")]
    with pytest.raises(OcrError, match="Unknown pipeline") as e:
        Client().submit(big_doc, "foo@1")
    assert e.value.code == "invalid_request" and len(fake.log) == 1 and fake.log[0]["body"] == b""
    assert "ocr_testkey" not in str(e.value)


def test_big_upload_cut_while_key_and_pipeline_are_fine_is_retried_with_the_same_idempotency_key(fake, big_doc, reset_on_upload):
    fake.scripted[("POST", "/v1/jobs")] = [err(400, "empty_document", "document is empty"), (202, {}, {"job_id": "j3"})]
    assert Client().submit(big_doc, "p@1")["job_id"] == "j3"
    assert [c["body"] == b"" for c in fake.log] == [True, False] and fake.log[0]["idem"] is None and fake.log[1]["idem"]


def test_big_upload_cut_with_a_locked_out_key_raises_locked_out(fake, big_doc, reset_on_upload):
    fake.scripted[("POST", "/v1/jobs")] = [err(429, "locked_out", "locked", {"Retry-After": "705"})]
    with pytest.raises(OcrError, match="locked_out") as e:
        Client().submit(big_doc, "p@1")
    assert e.value.http_status == 429 and len(fake.log) == 1


def test_split_pages_text_and_markdown():
    from ocr_software import split_pages
    assert split_pages("a\nb\n\fc\n\f\nd\n") == [(1, "a\nb\n"), (2, "c\n"), (3, "\nd\n")]
    assert split_pages("", "text") == [(1, "")]
    md = "## Page 1\n\nfirst\n\n## Page 2\n\n## Page 7\n\nnot a page\n\n## Page 3\n\nlast\n"
    assert split_pages(md, "md") == [(1, "first\n"), (2, "## Page 7\n\nnot a page\n"), (3, "last\n")]
    assert split_pages("no headings", "md") == [(1, "no headings")]


def test_both_names_set_the_ocr_software_name_wins_for_key_url_and_folder(monkeypatch):
    for short, long_, bad, good in (("OCR_KEY", "OCR_SOFTWARE_API_KEY", "ocr_bogus", "ocr_good"),
                                    ("OCR_BASE_URL", "OCR_SOFTWARE_BASE_URL", "http://bad:1", "http://good:1"),
                                    ("OCR_ALLOWED_DIR", "OCR_SOFTWARE_ALLOWED_DIR", "/bad", "/good")):
        monkeypatch.setenv(short, bad)
        monkeypatch.setenv(long_, good)
    c = Client()
    assert (c._key, c.base_url, c.allowed_dir) == ("ocr_good", "http://good:1", "/good")
