import math
from types import SimpleNamespace

import pytest

from vibe_flipper.pricing import compute_stats, listing_profit, remove_outliers, variant_market


def test_remove_outliers_drops_extremes():
    prices = [300, 310, 290, 305, 295, 20, 1500]
    assert remove_outliers(prices) == [290, 295, 300, 305, 310]


def test_small_samples_kept():
    assert remove_outliers([10, 1000]) == [10, 1000]


def test_compute_stats():
    st = compute_stats([300, 310, 290, 305, 295, 20])
    assert st.median == 300
    assert st.count == 5
    assert st.p25 <= st.median <= st.p75
    assert compute_stats([]) is None


def _listing(price, buy_cost=None, variant=None, **flags):
    return SimpleNamespace(price=price, buy_cost=buy_cost, variant=variant, is_wanted_ad=flags.get("wanted", False),
                           is_broken=flags.get("broken", False))


def _product(median=400, target=20, samples=10, spec_keys=None, variant_stats=None):
    return SimpleNamespace(market_median=median, target_margin_pct=target, sample_count=samples,
                           spec_keys=spec_keys or [], variant_stats=variant_stats or {})


MACBOOK = dict(median=700, spec_keys=["ram", "storage"], variant_stats={
    "8GB / 256GB": {"median": 600, "count": 10},
    "16GB / 512GB": {"median": 850, "count": 6},
    "24GB / 1TB": {"median": 1100, "count": 1},  # too few samples
})


def test_profit_uses_own_variant():
    pr = listing_profit(_listing(700, variant="16GB / 512GB"), _product(**MACBOOK), 0, 0, 5)
    assert pr.market == 850 and pr.variant == "16GB / 512GB" and not pr.assumed


def test_unknown_variant_compared_to_cheapest_variant():
    pr = listing_profit(_listing(500), _product(**MACBOOK), 0, 0, 5)
    assert pr.market == 600 and pr.variant == "8GB / 256GB" and pr.assumed


def test_unknown_variant_uses_product_median_when_lower():
    """RTX 2060: the few ads stating "6GB" are the pricier ones (median 202.5), the
    whole product goes for 180 -> a spec-less ad is compared to 180."""
    gpu = dict(median=180, samples=13, spec_keys=["ram"], variant_stats={"6GB": {"median": 202.5, "count": 4}})
    pr = listing_profit(_listing(130), _product(**gpu), 0, 0, 5)
    assert pr.market == 180 and pr.variant == "6GB" and pr.assumed and not pr.low_data


def test_rare_variant_is_estimated_from_nearest_variant():
    """24GB / 1TB has one ad: estimated from 16GB / 512GB (+10% per doubling, no
    pair to measure the step from), blended with its own ad."""
    pr = listing_profit(_listing(900, variant="24GB / 1TB"), _product(**MACBOOK), 0, 0, 5)
    est = 850 * 1.1 ** (math.log2(24 / 16) + 1)
    assert pr.market == pytest.approx((1100 + 2 * est) / 3, abs=0.01)
    assert pr.estimated and pr.basis == "16GB / 512GB" and not pr.assumed and not pr.low_data


IPHONE = dict(median=400, spec_keys=["storage"], variant_stats={
    "128GB": {"median": 400, "count": 12},
    "256GB": {"median": 440, "count": 8},
    "512GB": {"median": 300, "count": 1},  # one odd cheap ad
})


def test_estimate_uses_measured_step():
    # 128 -> 256 is +10%; 1TB is two doublings above 256GB and has no ads
    m = variant_market(_product(**IPHONE), "1TB")
    assert m.price == pytest.approx(440 * 1.1 ** 2, abs=0.01) and m.basis == "256GB"


def test_estimate_never_below_smaller_variant():
    m = variant_market(_product(**IPHONE), "512GB")
    assert m.price >= 440 and m.estimated


def test_well_sampled_variant_uses_its_median():
    m = variant_market(_product(**IPHONE), "256GB")
    assert m.price == 440 and not m.estimated


def test_variant_without_any_reliable_neighbour():
    p = _product(spec_keys=["storage"], variant_stats={"128GB": {"median": 500, "count": 1}})
    assert variant_market(p, "128GB").price == 500
    assert variant_market(p, "256GB") is None
    assert listing_profit(_listing(450, variant="256GB"), p, 0, 0, 5).market == 400  # product median


def test_profit_and_deal():
    pr = listing_profit(_listing(300), _product(), fee_pct=0, fee_fixed=0, min_samples=5)
    assert pr.profit == 100
    assert pr.margin_pct == pytest.approx(33.33, abs=0.01)
    assert pr.is_deal and not pr.low_data


def test_profit_uses_buy_cost_and_fees():
    pr = listing_profit(_listing(300, buy_cost=315), _product(), fee_pct=10, fee_fixed=5, min_samples=5)
    assert pr.profit == pytest.approx(400 * 0.9 - 5 - 315)
    assert not pr.is_deal


def test_wanted_ads_are_never_deals():
    pr = listing_profit(_listing(100, wanted=True), _product(), 0, 0, 5)
    assert not pr.is_deal


def test_no_market_price():
    assert listing_profit(_listing(100), _product(median=None), 0, 0, 5) is None
    assert listing_profit(_listing(None), _product(), 0, 0, 5) is None
