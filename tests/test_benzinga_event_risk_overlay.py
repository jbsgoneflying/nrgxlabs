import datetime as dt

from backend.config import FeatureFlags
from backend.earnings_calendar import benzinga_next_earnings, infer_timing_from_time_str
from backend.earnings_logic import compute_breach_stats
from tests.replay_orats_client import ReplayOratsClient


class FakeBenzinga:
    def __init__(self, *, earnings_rows=None, econ_rows=None, ratings_rows=None, news_rows=None, wiim_rows=None, opt_rows=None):
        self._earnings_rows = earnings_rows or []
        self._econ_rows = econ_rows or []
        self._ratings_rows = ratings_rows or []
        self._news_rows = news_rows or []
        self._wiim_rows = wiim_rows or []
        self._opt_rows = opt_rows or []

    class _Resp:
        def __init__(self, rows):
            self.rows = rows
            self.raw = rows

    def calendar_earnings(self, **kwargs):
        return self._Resp(list(self._earnings_rows))

    def calendar_economics(self, **kwargs):
        return self._Resp(list(self._econ_rows))

    def calendar_ratings(self, **kwargs):
        return self._Resp(list(self._ratings_rows))

    def news(self, **kwargs):
        # WIIM is encoded via channels=WIIM per docs.
        if str(kwargs.get("channels") or "") == "WIIM":
            return self._Resp(list(self._wiim_rows))
        return self._Resp(list(self._news_rows))

    def signal_option_activity(self, **kwargs):
        return self._Resp(list(self._opt_rows))


def test_infer_timing_from_time_str():
    assert infer_timing_from_time_str("16:00:00") == "AMC"
    assert infer_timing_from_time_str("19:30:00") == "AMC"
    assert infer_timing_from_time_str("09:30:00") == "BMO"
    assert infer_timing_from_time_str("08:00:00") == "BMO"
    assert infer_timing_from_time_str("12:00:00") == "UNK"
    assert infer_timing_from_time_str(None) == "UNK"


def test_benzinga_next_earnings_selects_nearest_and_confidence():
    bz = FakeBenzinga(
        earnings_rows=[
            {"ticker": "XYZ", "date": "2025-03-20", "time": "16:05:00", "date_confirmed": "1"},
            {"ticker": "XYZ", "date": "2025-02-10", "time": "08:15:00", "date_confirmed": "0"},
        ]
    )
    out = benzinga_next_earnings(bz, ticker="XYZ", now=dt.date(2025, 2, 1), lookahead_days=365)
    assert out is not None
    assert out.earn_date == "2025-02-10"
    assert out.timing == "BMO"
    assert out.source == "benzinga"
    # date_confirmed=0 but timing known => MED
    assert out.confidence in ("MED", "HIGH")


def test_compute_breach_stats_includes_event_risk_only_when_enabled():
    # Reuse golden tape to avoid any ORATS network.
    import json
    from pathlib import Path

    fixtures = Path(__file__).resolve().parent / "fixtures" / "golden"
    tape = json.loads((fixtures / "MU.tape.json").read_text(encoding="utf-8"))
    client = ReplayOratsClient(tape)

    today = dt.date.fromisoformat("2025-03-01")

    # 1) Disabled: no eventRisk key
    flags_off = FeatureFlags(ENABLE_BENZINGA=False, BENZINGA_ENABLE_EVENT_RISK=False)
    out0 = compute_breach_stats(client=client, ticker="MU", n=20, years=5, k=1.0, today=today, flags_override=flags_off, benzinga_client=None)
    assert "eventRisk" not in out0

    # 2) Enabled: eventRisk present and deterministic
    bz = FakeBenzinga(
        econ_rows=[
            {"date": "2025-03-03", "country": "US", "event_name": "CPI", "importance": 4},
            {"date": "2025-03-04", "country": "US", "event_name": "FOMC", "importance": 5},
        ],
        news_rows=[{"id": 1, "title": "XYZ headline"}],
        wiim_rows=[{"id": 2, "title": "WIIM"}],
        ratings_rows=[{"date": "2025-02-28", "action_company": "Upgrade"}],
        opt_rows=[{"date": "2025-02-28", "sentiment": "bullish"}],
    )
    flags_on = FeatureFlags(
        ENABLE_BENZINGA=True,
        BENZINGA_ENABLE_EVENT_RISK=True,
        BENZINGA_EVENT_RISK_AFFECTS_REGIME=False,
        BENZINGA_EVENT_RISK_AFFECTS_MC=False,
    )
    out1 = compute_breach_stats(client=client, ticker="MU", n=20, years=5, k=1.0, today=today, flags_override=flags_on, benzinga_client=bz)
    assert "eventRisk" in out1
    er = out1["eventRisk"]
    assert er["enabled"] is True
    assert 0.0 <= float(er["score01"]) <= 1.0
    assert er["label"] in ("LOW", "MED", "HIGH")

