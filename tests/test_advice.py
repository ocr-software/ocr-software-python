import itertools

from ocr_software.advice import default_pipeline


def p(name, factor, **kw):
    return dict({"pipeline": name, "factor": factor, "available": True, "orderable": True}, **kw)


def test_default_is_the_cheapest_usable_one():
    assert default_pipeline([p("slow@1", 4), p("a@1", 1), p("b@1", 1.5)]) == "a@1"


def test_not_ready_not_orderable_and_preview_never_win():
    catalog = [p("pre@0", 0.1), p("flagged@2", 0.1, preview=True), p("down@1", 0.2, available=False),
               p("closed@1", 0.3, orderable=False), p("ok@1", 2)]
    assert default_pipeline(catalog) == "ok@1"
    assert default_pipeline(catalog[:4]) is None


def test_a_tie_never_depends_on_the_order_of_the_catalog():
    catalog = [p("b@1", 1), p("a@1", 1), p("c@1", 1)]
    assert {default_pipeline(list(order)) for order in itertools.permutations(catalog)} == {"a@1"}


def test_on_a_tie_the_faster_one_not_autoregressive_wins():
    assert default_pipeline([p("a@1", 1, autoregressive=True), p("z@1", 1)]) == "z@1"


def test_price_field_and_factor_compare_in_the_same_unit():
    """While billing is off the catalog has no price field, only the factor; 100 millicents = factor 1."""
    assert default_pipeline([p("priced@1", 1, price_per_page_millicents=150), p("plain@1", 1)]) == "plain@1"
    assert default_pipeline([p("priced@1", 1, price_per_page_millicents=90), p("plain@1", 1)]) == "priced@1"
