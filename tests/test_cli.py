import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ENV_NAMES, PDF, err, job_flow

from ocr_software import cli

PACKAGE_DIR = Path(__file__).resolve().parent.parent


def run(argv, capsysbinary):
    try:
        code = cli.main(argv)
    except SystemExit as e:  # argparse: wrong call
        code = e.code
    out = capsysbinary.readouterr()
    return code, out.out, out.err.decode("utf-8", "replace")


def test_text_goes_to_stdout_as_utf8(fake, doc, capsysbinary):
    job_flow(fake, result="Grüße aus München\n".encode("utf-8"))
    code, out, errtext = run([doc, "--pipeline", "p@1"], capsysbinary)
    assert (code, out, errtext) == (0, "Grüße aus München\n".encode("utf-8"), "")
    assert "format=text" in fake.log[-1]["query"]  # default format, like tesseract's plain text


def test_output_file_and_format(fake, doc, tmp_path, capsysbinary):
    job_flow(fake, result=b"## Page 1\n")
    out_file = tmp_path / "out dir" / "a.md"
    out_file.parent.mkdir()
    code, out, _ = run([doc, "-o", str(out_file), "--pipeline", "p@1", "--format", "md"], capsysbinary)
    assert code == 0 and out == b"" and out_file.read_bytes() == b"## Page 1\n"
    assert [p.name for p in out_file.parent.iterdir()] == ["a.md"]  # no .part file left


def test_existing_output_is_replaced_whole(fake, doc, tmp_path, capsysbinary):
    job_flow(fake, result=b"new")
    out_file = tmp_path / "a.txt"
    out_file.write_bytes(b"old and longer")
    assert run([doc, "-o", str(out_file), "--pipeline", "p@1"], capsysbinary)[0] == 0
    assert out_file.read_bytes() == b"new"


def test_pdf_needs_output_file_and_sends_original(fake, doc, tmp_path, capsysbinary):
    code, _, errtext = run([doc, "--pipeline", "p@1", "--format", "pdf"], capsysbinary)
    assert code == 2 and "-o" in errtext and fake.log == []
    job_flow(fake)
    fake.scripted[("POST", "/v1/jobs/j1/result")] = [(200, {}, b"%PDF-out")]
    target = tmp_path / "searchable.pdf"
    assert run([doc, "-o", str(target), "--pipeline", "p@1", "--format", "pdf"], capsysbinary)[0] == 0
    assert target.read_bytes() == b"%PDF-out" and fake.log[-1]["body"] == PDF


def test_without_pipeline_the_default_is_used_and_named_on_stderr(fake, doc, capsysbinary):
    fake.scripted[("GET", "/v1/catalog")] = [(200, {}, CATALOG)]
    job_flow(fake, result=b"hello")
    code, out, errtext = run([doc], capsysbinary)
    assert code == 0 and out == b"hello"  # stdout stays the result
    assert "using a@1" in errtext and "fastest and cheapest" in errtext
    assert [c for c in fake.log if c["method"] == "POST"][0]["query"].count("pipeline=a@1") == 1


def test_without_pipeline_and_nothing_available_fails_clearly_and_sends_nothing(fake, doc, capsysbinary):
    fake.scripted[("GET", "/v1/catalog")] = [(200, {}, {"pipelines": [{"pipeline": "b@1", "available": False, "factor": 1}]})]
    code, _, errtext = run([doc], capsysbinary)
    assert code == 1 and "no pipeline is available" in errtext and all(c["method"] == "GET" for c in fake.log)


def test_output_must_not_be_the_input(fake, doc, capsysbinary):
    code, _, errtext = run([doc, "-o", doc, "--pipeline", "p@1"], capsysbinary)
    assert code == 2 and "overwritten" in errtext and Path(doc).read_bytes() == PDF and fake.log == []


def test_output_in_missing_folder_is_exit_2_without_traceback(fake, doc, tmp_path, capsysbinary):
    job_flow(fake)
    code, _, errtext = run([doc, "-o", str(tmp_path / "nope" / "a.txt"), "--pipeline", "p@1"], capsysbinary)
    assert code == 2 and "does not exist" in errtext and "nope" in errtext and "Traceback" not in errtext and fake.log == []


