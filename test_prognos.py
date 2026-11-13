import numpy as np
import pandas as pd
import prognos


def test_parse_preem():
    def sheet(rows):
        return pd.DataFrame([["Titel", None, None], ["Info", None, None],
                             ["Datum", "95 oktan", "Diesel"], *rows])
    sheets = {
        "2026": sheet([[pd.Timestamp("2026-01-02"), 15.0, 16.39],
                       [pd.Timestamp("2026-01-03"), np.nan, np.nan],
                       ["Snitt- dec", 15.0, 16.3]]),
        "2025": sheet([[pd.Timestamp("2025-12-31"), 14.8, 16.24]]),
    }
    s = prognos.parse_preem(sheets)
    assert isinstance(s.index, pd.DatetimeIndex)
    assert s.index.is_monotonic_increasing
    assert list(s.values) == [16.24, 16.39]


def _synthetic(days=6 * 365):
    rng = np.random.default_rng(0)
    idx = pd.date_range("2015-01-01", periods=days, freq="D")
    market = pd.Series(8 + np.cumsum(rng.normal(0, 0.05, days)), idx)  # SEK/l
    usdsek = pd.Series(10.0, idx)
    ulsd = market * prognos.LITERS_PER_GALLON / usdsek  # USD/gallon
    preem = 1.25 * market.shift(7).bfill() + 5
    return preem, ulsd, usdsek


def test_backtest_learns_lag():
    m = prognos.metrics(prognos.backtest(prognos.build_weekly(*_synthetic())))
    assert m["hit_rate"] > 0.8
    assert m["mae"] < m["naive_mae"] * 0.5


def test_build_weekly_no_nan_features():
    preem, ulsd, usdsek = _synthetic()
    ulsd = ulsd[np.arange(len(ulsd)) % 5 != 0]
    df = prognos.build_weekly(preem, ulsd, usdsek)
    assert not df[prognos.FEATURES].iloc[26:].isna().any().any()


def test_live_row_anchored_on_last_preem_date():
    preem, ulsd, usdsek = _synthetic()
    preem = preem[:-5]  # Preem-filen ligger efter marknaden
    df = prognos.build_weekly(preem, ulsd, usdsek)
    assert df.index[-1] == preem.index[-1]
    assert df["preem"].iloc[-1] == preem.iloc[-1]
    expected_market = ulsd[preem.index[-1]] * usdsek[preem.index[-1]] / prognos.LITERS_PER_GALLON
    assert abs(df["market"].iloc[-1] - expected_market) < 1e-9


def test_parse_current_price_takes_first_kr_per_litre_row():
    row = ('<span>Preem Evolution Diesel</span></td><td><span class="x">Pris inkl. moms</span>'
           '<span>{p}</span></td><td><span class="x">Gäller fr.om</span><span>{d}</span></td>')
    html = row.format(p="21,59 kr/l", d="2026-11-12") + row.format(p="22,23 kr/l", d="2026-11-09")
    assert prognos.parse_current_price(html) == (pd.Timestamp("2026-11-12"), 21.59)
    assert prognos.parse_current_price("<html>ny layout</html>") is None


def test_short_term_learns_catch_up():
    df = prognos.build_daily(*_synthetic())
    bt = prognos.backtest(df, window=365, features=prognos.DAILY_FEATURES,
                           skip=prognos.SHORT_H - 1)
    m = prognos.metrics(bt)
    assert m["hit_rate"] > 0.8
    assert m["mae"] < m["naive_mae"] * 0.5
    assert df.index[-1] == _synthetic()[0].index[-1]  # sista raden = senaste kända Preem-dag


def test_render_report_injects_data():
    import json
    ctx = {"price": 21.59, "price_date": "2026-11-12", "history": [["2026-11-12", 21.59]]}
    html = prognos.render_report(ctx)
    assert "__DATA__" not in html
    blob = html.split("const D = ", 1)[1].split(";\n", 1)[0]
    assert json.loads(blob) == ctx
    assert "</script><script>" not in prognos.render_report({"x": "</script><script>"})


def test_band_uses_recent_errors_and_reports_coverage():
    idx = pd.date_range("2020-01-01", periods=200, freq="W")
    rng = np.random.default_rng(1)
    err = np.r_[rng.normal(0, 1.0, 100), rng.normal(0, 0.1, 100)]  # lugnare på slutet
    bt = pd.DataFrame({"pred": 0.0, "y": err}, index=idx)
    m = prognos.metrics(bt, band_n=52)
    assert -0.3 < m["band_lo"] < 0 < m["band_hi"] < 0.3
    assert 0.5 < m["coverage"] <= 1
