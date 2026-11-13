"""坤寶存摺：每天自己從源頭抓市場資料，算長線燈號、衛星層四道濾網、總經戰術層、全天候核心配置。

策略規則參考 market-radar 的「核心／衛星／戰術」三層架構；資料全部自己抓，不讀別人算好的結果：
- 台股清單與產業別：證交所 ISIN 公開頁
- 台股本益比／殖利率／淨值比、月營收、官方收盤：證交所、櫃買中心 OpenAPI
- 日線、月線、基本面、美元匯率：Yahoo Finance（yfinance）
- 美股清單與 GICS 產業：維基百科 S&P 500／S&P 400
- 總經：FRED（連不上時殖利率曲線改用美國財政部）

用法：python market.py --market tw|us|macro|core|all [--limit 50]
輸出：data/tw.json、data/us.json、data/macro.json、data/core.json、data/summary.json
"""
import argparse
import io
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

TPE = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("KB_DATA_DIR") or os.path.join(ROOT, "data")
CACHE = os.path.join(DATA, "cache")
LIQ_MIN = {"tw": 30_000_000, "us": 10_000_000}   # 近 20 日平均成交額門檻（台幣／美元）
FUND_MAX_AGE = 7                                 # 基本面每檔最多 7 天更新一次
FUND_CAP = int(os.environ.get("KB_FUND_CAP", "700"))  # 每次最多更新幾檔基本面（避免被 Yahoo 限流）
UA = {"User-Agent": "Mozilla/5.0 (compatible; kunbao-ledger)"}


# ───────────────────────── 小工具 ─────────────────────────
def log(*a):
    print(datetime.now(TPE).strftime("%H:%M:%S"), *a, flush=True)


def http_get(url, tries=3, timeout=40):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa
            last = e
            time.sleep(3 * (i + 1))
    raise last


def to_num(x):
    if x is None:
        return None
    try:
        s = str(x).replace(",", "").replace("%", "").strip()
        if s in ("", "-", "--", "N/A", "nan", "None"):
            return None
        v = float(s)
        return None if math.isnan(v) or math.isinf(v) else v
    except Exception:  # noqa
        return None


def rnd(x, n=2):
    if x is None:
        return None
    try:
        if isinstance(x, (float, np.floating)) and (math.isnan(x) or math.isinf(x)):
            return None
        return round(float(x), n)
    except Exception:  # noqa
        return None


def lin_down(x, good, bad):
    """越接近 good 分數越高（100），越接近 bad 越低（0）"""
    if x is None:
        return None
    try:
        if math.isnan(x):
            return None
    except TypeError:
        return None
    if good == bad:
        return 50.0
    t = (x - bad) / (good - bad)
    return float(max(0.0, min(1.0, t)) * 100)


def mean_ok(vals):
    v = [x for x in vals if x is not None]
    return float(sum(v) / len(v)) if v else None


def wmean_ok(pairs):
    p = [(v, w) for v, w in pairs if v is not None]
    sw = sum(w for _, w in p)
    return float(sum(v * w for v, w in p) / sw) if sw else None


def light_of(v):
    if v is None:
        return None
    return "cheap" if v >= 65 else "exp" if v <= 35 else "fair"


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def find_key(d, *groups):
    """在 dict 的鍵裡找第一個包含任一關鍵字的（OpenAPI 欄位名稱有時是中文、有時是英文）"""
    for g in groups:
        for k in d:
            if any(w.lower() in str(k).lower() for w in g):
                return k
    return None


def roc_to_ymd(s):
    s = re.sub(r"\D", "", str(s or ""))
    if len(s) == 7:
        return f"{int(s[:3]) + 1911}{s[3:]}"
    if len(s) == 8:
        return s
    return None


# ───────────────────────── Yahoo 下載 ─────────────────────────
def yf_download(tickers, period, interval, chunk=100, pause=2.0):
    """分批下載，回傳 {欄位: DataFrame(index=日期, columns=ticker)}"""
    import yfinance as yf
    out = {"Close": [], "High": [], "Low": [], "Volume": []}
    tickers = list(dict.fromkeys(tickers))
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        df = None
        for attempt in range(3):
            try:
                df = yf.download(part, period=period, interval=interval, auto_adjust=True,
                                 group_by="column", threads=True, progress=False)
                break
            except Exception as e:  # noqa
                log("  下載失敗重試", attempt + 1, e)
                time.sleep(10 * (attempt + 1))
        if df is None or df.empty:
            log(f"  {interval} 第 {i // chunk + 1} 批沒有資料")
            continue
        for fld in out:
            if isinstance(df.columns, pd.MultiIndex):
                if fld in df.columns.get_level_values(0):
                    sub = df[fld]
                    if isinstance(sub, pd.Series):
                        sub = sub.to_frame(part[0])
                    out[fld].append(sub)
            elif fld in df.columns and len(part) == 1:
                out[fld].append(df[[fld]].rename(columns={fld: part[0]}))
        log(f"  {interval} 第 {i // chunk + 1}/{math.ceil(len(tickers) / chunk)} 批")
        time.sleep(pause)
    res = {}
    for fld, lst in out.items():
        if lst:
            d = pd.concat(lst, axis=1)
            d = d.loc[:, ~d.columns.duplicated()]
            idx = pd.to_datetime(d.index)
            d.index = idx.tz_localize(None) if getattr(idx, "tz", None) else idx
            res[fld] = d.sort_index()
        else:
            res[fld] = pd.DataFrame()
    return res


def col(frames, fld, t):
    df = frames.get(fld)
    if df is None or df.empty or t not in df.columns:
        return None
    s = df[t].dropna()
    return s if len(s) else None


# ───────────────────────── 基本面（Yahoo，滾動快取） ─────────────────────────
FUND_KEYS = {"trailingPE": "pe", "priceToBook": "pb", "returnOnEquity": "roe", "debtToEquity": "de",
             "revenueGrowth": "rg", "profitMargins": "pm", "freeCashflow": "fcf", "marketCap": "mcap",
             "trailingEps": "eps"}