@pytest.mark.parametrize("job", [{"pages_failed": 1}, {"pages_not_run": 2}, {"stop": "job_budget"}])
def test_unread_pages_exit_3_and_say_so(fake, doc, tmp_path, capsysbinary, job):
    job_flow(fake, job=job, result=b"partial")
    target = tmp_path / "a.txt"
    code, _, errtext = run([doc, "-o", str(target), "--pipeline", "p@1"], capsysbinary)
    assert code == 3 and "not every page was read" in errtext and "incomplete" in errtext and "j1" in errtext
    assert target.read_bytes() == b"partial"  # the result is kept, the exit code and message say it is incomplete


def test_unread_pages_to_stdout_still_exit_3(fake, doc, capsysbinary):
    job_flow(fake, job={"pages_failed": 1}, result=b"partial")
    code, out, errtext = run([doc, "--pipeline", "p@1"], capsysbinary)
    assert (code, out) == (3, b"partial") and "failed" in errtext


def test_service_error_exit_1_without_key_in_message(fake, doc, capsysbinary):
    fake.scripted[("POST", "/v1/jobs")] = [err(401, "unauthorized", "Wrong key")]
    code, out, errtext = run([doc, "--pipeline", "p@1"], capsysbinary)
    assert code == 1 and out == b"" and "unauthorized" in errtext and "ocr_testkey" not in errtext


def test_unknown_pipeline_exit_1(fake, doc, capsysbinary):
    fake.scripted[("POST", "/v1/jobs")] = [err(400, "invalid_request", "Unknown or missing pipeline")]
    assert run([doc, "--pipeline", "nope@1"], capsysbinary)[0] == 1


def test_unknown_pipeline_message_appears_once_and_without_raw_json(fake, doc, capsysbinary):
    problem = "Unknown pipeline. Use name@version, one of: a@1."
    fake.scripted[("POST", "/v1/jobs")] = [(400, {}, {"error": {"code": "invalid_request", "message": problem, "details": [problem]}})]
    code, _, errtext = run([doc, "--pipeline", "nope@1"], capsysbinary)
    assert code == 1 and errtext.count("Unknown pipeline") == 1 and "{" not in errtext


def test_no_key_exit_2(fake, doc, monkeypatch, capsysbinary):
    monkeypatch.delenv("OCR_SOFTWARE_API_KEY")
    code, _, errtext = run([doc, "--pipeline", "p@1"], capsysbinary)
    assert code == 2 and "OCR_SOFTWARE_API_KEY" in errtext and fake.log == []


def test_bad_input_file_exit_2(fake, tmp_path, capsysbinary):
    text = tmp_path / "notes.txt"
    text.write_bytes(b"hello")
    for path in (str(text), str(tmp_path / "missing.pdf"), str(tmp_path)):
        assert run([path, "--pipeline", "p@1"], capsysbinary)[0] == 2
    assert fake.log == []


def test_timeout_exit_4_names_the_job(fake, doc, capsysbinary):
    fake.scripted[("POST", "/v1/jobs")] = [(202, {}, {"job_id": "j1"})]
    fake.scripted[("GET", "/v1/jobs/j1")] = [(200, {}, {"state": "running"})]
    code, _, errtext = run([doc, "--pipeline", "p@1", "--timeout", "0"], capsysbinary)
    assert code == 4 and "j1" in errtext


def test_network_error_exit_1(doc, refused, monkeypatch, capsysbinary):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OCR_SOFTWARE_API_KEY", "ocr_k")
    code, _, errtext = run([doc, "--pipeline", "p@1", "--base-url", "http://127.0.0.1:1"], capsysbinary)
    assert code == 1 and "cannot connect" in errtext


def test_unreachable_address_is_exit_1_a_malformed_one_is_exit_2(doc, refused, monkeypatch, capsysbinary):
    """A typo and an outage look the same from here, so a script may retry exit 1; only a malformed address is a wrong call."""
    monkeypatch.setenv("OCR_SOFTWARE_API_KEY", "ocr_k")
    assert run([doc, "--pipeline", "p@1", "--base-url", "http://nohost.invalid"], capsysbinary)[0] == 1
    assert len(refused) == 3  # 1 try and 2 retries, then it names the address
    assert run([doc, "--pipeline", "p@1", "--base-url", "nohost.invalid"], capsysbinary)[0] == 2


