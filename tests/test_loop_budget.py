from harn.loop import _over_budget
from harn.config import Config

def _cfg(**kw):
    c = Config(); [setattr(c, k, v) for k, v in kw.items()]; return c

def test_cost_ceiling_trips():
    r = _over_budget(3.5, 1000, _cfg(max_cost_usd=3.0, max_tokens=0))
    assert r and "3.5" in r and "3.0" in r

def test_token_ceiling_trips():
    r = _over_budget(0.1, 500_000, _cfg(max_cost_usd=0.0, max_tokens=400_000))
    assert r and "500000" in r

def test_zero_ceilings_never_trip():
    assert _over_budget(9_999.0, 99_000_000, _cfg(max_cost_usd=0.0, max_tokens=0)) == ""

def test_under_budget_is_clear():
    assert _over_budget(0.5, 1000, _cfg(max_cost_usd=3.0, max_tokens=400_000)) == ""