def _quarters(tk):
    """最近 6 季：[[年月, 營收, 毛利, EPS], ...]（舊到新）"""
    try:
        q = tk.quarterly_income_stmt
    except Exception:  # noqa
        return []
    if q is None or q.empty:
        return []

    def row(*names):
        for n in names:
            if n in q.index:
                return q.loc[n]
        return None
    rows = [row("Total Revenue", "Operating Revenue"), row("Gross Profit"), row("Diluted EPS", "Basic EPS")]
    out = []
    for c in sorted(q.columns)[-6:]:
        vals = [to_num(r[c]) if r is not None else None for r in rows]
        if any(v is not None for v in vals):
            out.append([pd.Timestamp(c).strftime("%Y-%m")] + vals)
    return out


def _fetch_fund(t):
    import yfinance as yf
    try:
        tk = yf.Ticker(t)
        info = tk.info or {}
    except Exception as e:  # noqa
        return t, None, str(e)
    if len(info) < 5:
        return t, None, "empty"
    d = {v: to_num(info.get(k)) for k, v in FUND_KEYS.items()}
    d["qs"] = _quarters(tk)
    return t, d, None


def update_fund(market, tickers, today):
    path = os.path.join(CACHE, f"fund_{market}.json")
    cache = load_json(path, {})
    cutoff = (today - timedelta(days=FUND_MAX_AGE)).strftime("%Y-%m-%d")
    stale = [t for t in tickers if t not in cache or cache[t].get("_d", "") < cutoff]
    stale.sort(key=lambda t: cache.get(t, {}).get("_d", ""))
    todo = stale[:FUND_CAP]
    log(f"基本面 {market}：快取 {len(cache)} 檔，這次更新 {len(todo)} 檔")
    fails = done = 0
    with ThreadPoolExecutor(max_workers=2) as ex:
        for i in range(0, len(todo), 40):
            got = list(ex.map(_fetch_fund, todo[i:i + 40]))
            bad = 0
            for t, d, _ in got:
                if d:
                    d["_d"] = today.strftime("%Y-%m-%d")
                    cache[t] = d
                    done += 1
                else:
                    bad += 1
            fails = fails + bad if bad == len(got) else 0
            if fails >= 80:
                log("  基本面連續失敗（可能被 Yahoo 限流），下次再補")
                break
            time.sleep(2.5 if bad else 1.5)
    keep = set(tickers)
    cache = {k: v for k, v in cache.items() if k in keep}
    save_json(path, cache)
    log(f"  基本面更新 {done} 檔")
    return cache


def is_fin(sec):
    s = str(sec or "")
    return any(w in s for w in ("金融", "保險", "銀行", "證券"))


def quality(fd, sec, rev_yoy=None):
    """品質分數：ROE、淨利率、營收成長、負債比、自由現金流"""
    fd = fd or {}
    rg = rev_yoy if rev_yoy is not None else fd.get("rg")
    parts = [lin_down(fd.get("roe"), 0.20, 0.0), lin_down(fd.get("pm"), 0.15, -0.05), lin_down(rg, 0.20, -0.20)]
    if not is_fin(sec):
        parts.append(lin_down(fd.get("de"), 30, 200))
        if fd.get("fcf") is not None:
            parts.append(100.0 if fd["fcf"] > 0 else 0.0)
    got = [p for p in parts if p is not None]
    q = float(sum(got) / len(got)) if len(got) >= 2 else None
    warn = []
    if (fd.get("eps") is not None and fd["eps"] < 0) or (fd.get("pm") is not None and fd["pm"] < 0):
        warn.append("虧損")
    if rg is not None and rg <= -0.15:
        warn.append(f"營收衰退{round(rg * 100)}%")
    if not is_fin(sec) and fd.get("de") is not None and fd["de"] >= 200:
        warn.append("高負債")
    if not is_fin(sec) and fd.get("fcf") is not None and fd["fcf"] < 0:
        warn.append("自由現金流為負")
    return q, warn


def fin_trend(fd, rev_yoy=None):
    """營收年增、毛利率變化、EPS 年增：兩項以上變好且沒有變差＝up；兩項以上變差＝down"""
    qs = [q for q in ((fd or {}).get("qs") or []) if q[1]]
    gmd = epsg = revg = None
    if qs:
        last = qs[-1]
        ya = qs[-5] if len(qs) >= 5 else None
        gm = lambda q: q[2] / q[1] if q[2] is not None and q[1] else None  # noqa: E731
        base = ya if ya is not None and gm(ya) is not None else (qs[-2] if len(qs) >= 2 else None)
        if base is not None and gm(last) is not None and gm(base) is not None:
            gmd = gm(last) - gm(base)
        if ya is not None and last[3] is not None and ya[3]:
            epsg = (last[3] - ya[3]) / abs(ya[3])
        if ya is not None and ya[1]:
            revg = last[1] / ya[1] - 1
    if rev_yoy is not None:
        revg = rev_yoy
    signs = []
    if revg is not None:
        signs.append(1 if revg > 0.02 else -1 if revg < -0.02 else 0)
    if gmd is not None:
        signs.append(1 if gmd > 0.005 else -1 if gmd < -0.005 else 0)
    if epsg is not None:
        signs.append(1 if epsg > 0.05 else -1 if epsg < -0.05 else 0)
    fin = None
    if len(signs) >= 2:
        up, dn = signs.count(1), signs.count(-1)
        fin = "up" if up >= 2 and dn == 0 else "down" if dn >= 2 else "flat"
    return fin, revg


def valuation(pe, pb, fcfy, dy, sec, pe_med, pb_med):
    """估值：跟同族群中位數比（0.6 倍＝100 分、1.6 倍＝0 分），加上自由現金流殖利率、現金殖利率"""
    parts = []
    m = pe_med.get(sec) or pe_med.get(None)
    if pe is not None and pe > 0 and m:
        parts.append(lin_down(pe / m, 0.6, 1.6))
    m = pb_med.get(sec) or pb_med.get(None)
    if pb is not None and pb > 0 and m:
        parts.append(lin_down(pb / m, 0.6, 1.6))
    if fcfy is not None and not is_fin(sec):
        parts.append(lin_down(fcfy, 0.08, 0.0))
    if dy is not None:
        parts.append(lin_down(dy, 6.0, 0.5))
    got = [p for p in parts if p is not None]
    return float(sum(got) / len(got)) if len(got) >= 2 else None