def test_base_url_flag_beats_environment(fake, doc, monkeypatch, capsysbinary):
    job_flow(fake, result=b"ok")
    service = os.environ["OCR_SOFTWARE_BASE_URL"]
    monkeypatch.setenv("OCR_SOFTWARE_BASE_URL", "http://127.0.0.1:1")  # dead address in the environment
    assert run([doc, "--pipeline", "p@1", "--base-url", service], capsysbinary)[1] == b"ok"
    code, _, errtext = run([doc, "--pipeline", "p@1", "--base-url", "ftp://x"], capsysbinary)
    assert code == 2 and "http" in errtext


CATALOG = {"pipelines": [
    {"pipeline": "a@1", "description": "fast", "available": True, "factor": 1, "level": "word", "features": ["word_boxes"],
     "switch": [{"when": "hard_pages", "pipeline": "c@1"}]},
    {"pipeline": "b@1", "description": "broken", "available": False, "factor": 0.5},
    {"pipeline": "c@1", "description": "slow", "available": True, "factor": 4, "level": "word", "autoregressive": True}]}


def test_list_pipelines_shows_only_available_with_price_and_default_and_needs_no_key(fake, monkeypatch, capsys):
    monkeypatch.delenv("OCR_SOFTWARE_API_KEY")
    fake.scripted[("GET", "/v1/catalog")] = [(200, {}, CATALOG)]
    assert cli.main(["--list-pipelines"]) == 0
    out = capsys.readouterr()
    assert out.out.splitlines() == [
        "a@1	word boxes; price EUR 0.001 per page; the default (fastest and cheapest); for hard pages (poor scans, doubtful words) use c@1",
        "c@1	word boxes, slower; price EUR 0.004 per page"]
    assert "the default is a@1" in out.err and fake.log[0]["auth"] is None


def test_dry_run_without_pipeline_names_the_default_in_the_sentence(fake, doc, capsys):
    fake.scripted[("GET", "/v1/catalog")] = [(200, {}, CATALOG)]
    fake.scripted[("POST", "/v1/jobs")] = [(200, {}, {"dry_run": True, "pages": 1, "price_per_page_millicents": 100,
                                                      "price_millicents": 100, "max_charge_millicents": 0, "pages_covered": 1})]
    assert cli.main([doc, "--dry-run"]) == 0
    assert capsys.readouterr().out.startswith("1 page with a@1:")


