"""Command line: ocr-software IN [-o OUT] --pipeline NAME@VERSION [--format md|text|json|hocr|pdf].

Exit codes: 0 done, every page read | 1 service or network error | 2 wrong call, unreadable input or output, no key
| 3 job done but a page failed or did not run (the result is still written, the message says what is missing)
| 4 the job did not finish within --timeout (it keeps running; the message names the job).
"""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .advice import default_pipeline, price_sentence, summary, warning_lines
from .client import DEFAULT_WAIT_TIMEOUT_S, FORMATS, Client, OcrError, PagesUnread

EXIT_ERROR, EXIT_USAGE, EXIT_UNREAD, EXIT_TIMEOUT = 1, 2, 3, 4
USAGE_CODES = ("bad_input", "no_key")


def _parser():
    p = argparse.ArgumentParser(prog="ocr-software", description="Read a PDF, PNG, JPEG or TIFF with the ocr-software.com API.")
    p.add_argument("input", nargs="?", help="the document")
    p.add_argument("-o", "--output", help="output file; without it the result goes to stdout (not for pdf)")
    p.add_argument("--pipeline", help="name@version (default: the fastest and cheapest available one, named on stderr); --list-pipelines shows the choices")
    p.add_argument("--format", default="text", choices=FORMATS, help="default text")
    p.add_argument("--dpi", type=int, choices=(150, 200, 300), help="render resolution of a PDF (default 150)")
    p.add_argument("--job-budget", type=int, help="cap in millicents (100,000 = 1 EUR; one page of the cheapest pipeline = 100). Where billing is off it is ignored")
    p.add_argument("-q", "--quiet", action="store_true", help="do not name the default pipeline on stderr (for bulk runs); errors and warnings stay")
    p.add_argument("--dry-run", action="store_true", help="show the page count and the price in euros, run nothing, cost nothing")
    p.add_argument("--timeout", type=int, default=DEFAULT_WAIT_TIMEOUT_S, help="seconds to wait for the job (default %(default)s)")
    p.add_argument("--base-url", help="address of the service (default OCR_SOFTWARE_BASE_URL, else the hosted API)")
    p.add_argument("--list-pipelines", action="store_true", help="print the available pipelines (name, tab, what it delivers and costs) and the default one, then exit")
    p.add_argument("--version", action="version", version="ocr-software " + __version__)
    return p


def _write(path, data):
    """Atomic: a half written output file never exists (a pre-consume hook may pick it up at once)."""
    tmp = "%s.%d.part" % (path, os.getpid())
    try:
        with open(tmp, "xb") as f:  # normal file mode, unlike mkstemp
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _same_file(a, b):
    try:
        return os.path.exists(b) and os.path.samefile(a, b)
    except OSError:  # input missing: the client reports it with the right code
        return False


def _is_devnull(path):
    # "-o NUL" on Windows, "-o /dev/null" everywhere (also typed on Windows from Git Bash, where no folder \dev exists)
    return bool(path) and os.path.normcase(path) in (os.path.normcase(os.devnull), os.path.normcase("/dev/null"))


def _print_warnings(answer):
    for line in warning_lines(answer):  # an older service sends none: nothing to print
        print("ocr-software: " + line, file=sys.stderr)


def _emit(data, output):
    if _is_devnull(output):
        return  # discard: a .part file next to the null device cannot exist
    if output:
        _write(output, data)
    else:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def main(argv=None):
    for stream in (sys.stderr, sys.stdout):  # a Windows console must not fail on a file name
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        client = Client(base_url=args.base_url)
        if args.list_pipelines:
            pipelines = client.catalog().get("pipelines", [])
            default = default_pipeline(pipelines)
            for p in pipelines:
                if p.get("available"):
                    print("%s	%s" % (p.get("pipeline"), summary(p, default)))
            if default:
                print("ocr-software: without --pipeline the default is %s (the fastest and cheapest available one)" % default, file=sys.stderr)
            return 0
        if not args.input:
            parser.error("IN is required (--list-pipelines shows the pipelines)")
        if args.format == "pdf" and not args.output:
            parser.error("--format pdf needs -o OUT (a PDF is not written to the terminal)")
        if args.output and not _is_devnull(args.output) and _same_file(args.input, args.output):
            parser.error("OUT is the input file; it would be overwritten")
        if args.output and not _is_devnull(args.output) and not os.path.isdir(os.path.dirname(os.path.abspath(args.output))):
            parser.error("folder of %s does not exist; nothing was sent" % args.output)  # before the job, not after it
        pipeline = args.pipeline
        if not pipeline:  # the default is named, never silent (ziel.md: fastest and cheapest available; stdout stays the result)
            pipeline = client.default_pipeline()
            if not args.quiet:
                print("ocr-software: no --pipeline given, using %s (the fastest and cheapest available one; --list-pipelines shows the others)" % pipeline,
                      file=sys.stderr)
        if args.dry_run:
            q = client.quote(args.input, pipeline, args.dpi)
            q.setdefault("pipeline", pipeline)
            print(price_sentence(q))
            _print_warnings(q)
            return 0
        data = client.ocr(args.input, pipeline, args.format, timeout=args.timeout, dpi=args.dpi, job_budget=args.job_budget,
                          on_accepted=_print_warnings)  # the service names the warnings in its answer: no extra dry run
        _emit(data, args.output)
        return 0
    except PagesUnread as e:
        try:
            _emit(e.data, args.output)
        except OSError as w:
            print("ocr-software: cannot write output: %s" % w, file=sys.stderr)
        print("ocr-software: %s; the result is incomplete" % e, file=sys.stderr)
        return EXIT_UNREAD
    except OcrError as e:
        print("ocr-software: %s" % e, file=sys.stderr)
        return EXIT_USAGE if e.code in USAGE_CODES else EXIT_TIMEOUT if e.code == "timeout" else EXIT_ERROR
    except OSError as e:
        print("ocr-software: cannot write output: %s" % e, file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:  # Ctrl+C: no traceback; a job already submitted keeps running and is not billed twice on retry
        print("ocr-software: interrupted; a job already submitted keeps running on the service", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