def sector_medians(pairs):
    by = {}
    for sec, v in pairs:
        if v is not None and v > 0:
            by.setdefault(sec, []).append(v)
            by.setdefault(None, []).append(v)
    return {k: float(np.median(v)) for k, v in by.items() if len(v) >= 3}


# ───────────────────────── 價格指標 ─────────────────────────
def kd_last(h, l, c, n=9):
    """台灣常用 KD(9,3,3)，回傳最後一個 K 值"""
    hh = h.rolling(n, min_periods=n).max().values
    ll = l.rolling(n, min_periods=n).min().values
    cv = c.values
    k = d = 50.0
    last = None
    for i in range(len(cv)):
        if np.isnan(hh[i]) or np.isnan(ll[i]) or np.isnan(cv[i]):
            continue
        rng = hh[i] - ll[i]
        rsv = 50.0 if rng == 0 else (cv[i] - ll[i]) / rng * 100
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
        last = k
    return last


def metrics(dc, dv, mc, mh, ml):
    """日收盤、成交量、月收盤／高／低 → 價格位置與買點指標"""
    dc = dc.dropna()
    if len(dc) < 30:
        return None
    dv = (dv if dv is not None else pd.Series(0, index=dc.index)).reindex(dc.index).fillna(0)
    px = float(dc.iloc[-1])
    if px <= 0:
        return None
    r = {"px": px}
    r["ch"] = px / float(dc.iloc[-2]) - 1 if len(dc) >= 2 and dc.iloc[-2] else None
    r["r1w"] = px / float(dc.iloc[-6]) - 1 if len(dc) >= 6 else None
    r["r1m"] = px / float(dc.iloc[-22]) - 1 if len(dc) >= 22 else None
    r["bias"] = px / float(dc.iloc[-240:].mean()) - 1 if len(dc) >= 240 else None
    tv = dc * dv
    r["tv5"] = float(tv.iloc[-5:].mean())
    r["tv20"] = float(tv.iloc[-20:].mean()) if len(tv) >= 20 else float(tv.mean())
    r["tv_prev20"] = float(tv.iloc[-25:-5].mean()) if len(tv) >= 25 else None
    tv60 = float(tv.iloc[-60:].mean()) if len(tv) >= 60 else None
    r["vr"] = r["tv5"] / tv60 if tv60 else None
    r["dd52"] = px / float(dc.iloc[-250:].max()) - 1
    r["hi52"] = float(dc.iloc[-250:].max())
    r["ma240"] = float(dc.iloc[-240:].mean()) if len(dc) >= 240 else None
    r["wma"] = r["wma_up"] = None
    wk = dc.resample("W-FRI").last().dropna()
    if len(wk) >= 57:
        ma = wk.rolling(52).mean()
        r["wma"] = px / float(ma.iloc[-1]) - 1
        r["wma_up"] = bool(ma.iloc[-1] > ma.iloc[-5])
        r["wma_v"] = float(ma.iloc[-1])
    k = dd5 = pos5 = ma60 = None
    r["macd_x"] = None
    if mc is not None and len(mc.dropna()) >= 2:
        mc = mc.dropna().copy()
        mh2 = (mh if mh is not None else mc).reindex(mc.index).fillna(mc)
        ml2 = (ml if ml is not None else mc).reindex(mc.index).fillna(mc)
        mc.iloc[-1] = px
        mh2.iloc[-1] = max(float(mh2.iloc[-1]), px)
        ml2.iloc[-1] = min(float(ml2.iloc[-1]), px)
        partial = (mc.index[-1].year, mc.index[-1].month) == (dc.index[-1].year, dc.index[-1].month)
        end = -1 if partial else None   # 月 KD、MACD 只用已收完的月份
        done_c = mc.iloc[:end]
        if len(done_c) >= 12:
            k = kd_last(mh2.iloc[:end], ml2.iloc[:end], done_c)
        if len(done_c) >= 40:
            dif = done_c.ewm(span=12, adjust=False).mean() - done_c.ewm(span=26, adjust=False).mean()
            h_ = (dif - dif.ewm(span=9, adjust=False).mean()).values
            r["macd_x"] = bool(h_[-1] > 0 and (h_[-2] <= 0 or h_[-3] <= 0))
        last60 = mc.iloc[-60:]
        hi = float(mh2.iloc[-60:].max())
        dd5 = px / hi - 1 if hi > 0 else None
        if len(last60) >= 24:
            pos5 = float((last60 < px).sum()) / (len(last60) - 1)
        if len(mc) >= 60:
            ma60 = float(mc.iloc[-60:].mean())
    r.update({"k": k, "dd5": dd5, "pos5": pos5, "below60": (px < ma60) if ma60 else None})
    r["value"] = mean_ok([lin_down(r["bias"], -0.30, 0.30), lin_down(k, 20, 80),
                          lin_down(dd5, -0.50, 0.0), lin_down(pos5, 0.0, 1.0)])
    return r


def buy_triggers(m):
    trig = []
    if m.get("wma") is not None and 0 <= m["wma"] <= 0.04 and m.get("wma_up"):
        trig.append("回測週MA52不破")
    if m.get("dd52") is not None and m["dd52"] <= -0.20:
        trig.append(f"距52週高點{round(m['dd52'] * 100)}%")
    if m.get("macd_x"):
        trig.append("月線MACD黃金交叉")
    return trig


def tags_of(m):
    t = []
    if m.get("below60"):
        t.append("跌破5年線")
    elif m.get("bias") is not None and m["bias"] < 0:
        t.append("跌破年線")
    if m.get("k") is not None and m["k"] < 25:
        t.append("月KD低檔")
    if m.get("dd5") is not None and m["dd5"] <= -0.30:
        t.append(f"回撤{round(m['dd5'] * 100)}%")
    if m.get("vr") and m["vr"] >= 1.5:
        t.append("量能放大")
    return t