def test_dry_run_prints_the_price_in_euros_and_runs_nothing(fake, doc, capsys):
    fake.scripted[("POST", "/v1/jobs")] = [(200, {}, {"dry_run": True, "pages": 3, "price_per_page_millicents": 100,
                                                      "price_millicents": 300, "max_charge_millicents": 0, "pages_covered": 3})]
    assert cli.main([doc, "--pipeline", "a@1", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("3 pages with a@1: you pay EUR 0.00 today. List price EUR 0.003 in total (EUR 0.001 per page)")
    assert "fair-usage" in out and "millicents" not in out and "dry_run=1" in fake.log[0]["query"] and len(fake.log) == 1


def test_python_dash_m_end_to_end(fake, doc, tmp_path):
    """The real command in a real process: wiring, base URL flag, exit code, bytes on stdout."""
    job_flow(fake, result="München".encode("utf-8"))
    env = {k: v for k, v in os.environ.items() if k not in ENV_NAMES}
    env.update(OCR_SOFTWARE_API_KEY="ocr_testkey", PYTHONPATH=str(PACKAGE_DIR))
    base = os.environ["OCR_SOFTWARE_BASE_URL"]
    done = subprocess.run([sys.executable, "-m", "ocr_software", doc, "--pipeline", "p@1", "--base-url", base],
                          env=env, capture_output=True, timeout=60)
    assert done.returncode == 0 and done.stdout == "München".encode("utf-8") and done.stderr == b""
    version = subprocess.run([sys.executable, "-m", "ocr_software", "--version"], env=env, capture_output=True, timeout=60)
    assert version.returncode == 0 and version.stdout.startswith(b"ocr-software ")


def test_missing_input_with_existing_output_is_exit_2_with_a_clear_message(fake, tmp_path, capsysbinary):
    out = tmp_path / "out.txt"
    out.write_bytes(b"old")
    code, _, err_text = run([str(tmp_path / "missing.pdf"), "--pipeline", "p@1", "-o", str(out)], capsysbinary)
    assert code == 2 and "not a file" in err_text and out.read_bytes() == b"old"


def test_ctrl_c_ends_without_a_stack_trace(fake, doc, monkeypatch, capsysbinary):
    """Review 13.11.2026: Strg+C printed a full traceback."""
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli.Client, "ocr", interrupted)
    code, out, errtext = run([doc, "--pipeline", "p@1"], capsysbinary)
    assert code == 130 and out == b"" and "interrupted" in errtext and "Traceback" not in errtext


def test_quiet_drops_the_default_pipeline_line_only(fake, doc, capsysbinary):
    fake.scripted[("GET", "/v1/catalog")] = [(200, {}, {"pipelines": [{"pipeline": "p@1", "available": True, "price_per_page_millicents": 100}]})]
    job_flow(fake, result=b"x")
    code, _, errtext = run([doc, "-q"], capsysbinary)
    assert code == 0 and errtext == ""
    job_flow(fake, result=b"x")
    code, _, errtext = run([doc], capsysbinary)
    assert code == 0 and "no --pipeline given, using p@1" in errtext


def test_output_to_the_null_device_is_accepted_and_discards(fake, doc, capsysbinary):
    job_flow(fake, result=b"gone")
    code, out, errtext = run([doc, "--pipeline", "p@1", "-o", os.devnull], capsysbinary)
    assert (code, out, errtext) == (0, b"", "")


@pytest.mark.parametrize("target", [os.devnull, "/dev/null"])
def test_both_null_device_spellings_discard_on_every_system(fake, doc, capsysbinary, target):
    job_flow(fake, result=b"gone")
    code, out, errtext = run([doc, "--pipeline", "p@1", "-o", target], capsysbinary)
    assert (code, out, errtext) == (0, b"", "")


DPI_WARNING = {"code": "dpi_ignored", "message": "dpi applies to the pages of a PDF."}
DRY_WITH_WARNING = {"dry_run": True, "pages": 1, "price_per_page_millicents": 100, "price_millicents": 100, "max_charge_millicents": 0,
                    "pages_covered": 1, "warnings": [DPI_WARNING]}


def test_dry_run_shows_every_warning_as_its_own_stderr_line(fake, doc, capsysbinary):
    fake.scripted[("POST", "/v1/jobs")] = [(200, {}, dict(DRY_WITH_WARNING, warnings=[DPI_WARNING, {"code": "job_budget", "message": "covers 1 of 2"}]))]
    code, out, errtext = run([doc, "--pipeline", "a@1", "--dry-run", "--dpi", "300"], capsysbinary)
    assert code == 0 and b"1 page with a@1" in out
    assert errtext.splitlines() == ["ocr-software: warning dpi_ignored: dpi applies to the pages of a PDF.",
                                    "ocr-software: warning job_budget: covers 1 of 2"]


def test_a_real_job_shows_the_warnings_of_the_answer_in_one_upload(fake, doc, capsysbinary):
    job_flow(fake, result=b"text")
    fake.scripted[("POST", "/v1/jobs")] = [(202, {}, {"job_id": "j1", "state": "running", "warnings": [DPI_WARNING]})]
    code, out, errtext = run([doc, "--pipeline", "a@1", "--dpi", "300", "--job-budget", "1"], capsysbinary)
    assert code == 0 and out == b"text" and errtext == "ocr-software: warning dpi_ignored: dpi applies to the pages of a PDF.\n"
    posts = [c for c in fake.log if c["method"] == "POST" and "/result" not in c["path"]]
    assert len(posts) == 1 and "dry_run" not in posts[0]["query"]


def test_an_older_service_without_warnings_breaks_nothing(fake, doc, capsysbinary):
    job_flow(fake, result=b"text")
    assert run([doc, "--pipeline", "a@1", "--dpi", "300"], capsysbinary) == (0, b"text", "")


def test_a_plain_real_job_stays_one_upload(fake, doc, capsysbinary):
    job_flow(fake)
    run([doc, "--pipeline", "a@1"], capsysbinary)
    assert sum(1 for c in fake.log if c["method"] == "POST" and "/result" not in c["path"]) == 1
