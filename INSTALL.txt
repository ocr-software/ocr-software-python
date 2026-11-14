# ocr-software

Python client and command line for the [ocr-software.com](https://ocr-software.com/) OCR API. MIT, standard library only, Python 3.9 or newer.

## Install

```sh
pipx install ocr-software        # command line only, in its own environment
pip install ocr-software         # client library, inside a venv
uvx ocr-software --version       # run once without installing (uv)
```

First call (list the pipelines, then read one file):

```sh
ocr-software --list-pipelines
ocr-software scan.pdf --pipeline scan-ctc@2 -o scan.txt
```

## Settings

Environment variables; the command line flag wins.

| Variable | Meaning |
|---|---|
| `OCR_SOFTWARE_API_KEY` | API key (starts with `ocr_`). `OCR_KEY` is read too, it is the name used in the API docs; if both are set, `OCR_SOFTWARE_API_KEY` wins. |
| `OCR_SOFTWARE_BASE_URL` | Default `https://api.ocr-software.com`; own server e.g. `http://127.0.0.1:8080` (also `--base-url`). `OCR_BASE_URL` is read too. |
| `OCR_SOFTWARE_ALLOWED_DIR` | Optional: read documents only from below this folder (symlinks and `..` do not escape). |

The key is never printed and never part of an error message. Without `--pipeline` the default is used: the fastest and cheapest available pipeline (no previews). It is named on stderr (`-q` silences that line for bulk runs; `-o NUL` or `-o /dev/null` discards the result); `--list-pipelines` marks it. Warnings of the service (`dpi_ignored`, `job_budget`, `free_limit`, `balance_too_low`) are printed as `warning CODE: text` lines on stderr by `--dry-run` and before a job that uses `--dpi` or `--job-budget` (that job asks once with a dry run first). In Python: `client.ocr(path, client.default_pipeline())`.

## Command line

```sh
ocr-software --list-pipelines                         # available pipelines, one per line: name@version, tab, what it delivers and costs; the default one on stderr (no key needed)
ocr-software scan.pdf --pipeline NAME@VERSION --dry-run   # page count and price in euros, nothing runs, nothing is billed
ocr-software scan.pdf                                 # text to stdout, with the default pipeline (named on stderr)
ocr-software scan.pdf --pipeline NAME@VERSION         # text to stdout
ocr-software scan.pdf -o scan.md --pipeline NAME@VERSION --format md
ocr-software scan.pdf -o scan.pdf.txt --pipeline NAME@VERSION
ocr-software scan.pdf -o searchable.pdf --pipeline NAME@VERSION --format pdf   # searchable PDF, -o is required
ocr-software scan.pdf --pipeline NAME@VERSION --format json > scan.json
ocr-software scan.pdf --pipeline NAME@VERSION --base-url http://10.0.0.5:8080  # your own server
python -m ocr_software ...                            # the same command without the script on PATH
```

Options:
- `--format text` (default) `| md | json | hocr | pdf`
- `--dpi 150|200|300` (PDF render resolution)
- `--timeout SECONDS` (wait for the job, default 3600)
- `--job-budget MILLICENTS` (cap; ignored where billing is off)
- `-q`, `--dry-run`, `--version`

Without `-o` the result goes to stdout as UTF-8 (not for pdf). With `-o` the file appears whole or not at all, an existing file is replaced, and the input file itself is refused as output.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | done, every page read |
| 1 | error from the service or the network (message on stderr with the API error code, for example 401 unauthorized); also an address that does not answer: a typo and an outage look the same, so a script may retry this |
| 2 | wrong call: unreadable or unsupported input, output not writable, no key, malformed address (not http/https) |
| 3 | the job is done but a page failed or did not run, or the job stopped early: the result is written anyway, stderr says what is missing and names the job; treat the file as incomplete |
| 4 | the job did not finish within `--timeout`; it keeps running, stderr names the job |

## Python

```python
from ocr_software import Client, PagesUnread
client = Client()                                    # key and address from the environment
text = client.ocr("scan.pdf", "NAME@VERSION", "md")  # bytes (text.decode("utf-8") for a string); raises PagesUnread (with .data) if a page was not read
pdf = client.ocr("scan.pdf", "NAME@VERSION", "pdf")  # searchable PDF
job = client.submit("scan.pdf", "NAME@VERSION")      # step by step: submit, wait, result
client.wait(job["job_id"]); client.result(job["job_id"], "json")
q = client.quote("scan.pdf", "NAME@VERSION")         # dry run: page count and price, nothing runs
from ocr_software import price_sentence; price_sentence(q)   # the same sentence as --dry-run, in euros
```

**Price:** the API counts money in millicents, 100,000 millicents = 1 EUR (a page of the cheapest pipeline costs 100 = EUR 0.001). Today nothing is charged ("you pay EUR 0.00 today"); the list price is shown for information. Free use has limits: [fair usage](https://ocr-software.com/legal/fair-usage/); for more volume write to info@ocr-software.com.

**Word boxes (json):** `box = [x0, y0, x1, y1]` in pixels of the page image, origin top left; `width`, `height` and `dpi` of the page say which image. In PDF points: pixels * 72 / dpi. Per word, `verified` is true only where a second reader confirmed it (`scan-vlm-ctc@2`); `safe_ok` is always false today. Judge by `confidence` (0 to 1). Every field: [reference](https://ocr-software.com/docs/reference/).

**Retries** follow the API rules: 429, 500, 502, 503, 504 and network errors up to 5 times (waits 1, 2, 4, 8, 16 s or `Retry-After`). An upload the service cuts off (it does that beyond 2 uploads at once per key, instead of answering 429) counts like 429. If no connection can be made at all (wrong address, host down) it gives up after 2 retries and names the address: a few seconds, up to about 10 s on Windows, where a refused port itself takes 2 s per try (connect timeout 10 s). A `Retry-After` over 60 s (`locked_out`) is not waited for. A submit carries an `Idempotency-Key`, so a retry never creates a second job.

Every document is checked locally first: PDF, PNG, JPEG or TIFF by its first bytes, at most 100 MiB.

## Paperless-ngx

Pre-consume script (sketch, not tried against a real Paperless; a non-PDF input keeps its file name but becomes a PDF):

```sh
#!/bin/sh
ocr-software "$DOCUMENT_WORKING_PATH" --pipeline NAME@VERSION --format pdf -o "$DOCUMENT_WORKING_PATH.ocr" && mv "$DOCUMENT_WORKING_PATH.ocr" "$DOCUMENT_WORKING_PATH"
```

with `OCR_SOFTWARE_API_KEY` and `OCR_SOFTWARE_BASE_URL` set in the environment of the Paperless container.

## Tests

Fake local service, no network, no key:

```sh
python -m venv .venv
.venv/bin/pip install pytest                          # Windows: .venv\Scripts\pip install pytest
.venv/bin/python -m pytest -p no:cacheprovider .      # Windows: .venv\Scripts\python -m pytest -p no:cacheprovider .
```