def long_term(m, q, val, is_etf, warn):
    v = m["value"]
    lt = v if (is_etf or (q is None and val is None)) else wmean_ok([(v, 0.4), (val, 0.3), (q, 0.3)])
    light = light_of(lt)
    if not is_etf and light in ("cheap", "fair") and ((q is not None and q < 35) or "虧損" in warn):
        light, lt = "trap", min(lt, 50.0)
    return lt, light


def sector_stats(rows, close_df, tick_sec, weights):
    """族群：近 5 日成交比重變化（資金流入）、近 4 週相對大盤強弱 rs4"""
    df = pd.DataFrame([r for r in rows if r.get("sec")])
    out = {}
    if df.empty:
        return out
    tot5, totp = df["tv5"].sum(), df["tv_prev20"].sum()
    for name, sub in df.groupby("sec"):
        if len(sub) < 3:
            continue
        share = sub["tv5"].sum() / tot5 if tot5 else None
        sharep = sub["tv_prev20"].sum() / totp if totp else None
        out[name] = {"name": name, "n": int(len(sub)), "share": rnd(share, 4),
                     "share_chg": rnd(share - sharep, 4) if share is not None and sharep is not None else None, "rs4": None}
    cols = [t for t in tick_sec if t in close_df.columns]
    if cols:
        wk = close_df[cols].resample("W-FRI").last().pct_change(fill_method=None).iloc[-4:]
        w = pd.Series({t: max(weights.get(t) or 0, 0) for t in cols})

        def wavg(r, ww):
            return (r.fillna(0) * ww).sum(axis=1) / (r.notna() * ww).sum(axis=1).replace(0, np.nan)
        mkt = wavg(wk, w)
        secs = pd.Series({t: tick_sec[t] for t in cols})
        for name in secs.unique():
            cs = secs[secs == name].index
            if name in out and len(cs) >= 3:
                sr = wavg(wk[cs], w[cs])
                if sr.notna().all() and mkt.notna().all():
                    out[name]["rs4"] = rnd(float((1 + sr).prod() - (1 + mkt).prod()), 4)
    return out


def hot_sectors(secs):
    s = [x for x in secs.values() if x.get("rs4") is not None]
    s.sort(key=lambda x: -x["rs4"])
    return {x["name"] for x in s[:max(1, math.ceil(len(s) / 3))] if x["rs4"] > 0}


def build_rows(uni, tick, dly, mon, fund, extra_fn, market, today):
    """共用：每檔算指標、品質、估值、財報、燈號、資金分"""
    stocks = [u for u in uni if not u.get("etf")]
    pe_med = sector_medians([(u["sec"], extra_fn(u)["pe"]) for u in stocks])
    pb_med = sector_medians([(u["sec"], extra_fn(u)["pb"]) for u in stocks])
    rows, tick_sec, tick_w, fin_cnt = [], {}, {}, {"up": 0, "down": 0}
    for u in uni:
        t = tick[u["c"]]
        dc = col(dly, "Close", t)
        if dc is None:
            continue
        ex = extra_fn(u)
        if ex.get("close"):   # 官方收盤比 Yahoo 新就補上
            od, cp = ex["close"]
            if od > dc.index[-1]:
                dc = dc.copy()
                dc.loc[od] = cp
        m = metrics(dc, col(dly, "Volume", t), col(mon, "Close", t), col(mon, "High", t), col(mon, "Low", t))
        if not m:
            continue
        is_etf = bool(u.get("etf"))
        q = val = fin = None
        warn = []
        if not is_etf:
            fd = fund.get(t) or {}
            q, warn = quality(fd, u["sec"], ex.get("rev_cum"))
            fcfy = fd["fcf"] / fd["mcap"] if fd.get("fcf") is not None and fd.get("mcap") else None
            val = valuation(ex["pe"], ex["pb"], fcfy, ex.get("dy"), u["sec"], pe_med, pb_med)
            fin, _ = fin_trend(fd, ex.get("rev_yoy"))
            if fin in fin_cnt:
                fin_cnt[fin] += 1
            tick_sec[t], tick_w[t] = u["sec"], m["tv20"]
        lt, light = long_term(m, q, val, is_etf, warn)
        flow_parts = [lin_down(m["vr"], 1.6, 0.7)]
        if ex.get("rs") is not None:
            flow_parts.append(lin_down(ex["rs"], 0.10, -0.10))
        row = {"c": u["c"], "n": u["n"], "sec": u["sec"], "px": rnd(m["px"], 2), "ch": rnd(m["ch"], 4),
               "b": rnd(m["bias"], 4), "k": rnd(m["k"], 1), "dd": rnd(m["dd5"], 4), "pos": rnd(m["pos5"], 3),
               "v": rnd(m["value"], 1), "lt": rnd(lt, 1), "l": light, "f": rnd(mean_ok(flow_parts), 1),
               "q": rnd(q, 1), "val": rnd(val, 1), "fin": fin, "trig": buy_triggers(m),
               "liq": (m["tv20"] or 0) >= LIQ_MIN[market], "tags": tags_of(m) + warn,
               "lv": [rnd(m.get("wma_v"), 2), rnd(m.get("hi52"), 2), rnd(m.get("ma240"), 2)], "wu": m.get("wma_up"),
               "pe": rnd(ex.get("pe"), 1), "dy": rnd(ex.get("dy"), 2), "_tv5": m["tv5"], "_tvp": m["tv_prev20"],
               "_tv20": m["tv20"]}
        if is_etf:
            row["etf"] = True
        if ex.get("mk"):
            row["mk"] = ex["mk"]
        rows.append(row)
    secs = sector_stats([{"sec": r["sec"], "tv5": r["_tv5"], "tv_prev20": r["_tvp"]}
                         for r in rows if not r.get("etf") and r["_tvp"] is not None],
                        dly.get("Close", pd.DataFrame()), tick_sec, tick_w)
    for r in rows:   # 族群資金比重變化補進個股資金分（比重每增加 0.1 個百分點 +1 分，上下限 ±10）
        sc = (secs.get(r["sec"]) or {}).get("share_chg")
        if sc is not None and r["f"] is not None:
            r["f"] = rnd(max(0.0, min(100.0, r["f"] + max(-10.0, min(10.0, sc * 1000)))), 1)
        for k in ("_tv5", "_tvp", "_tv20"):
            r.pop(k, None)
    return rows, secs, fin_cnt


