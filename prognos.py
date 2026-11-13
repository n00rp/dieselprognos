"""Prognos för Preems listpris på diesel (företagskort, kr/l inkl moms):
kort sikt (om några dagar) och snittpris kommande 7 dagar.

Kör: python prognos.py [--preem lokal_fil.xls]   → output/rapport.html
"""
import argparse
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
import warnings
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

PREEM_PAGE = "https://www.preem.se/foretag/listpriser/"
PREEM_XLS_RE = r"https://www\.datocms-assets\.com/[^\"']*prishistorik-listpriser[^\"']*\.xls"
ULSD_URL = "https://www.eia.gov/dnav/pet/hist_xls/EER_EPD2DXL0_PF4_Y35NY_DPGd.xls"
USDSEK_URL = "https://api.riksbank.se/swea/v1/Observations/SEKUSDPMI/2008-09-01/{today}"
LITERS_PER_GALLON = 3.785411784
HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "rapport_mall.html"
OUTPUT = HERE / "output"


def _get(url, tries=4):
    req = urllib.request.Request(url, headers={"User-Agent": "dieselprognos/1.0"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == tries - 1:  # Riksbanken begränsar antal anrop per minut
                raise
            time.sleep(20 * (attempt + 1))


def parse_preem(sheets):
    """Ett blad per år; rubrikrad börjar med 'Datum'. Returnerar daglig Diesel-serie."""
    parts = []
    for df in sheets.values():
        hdr = df.index[df[0].astype(str).str.startswith("Datum")][0]
        cols = [str(c).strip() for c in df.loc[hdr]]
        body = df.loc[hdr + 1:, [0, cols.index("Diesel")]]
        body.columns = ["date", "price"]
        body["date"] = pd.to_datetime(body["date"], errors="coerce")  # 'Snitt-'-rader → NaT
        body["price"] = pd.to_numeric(body["price"], errors="coerce")  # framtida tomma → NaN
        parts.append(body.dropna())
    s = pd.concat(parts).set_index("date")["price"].sort_index()
    return s[~s.index.duplicated(keep="last")].astype(float)


def fetch_preem(path=None):
    if path is None:
        html = _get(PREEM_PAGE).decode("utf-8", "replace")
        m = re.search(PREEM_XLS_RE, html)
        if not m:
            raise SystemExit(f"Hittade ingen prishistorik-länk på {PREEM_PAGE}. "
                             "Ladda ner filen manuellt och kör med --preem fil.xls")
        path = io.BytesIO(_get(m.group(0)))
        current = parse_current_price(html)
    else:
        current = None
    s = parse_preem(pd.read_excel(path, sheet_name=None, header=None))
    # Excel-filen släpar några dagar; sidans aktuella pris gör sista punkten färsk.
    # ponytail: dagar mellan filens slut och sidans datum fylls med filens sista pris; okända mellanliggande ändringar missas
    if current and current[0] > s.index.max():
        s.loc[current[0]] = current[1]
    return s


def parse_current_price(html):
    """Första kr/l-raden för Preem Evolution Diesel (företagskort) → (datum, pris) eller None."""
    m = re.search(r"Preem Evolution Diesel</span>.*?<span>(\d+,\d+) kr/l</span>"
                  r".*?<span>(\d{4}-\d{2}-\d{2})</span>", html, re.S)
    return (pd.Timestamp(m.group(2)), float(m.group(1).replace(",", "."))) if m else None


def fetch_ulsd():
    df = pd.read_excel(io.BytesIO(_get(ULSD_URL)), sheet_name="Data 1", header=None, skiprows=3)
    df[0] = pd.to_datetime(df[0], errors="coerce")
    return df.dropna().set_index(0)[1].astype(float).sort_index()


def fetch_usdsek():
    rows = json.loads(_get(USDSEK_URL.format(today=date.today().isoformat())))
    return pd.Series({pd.Timestamp(r["date"]): float(r["value"]) for r in rows}).sort_index()


FEATURES = ["d0", "d1", "d2", "wavg", "gap"]
DEADBAND = 0.05  # kr/l, mindre än så räknas som oförändrat
VAT = 1.25


def build_weekly(preem, ulsd, usdsek):
    """Veckorader som slutar på Preems senaste kända veckodag, så att sista raden är
    en hel vecka med samma datum för pris och marknad. y = snitt Preem-pris kommande
    7 dagar − pris nu."""
    end = preem.index.max()
    rule = "W-" + ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][end.weekday()]
    days = pd.date_range(min(s.index.min() for s in (preem, ulsd, usdsek)), end, freq="D")
    p = preem.reindex(days).ffill()  # listpris gäller tills det ändras; days slutar på sista kända pris
    market = (ulsd.reindex(days).ffill() * usdsek.reindex(days).ffill() / LITERS_PER_GALLON)
    wk = pd.DataFrame({"preem": p.resample(rule).last(), "market": market.resample(rule).last()})
    nxt = p.resample(rule).agg(["mean", "count"]).shift(-1)
    wk["y"] = nxt["mean"].where(nxt["count"] == 7) - wk["preem"]
    wk["d0"] = wk["market"].diff()
    wk["d1"], wk["d2"] = wk["d0"].shift(1), wk["d0"].shift(2)
    wk["wavg"] = market.resample(rule).mean() - wk["market"]  # rörelsen inom veckan
    gap = wk["preem"] / VAT - wk["market"]
    # ponytail: rullande medel tar hand om skatteskiften grovt; explicita skattedummies om backtesten visar bias efter ändringar
    wk["gap"] = gap - gap.rolling(26).mean()
    return wk.dropna(subset=["preem", "market"])


SHORT_H = 3  # dagar; där kortsiktssignalen var starkast i test mot historiken
DAILY_FEATURES = [f"m{k}" for k in range(7)]


def build_daily(preem, ulsd, usdsek, h=SHORT_H):
    """Dagsrader till Preems senaste kända dag. Features: marknadens dagsförändring
    (kr/l inkl moms) de senaste 7 dagarna. y = Preem-pris om h dagar − pris nu."""
    days = pd.date_range(preem.index.min(), preem.index.max(), freq="D")
    p = preem.reindex(days).ffill()
    move = (ulsd.reindex(days).ffill() * usdsek.reindex(days).ffill() / LITERS_PER_GALLON).diff() * VAT
    df = pd.DataFrame({f"m{k}": move.shift(k) for k in range(7)})
    df["y"] = p.shift(-h) - p
    return df


def _X(df, features):
    return np.column_stack([np.ones(len(df)), df[features].to_numpy()])


def fit(df, features=FEATURES):
    return np.linalg.lstsq(_X(df, features), df["y"].to_numpy(), rcond=None)[0]


def predict(coef, df, features=FEATURES):
    return _X(df, features) @ coef


def backtest(df, window=156, features=FEATURES, skip=0):
    """Rullande: rad i prediceras av en modell tränad på window rader före. skip = antal
    rader närmast före vars y ännu inte var känt vid rad i (h−1 för h-dagarsprognos)."""
    df = df.dropna(subset=features + ["y"])
    gap = skip
    out = []
    for i in range(window + gap, len(df)):
        coef = fit(df.iloc[i - window - gap:i - gap], features)
        out.append((df.index[i], df["y"].iloc[i], predict(coef, df.iloc[[i]], features)[0]))
    return pd.DataFrame(out, columns=["week", "y", "pred"]).set_index("week")


def direction(x):
    return np.where(x > DEADBAND, 1, np.where(x < -DEADBAND, -1, 0))


# 7,5/92,5 %-kvantiler av senaste årets fel gav i test mot historiken ~80 % träff (10/90 gav bara ~75 %,
# eftersom felen varierar med hur orolig marknaden är).
BAND_Q = 0.075


def metrics(bt, band_n=52, lag=1):
    """band_n = antal senaste fel som spannet bygger på (ett år). lag = rader innan ett fel blir känt.
    coverage = andel gånger verkligheten hamnade i spannet, när spannet bara byggde på tidigare fel."""
    err = bt["y"] - bt["pred"]
    roll = err.rolling(band_n, min_periods=band_n)
    lo, hi = roll.quantile(BAND_Q).shift(lag), roll.quantile(1 - BAND_Q).shift(lag)
    ok = lo.notna()
    recent = err.iloc[-band_n:]
    return {
        "hit_rate": float((direction(bt["y"]) == direction(bt["pred"])).mean()),
        "mae": float(err.abs().mean()),
        "naive_mae": float(bt["y"].abs().mean()),
        "band_lo": float(recent.quantile(BAND_Q)),
        "band_hi": float(recent.quantile(1 - BAND_Q)),
        "coverage": float(((err[ok] >= lo[ok]) & (err[ok] <= hi[ok])).mean()),
    }


def _dir_word(x):
    return {1: "UPP", -1: "NER", 0: "OFÖRÄNDRAT"}[int(direction(np.array([x]))[0])]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--preem", help="lokal Preem prishistorik (.xls) i stället för nedladdning")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")  # Windows-konsolen är cp1252 annars
    warnings.filterwarnings("ignore", message=".*xlrd.*")

    preem = fetch_preem(args.preem)
    ulsd, usdsek = fetch_ulsd(), fetch_usdsek()

    # Kort sikt: Preem tar efter marknaden inom 1–5 dagar.
    day = build_daily(preem, ulsd, usdsek)
    window = 3 * 365
    ms = metrics(backtest(day, window, DAILY_FEATURES, skip=SHORT_H - 1), band_n=365, lag=SHORT_H)
    known = day.dropna(subset=DAILY_FEATURES + ["y"]).iloc[-window:]
    pred_s = float(predict(fit(known, DAILY_FEATURES), day.iloc[[-1]], DAILY_FEATURES)[0])
    moved = float(day[DAILY_FEATURES[0]].iloc[-7:].sum())

    # Vecka: snitt kommande 7 dagar.
    wk = build_weekly(preem, ulsd, usdsek)
    bt = backtest(wk)
    m = metrics(bt)
    train = wk.dropna(subset=FEATURES + ["y"]).iloc[-156:]
    last = wk.iloc[[-1]]
    pred = float(predict(fit(train), last)[0])

    def report(p, mm, unit):
        print(f"  Väntad förändring: {p:+.2f} kr/l  (troligt spann {p + mm['band_lo']:+.2f} till "
              f"{p + mm['band_hi']:+.2f}, träffat {mm['coverage']:.0%} historiskt)  → {_dir_word(p)}")
        print(f"  Historiskt: rätt riktning {mm['hit_rate']:.0%} av {unit}, medelfel {mm['mae']:.2f} kr/l "
              f"(att gissa \"oförändrat\" ger {mm['naive_mae']:.2f})")
        if mm["mae"] >= mm["naive_mae"]:
            print("  VARNING: slår inte 'oförändrat' – lita inte på riktningen, bara på spannet.")

    print(f"Preem diesel (företagskort) senast: {preem.iloc[-1]:.2f} kr/l ({preem.index[-1]:%Y-%m-%d})")
    print(f"\nKORT SIKT – Preems pris om {SHORT_H} dagar")
    print(f"  Marknaden senaste 7 dagarna: {moved:+.2f} kr/l (omräknat inkl moms)")
    report(pred_s, ms, "dagarna")
    print("\nVECKA – snittpris kommande 7 dagar mot nu")
    report(pred, m, "veckorna")

    OUTPUT.mkdir(exist_ok=True)
    out = bt.copy()
    out.loc[last.index[0], ["y", "pred"]] = [np.nan, pred]
    out.round(4).to_csv(OUTPUT / "prognos_vecka.csv")

    now, base = preem.index[-1], float(preem.iloc[-1])
    hist = preem.reindex(pd.date_range(now - pd.Timedelta(days=365), now)).ffill().dropna()

    def summary(p, mm):
        return {"pred": p, "lo": p + mm["band_lo"], "hi": p + mm["band_hi"], "dir": _dir_word(p),
                "hit": mm["hit_rate"], "cov": mm["coverage"], "mae": mm["mae"], "naive": mm["naive_mae"]}

    def point(p, mm, days, label, color):
        return {"date": f"{now + pd.Timedelta(days=days):%Y-%m-%d}", "value": base + p, "label": label,
                "lo": base + p + mm["band_lo"], "hi": base + p + mm["band_hi"], "color": color}

    ctx = {
        "generated": f"{date.today():%Y-%m-%d}",
        "price": base,
        "price_date": f"{now:%Y-%m-%d}",
        "short": {**summary(pred_s, ms), "h": SHORT_H, "moved": moved},
        "week": summary(pred, m),
        "history": [[f"{d:%Y-%m-%d}", round(float(v), 2)] for d, v in hist.items()],
        "forecasts": [point(pred_s, ms, SHORT_H, f"Om {SHORT_H} dagar", "--series-3"),
                      point(pred, m, 4, "Snitt kommande 7 dagar", "--series-2")],  # mitt i veckan
    }
    path = OUTPUT / "rapport.html"
    path.write_text(render_report(ctx), encoding="utf-8")
    print(f"\nRapport: {path}")


def render_report(ctx):
    """Fyller mallen med data som JSON. '</' escapas så att text aldrig kan stänga <script>."""
    blob = json.dumps(ctx, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", blob, 1)


if __name__ == "__main__":
    main()
