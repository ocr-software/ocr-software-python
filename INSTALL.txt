ocr-software: Python client and command line for the ocr-software.com OCR API (MIT, standard library only, Python 3.9 or newer).

Install:
  pipx install ocr-software        command line only, in its own environment
  pip install ocr-software         client library, inside a venv
  uvx ocr-software --version       run once without installing (uv)

Settings (environment; the command line flag wins):
  OCR_SOFTWARE_API_KEY        API key (starts with ocr_). OCR_KEY is read too, it is the name used in the API docs.
  OCR_SOFTWARE_BASE_URL       default https://api.ocr-software.com ; own server e.g. http://127.0.0.1:8080 (also --base-url). OCR_BASE_URL is read too.
  OCR_SOFTWARE_ALLOWED_DIR    optional: read documents only from below this folder (symlinks and .. do not escape)
The key is never printed and never part of an error message. The pipeline has no default: you name it every time.

Command line:
  ocr-software --list-pipelines                       available pipelines, one per line: name@version, tab, description (no key needed)
  ocr-software scan.pdf --pipeline NAME@VERSION       text to stdout
  ocr-software scan.pdf -o scan.md --pipeline NAME@VERSION --format md
  ocr-software scan.pdf -o scan.pdf.txt --pipeline NAME@VERSION
  ocr-software scan.pdf -o searchable.pdf --pipeline NAME@VERSION --format pdf      searchable PDF, -o is required
  ocr-software scan.pdf --pipeline NAME@VERSION --format json > scan.json
  ocr-software scan.pdf --pipeline NAME@VERSION --base-url http://10.0.0.5:8080     your own server
  python -m ocr_software ...                          the same command without the script on PATH
Options: --format text (default) | md | json | hocr | pdf ; --dpi 150|200|300 (PDF render resolution) ;
  --timeout SECONDS (wait for the job, default 3600) ; --job-budget MILLICENTS (only if the operator set prices) ; --version
Without -o the result goes to stdout as UTF-8 (not for pdf). With -o the file appears whole or not at all, an existing
file is replaced, and the input file itself is refused as output.

Exit codes:
  0  done, every page read
  1  error from the service or the network (message on stderr with the API error code, for example 401 unauthorized)
  2  wrong call: missing --pipeline, unreadable or unsupported input, output not writable, no key, bad address
  3  the job is done but a page failed or did not run, or the job stopped early: the result is written anyway,
     stderr says what is missing and names the job; treat the file as incomplete
  4  the job did not finish within --timeout; it keeps running, stderr names the job

Python:
  from ocr_software import Client, PagesUnread
  client = Client()                                    # key and address from the environment
  text = client.ocr("scan.pdf", "NAME@VERSION", "md")  # bytes; raises PagesUnread (with .data) if a page was not read
  pdf = client.ocr("scan.pdf", "NAME@VERSION", "pdf")  # searchable PDF
  job = client.submit("scan.pdf", "NAME@VERSION")      # step by step: submit, wait, result
  client.wait(job["job_id"]); client.result(job["job_id"], "json")
  client.quote("scan.pdf", "NAME@VERSION")             # dry run: page count, nothing runs
Retries follow the API rules: 429, 503, 500 and network errors up to 5 times; if no connection can be made at all (wrong address)
it gives up after 2 retries, about 3 s, and names the address (connect timeout 10 s) (1, 2, 4, 8, 16 s or Retry-After); a Retry-After
over 60 s (locked_out) is not waited for. A submit carries an Idempotency-Key, so a retry never creates a second job.
Every document is checked locally first: PDF, PNG, JPEG or TIFF by its first bytes, at most 100 MiB.

Paperless-ngx, pre-consume script (sketch, not tried against a real Paperless; a non-PDF input keeps its file name but becomes a PDF):
  #!/bin/sh
  ocr-software "$DOCUMENT_WORKING_PATH" --pipeline NAME@VERSION --format pdf -o "$DOCUMENT_WORKING_PATH.ocr" && mv "$DOCUMENT_WORKING_PATH.ocr" "$DOCUMENT_WORKING_PATH"
  with OCR_SOFTWARE_API_KEY and OCR_SOFTWARE_BASE_URL set in the environment of the Paperless container.

Tests (fake local service, no network, no key):
  python -m venv .venv
  .venv/bin/pip install pytest             (Windows: .venv\Scripts\pip install pytest)
  .venv/bin/python -m pytest -p no:cacheprovider .      (Windows: .venv\Scripts\python -m pytest -p no:cacheprovider .)
