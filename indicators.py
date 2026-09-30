"""
~180 technical indicators dari OHLCV (pure pandas/numpy, tanpa dependency TA).

    from indicators import compute_indicators
    out = compute_indicators(df)   # df kolom: Open, High, Low, Close, Volume (urut waktu)

Mengembalikan DataFrame yang sama indeksnya dengan `df`, hanya berisi kolom indikator.
Kolom yang belum punya cukup histori (mis. SMA_200 dengan 50 bar) bernilai NaN.
Untuk menambah/mengurangi indikator cukup edit fungsi `_build()` di bawah.
"""

import numpy as np
import pandas as pd


# ---------------------------------------------------------------- primitives
def sma(s, n):
    return s.rolling(n, min_periods=n).mean()


def ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rma(s, n):  # Wilder smoothing
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def wma(s, n):
    w = np.arange(1, n + 1, dtype=float)
    return s.rolling(n, min_periods=n).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)


def hma(s, n):
    return wma(2 * wma(s, max(n // 2, 1)) - wma(s, n), max(int(np.sqrt(n)), 1))


def true_range(h, l, c):
    pc = c.shift(1)
    return pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def _linreg(s, n):
    """slope & r^2 dari regresi linier rolling."""
    x = np.arange(n, dtype=float)
    xm = x.mean()
    sxx = ((x - xm) ** 2).sum()

    def slope(y):
        return ((x - xm) * (y - y.mean())).sum() / sxx

    def r2(y):
        ss_tot = ((y - y.mean()) ** 2).sum()
        if ss_tot == 0:
            return np.nan
        b = ((x - xm) * (y - y.mean())).sum() / sxx
        a = y.mean() - b * xm
        ss_res = ((y - (a + b * x)) ** 2).sum()
        return 1 - ss_res / ss_tot

    r = s.rolling(n, min_periods=n)
    return r.apply(slope, raw=True), r.apply(r2, raw=True)


def _psar(h, l, step=0.02, max_step=0.2):
    hi, lo = h.values, l.values
    n = len(hi)
    out = np.full(n, np.nan)
    if n < 3:
        return pd.Series(out, index=h.index)
    bull = True
    af = step
    ep = hi[0]
    sar = lo[0]
    out[0] = sar
    for i in range(1, n):
        sar = sar + af * (ep - sar)
        if bull:
            sar = min(sar, lo[i - 1], lo[i - 2] if i > 1 else lo[i - 1])
            if lo[i] < sar:
                bull, sar, ep, af = False, ep, lo[i], step
            elif hi[i] > ep:
                ep, af = hi[i], min(af + step, max_step)
        else:
            sar = max(sar, hi[i - 1], hi[i - 2] if i > 1 else hi[i - 1])
            if hi[i] > sar:
                bull, sar, ep, af = True, ep, hi[i], step
            elif lo[i] < ep:
                ep, af = lo[i], min(af + step, max_step)
        out[i] = sar
    return pd.Series(out, index=h.index)


def _supertrend(h, l, c, n=10, mult=3.0):
    atr = rma(true_range(h, l, c), n)
    hl2 = (h + l) / 2
    ub = (hl2 + mult * atr).values
    lb = (hl2 - mult * atr).values
    cv = c.values
    m = len(cv)
    fub, flb = ub.copy(), lb.copy()
    st = np.full(m, np.nan)
    direction = np.full(m, np.nan)
    for i in range(1, m):
        if np.isnan(ub[i]) or np.isnan(ub[i - 1]):
            continue
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or cv[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or cv[i - 1] < flb[i - 1]) else flb[i - 1]
        if np.isnan(st[i - 1]):
            d = 1 if cv[i] >= flb[i] else -1
        elif st[i - 1] == fub[i - 1]:
            d = -1 if cv[i] <= fub[i] else 1
        else:
            d = 1 if cv[i] >= flb[i] else -1
        direction[i] = d
        st[i] = flb[i] if d == 1 else fub[i]
    return pd.Series(st, index=c.index), pd.Series(direction, index=c.index)


# ---------------------------------------------------------------- builder
def _build(df):
    o, h, l, c = df["Open"], df["High"], df["Low"], df["Close"]
    v = df["Volume"] if "Volume" in df else pd.Series(np.nan, index=df.index)
    f = {}
    tr = true_range(h, l, c)
    tp = (h + l + c) / 3

    # --- Moving averages (44)
    for n in (5, 10, 20, 30, 50, 100, 200):
        f[f"SMA_{n}"] = sma(c, n)
        f[f"EMA_{n}"] = ema(c, n)
    for n in (5, 10, 20, 50):
        f[f"WMA_{n}"] = wma(c, n)
    for n in (9, 21, 50):
        f[f"HMA_{n}"] = hma(c, n)
    for n in (10, 20, 50):
        e1 = ema(c, n)
        e2 = ema(e1, n)
        e3 = ema(e2, n)
        f[f"DEMA_{n}"] = 2 * e1 - e2
        f[f"TEMA_{n}"] = 3 * e1 - 3 * e2 + e3
    for n in (10, 20, 50):
        f[f"VWMA_{n}"] = (c * v).rolling(n, min_periods=n).sum() / v.rolling(n, min_periods=n).sum()
    for n in (20, 50, 200):
        f[f"PCT_VS_SMA_{n}"] = (c / sma(c, n) - 1) * 100
    f["SMA_5_20_DIFF"] = f["SMA_5"] - f["SMA_20"]
    f["SMA_20_50_DIFF"] = f["SMA_20"] - f["SMA_50"]
    f["SMA_50_200_DIFF"] = f["SMA_50"] - f["SMA_200"]
    f["EMA_12_26_DIFF"] = ema(c, 12) - ema(c, 26)

    # --- Momentum
    for n in (7, 9, 14, 21, 28):
        d = c.diff()
        rs = rma(d.clip(lower=0), n) / rma((-d).clip(lower=0), n).replace(0, np.nan)
        f[f"RSI_{n}"] = 100 - 100 / (1 + rs)
    for k, dd, sm in ((5, 3, 3), (9, 3, 3), (14, 3, 3), (21, 5, 5)):
        ll, hh = l.rolling(k, min_periods=k).min(), h.rolling(k, min_periods=k).max()
        pk = 100 * (c - ll) / (hh - ll).replace(0, np.nan)
        pk = sma(pk, sm) if sm > 1 else pk
        f[f"STOCH_K_{k}_{dd}"] = pk
        f[f"STOCH_D_{k}_{dd}"] = sma(pk, dd)
    d = c.diff()
    rsi14 = f["RSI_14"]
    lo_r, hi_r = rsi14.rolling(14, min_periods=14).min(), rsi14.rolling(14, min_periods=14).max()
    srsi = (rsi14 - lo_r) / (hi_r - lo_r).replace(0, np.nan)
    f["STOCHRSI_K"] = sma(srsi, 3) * 100
    f["STOCHRSI_D"] = sma(f["STOCHRSI_K"], 3)
    for fa, sl, sg in ((12, 26, 9), (5, 35, 5), (8, 17, 9)):
        m = ema(c, fa) - ema(c, sl)
        s = ema(m, sg)
        f[f"MACD_{fa}_{sl}_{sg}"] = m
        f[f"MACD_SIGNAL_{fa}_{sl}_{sg}"] = s
        f[f"MACD_HIST_{fa}_{sl}_{sg}"] = m - s
    ppo = (ema(c, 12) - ema(c, 26)) / ema(c, 26) * 100
    f["PPO"] = ppo
    f["PPO_SIGNAL"] = ema(ppo, 9)
    f["PPO_HIST"] = ppo - f["PPO_SIGNAL"]
    for n in (5, 10, 14, 20, 50):
        f[f"ROC_{n}"] = c.pct_change(n) * 100
    for n in (5, 10, 20):
        f[f"MOM_{n}"] = c.diff(n)
    for n in (14, 20, 50):
        mad = tp.rolling(n, min_periods=n).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
        f[f"CCI_{n}"] = (tp - sma(tp, n)) / (0.015 * mad.replace(0, np.nan))
    for n in (7, 14, 28):
        hh, ll = h.rolling(n, min_periods=n).max(), l.rolling(n, min_periods=n).min()
        f[f"WILLR_{n}"] = -100 * (hh - c) / (hh - ll).replace(0, np.nan)
    for n in (9, 14):
        up = d.clip(lower=0).rolling(n, min_periods=n).sum()
        dn = (-d).clip(lower=0).rolling(n, min_periods=n).sum()
        f[f"CMO_{n}"] = 100 * (up - dn) / (up + dn).replace(0, np.nan)
    for n in (9, 15):
        t = ema(ema(ema(np.log(c), n), n), n)
        f[f"TRIX_{n}"] = t.diff() * 100
    bp = c - pd.concat([l, c.shift(1)], axis=1).min(axis=1)
    trr = pd.concat([h, c.shift(1)], axis=1).max(axis=1) - pd.concat([l, c.shift(1)], axis=1).min(axis=1)
    a1 = bp.rolling(7).sum() / trr.rolling(7).sum()
    a2 = bp.rolling(14).sum() / trr.rolling(14).sum()
    a3 = bp.rolling(28).sum() / trr.rolling(28).sum()
    f["ULTOSC"] = 100 * (4 * a1 + 2 * a2 + a3) / 7
    f["AO"] = sma((h + l) / 2, 5) - sma((h + l) / 2, 34)
    f["DPO_20"] = c.shift(11) - sma(c, 20)

    # --- Volatility
    f["TRUE_RANGE"] = tr
    for n in (7, 14, 21):
        f[f"ATR_{n}"] = rma(tr, n)
    f["NATR_14"] = f["ATR_14"] / c * 100
    for n, k, tag in ((20, 2.0, "20_2"), (10, 1.5, "10_1.5")):
        mid, sd = sma(c, n), c.rolling(n, min_periods=n).std(ddof=0)
        up, lo = mid + k * sd, mid - k * sd
        f[f"BB_UPPER_{tag}"], f[f"BB_MID_{tag}"], f[f"BB_LOWER_{tag}"] = up, mid, lo
        f[f"BB_WIDTH_{tag}"] = (up - lo) / mid * 100
        f[f"BB_PCTB_{tag}"] = (c - lo) / (up - lo).replace(0, np.nan)
    mid, sd = sma(c, 50), c.rolling(50, min_periods=50).std(ddof=0)
    f["BB_UPPER_50_2"], f["BB_LOWER_50_2"] = mid + 2 * sd, mid - 2 * sd
    f["BB_PCTB_50_2"] = (c - f["BB_LOWER_50_2"]) / (f["BB_UPPER_50_2"] - f["BB_LOWER_50_2"]).replace(0, np.nan)
    kmid = ema(c, 20)
    f["KC_UPPER"], f["KC_MID"], f["KC_LOWER"] = kmid + 1.5 * rma(tr, 10), kmid, kmid - 1.5 * rma(tr, 10)
    for n in (10, 20, 55):
        up, lo = h.rolling(n, min_periods=n).max(), l.rolling(n, min_periods=n).min()
        f[f"DONCH_UPPER_{n}"], f[f"DONCH_LOWER_{n}"], f[f"DONCH_MID_{n}"] = up, lo, (up + lo) / 2

    # --- Trend strength
    up_m, dn_m = h.diff(), -l.diff()
    pdm = pd.Series(np.where((up_m > dn_m) & (up_m > 0), up_m, 0.0), index=c.index)
    ndm = pd.Series(np.where((dn_m > up_m) & (dn_m > 0), dn_m, 0.0), index=c.index)
    for n in (7, 14, 21):
        atr_n = rma(tr, n)
        pdi = 100 * rma(pdm, n) / atr_n
        ndi = 100 * rma(ndm, n) / atr_n
        dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
        f[f"ADX_{n}"] = rma(dx, n)
        if n == 14:
            f["PLUS_DI_14"], f["MINUS_DI_14"] = pdi, ndi
    for n in (14, 25):
        idx_h = h.rolling(n + 1, min_periods=n + 1).apply(lambda x: n - np.argmax(x), raw=True)
        idx_l = l.rolling(n + 1, min_periods=n + 1).apply(lambda x: n - np.argmin(x), raw=True)
        f[f"AROON_UP_{n}"] = 100 * (n - idx_h) / n
        f[f"AROON_DOWN_{n}"] = 100 * (n - idx_l) / n
        f[f"AROON_OSC_{n}"] = f[f"AROON_UP_{n}"] - f[f"AROON_DOWN_{n}"]
    vmp = (h - l.shift(1)).abs().rolling(14).sum()
    vmm = (l - h.shift(1)).abs().rolling(14).sum()
    trs = tr.rolling(14).sum()
    f["VORTEX_PLUS_14"], f["VORTEX_MINUS_14"] = vmp / trs, vmm / trs
    t9, k26 = (h.rolling(9).max() + l.rolling(9).min()) / 2, (h.rolling(26).max() + l.rolling(26).min()) / 2
    f["ICHI_TENKAN"], f["ICHI_KIJUN"] = t9, k26
    f["ICHI_SENKOU_A"] = ((t9 + k26) / 2).shift(26)
    f["ICHI_SENKOU_B"] = ((h.rolling(52).max() + l.rolling(52).min()) / 2).shift(26)
    f["PSAR"] = _psar(h, l)
    f["SUPERTREND_10_3"], f["SUPERTREND_DIR_10_3"] = _supertrend(h, l, c)

    # --- Volume
    f["OBV"] = (np.sign(d).fillna(0) * v).cumsum()
    clv = ((c - l) - (h - c)) / (h - l).replace(0, np.nan)
    f["ADL"] = (clv.fillna(0) * v).cumsum()
    f["CMF_20"] = (clv * v).rolling(20).sum() / v.rolling(20).sum()
    mf = tp * v
    pos = mf.where(tp > tp.shift(1), 0.0).rolling(14).sum()
    neg = mf.where(tp < tp.shift(1), 0.0).rolling(14).sum()
    f["MFI_14"] = 100 - 100 / (1 + pos / neg.replace(0, np.nan))
    fi = d * v
    f["FORCE_2"], f["FORCE_13"] = ema(fi, 2), ema(fi, 13)
    emv = ((h + l) / 2).diff() / ((v / 1e6) / (h - l).replace(0, np.nan))
    f["EOM_14"] = sma(emv, 14)
    day = pd.Series(pd.DatetimeIndex(df.index).date, index=df.index) if isinstance(df.index, pd.DatetimeIndex) else pd.Series(0, index=df.index)
    f["VWAP_DAY"] = (tp * v).groupby(day).cumsum() / v.groupby(day).cumsum()
    f["VPT"] = (c.pct_change() * v).cumsum()
    for n in (5, 20, 50):
        f[f"VOL_SMA_{n}"] = sma(v, n)
    f["VOL_RATIO_20"] = v / f["VOL_SMA_20"]

    # --- Statistik
    r1 = c.pct_change()
    for n in (10, 20, 50):
        f[f"RET_STD_{n}"] = r1.rolling(n, min_periods=n).std()
    f["ZSCORE_20"] = (c - sma(c, 20)) / c.rolling(20, min_periods=20).std(ddof=0).replace(0, np.nan)
    f["SKEW_20"], f["KURT_20"] = r1.rolling(20).skew(), r1.rolling(20).kurt()
    for n in (10, 20, 50):
        sl, r2 = _linreg(c, n)
        f[f"LINREG_SLOPE_{n}"] = sl
        if n == 20:
            f["LINREG_R2_20"] = r2
    for n in (20, 50):
        f[f"PCT_FROM_HIGH_{n}"] = (c / h.rolling(n, min_periods=n).max() - 1) * 100
        f[f"PCT_FROM_LOW_{n}"] = (c / l.rolling(n, min_periods=n).min() - 1) * 100

    # --- Return
    for n in (1, 3, 5, 10, 20):
        f[f"RET_{n}"] = c.pct_change(n) * 100
    f["LOG_RET_1"] = np.log(c / c.shift(1))

    # --- Candle
    body = (c - o).abs()
    rng = (h - l)
    f["CANDLE_BODY"] = c - o
    f["CANDLE_UPPER_WICK"] = h - pd.concat([o, c], axis=1).max(axis=1)
    f["CANDLE_LOWER_WICK"] = pd.concat([o, c], axis=1).min(axis=1) - l
    f["CANDLE_RANGE"] = rng
    f["CANDLE_BODY_PCT"] = body / rng.replace(0, np.nan) * 100
    f["GAP_PCT"] = (o / c.shift(1) - 1) * 100
    f["TYPICAL_PRICE"] = tp
    f["MEDIAN_PRICE"] = (h + l) / 2
    f["WEIGHTED_CLOSE"] = (h + l + 2 * c) / 4
    f["PAT_DOJI"] = (body <= 0.1 * rng).astype(float).where(rng > 0)
    lw, uw = f["CANDLE_LOWER_WICK"], f["CANDLE_UPPER_WICK"]
    f["PAT_HAMMER"] = ((lw >= 2 * body) & (uw <= body) & (rng > 0)).astype(float)
    f["PAT_BULL_ENGULF"] = ((c > o) & (c.shift(1) < o.shift(1)) & (c >= o.shift(1)) & (o <= c.shift(1))).astype(float)
    f["PAT_BEAR_ENGULF"] = ((c < o) & (c.shift(1) > o.shift(1)) & (c <= o.shift(1)) & (o >= c.shift(1))).astype(float)
    f["PAT_INSIDE_BAR"] = ((h < h.shift(1)) & (l > l.shift(1))).astype(float)
    return f


def compute_indicators(df):
    """df: kolom Open/High/Low/Close[/Volume], urut waktu naik. Return DataFrame indikator."""
    df = df.copy()
    for col in ("Open", "High", "Low", "Close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "Volume" in df:
        df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce")
    with np.errstate(all="ignore"):
        feats = _build(df)
    out = pd.DataFrame(feats, index=df.index)
    return out.replace([np.inf, -np.inf], np.nan)


def indicator_names():
    idx = pd.date_range("2026-01-01", periods=300, freq="5min")
    rng = np.random.default_rng(0)
    c = 100 + rng.normal(0, 1, 300).cumsum()
    df = pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": 1000}, index=idx)
    return list(compute_indicators(df).columns)
