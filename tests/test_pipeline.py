from vibe_flipper.matching.llm import LLMResult
from vibe_flipper.matching.pipeline import Matcher
from vibe_flipper.models import Listing, Product
from vibe_flipper.runtime_settings import RuntimeSettings

PRODUCTS = [
    Product(id=1, name="RTX 3070", category="GPU", include_keywords=["3070"], exclude_keywords=["ti"],
            min_price=190, max_price=420, active=True),
    Product(id=2, name="RTX 3080", category="GPU", include_keywords=["3080"], exclude_keywords=["ti"],
            min_price=280, max_price=600, active=True),
    Product(id=3, name="PlayStation 5 (Disc)", category="Consoles", include_keywords=["ps 5", "playstation 5"],
            exclude_keywords=["digital"], min_price=250, max_price=650, active=True),
]


class FakeLLM:
    def __init__(self, answer: int | None, **flags):
        self.answer, self.flags, self.calls = answer, flags, 0

    def classify(self, title, price, raw_condition, description, products):
        self.calls += 1
        return LLMResult(self.answer, 0.95, self.flags.get("condition", "good"), self.flags.get("bundle", False),
                         False, self.flags.get("broken", False))


def rs(mode):
    return RuntimeSettings(20, mode, "http://fake", "qwen", 0, 0, 30, "GR", "insomnia,vendora,vinted", 30)


def listing(title, price):
    return Listing(title=title, price=price, is_wanted_ad=False, llm_checked=False)


def test_hybrid_confident_rule_skips_llm():
    llm = FakeLLM(2)
    m = Matcher(PRODUCTS, rs("hybrid"), llm_budget=10, classifier=llm)
    l = listing("Gigabyte RTX 3070 Gaming OC", 300)
    m.apply(l)
    assert l.product_id == 1 and l.match_method == "rule" and llm.calls == 0


def test_hybrid_ambiguous_goes_to_llm_and_is_cached():
    llm = FakeLLM(2, bundle=True)
    m = Matcher(PRODUCTS, rs("hybrid"), llm_budget=10, classifier=llm)
    l = listing("RTX 3070 / RTX 3080 MSI Gaming Z Trio", 330)
    m.apply(l)
    assert (l.product_id, l.match_method, l.is_bundle, l.llm_checked) == (2, "llm", True, True)
    m.apply(l)  # cached: no second call
    assert llm.calls == 1


def test_llm_answer_outside_price_range_is_rejected():
    m = Matcher(PRODUCTS, rs("hybrid"), llm_budget=10, classifier=FakeLLM(3))
    l = listing("Κονσόλα Sony τελευταίας γενιάς playstation 5 χειριστήριο", 45)
    m.apply(l)
    assert l.product_id is None and "price outside" in l.match_note


def test_accessory_rejected_by_price_does_not_call_llm():
    llm = FakeLLM(3)
    m = Matcher(PRODUCTS, rs("hybrid"), llm_budget=10, classifier=llm)
    l = listing("God of War Ragnarok PS5", 35)
    m.apply(l)
    assert l.product_id is None and llm.calls == 0


def test_rules_mode_never_calls_llm():
    llm = FakeLLM(2)
    m = Matcher(PRODUCTS, rs("rules"), llm_budget=10, classifier=llm)
    m.apply(listing("RTX 3070 / RTX 3080", 330))
    assert llm.calls == 0


def test_budget_exhausted_falls_back_to_rules_and_marks_pending():
    m = Matcher(PRODUCTS, rs("hybrid"), llm_budget=0, classifier=FakeLLM(2))
    l = listing("RTX 3070 / RTX 3080", 330)
    m.apply(l)
    assert l.product_id is None and l.match_note.endswith("(llm pending)")


def test_manual_is_untouched():
    m = Matcher(PRODUCTS, rs("llm"), llm_budget=10, classifier=FakeLLM(2))
    l = listing("RTX 3070", 300)
    l.product_id, l.match_method = None, "manual"
    m.apply(l)
    assert l.product_id is None and l.match_method == "manual"


def test_rules_flag_bundles_and_keep_existing_flag():
    m = Matcher(PRODUCTS, rs("rules"))
    l = listing("RTX 3080 + τροφοδοτικό 750W", 450)
    m.apply(l)
    assert l.product_id == 2 and l.is_bundle
    l2 = listing("Gigabyte RTX 3080 Gaming OC", 450)
    l2.is_bundle = True  # found by the LLM earlier
    m.apply(l2)
    assert l2.is_bundle


def test_flags_set_by_hand_survive_rematching():
    m = Matcher(PRODUCTS, rs("rules"))
    l = listing("RTX 3080 μικρό θέμα με ανεμιστήρα", 300)
    l.is_broken, l.manual_flags = True, {"is_broken": True}  # the title says nothing
    m.apply(l)
    assert l.is_broken
    l2 = listing("RTX 3080 + τροφοδοτικό 750W", 450)  # the PSU is only a gift
    l2.manual_flags = {"is_bundle": False}
    m.apply(l2)
    assert not l2.is_bundle