def picks_of(rows, secs):
    hot = hot_sectors(secs)
    p = [r for r in rows if not r.get("etf") and r["liq"] and r["sec"] in hot and (r["f"] or 0) >= 55
         and r["fin"] == "up" and r["trig"]]
    p.sort(key=lambda r: -(r["lt"] if r["lt"] is not None else -1))
    return [r["c"] for r in p], sorted(hot)


def bench_rows(items, dly, mon):
    out = []
    for t, name in items:
        dc = col(dly, "Close", t)
        if dc is None:
            continue
        m = metrics(dc, col(dly, "Volume", t), col(mon, "Close", t), col(mon, "High", t), col(mon, "Low", t))
        if m:
            out.append({"c": t.replace(".TW", ""), "n": name, "px": rnd(m["px"], 2), "ch": rnd(m["ch"], 4),
                        "b": rnd(m["bias"], 4), "k": rnd(m["k"], 1), "dd": rnd(m["dd5"], 4), "pos": rnd(m["pos5"], 3),
                        "lt": rnd(m["value"], 1), "l": light_of(m["value"]), "below52": (m["wma"] or 0) < 0 if m["wma"] is not None else None,
                        "lv": [rnd(m.get("wma_v"), 2), rnd(m.get("hi52"), 2), rnd(m.get("ma240"), 2)],
                        "etf": True})
    return out


# ───────────────────────── 台股 ─────────────────────────
ETF_EXCLUDE = re.compile(r"正2|反1|反向|槓桿|2倍|-1")


def tw_universe():
    cache = os.path.join(CACHE, "tw_universe.json")
    rows = []
    try:
        for mode, mk in ((2, "上市"), (4, "上櫃")):
            html = http_get(f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}").content.decode("cp950", errors="ignore")
            tb = pd.read_html(io.StringIO(html), header=0)[0]
            cols = list(tb.columns)
            c_code = next(c for c in cols if "代號" in str(c))
            c_cfi = next((c for c in cols if "CFI" in str(c)), None)
            c_ind = next((c for c in cols if "產業" in str(c)), None)
            for _, x in tb.iterrows():
                s = str(x[c_code])
                if "　" not in s:
                    continue
                code, name = [p.strip() for p in s.split("　", 1)]
                cfi = str(x[c_cfi]) if c_cfi else ""
                if cfi.startswith("ES") and code.isdigit() and len(code) == 4:
                    ind = str(x[c_ind]).strip() if c_ind and pd.notna(x[c_ind]) else ""
                    rows.append({"c": code, "n": name, "m": mk, "sec": ind})
                elif cfi.startswith("CE") and re.fullmatch(r"00\d{2,4}[AB]?", code) and not ETF_EXCLUDE.search(name):
                    rows.append({"c": code, "n": name, "m": mk, "sec": "債券ETF" if code.endswith("B") else "股票ETF", "etf": True})
            time.sleep(2)
        if len(rows) < 1000:
            raise RuntimeError(f"清單太少（{len(rows)}）")
        save_json(cache, rows)
        log(f"台股清單 {len(rows)} 檔")
        return rows
    except Exception as e:  # noqa
        old = load_json(cache, [])
        log("台股清單失敗，改用上次的：", e, len(old))
        if not old:
            raise
        return old


def tw_pe():
    out = {}
    for url, kc, kpe, kdy, kpb in (
            ("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL", ("code", "代號"), ("peratio", "本益比"), ("dividendyield", "殖利率"), ("pbratio", "淨值比")),
            ("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis", ("companycode", "code", "代號"), ("priceearning", "peratio", "本益比"), ("yieldratio", "yield", "殖利率"), ("pricebook", "pbratio", "淨值比"))):
        try:
            for x in http_get(url).json():
                code = x.get(find_key(x, kc))
                if code:
                    out[str(code).strip()] = (to_num(x.get(find_key(x, kpe))), to_num(x.get(find_key(x, kdy))), to_num(x.get(find_key(x, kpb))))
        except Exception as e:  # noqa
            log("本益比失敗：", url, e)
    log(f"本益比 {len(out)} 檔")
    return out


