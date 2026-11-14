"""Words for a human: price in euros, the default pipeline, a short pipeline line.

One copy for the command line and the MCP server, so both say the same. Nothing here calls the network.
Unit: 100,000 millicents = 1 EUR; factor 1 = 100 millicents = EUR 0.001 per page.
"""
from __future__ import annotations

# The price rate lives here and nowhere else: showing pages of the base pipeline instead of euros changes eur() and price_sentence().
MILLICENTS_PER_EUR = 100000
MILLICENTS_PER_FACTOR = 100  # factor 1 = the base price per page
FAIR_USE = "Free use has limits (https://ocr-software.com/legal/fair-usage/); for more volume write to info@ocr-software.com."
SWITCH_HINTS = {
    "hard_pages": "for hard pages (poor scans, doubtful words) use %s",
    "turned_pages": "for turned pages use %s",
    "no_tables": "without tables %s is cheaper",
    "clean_bulk": "for clean bulk text %s is cheaper",
}


def eur(millicents):
    """1 EUR = 100000 millicents; at least two decimals, as many as needed up to four."""
    text = ("%.4f" % (millicents / MILLICENTS_PER_EUR)).rstrip("0")
    return "EUR " + text + "0" * (2 - len(text.partition(".")[2]))


def price_sentence(q, show_list_price=True):
    """What the human reads before saying yes, from a dry run. Nothing is charged today (billing off or free pages): the price
    is still shown, as information, with the fair-use link and the contact."""
    pages, name = q["pages"], q["pipeline"]
    price, charge, per_page = q.get("price_millicents") or 0, q.get("max_charge_millicents") or 0, q.get("price_per_page_millicents") or 0
    start = "%d page%s with %s" % (pages, "" if pages == 1 else "s", name)
    if not price:
        text = "%s: nothing is charged (this service has no prices set)." % start
    elif not charge:
        text = "%s: you pay %s today." % (start, eur(0))
        if show_list_price:
            text += " List price %s in total (%s per page), shown for information only. %s" % (eur(price), eur(per_page), FAIR_USE)
    else:
        text = "%s: up to %s is charged (%s per page)." % (start, eur(charge), eur(per_page))
    covered = q.get("pages_covered", pages)
    if covered < pages:
        text += " Only %d of %d pages would run." % (covered, pages)
    return text


def warning_lines(q):
    """One line per warning of a dry run (dpi_ignored, job_budget, free_limit, balance, balance_too_low), for stderr:
    price_sentence says what it costs, these say why fewer pages run or why an option does nothing."""
    return ["warning %s: %s" % (w.get("code", "?"), w.get("message", "")) for w in q.get("warnings") or []]


def cost(p):
    """Price per page in millicents, comparable between pipelines: the catalog price, else factor * 100 (the catalog hides the
    price while billing is off, the factor stays)."""
    price = p.get("price_per_page_millicents")
    if isinstance(price, (int, float)) and price > 0:
        return price
    factor = p.get("factor")
    return factor * MILLICENTS_PER_FACTOR if isinstance(factor, (int, float)) else None


def preview(p):
    """A preview (preview: true, or version @0) has no long-term promise: never a default."""
    return bool(p.get("preview")) or str(p.get("pipeline", "")).endswith("@0")


def usable(p):
    return p.get("available", True) and p.get("orderable", True) is not False and not preview(p)


def default_pipeline(pipelines):
    """name@version of the pipeline used when the caller names none: the fastest and cheapest usable one (rule in ziel.md).
    Not ready and preview pipelines are out. Order: price, then not autoregressive (faster), then the name, so a tie never
    depends on the order of the catalog. None if nothing is usable. The caller must say which one was chosen."""
    best = min(((cost(p), bool(p.get("autoregressive")), p.get("pipeline", "")) for p in pipelines if usable(p) and cost(p) is not None),
               default=None)
    return best[2] if best else None


def summary(p, default=None):
    """One short line for a pipeline (without its name): what it delivers, what it costs, when to choose another one."""
    feats = p.get("features") or []
    parts = ["%s boxes" % p["level"] if p.get("level") else "", "tables" if "table_cells" in feats else "",
             "layout" if "layout_classes" in feats else "", "slower" if p.get("autoregressive") else ""]
    line = ", ".join(x for x in parts if x) or p.get("description", "")[:80]
    price = cost(p)
    if price is not None:
        line += "; price %s per page" % eur(price)
    if default and p.get("pipeline") == default:
        line += "; the default (fastest and cheapest)"
    hints = [SWITCH_HINTS[s["when"]] % s["pipeline"] for s in p.get("switch") or [] if s.get("when") in SWITCH_HINTS and s.get("pipeline")]
    return line + ("; " + "; ".join(hints) if hints else "")