def tw_revenue():
    """{code: (單月年增率, 累計年增率)}"""
    out = {}
    for url in ("https://openapi.twse.com.tw/v1/opendata/t187ap05_L", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"):
        try:
            for x in http_get(url).json():
                code = x.get(find_key(x, ("公司代號",), ("代號",)))
                yoy = to_num(x.get(find_key(x, ("去年同月增減",))))
                cum = to_num(x.get(find_key(x, ("前期比較增減",))))
                if code:
                    out[str(code).strip()] = (yoy / 100 if yoy is not None else None, cum / 100 if cum is not None else None)
        except Exception as e:  # noqa
            log("月營收失敗：", url, e)
    log(f"月營收 {len(out)} 檔")
    return out


def tw_official_close():
    out = {}
    try:
        for x in http_get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL").json():
            d, p = roc_to_ymd(x.get("Date")), to_num(x.get("ClosingPrice"))
            if p and d:
                out[str(x.get("Code", "")).strip()] = (pd.Timestamp(d), p)
    except Exception as e:  # noqa
        log("證交所收盤失敗：", e)
    try:
        for x in http_get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes").json():
            d, p = roc_to_ymd(x.get("Date")), to_num(x.get("Close"))
            code = str(x.get("SecuritiesCompanyCode", "")).strip()
            if p and d and code not in out:
                out[code] = (pd.Timestamp(d), p)
    except Exception as e:  # noqa
        log("櫃買收盤失敗：", e)
    log(f"官方收盤 {len(out)} 檔")
    return out


def run_tw(limit=None):
    today = datetime.now(TPE)
    uni = tw_universe()
    if limit:
        keep = ("2330", "2317", "2454", "1101", "0050", "006208", "00679B")
        uni = list({u["c"]: u for u in ([u for u in uni if u["c"] in keep] + [u for u in uni if not u.get("etf")][:limit])}.values())
    tick = {u["c"]: u["c"] + (".TW" if u["m"] == "上市" else ".TWO") for u in uni}
    bench = [("^TWII", "加權指數"), ("0050.TW", "元大台灣50"), ("006208.TW", "富邦台50")]
    tks = list(tick.values()) + [b[0] for b in bench]
    log(f"台股日線 {len(tks)} 檔")
    dly = yf_download(tks, "2y", "1d")
    log("台股月線")
    mon = yf_download(tks, "6y", "1mo")
    pe, rev, close = tw_pe(), tw_revenue(), tw_official_close()
    fund = update_fund("tw", [tick[u["c"]] for u in uni if not u.get("etf")], today)

    def extra(u):
        p, dy, pb = pe.get(u["c"], (None, None, None))
        fd = fund.get(tick[u["c"]]) or {}
        yoy, cum = rev.get(u["c"], (None, None))
        return {"pe": p if p is not None else fd.get("pe"), "pb": pb if pb is not None else fd.get("pb"), "dy": dy,
                "rev_yoy": yoy, "rev_cum": cum, "close": close.get(u["c"]), "mk": u["m"]}
    rows, secs, fin_cnt = build_rows(uni, tick, dly, mon, fund, extra, "tw", today)
    if len(rows) < 500 and not limit:
        raise RuntimeError(f"台股只算出 {len(rows)} 檔，資料不完整，這次不更新")
    picks, hot = picks_of(rows, secs)
    benches = bench_rows(bench, dly, mon)
    finish("tw", rows, secs, fin_cnt, picks, hot, benches)


# ───────────────────────── 美股 ─────────────────────────
GICS_ZH = {"Information Technology": "資訊科技", "Health Care": "醫療保健", "Financials": "金融",
           "Consumer Discretionary": "非必需消費", "Communication Services": "通訊服務", "Industrials": "工業",
           "Consumer Staples": "必需消費", "Energy": "能源", "Utilities": "公用事業", "Real Estate": "房地產",
           "Materials": "原物料"}
US_BENCH = [("^GSPC", "S&P 500"), ("^NDX", "那斯達克100"), ("IWM", "美國中小型股"), ("VT", "全球股市")]
US_ETF = [("VOO", "Vanguard S&P 500"), ("VTI", "Vanguard 全美股市"), ("QQQ", "Invesco QQQ"), ("VT", "Vanguard 全球股市"),
          ("SPY", "SPDR S&P 500"), ("TLT", "美國長期公債"), ("IEF", "美國中期公債"), ("BND", "美國總體債券"),
          ("GLD", "黃金"), ("DBC", "商品"), ("SCHD", "Schwab 高股息"), ("VXUS", "Vanguard 美國以外")]


def us_universe():
    cache = os.path.join(CACHE, "us_universe.json")
    rows = []
    try:
        for url in ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"):
            for tb in pd.read_html(io.StringIO(http_get(url).text)):
                cols = [str(c) for c in tb.columns]
                if "Symbol" in cols and "Security" in cols and len(tb) > 300:
                    cg = next((c for c in tb.columns if "Sector" in str(c)), None)
                    for _, x in tb.iterrows():
                        sym = str(x["Symbol"]).strip().replace(".", "-")
                        if sym and sym != "nan":
                            sec = str(x[cg]).strip() if cg else ""
                            rows.append({"c": sym, "n": str(x["Security"]).strip(), "sec": GICS_ZH.get(sec, sec)})
                    break
            time.sleep(1)
        rows = list({r["c"]: r for r in rows}.values())
        if len(rows) < 800:
            raise RuntimeError(f"清單太少（{len(rows)}）")
        save_json(cache, rows)
        log(f"美股清單 {len(rows)} 檔")
        return rows
    except Exception as e:  # noqa
        old = load_json(cache, [])
        log("美股清單失敗，改用上次的：", e, len(old))
        if not old:
            raise
        return old


def run_us(limit=None):
    today = datetime.now(TPE)
    uni = us_universe()
    if limit:
        uni = list({u["c"]: u for u in ([u for u in uni if u["c"] in ("AAPL", "MSFT", "NVDA", "JPM")] + uni[:limit])}.values())
    have = {u["c"] for u in uni}
    uni += [{"c": c, "n": n, "sec": "ETF", "etf": True} for c, n in US_ETF if c not in have]
    tick = {u["c"]: u["c"] for u in uni}
    tks = list(tick.values()) + [b[0] for b in US_BENCH] + ["SPY"]
    log(f"美股日線 {len(tks)} 檔")
    dly = yf_download(tks, "2y", "1d")
    log("美股月線")
    mon = yf_download(tks, "6y", "1mo")
    fund = update_fund("us", [u["c"] for u in uni if not u.get("etf")], today)
    spy = col(dly, "Close", "SPY")
    spy_r1m = float(spy.iloc[-1] / spy.iloc[-22] - 1) if spy is not None and len(spy) >= 22 else None

    def extra(u):
        fd = fund.get(u["c"]) or {}
        dc = col(dly, "Close", u["c"])
        r1m = float(dc.iloc[-1] / dc.iloc[-22] - 1) if dc is not None and len(dc) >= 22 else None
        return {"pe": fd.get("pe"), "pb": fd.get("pb"), "dy": None,
                "rs": (r1m - spy_r1m) if r1m is not None and spy_r1m is not None else None}
    rows, secs, fin_cnt = build_rows(uni, tick, dly, mon, fund, extra, "us", today)
    if len(rows) < 500 and not limit:
        raise RuntimeError(f"美股只算出 {len(rows)} 檔，資料不完整，這次不更新")
    picks, hot = picks_of(rows, secs)
    benches = bench_rows(US_BENCH, dly, mon)
    finish("us", rows, secs, fin_cnt, picks, hot, benches)


def finish(market, rows, secs, fin_cnt, picks, hot, benches):
    now = datetime.now(TPE)
    out = {"market": market, "updated": now.strftime("%Y-%m-%d %H:%M"), "scanned": len(rows),
           "sectors": sorted(secs.values(), key=lambda s: -(s["rs4"] if s["rs4"] is not None else -9)),
           "hot": hot, "picks": picks, "fin": fin_cnt, "benchmarks": benches, "stocks": rows}
    save_json(os.path.join(DATA, f"{market}.json"), out)
    log(f"{market} 完成：{len(rows)} 檔，熱門族群 {len(hot)} 個，四道全過 {len(picks)} 檔")


# ───────────────────────── 總經與戰術層 ─────────────────────────
FRED = [("curve", "T10Y2Y"), ("hy", "BAMLH0A0HYM2"), ("real", "DFII10")]
CYCLE = ["擴張初期", "擴張後期", "緊縮", "衰退"]


def fred_series(sid, start):
    key = os.environ.get("FRED_API_KEY")
    if key:
        j = http_get(f"https://api.stlouisfed.org/fred/series/observations?series_id={sid}&api_key={key}&file_type=json&observation_start={start}").json()
        s = pd.Series({pd.Timestamp(o["date"]): to_num(o["value"]) for o in j["observations"]}).dropna()
    else:
        df = pd.read_csv(io.StringIO(http_get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}").text))
        df.columns = ["date", "v"]
        s = pd.Series(pd.to_numeric(df["v"], errors="coerce").values, index=pd.to_datetime(df["date"])).dropna()
    if len(s) < 50:
        raise RuntimeError("資料太少")
    return s


def treasury_curve(start_year):
    """FRED 連不上時，用美國財政部每日殖利率算 10Y−2Y"""
    parts = []
    for y in range(start_year, datetime.now(TPE).year + 1):
        try:
            url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
                   f"{y}/all?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv")
            parts.append(pd.read_csv(io.StringIO(http_get(url).text)))
        except Exception as e:  # noqa
            log("財政部失敗：", y, e)
    if not parts:
        return None
    df = pd.concat(parts)
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date").sort_index()
    return (df["10 Yr"] - df["2 Yr"]).dropna() if {"10 Yr", "2 Yr"} <= set(df.columns) else None


def run_macro():
    now = datetime.now(TPE)
    start = (now - timedelta(days=365 * 4)).strftime("%Y-%m-%d")
    raw, src = {}, {}
    for key, sid in FRED:
        try:
            raw[key] = fred_series(sid, start)
            src[key] = "FRED"
        except Exception as e:  # noqa
            log("FRED 失敗：", sid, e)
        time.sleep(1)
    if "curve" not in raw:
        c = treasury_curve(int(start[:4]))
        if c is not None and len(c) > 300:
            raw["curve"], src["curve"] = c, "美國財政部"
    px = yf_download(["^TWII", "^GSPC", "XLK", "XLY", "XLI", "XLF", "XLB", "XLE", "XLP", "XLU", "XLV"], "5y", "1d", chunk=20, pause=1)
    close = px.get("Close", pd.DataFrame())

    why, stage = [], None
    c, h = raw.get("curve"), raw.get("hy")
    h = h if h is not None and len(h) > 70 else None
    inv6, h3 = False, None
    if c is not None and len(c) > 300:
        cl = float(c.iloc[-1])
        hl = float(h.iloc[-1]) if h is not None else 0.0
        h3 = float(hl - h.iloc[-64]) if h is not None else None
        inv24, inv6 = bool((c.iloc[-504:] < 0).any()), bool((c.iloc[-126:] < 0).any())
        why.append(f"10Y−2Y 殖利率差 {cl:+.2f}%{'（倒掛中）' if cl < 0 else ''}")
        why.append(f"高收益債利差 {hl:.2f}%，3 個月變化 {h3:+.2f}%" if h is not None else "高收益債利差沒有資料，只用殖利率曲線判斷")
        if h is not None and (hl >= 6 or h3 >= 1.5):
            stage = 3
        elif cl < 0:
            stage = 2
        elif inv24 or cl < 0.5 or (h is not None and hl >= 4.5):
            stage = 1
        else:
            stage = 0
        if "real" in raw:
            why.append(f"10 年期實質利率 {float(raw['real'].iloc[-1]):.2f}%")

    def grp(cols, n):
        cs = [x for x in cols if x in close]
        if not cs:
            return None
        sub = close[cs].dropna()
        return float((sub.iloc[-1] / sub.iloc[-1 - n] - 1).mean()) if len(sub) > n else None
    cyc, dfn = ["XLK", "XLY", "XLI", "XLF", "XLB", "XLE"], ["XLP", "XLU", "XLV"]
    r20 = grp(cyc, 20) - grp(dfn, 20) if grp(cyc, 20) is not None and grp(dfn, 20) is not None else None
    r60 = grp(cyc, 60) - grp(dfn, 60) if grp(cyc, 60) is not None and grp(dfn, 60) is not None else None
    risk = "中性"
    if r20 is not None:
        if r20 >= 0.02 and (r60 is None or r60 >= 0):
            risk = "偏冒險"
        elif r20 <= -0.02 and (r60 is None or r60 <= 0):
            risk = "偏避險"

    idx = []
    for t, name in (("^TWII", "台股加權"), ("^GSPC", "S&P 500")):
        if t in close:
            w = close[t].dropna().resample("W-FRI").last().dropna()
            ma = w.rolling(52).mean()
            if len(ma.dropna()):
                idx.append({"c": t, "n": name, "below": bool(w.iloc[-1] < ma.iloc[-1]), "gap": rnd(float(w.iloc[-1] / ma.iloc[-1] - 1), 4)})

    macro_hit = []
    if c is not None and len(c) and float(c.iloc[-1]) > 0 and inv6:
        macro_hit.append("殖利率曲線近半年由負轉正")
    if h3 is not None and h3 >= 1.5:
        macro_hit.append("信用利差 3 個月急升超過 1.5%")
    markets = {}
    for m, name, ic in (("tw", "台股", "^TWII"), ("us", "美股", "^GSPC")):
        d = load_json(os.path.join(DATA, f"{m}.json"), None)
        fin_on, fin_why = None, "沒有資料"
        if d and d.get("fin"):
            up, dn = d["fin"].get("up", 0), d["fin"].get("down", 0)
            fin_on, fin_why = dn > up, f"個股財報轉強 {up}／轉弱 {dn}"
        flow_on = risk == "偏避險" if r20 is not None else None
        below = next((i["below"] for i in idx if i["c"] == ic), False)
        on = bool(macro_hit) and bool(fin_on) and bool(flow_on)
        markets[m] = {"name": name, "fin": fin_on, "fin_why": fin_why, "flow": flow_on, "below": below, "on": on,
                      "step": (10 if below else 5) if on else 0}
    out = {"updated": now.strftime("%Y-%m-%d %H:%M"), "src": src,
           "stage": {"idx": stage, "name": CYCLE[stage] if stage is not None else None, "why": why},
           "risk": {"label": risk, "r20": rnd(r20, 4), "r60": rnd(r60, 4)}, "idx": idx,
           "tactical": {"on": any(v["on"] for v in markets.values()), "macro": macro_hit, "markets": markets,
                        "rule": "總經、財報、資金流三個條件都轉弱才動：核心層股票減 5 個百分點；大盤週線跌破 MA52 再減到 10 個百分點；只做防守、不加碼"}}
    save_json(os.path.join(DATA, "macro.json"), out)
    log(f"總經完成：{out['stage']['name']}、{risk}、戰術層{'觸發' if out['tactical']['on'] else '未觸發'}")


# ───────────────────────── 核心層：全天候 ─────────────────────────
CORE = {"0050.TW": ("元大台灣50", 0.15), "VTI": ("美國全市場股票", 0.15), "TLT": ("美國長期公債", 0.40),
        "IEF": ("美國中期公債", 0.15), "GLD": ("黃金", 0.075), "DBC": ("商品", 0.075)}


def risk_parity(cov, iters=2000):
    n = cov.shape[0]
    w = 1 / np.sqrt(np.diag(cov))
    w = w / w.sum()
    for _ in range(iters):
        rc = w * (cov @ w)
        target = rc.sum() / n
        w = w * (target / np.maximum(rc, 1e-18)) ** 0.5
        w = w / w.sum()
    return w


def run_core():
    now = datetime.now(TPE)
    tks = list(CORE) + ["TWD=X"]
    mon = yf_download(tks, "5y", "1mo", chunk=10, pause=1).get("Close", pd.DataFrame())
    fx = None
    if "TWD=X" in mon and len(mon["TWD=X"].dropna()):
        fx = float(mon["TWD=X"].dropna().iloc[-1])
    rets = mon[[t for t in CORE if t in mon]].dropna().pct_change().dropna().iloc[-36:]
    if "TWD=X" in mon and "0050.TW" in rets:   # 0050 換成美元報酬，跟其他資產同一幣別比較
        fxr = mon["TWD=X"].reindex(rets.index.union(mon.index)).pct_change().reindex(rets.index)
        rets["0050.TW"] = (1 + rets["0050.TW"]) / (1 + fxr.fillna(0)) - 1
    rp = None
    if len(rets) >= 24 and rets.shape[1] == len(CORE):
        rp = dict(zip(rets.columns, [rnd(x, 4) for x in risk_parity(rets.cov().values)]))
    out = {"updated": now.strftime("%Y-%m-%d %H:%M"), "latest": {"fx": rnd(fx, 4)},
           "layers": {"core": 50, "sat": 40, "tac": 10},
           "assets": {t.replace(".TW", ""): {"name": n, "fixed": w, "rp": (rp or {}).get(t)} for t, (n, w) in CORE.items()},
           "note": "核心層全天候：固定比例，或用近 36 個月報酬算風險平價（每個資產對整體波動的貢獻一樣）"}
    save_json(os.path.join(DATA, "core.json"), out)
    log("核心層完成：", out["assets"])


# ───────────────────────── 今日判讀（規則組句，不用 AI） ─────────────────────────
def run_summary():
    tw, us, mc = (load_json(os.path.join(DATA, f), None) for f in ("tw.json", "us.json", "macro.json"))
    zh = {"cheap": "便宜", "fair": "合理", "exp": "偏貴", None: "沒資料"}

    def bl(d, code):
        b = next((x for x in (d or {}).get("benchmarks", []) if x["c"] == code), None)
        return b
    t, s = bl(tw, "^TWII"), bl(us, "^GSPC")
    parts, watch = [], []
    if mc:
        parts.append("戰術層" + ("觸發，核心層股票要減碼" if mc["tactical"]["on"] else "未觸發"))
    if t or s:
        parts.append(f"台股燈號{zh.get(t and t['l'])}、美股燈號{zh.get(s and s['l'])}")
    if mc:
        parts.append(f"資金風向{mc['risk']['label']}")
    if mc and mc["stage"]["name"]:
        watch.append(f"債務週期判斷：{mc['stage']['name']}（{'；'.join(mc['stage']['why'][:2])}）")
    if mc:
        watch.append("戰術層未觸發，核心層照原規則每月定投，不因新聞或情緒擇時" if not mc["tactical"]["on"]
                     else "戰術層觸發：" + "、".join(f"{v['name']}減 {v['step']} 個百分點" for v in mc["tactical"]["markets"].values() if v["on"]))
    for d, name in ((tw, "台股"), (us, "美股")):
        if d:
            watch.append(f"{name}衛星層四道濾網全過 {len(d['picks'])} 檔；單股不超過衛星層 10%")
    out = {"headline": "，".join(parts) or "資料還沒產生", "watch": watch,
           "generated": datetime.now(TPE).strftime("%Y-%m-%d %H:%M"), "source": "rules"}
    save_json(os.path.join(DATA, "summary.json"), out)
    log("今日判讀：", out["headline"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="all", choices=["tw", "us", "macro", "core", "all"])
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    jobs = {"tw": [lambda: run_tw(a.limit)], "us": [lambda: run_us(a.limit)], "macro": [run_macro], "core": [run_core],
            "all": [lambda: run_tw(a.limit), lambda: run_us(a.limit), run_macro, run_core]}[a.market]
    errors = []
    for j in jobs:
        try:
            j()
        except Exception as e:  # noqa
            log("失敗：", repr(e))
            errors.append(repr(e))
    run_summary()
    if errors and len(errors) == len(jobs):
        raise SystemExit("全部失敗：" + "；".join(errors))


if __name__ == "__main__":
    main()
