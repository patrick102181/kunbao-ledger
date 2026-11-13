# -*- coding: utf-8 -*-
"""
坤寶存摺 每日市場資料（台美股掃描、總經戰術層、核心層、今日判讀）
策略與指標照市場雷達（market-radar 的 scripts/update.py）：資料全部自己從證交所、櫃買中心、Yahoo、FRED 等源頭抓，
另外加上 App 用的點位（週 MA52、52 週高點、年線）、核心層風險平價、規則版今日判讀。
用法：
    python market.py --market tw|us|macro|core|summary|all
    python market.py --market tw --limit 80   # 本機測試：只跑前 80 檔
輸出：
    data/tw.json / data/us.json              # 網站主資料
    data/series/{tw|us}_{X}.json             # 個股20年月線＋歷史燈號（點進個股時才載入）
    data/alerts.json                         # 燈號變化紀錄
    data/cache/...                           # 歷史快取（法人歷史、股票清單、基本面）

分數說明（都是 0~100）：
    v   價格位置：年線乖離、月KD、5年高點回撤、5年位置（越高＝價格越低檔）
    val 估值：本益比、股價淨值比跟同族群中位數比較，自由現金流殖利率、殖利率（越高＝越便宜）
    q   品質：ROE、淨利率、負債比、營收成長、自由現金流（越高＝體質越好）
    lt  長線總分：v 40%、val 30%、q 30%（ETF 和指數只看 v）
    f   資金：量能、法人、族群資金（短線參考）
"""
import argparse
import io
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("KB_DATA_DIR") or os.environ.get("MR_DATA_DIR") or os.path.join(ROOT, "data")  # 本機測試可改輸出位置
CACHE = os.path.join(DATA, "cache")
SERIES = os.path.join(DATA, "series")
TPE = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

LIQ_MIN = {"tw": 30_000_000, "us": 10_000_000}  # 近20日平均成交額門檻（台幣 / 美元）
FUND_MAX_AGE_DAYS = 7  # 基本面每檔最多 7 天更新一次


def log(*a):
    print(datetime.now(TPE).strftime("%H:%M:%S"), *a, flush=True)


# ───────────────────────── 共用工具 ─────────────────────────

def http_get(url, tries=3, timeout=30, sleep=3):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa
            last = str(e)
        time.sleep(sleep * (i + 1))
    raise RuntimeError(f"抓取失敗 {url}：{last}")


def to_num(x):
    if x is None:
        return None
    if isinstance(x, (int, float, np.integer, np.floating)):
        v = float(x)
        return None if math.isnan(v) or math.isinf(v) else v
    s = str(x).replace(",", "").replace("%", "").strip()
    if s in ("", "-", "--", "N/A", "nan", "None", "X"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def rnd(x, n=2):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, n)


def find_key(d, *groups):
    """在 dict 的欄位名稱裡找出同時包含某組關鍵字的欄位。groups 是多組候選，每組是 tuple。"""
    keys = list(d.keys())
    for g in groups:
        for k in keys:
            if all(w.lower() in str(k).lower() for w in g):
                return k
    return None


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def lin_down(x, good, bad):
    """x 越接近 good 分數越高（100），越接近 bad 越低（0）"""
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
    """pairs：[(值, 權重)]，忽略 None"""
    p = [(v, w) for v, w in pairs if v is not None]
    sw = sum(w for _, w in p)
    return float(sum(v * w for v, w in p) / sw) if sw else None


def light_of(v):
    if v is None:
        return None
    if v >= 65:
        return "cheap"
    if v <= 35:
        return "exp"
    return "fair"


def median_ok(vals):
    v = [x for x in vals if x is not None]
    return float(np.median(v)) if v else None


# ───────────────────────── 價格下載（yfinance） ─────────────────────────

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
            log(f"  第 {i // chunk + 1} 批沒有資料")
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
        log(f"  {interval} 第 {i // chunk + 1}/{math.ceil(len(tickers) / chunk)} 批完成")
        time.sleep(pause)
    res = {}
    for fld, lst in out.items():
        if lst:
            d = pd.concat(lst, axis=1)
            d = d.loc[:, ~d.columns.duplicated()]
            d.index = pd.to_datetime(d.index).tz_localize(None) if getattr(d.index, "tz", None) else pd.to_datetime(d.index)
            res[fld] = d.sort_index()
        else:
            res[fld] = pd.DataFrame()
    return res


# ───────────────────────── 基本面（Yahoo，滾動更新快取） ─────────────────────────

FUND_KEYS = {"trailingPE": "pe", "priceToBook": "pb", "returnOnEquity": "roe", "debtToEquity": "de",
             "revenueGrowth": "rg", "earningsGrowth": "eg", "profitMargins": "pm",
             "freeCashflow": "fcf", "marketCap": "mcap", "trailingEps": "eps"}


def _quarterly(tk):
    """最近 6 季損益表：[[年月, 營收, 毛利, 淨利, EPS], ...]（由舊到新）"""
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
    rows = [row("Total Revenue", "Operating Revenue"), row("Gross Profit"),
            row("Net Income", "Net Income Common Stockholders"), row("Diluted EPS", "Basic EPS")]
    out = []
    for col in sorted(q.columns)[-6:]:
        vals = [to_num(r[col]) if r is not None else None for r in rows]
        if any(v is not None for v in vals):
            out.append([pd.Timestamp(col).strftime("%Y-%m")] + vals)
    return out


def _fetch_info(t):
    import yfinance as yf
    try:
        tk = yf.Ticker(t)
        info = tk.info or {}
    except Exception as e:  # noqa
        return t, None, str(e)
    if not info or len(info) < 5:
        return t, None, "empty"
    d = {v: to_num(info.get(k)) for k, v in FUND_KEYS.items()}
    d["qs"] = _quarterly(tk)
    return t, d, None


def update_fundamentals(market, tickers, today):
    """每檔基本面最多 FUND_MAX_AGE_DAYS 天更新一次；第一次會抓比較多。回傳 {ticker: dict}"""
    path = os.path.join(CACHE, f"fund_{market}.json")
    cache = load_json(path, {})
    # 舊版快取的季資料是 [年月, 營收, 毛利, EPS] 四欄，新版多了淨利：[年月, 營收, 毛利, 淨利, EPS]。
    # 沒轉換的話 fin_trend 讀 q[4] 會 IndexError，整個台股／美股都寫不出來（2026-11-13 踩到）
    for v in cache.values():
        if isinstance(v, dict) and v.get("qs"):
            v["qs"] = [list(q[:3]) + [None, q[3]] if len(q) == 4 else list(q) for q in v["qs"]]
    tstr = today.strftime("%Y-%m-%d")
    cutoff = (today - timedelta(days=FUND_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
    stale = [t for t in tickers if t not in cache or cache[t].get("_d", "") < cutoff or "qs" not in cache[t]]
    # 先更新最舊的；快取還很空時一次多抓一點
    stale.sort(key=lambda t: cache.get(t, {}).get("_d", ""))
    cap = 3000 if len(cache) < len(tickers) * 0.5 else max(400, len(tickers) // FUND_MAX_AGE_DAYS + 50)
    todo = stale[:cap]
    log(f"基本面：快取 {len(cache)} 檔，這次更新 {len(todo)} 檔")
    fails, done = 0, 0
    with ThreadPoolExecutor(max_workers=2) as ex:
        for i in range(0, len(todo), 40):
            batch = todo[i:i + 40]
            got = list(ex.map(_fetch_info, batch))
            bad = 0
            for t, d, err in got:
                if d:
                    d["_d"] = tstr
                    cache[t] = d
                    done += 1
                else:
                    bad += 1
            fails = fails + bad if bad == len(batch) else 0
            if fails >= 80:
                log("  基本面連續失敗太多（可能被 Yahoo 限流），這次先停止，下次更新會從沒抓到的開始補")
                break
            time.sleep(2.5 if bad else 1.5)
    log(f"  基本面更新完成 {done} 檔")
    keep = set(tickers)
    cache = {k: v for k, v in cache.items() if k in keep}
    save_json(path, cache)
    return cache


def is_fin(sec):
    s = str(sec or "")
    return ("金融" in s) or ("保險" in s) or ("銀行" in s) or ("證券" in s)


def quality_score(fd, sec, rev_yoy=None):
    """品質分數與警示標籤。fd：基本面 dict；rev_yoy：台股累計營收年增率（小數）"""
    if not fd:
        fd = {}
    roe, pm, de, fcf, eps = fd.get("roe"), fd.get("pm"), fd.get("de"), fd.get("fcf"), fd.get("eps")
    rg = rev_yoy if rev_yoy is not None else fd.get("rg")
    parts = [lin_down(roe, 0.20, 0.0), lin_down(pm, 0.15, -0.05), lin_down(rg, 0.20, -0.20)]
    if not is_fin(sec):
        parts.append(lin_down(de, 30, 200))
        if fcf is not None:
            parts.append(100.0 if fcf > 0 else 0.0)
    got = [p for p in parts if p is not None]
    q = float(sum(got) / len(got)) if len(got) >= 2 else None
    warn = []
    if (eps is not None and eps < 0) or (pm is not None and pm < 0):
        warn.append("虧損")
    if rg is not None and rg <= -0.15:
        warn.append(f"營收衰退{round(rg * 100)}%")
    if not is_fin(sec) and de is not None and de >= 200:
        warn.append("高負債")
    if not is_fin(sec) and fcf is not None and fcf < 0:
        warn.append("自由現金流為負")
    return q, warn


def fin_trend(fd, rev_yoy=None):
    """財報趨勢：營收年增、毛利率變化（跟去年同季比，沒有就跟上一季比）、EPS 年增。
    三項有兩項以上變好且沒有變差＝轉強(up)；兩項以上變差＝轉弱(down)；其他＝持平(flat)"""
    qs = [q for q in ((fd or {}).get("qs") or []) if len(q) >= 5 and q[1]]  # 欄位不齊的季資料直接略過，不讓整個市場掛掉
    gmd = epsg = revg = None
    if qs:
        last = qs[-1]
        ya = qs[-5] if len(qs) >= 5 else None

        def gm(q):
            return q[2] / q[1] if q[2] is not None and q[1] else None
        base = ya if ya is not None and gm(ya) is not None else (qs[-2] if len(qs) >= 2 else None)
        if base is not None and gm(last) is not None and gm(base) is not None:
            gmd = gm(last) - gm(base)
        if ya is not None and last[4] is not None and ya[4]:
            epsg = (last[4] - ya[4]) / abs(ya[4])
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
    return {"fin": fin, "gmd": rnd(gmd, 4), "epsg": rnd(epsg, 3), "frev": rnd(revg, 3),
            "eps_q": [rnd(q[4], 2) for q in qs[-5:]] if qs else None}


def qkey(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{y}Q{(m - 1) // 3 + 1}"


def industry_table(items):
    """items：[{sec, qs, rev_cur, rev_prev, fin, revg}]。回傳每個產業的營收年增、成長家數、
    最近 5 季毛利率與淨利合計（只用 5 季資料都齊全的公司，避免家數變動造成假趨勢）"""
    last_keys = [qkey(it["qs"][-1][0]) for it in items if it.get("qs")]
    latest = max(set(last_keys), key=last_keys.count) if last_keys else None
    quarters = []
    if latest:
        y, qn = int(latest[:4]), int(latest[-1])
        for _ in range(5):
            quarters.insert(0, f"{y}Q{qn}")
            qn -= 1
            if qn == 0:
                y, qn = y - 1, 4
    by = {}
    for it in items:
        if it.get("sec"):
            by.setdefault(it["sec"], []).append(it)
    out = []
    for sec, lst in by.items():
        if len(lst) < 3:
            continue
        cur = [it for it in lst if it.get("rev_cur") and it.get("rev_prev")]
        rev_yoy = (sum(it["rev_cur"] for it in cur) / sum(it["rev_prev"] for it in cur) - 1) if cur else None
        if rev_yoy is None:
            rr = [it["revg"] for it in lst if it.get("revg") is not None]
            rev_yoy = float(np.median(rr)) if rr else None
        grow = sum(1 for it in lst if it.get("revg") is not None and it["revg"] > 0)
        known = sum(1 for it in lst if it.get("revg") is not None)
        gm_s, ni_s, n_full = [], [], 0
        if quarters:
            full = []
            for it in lst:
                m = {qkey(q[0]): q for q in (it.get("qs") or [])}
                if all(k in m and m[k][1] and m[k][3] is not None for k in quarters):
                    full.append(m)
            n_full = len(full)
            if n_full >= 3:
                for k in quarters:
                    rv = sum(m[k][1] for m in full)
                    gp = [m[k][2] for m in full if m[k][2] is not None]
                    gm_s.append(rnd(sum(gp) / rv, 4) if len(gp) == n_full and rv else None)
                    ni_s.append(rnd(sum(m[k][3] for m in full), 0))
        fins = [it.get("fin") for it in lst]
        out.append({"name": sec, "n": len(lst), "rev_yoy": rnd(rev_yoy, 4), "grow": grow, "known": known,
                    "gm": gm_s or None, "ni": ni_s or None, "n_full": n_full,
                    "up": fins.count("up"), "down": fins.count("down"), "flat": fins.count("flat")})
    out.sort(key=lambda o: -(o["n"]))
    return {"quarters": quarters, "rows": out}


def sector_weekly(close_df, tick_sec, weights, weeks=12, vol_df=None):
    """族群相對大盤的每週報酬（成交額加權），最近 weeks 週；另算近 4 週相對強弱 rs4。
    有成交量時另算輪動軌跡 trail：每週一個點 [近 4 週相對強弱, 成交比重減前 4 週平均, 當週成交比重]"""
    cols = [t for t in tick_sec if t in close_df.columns]
    if not cols:
        return [], {}
    wk = close_df[cols].resample("W-FRI").last()
    ret_all = wk.pct_change(fill_method=None)
    ret = ret_all.iloc[-weeks:]
    w = pd.Series({t: max(weights.get(t) or 0, 0) for t in cols})
    secs = pd.Series({t: tick_sec[t] for t in cols})

    def wavg(r, ww):
        num_ = (r.fillna(0) * ww).sum(axis=1)
        den = (r.notna() * ww).sum(axis=1)
        return num_ / den.replace(0, np.nan)
    mkt = wavg(ret, w)
    mkt_all = wavg(ret_all, w)
    # 每週成交額（收盤價×成交量加總）→ 族群成交比重
    tvw = None
    if vol_df is not None:
        vc = [t for t in cols if t in vol_df.columns]
        if vc:
            tvw = (close_df[vc] * vol_df[vc]).resample("W-FRI").sum(min_count=1)
            tvw = tvw.reindex(wk.index)
    out = {}
    for sname in secs.unique():
        cs = secs[secs == sname].index
        if len(cs) < 3 or not sname:
            continue
        sr = wavg(ret[cs], w[cs])
        rel = sr - mkt
        last4 = sr.iloc[-4:]
        rs4 = float((1 + last4).prod() - (1 + mkt.iloc[-4:]).prod()) if last4.notna().all() else None
        out[sname] = {"wk": [rnd(v, 4) for v in rel.values], "rs4": rnd(rs4, 4)}
        if tvw is not None:
            csv_ = [t for t in cs if t in tvw.columns]
            tot = tvw.sum(axis=1).replace(0, np.nan)
            sh = tvw[csv_].sum(axis=1) / tot if csv_ else None
            sr_all = wavg(ret_all[cs], w[cs])
            # 近 4 週相對強弱（滾動）
            rs_roll = (1 + sr_all).rolling(4).apply(np.prod, raw=True) - (1 + mkt_all).rolling(4).apply(np.prod, raw=True)
            trail = []
            for i in range(len(wk) - weeks, len(wk)):
                if sh is None or i < 4:
                    trail.append(None)
                    continue
                x, s_now = rs_roll.iloc[i], sh.iloc[i]
                prev = sh.iloc[i - 4:i]
                y = s_now - prev.mean() if prev.notna().all() else np.nan
                trail.append([rnd(x, 4), rnd(y, 5), rnd(s_now, 4)] if pd.notna(x) and pd.notna(y) and pd.notna(s_now) else None)
            out[sname]["trail"] = trail
    labels = [d.strftime("%m/%d") for d in ret.index]
    return labels, out


FLOW_DAYS = 20      # 資金流向：保留最近 20 個交易日
FLOW_SPIKE = 5.0    # 單日成交額超過前 20 日平均的 5 倍 → 標「異常量」（可能是鉅額交易、指數調整）


def stock_flow(dc, dh, dl, dv, insti=None, code=None):
    """個股近 FLOW_DAYS 個交易日的每日：成交額 t、價量估算買賣力道 e、三大法人買賣超金額 i（只有台股）。
    e＝((收−低)−(高−收))÷(高−低) × 成交額，是用收盤在當日高低區間的位置估算主動買賣，只是估算。
    i＝當日三大法人買賣超股數 × 當日收盤價。回傳 (每日 dict, 摘要 dict)"""
    df = pd.DataFrame({"c": dc, "h": dh, "l": dl, "v": dv}).dropna()
    df = df[df["v"] > 0]
    if len(df) < 2:
        return None, {}
    tv_all = df["c"] * df["v"]
    df = df.iloc[-FLOW_DAYS:]
    tv = tv_all.iloc[-FLOW_DAYS:]
    rng = (df["h"] - df["l"]).replace(0, np.nan)
    clv = (((df["c"] - df["l"]) - (df["h"] - df["c"])) / rng).fillna(0)
    est = clv * tv
    ins = None
    if insti is not None and code is not None:
        vals = []
        for d_, c_ in zip(df.index, df["c"]):
            row = insti.get(d_.strftime("%Y%m%d"), {}).get(code)
            vals.append(sum(v or 0 for v in row) * c_ if row else np.nan)
        ins = pd.Series(vals, index=df.index)
    daily = {"t": tv, "e": est, "i": ins}
    agg = lambda x, n: rnd(float(x.iloc[-n:].sum()), 0) if x is not None and x.iloc[-n:].notna().any() else None  # noqa: E731
    summ = {"t": [agg(tv, 1), agg(tv, 5), agg(tv, 20)], "e": [agg(est, 1), agg(est, 5), agg(est, 20)]}
    if ins is not None:
        summ["i"] = [agg(ins, 1), agg(ins, 5), agg(ins, 20)]
    # 異常量：近 5 日內有哪一天的成交額 > 前 20 日平均 × FLOW_SPIKE
    base = tv_all.shift(1).rolling(20).mean()
    spk = (tv_all > base * FLOW_SPIKE).iloc[-5:]
    if spk.any():
        summ["spk"] = spk[spk].index[-1].strftime("%m/%d")
    return daily, summ


class FlowAgg:
    """把個股每日資金流加總成產業每日（給產業 × 日期熱力圖）"""
    def __init__(self):
        self.parts = {}

    def add(self, sec, daily):
        if daily is not None and sec:
            self.parts.setdefault(sec, []).append(daily)

    def result(self):
        idx = None
        for lst in self.parts.values():
            for dd in lst:
                ix = dd["t"].index
                idx = ix if idx is None or len(ix) > len(idx) or ix[-1] > idx[-1] else idx
        if idx is None:
            return None
        idx = idx[-FLOW_DAYS:]
        out = {}
        for sec, lst in self.parts.items():
            o = {}
            for k in ("t", "e", "i"):
                ss = [dd[k].reindex(idx) for dd in lst if dd.get(k) is not None]
                if ss:
                    tot = pd.concat(ss, axis=1).sum(axis=1, min_count=1)
                    o[k] = [rnd(float(v), 0) if pd.notna(v) else None for v in tot.values]
            out[sec] = o
        return {"d": [d.strftime("%m/%d") for d in idx], "sec": out}


def sector_medians(items, key):
    """items：[(sec, 值)]，回傳 {sec: 中位數}，並以 None 存全市場中位數"""
    by = {}
    allv = []
    for sec, v in items:
        if v is None or v <= 0:
            continue
        by.setdefault(sec, []).append(v)
        allv.append(v)
    out = {s: float(np.median(v)) for s, v in by.items() if len(v) >= 5}
    out[None] = float(np.median(allv)) if allv else None
    return out


def valuation_score(pe, pb, fcfy, dy, sec, pe_med, pb_med):
    """估值分數：跟同族群中位數比（比值 0.6 倍＝100 分、1.6 倍＝0 分）"""
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


# ───────────────────────── 指標計算 ─────────────────────────

def kd_series(h, l, c, n=9):
    """台灣常用 KD(9,3,3)。回傳 K 的 numpy 陣列（資料不足的期間為 nan）"""
    hh = h.rolling(n, min_periods=n).max().values
    ll = l.rolling(n, min_periods=n).min().values
    cv = c.values
    ks = np.full(len(cv), np.nan)
    k, d = 50.0, 50.0
    for i in range(len(cv)):
        if np.isnan(hh[i]) or np.isnan(ll[i]) or np.isnan(cv[i]):
            continue
        rng = hh[i] - ll[i]
        rsv = 50.0 if rng == 0 else (cv[i] - ll[i]) / rng * 100
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
        ks[i] = k
    return ks


def monthly_kd(h, l, c, n=9):
    ks = kd_series(h, l, c, n)
    ok = ks[~np.isnan(ks)]
    return (float(ok[-1]), None) if len(ok) else (None, None)


def calc_stock(dc, dv, mc, mh, ml):
    """dc/dv：日收盤、日成交量 Series；mc/mh/ml：月收盤/高/低 Series。回傳指標 dict"""
    dc = dc.dropna()
    if len(dc) < 30:
        return None
    dv = dv.reindex(dc.index).fillna(0)
    px = float(dc.iloc[-1])
    if px <= 0:
        return None
    r = {"px": px}
    r["ch"] = px / float(dc.iloc[-2]) - 1 if len(dc) >= 2 and dc.iloc[-2] else None
    r["r1w"] = px / float(dc.iloc[-6]) - 1 if len(dc) >= 6 else None
    r["r1w_prev"] = float(dc.iloc[-6]) / float(dc.iloc[-11]) - 1 if len(dc) >= 11 else None
    r["r1m"] = px / float(dc.iloc[-22]) - 1 if len(dc) >= 22 else None
    r["r1y"] = px / float(dc.iloc[-250]) - 1 if len(dc) >= 250 else None
    r["bias"] = px / float(dc.iloc[-240:].mean()) - 1 if len(dc) >= 240 else None
    tv = dc * dv
    r["tv_today"] = float(tv.iloc[-1])
    r["tv5"] = float(tv.iloc[-5:].mean())
    r["tv20"] = float(tv.iloc[-20:].mean()) if len(tv) >= 20 else float(tv.mean())
    r["tv_prev20"] = float(tv.iloc[-25:-5].mean()) if len(tv) >= 25 else None
    tv60 = float(tv.iloc[-60:].mean()) if len(tv) >= 60 else None
    r["vr"] = r["tv5"] / tv60 if tv60 else None
    r["dd52"] = px / float(dc.iloc[-250:].max()) - 1
    r["hi52"] = float(dc.iloc[-250:].max())
    r["ma240"] = float(dc.iloc[-240:].mean()) if len(dc) >= 240 else None
    r["wma"] = r["wma_up"] = r["wma_v"] = None
    wk = dc.resample("W-FRI").last().dropna()
    if len(wk) >= 57:
        ma = wk.rolling(52).mean()
        r["wma"] = px / float(ma.iloc[-1]) - 1
        r["wma_up"] = bool(ma.iloc[-1] > ma.iloc[-5])
        r["wma_v"] = float(ma.iloc[-1])

    k = dd5 = pos5 = ma60 = None
    chk = False
    if mc is not None:
        mc = mc.dropna()
        if len(mc) >= 2:
            mc = mc.copy()
            mc.iloc[-1] = px  # 用最新日收盤取代當月
            mh2 = (mh if mh is not None else mc).reindex(mc.index)
            ml2 = (ml if ml is not None else mc).reindex(mc.index)
            mh2 = mh2.fillna(mc)
            ml2 = ml2.fillna(mc)
            mh2.iloc[-1] = max(float(mh2.iloc[-1]), px)
            ml2.iloc[-1] = min(float(ml2.iloc[-1]), px)
            # 月KD只用已經收完的月份，避免月中燈號一直跳
            partial = (mc.index[-1].year, mc.index[-1].month) == (dc.index[-1].year, dc.index[-1].month)
            end = -1 if partial else None
            if len(mc.iloc[:end]) >= 12:
                k, _ = monthly_kd(mh2.iloc[:end], ml2.iloc[:end], mc.iloc[:end])
            if len(mc.iloc[:end]) >= 40:
                cc = mc.iloc[:end]
                dif = cc.ewm(span=12, adjust=False).mean() - cc.ewm(span=26, adjust=False).mean()
                dea = dif.ewm(span=9, adjust=False).mean()
                h_ = (dif - dea).values
                r["macd_x"] = bool(h_[-1] > 0 and (h_[-2] <= 0 or h_[-3] <= 0))  # 近 2 個月黃金交叉
                r["macd_h"] = float(h_[-1])
            last60 = mc.iloc[-60:]
            hi = float(mh2.iloc[-60:].max())
            dd5 = px / hi - 1 if hi > 0 else None
            if len(last60) >= 24:
                pos5 = float((last60 < px).sum()) / (len(last60) - 1) if len(last60) > 1 else None
            if len(mc) >= 60:
                ma60 = float(mc.iloc[-60:].mean())
            # 資料異常檢查：單月漲超過 200% 或跌超過 67%（常見於減資、分割沒調整）
            chg = last60.pct_change().dropna()
            if len(chg) and (chg.max() > 2.0 or chg.min() < -0.67):
                chk = True
    r.setdefault("macd_x", None)
    r.setdefault("macd_h", None)
    r.update({"k": k, "dd5": dd5, "pos5": pos5, "below60": (px < ma60) if ma60 else None, "chk": chk})

    r["v_bias"] = lin_down(r["bias"], -0.30, 0.30)
    r["v_k"] = lin_down(k, 20, 80)
    r["v_dd"] = lin_down(dd5, -0.50, 0.0)
    r["v_pos"] = lin_down(pos5, 0.0, 1.0)
    r["value"] = mean_ok([r["v_bias"], r["v_k"], r["v_dd"], r["v_pos"]])
    return r


# ───────────────────────── 歷史燈號回測 ─────────────────────────

def _clip_score(x, good, bad):
    return ((x - bad) / (good - bad)).clip(0, 1) * 100


def hist_lights(mc, mh, ml):
    """用月線重算每個月的「價格位置」燈號（年線乖離用 12 個月均線近似）。
    回傳 (DataFrame[c, light, f12, f36, f60], 月線 index)；資料不足回傳 None"""
    c = mc.dropna()
    if len(c) < 72:
        return None
    h = (mh.reindex(c.index) if mh is not None else c).fillna(c)
    l = (ml.reindex(c.index) if ml is not None else c).fillna(c)
    bias = c / c.rolling(12, min_periods=12).mean() - 1
    k = pd.Series(kd_series(h, l, c), index=c.index)
    hi60 = h.rolling(60, min_periods=60).max()
    dd = c / hi60 - 1
    cv = c.values
    pos = np.full(len(cv), np.nan)
    for i in range(59, len(cv)):
        w = cv[i - 59:i + 1]
        pos[i] = (w[:-1] < w[-1]).sum() / 59
    pos = pd.Series(pos, index=c.index)
    v = pd.concat([_clip_score(bias, -0.30, 0.30), _clip_score(k, 20, 80),
                   _clip_score(dd, -0.50, 0.0), _clip_score(pos, 0.0, 1.0)], axis=1).mean(axis=1, skipna=False)
    light = pd.Series(np.where(v >= 65, "c", np.where(v <= 35, "e", "f")), index=c.index)
    light[v.isna()] = "-"
    df = pd.DataFrame({"c": c, "light": light,
                       "f12": c.shift(-12) / c - 1, "f36": c.shift(-36) / c - 1, "f60": c.shift(-60) / c - 1})
    return df


class BTAgg:
    """把很多檔的歷史燈號＋未來報酬累積起來，最後算中位數和勝率"""
    def __init__(self):
        self.d = {}

    def add(self, group, df):
        g = self.d.setdefault(group, {})
        for lt in ("c", "f", "e"):
            sub = df[df["light"] == lt]
            b = g.setdefault(lt, {"f12": [], "f36": [], "f60": []})
            for h in ("f12", "f36", "f60"):
                b[h].extend(sub[h].dropna().tolist())
        sub = df[df["light"] != "-"]
        b = g.setdefault("all", {"f12": [], "f36": [], "f60": []})
        for h in ("f12", "f36", "f60"):
            b[h].extend(sub[h].dropna().tolist())

    def result(self):
        name = {"c": "cheap", "f": "fair", "e": "exp", "all": "all"}
        out = {}
        for grp, g in self.d.items():
            out[grp] = {}
            for lt, b in g.items():
                r = {"n": len(b["f12"]), "n5": len(b["f60"])}
                for h, yrs in (("f12", 1), ("f36", 3), ("f60", 5)):
                    a = np.array(b[h])
                    if len(a) >= 20:
                        med = float(np.median(a))
                        r[h] = rnd(med, 4)
                        r[h + "a"] = rnd((1 + med) ** (1 / yrs) - 1, 4) if med > -1 else None
                        r[h + "w"] = rnd(float((a > 0).mean()), 3)
                out[grp][name[lt]] = r
        return out


def stock_bt(df):
    """單一個股：各燈號出現月數、3年/5年後報酬中位數"""
    out = {}
    for lt, nm in (("c", "cheap"), ("f", "fair"), ("e", "exp")):
        sub = df[df["light"] == lt]
        f36, f60 = sub["f36"].dropna(), sub["f60"].dropna()
        out[nm] = [int(len(sub)),
                   rnd(float(f36.median()), 3) if len(f36) >= 6 else None,
                   rnd(float(f60.median()), 3) if len(f60) >= 6 else None]
    return out


def series_payload(mc, px, hl=None):
    mc = mc.dropna()
    if mc.empty:
        return None
    mc = mc.iloc[-240:].copy()
    mc.iloc[-1] = px
    start = mc.index[0].strftime("%Y-%m")
    p = {"s": start, "c": [rnd(v, 2) for v in mc.values]}
    if hl is not None:
        lights = hl["light"].reindex(mc.index).fillna("-")
        p["l"] = "".join(lights.values)
        p["bt"] = stock_bt(hl)
    return p


def write_series(market, payloads):
    os.makedirs(SERIES, exist_ok=True)
    for f in os.listdir(SERIES):
        if f.startswith(market + "_"):
            os.remove(os.path.join(SERIES, f))
    shards = {}
    for code, p in payloads.items():
        if p is None:
            continue
        key = shard_key(code)
        shards.setdefault(key, {})[code] = p
    for key, items in shards.items():
        # 一檔一行，讓 git 每天只記錄有變的行
        lines = [json.dumps(c, ensure_ascii=False) + ":" + json.dumps(p, separators=(",", ":"))
                 for c, p in sorted(items.items())]
        with open(os.path.join(SERIES, f"{market}_{key}.json"), "w", encoding="utf-8") as f:
            f.write("{\n" + ",\n".join(lines) + "\n}\n")


def shard_key(code):
    ch = str(code)[0].upper()
    return ch if ch.isalnum() else "_"


def streak(values):
    """values：由舊到新的淨買賣。回傳最新連續天數（買為正、賣為負）"""
    n = 0
    for v in reversed(values):
        if v is None:
            break
        if v > 0 and n >= 0:
            n += 1
        elif v < 0 and n <= 0:
            n -= 1
        else:
            break
    return n


def sector_table(rows, key_sector="sec"):
    """rows：每檔 dict（含 sec、tv5、tv_prev20、r1w、r1w_prev）。回傳族群資金比重與輪動排名"""
    df = pd.DataFrame(rows)
    if df.empty or key_sector not in df:
        return []
    df = df[df[key_sector].notna() & (df[key_sector] != "")]
    tot5 = df["tv5"].sum()
    totp = df["tv_prev20"].sum()
    g = df.groupby(key_sector)
    out = []
    for name, sub in g:
        if len(sub) < 3:
            continue
        share = sub["tv5"].sum() / tot5 if tot5 else None
        sharep = sub["tv_prev20"].sum() / totp if totp else None
        w = sub["tv20"].clip(lower=0)

        def wavg(col):
            s = sub[col]
            m = s.notna() & (w > 0)
            return float((s[m] * w[m]).sum() / w[m].sum()) if m.any() else None
        out.append({"name": name, "n": int(len(sub)), "share": share,
                    "share_chg": (share - sharep) if (share is not None and sharep is not None) else None,
                    "r1w": wavg("r1w"), "r1w_prev": wavg("r1w_prev")})

    def rank_by(key):
        vals = sorted([o for o in out if o[key] is not None], key=lambda o: -o[key])
        return {o["name"]: i + 1 for i, o in enumerate(vals)}
    rk, rkp = rank_by("r1w"), rank_by("r1w_prev")
    for o in out:
        o["rank"] = rk.get(o["name"])
        o["prev_rank"] = rkp.get(o["name"])
        for kk in ("share", "share_chg", "r1w", "r1w_prev"):
            o[kk] = rnd(o[kk], 4)
    out.sort(key=lambda o: -(o["share"] or 0))
    return out


def value_tags(m):
    t = []
    if m.get("below60"):
        t.append("跌破5年線")
    elif m.get("bias") is not None and m["bias"] < 0:
        t.append("跌破年線")
    if m.get("k") is not None and m["k"] < 25:
        t.append("月KD低檔")
    if m.get("dd5") is not None and m["dd5"] <= -0.30:
        t.append(f"回撤{round(m['dd5'] * 100)}%")
    if m.get("chk"):
        t.append("股價資料待確認")
    return t


def buy_triggers(m):
    """長線買點觸發條件。回傳 (已觸發的條件, 最接近的等待說明)"""
    trig, wait = [], []
    wma, up = m.get("wma"), m.get("wma_up")
    if wma is not None:
        if 0 <= wma <= 0.04 and up:
            trig.append("回測週MA52不破")
        elif wma > 0.04:
            wait.append(f"距週MA52 +{round(wma * 100)}%")
        else:
            wait.append(f"跌破週MA52 {round(wma * 100)}%")
    dd = m.get("dd52")
    if dd is not None:
        if dd <= -0.20:
            trig.append(f"距52週高點{round(dd * 100)}%")
        else:
            wait.append(f"距高點 {round(dd * 100)}%，未達 −20%")
    if m.get("macd_x"):
        trig.append("月線MACD黃金交叉")
    return trig, "；".join(wait[:2])


def long_term(m, q, val, is_etf, warn=()):
    """長線總分與燈號。ETF/指數只看價格位置；個股品質太差或虧損時亮「基本面弱」"""
    v = m["value"]
    if is_etf or (q is None and val is None):
        lt = v
    else:
        lt = wmean_ok([(v, 0.4), (val, 0.3), (q, 0.3)])
    light = light_of(lt)
    weak = (q is not None and q < 35) or ("虧損" in warn)
    if not is_etf and light in ("cheap", "fair") and weak:
        light = "trap"
        lt = min(lt, 50.0)  # 排序往後放，避免價值陷阱排在前面
    return lt, light


def stock_row(code, name, sec, m, flow_parts, tags, extra=None):
    flow = mean_ok(flow_parts)
    row = {
        "c": code, "n": name, "sec": sec,
        "px": rnd(m["px"], 2), "ch": rnd(m["ch"], 4), "r1m": rnd(m["r1m"], 4), "r1y": rnd(m["r1y"], 4),
        "b": rnd(m["bias"], 4), "k": rnd(m["k"], 1), "dd": rnd(m["dd5"], 4), "pos": rnd(m["pos5"], 3),
        "vr": rnd(m["vr"], 2), "tv": rnd(m["tv20"], 0),
        "v": rnd(m["value"], 1), "pl": light_of(m["value"]), "f": rnd(flow, 1), "tags": tags,
        "wma": rnd(m.get("wma"), 4), "dd52": rnd(m.get("dd52"), 4),
        # App 的點位：週 MA52、52 週高點、年線（絕對價位），以及週 MA52 是否往上彎
        "lv": [rnd(m.get("wma_v"), 2), rnd(m.get("hi52"), 2), rnd(m.get("ma240"), 2)], "wu": m.get("wma_up"),
    }
    trig, wait = buy_triggers(m)
    row["trig"], row["wait"] = trig, wait
    if extra:
        row.update(extra)
    return row


def bench_row(code, name, m, hl=None, pe=None):
    r = {"c": code, "n": name, "px": rnd(m["px"], 2), "ch": rnd(m["ch"], 4),
         "b": rnd(m["bias"], 4), "k": rnd(m["k"], 1), "dd": rnd(m["dd5"], 4),
         "pos": rnd(m["pos5"], 3), "v": rnd(m["value"], 1), "lt": rnd(m["value"], 1),
         "l": light_of(m["value"]), "pl": light_of(m["value"]), "pe": rnd(pe, 1), "etf": True,
         "lv": [rnd(m.get("wma_v"), 2), rnd(m.get("hi52"), 2), rnd(m.get("ma240"), 2)], "wu": m.get("wma_up"),
         "below52": (m["wma"] or 0) < 0 if m.get("wma") is not None else None}
    if hl is not None:
        r["bt"] = stock_bt(hl)
    return r


def col(frames, fld, t):
    return get_col(frames, fld, t)


def get_col(frames, fld, t):
    df = frames.get(fld)
    if df is None or df.empty or t not in df.columns:
        return None
    return df[t]


def fund_fields(fd, px=None):
    """前端顯示用的基本面欄位"""
    if not fd:
        return {}
    fcfy = (fd["fcf"] / fd["mcap"]) if fd.get("fcf") is not None and fd.get("mcap") else None
    return {"roe": rnd(fd.get("roe"), 3), "de": rnd(fd.get("de"), 0), "pm": rnd(fd.get("pm"), 3),
            "rg": rnd(fd.get("rg"), 3), "eg": rnd(fd.get("eg"), 3), "fcfy": rnd(fcfy, 3)}


# ───────────────────────── 燈號變化通知 ─────────────────────────

LIGHT_ZH = {"cheap": "便宜", "fair": "合理", "exp": "偏貴", "trap": "基本面弱", None: "資料不足"}


def diff_alerts(market, old, new):
    """比較前一次和這一次的資料，找出燈號變化。回傳事件 list"""
    if not old:
        return []
    ev = []
    date = new.get("data_date")
    ob = {b["c"]: b for b in old.get("benchmarks", [])}
    for b in new.get("benchmarks", []):
        o = ob.get(b["c"])
        if o and o.get("l") != b.get("l") and b.get("l"):
            ev.append({"d": date, "m": market, "c": b["c"], "n": b["n"], "kind": "bench",
                       "from": o.get("l"), "to": b.get("l")})
    os_ = {s["c"]: s for s in old.get("stocks", [])}
    newly = []
    for s in new.get("stocks", []):
        o = os_.get(s["c"])
        if o and o.get("l") != "cheap" and s.get("l") == "cheap" and s.get("liq") and (s.get("q") or 0) >= 60:
            newly.append(s)
    newly.sort(key=lambda s: -(s.get("lt") or 0))
    for s in newly[:10]:
        ev.append({"d": date, "m": market, "c": s["c"], "n": s["n"], "kind": "stock",
                   "from": os_[s["c"]].get("l"), "to": "cheap"})
    return ev


def push_alerts(events):
    path = os.path.join(DATA, "alerts.json")
    hist = load_json(path, [])
    hist = (events + hist)[:200]
    save_json(path, hist)
    # 手機通知改由 scripts/notify.py 發 App 推播（2026-11-06 使用者決定取消 Telegram）


def picks_of(out):
    """衛星層四道濾網：熱門族群（近 4 週相對強弱前 1/3 且為正）＋資金分≥55＋財報轉強＋長線買點觸發，排除流動性不足與 ETF"""
    secs = [x for x in out.get("sectors", []) if x.get("rs4") is not None]
    secs.sort(key=lambda x: -x["rs4"])
    hot = {x["name"] for x in secs[:max(1, math.ceil(len(secs) / 3))] if x["rs4"] > 0}
    p = [r for r in out.get("stocks", []) if not r.get("etf") and r.get("liq") and r.get("sec") in hot
         and (r.get("f") or 0) >= 55 and r.get("fin") == "up" and r.get("trig")]
    p.sort(key=lambda r: -(r["lt"] if r.get("lt") is not None else -1))
    fin = {"up": sum(1 for r in out.get("stocks", []) if r.get("fin") == "up"),
           "down": sum(1 for r in out.get("stocks", []) if r.get("fin") == "down")}
    return [r["c"] for r in p], sorted(hot), fin


def finish(market, out, series):
    out["picks"], out["hot"], out["fin"] = picks_of(out)
    path = os.path.join(DATA, f"{market}.json")
    old = load_json(path, None)
    events = diff_alerts(market, old, out)
    save_json(path, out)
    write_series(market, series)
    push_alerts(events)
    if events:
        log(f"燈號變化 {len(events)} 筆")


def update_val_hist(market, today, snap):
    """記錄每天的大盤估值，累積久了可以算歷史百分位"""
    path = os.path.join(CACHE, f"val_hist_{market}.json")
    hist = load_json(path, {})
    if any(v is not None for v in snap.values()):
        hist[today.strftime("%Y-%m-%d")] = snap
    keep = sorted(hist.keys())[-2600:]
    hist = {k: hist[k] for k in keep}
    save_json(path, hist)
    out = dict(snap)
    for k, v in snap.items():
        vals = [h.get(k) for h in hist.values() if h.get(k) is not None]
        out[k + "_pct"] = rnd(float((np.array(vals) < v).mean()), 3) if v is not None and len(vals) >= 120 else None
    out["hist_days"] = len(hist)
    return out


# ───────────────────────── 台股 ─────────────────────────

ETF_EXCLUDE = re.compile(r"正2|反1|反向|槓桿|2倍|-1")


def tw_universe():
    """上市＋上櫃普通股和 ETF 清單（含產業別），來源：證交所 ISIN 公開頁"""
    cache = os.path.join(CACHE, "tw_universe.json")
    rows = []
    try:
        for mode, mkt in ((2, "上市"), (4, "上櫃")):
            r = http_get(f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}")
            html = r.content.decode("cp950", errors="ignore")
            tb = pd.read_html(io.StringIO(html), header=0)[0]
            cols = list(tb.columns)
            c_code = next(c for c in cols if "代號" in str(c))
            c_cfi = next((c for c in cols if "CFI" in str(c)), None)
            c_ind = next((c for c in cols if "產業" in str(c)), None)
            for _, x in tb.iterrows():
                s = str(x[c_code])
                if "　" not in s:
                    continue
                code, name = s.split("　", 1)
                code, name = code.strip(), name.strip()
                cfi = str(x[c_cfi]) if c_cfi else ""
                if cfi.startswith("ES") and code.isdigit() and len(code) == 4:
                    ind = str(x[c_ind]) if c_ind and pd.notna(x[c_ind]) else ""
                    rows.append({"c": code, "n": name, "m": mkt, "sec": ind.strip()})
                elif cfi.startswith("CE") and re.fullmatch(r"00\d{2,4}B?", code) and not ETF_EXCLUDE.search(name):
                    # 排除槓桿、反向、期貨型（代號結尾 L/R/U 已被正規式排除）
                    rows.append({"c": code, "n": name, "m": mkt,
                                 "sec": "債券ETF" if code.endswith("B") else "股票ETF", "etf": True})
            time.sleep(2)
        if len(rows) > 1000:
            save_json(cache, rows)
            log(f"台股清單 {len(rows)} 檔（ETF {sum(1 for r in rows if r.get('etf'))} 檔）")
            return rows
        raise RuntimeError(f"清單太少（{len(rows)}）")
    except Exception as e:  # noqa
        old = load_json(cache, [])
        log("台股清單抓取失敗，改用上次的清單：", e, len(old))
        if not old:
            raise
        return old


def twse_json(url):
    return http_get(url).json()


def tw_pe():
    """本益比、殖利率、股價淨值比（上市＋上櫃）。回傳 {code: (pe, yield, pb)}"""
    out = {}
    try:
        for x in twse_json("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"):
            code = x.get(find_key(x, ("code",), ("代號",)))
            pe = to_num(x.get(find_key(x, ("peratio",), ("本益比",))))
            dy = to_num(x.get(find_key(x, ("dividendyield",), ("殖利率",))))
            pb = to_num(x.get(find_key(x, ("pbratio",), ("淨值比",))))
            if code:
                out[str(code).strip()] = (pe, dy, pb)
    except Exception as e:  # noqa
        log("上市本益比失敗：", e)
    try:
        for x in twse_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"):
            code = x.get(find_key(x, ("companycode",), ("code",), ("代號",)))
            pe = to_num(x.get(find_key(x, ("priceearning",), ("peratio",), ("本益比",))))
            dy = to_num(x.get(find_key(x, ("yieldratio",), ("yield",), ("殖利率",))))
            pb = to_num(x.get(find_key(x, ("pricebook",), ("pbratio",), ("淨值比",))))
            if code:
                out[str(code).strip()] = (pe, dy, pb)
    except Exception as e:  # noqa
        log("上櫃本益比失敗：", e)
    log(f"本益比 {len(out)} 檔")
    return out


def tw_revenue():
    """月營收（上市＋上櫃）。回傳 {code: (資料年月, 單月年增率, 累計年增率, 當月營收, 去年當月營收)}"""
    out = {}
    for url in ("https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
                "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"):
        try:
            for x in twse_json(url):
                code = x.get(find_key(x, ("公司代號",), ("代號",)))
                ym = x.get(find_key(x, ("資料年月",)))
                yoy = to_num(x.get(find_key(x, ("去年同月增減",))))
                cum = to_num(x.get(find_key(x, ("前期比較增減",))))
                k_cur = next((k for k in x if str(k).endswith("當月營收") and "去年" not in str(k)), None)
                k_prev = next((k for k in x if "去年當月營收" in str(k)), None)
                if code:
                    ymd = roc_to_ymd(str(ym) + "01") if ym else None
                    out[str(code).strip()] = (ymd[:6] if ymd else None,
                                              yoy / 100 if yoy is not None else None,
                                              cum / 100 if cum is not None else None,
                                              to_num(x.get(k_cur)) if k_cur else None,
                                              to_num(x.get(k_prev)) if k_prev else None)
        except Exception as e:  # noqa
            log("月營收失敗：", url, e)
    log(f"月營收 {len(out)} 檔")
    return out


def t86(date_str):
    """上市個股三大法人買賣超（股數）。回傳 {code: [外資, 投信, 自營]} 或 None"""
    url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={date_str}&selectType=ALLBUT0999&response=json"
    j = twse_json(url)
    if j.get("stat") != "OK" or not j.get("data"):
        return None
    f = j["fields"]

    def idx(*ws, start=None):
        for i, name in enumerate(f):
            if all(w in name for w in ws) and (start is None or name.startswith(start)):
                return i
        return None
    ic = idx("代號")
    i_f = idx("外陸資買賣超")
    if i_f is None:
        i_f = idx("買賣超", start="外資")
    i_t = idx("買賣超", start="投信")
    i_d = idx("買賣超", start="自營商")
    out = {}
    for row in j["data"]:
        code = str(row[ic]).strip()
        out[code] = [to_num(row[i_f]) if i_f is not None else None,
                     to_num(row[i_t]) if i_t is not None else None,
                     to_num(row[i_d]) if i_d is not None else None]
    return out


def tpex_insti():
    """上櫃個股三大法人（最新一日）。回傳 (日期YYYYMMDD或None, {code: [外資, 投信, 自營]})"""
    out, dt = {}, None
    data = twse_json("https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading")
    for x in data:
        kc = find_key(x, ("companycode",), ("code",), ("代號",))
        kf = find_key(x, ("foreign", "difference"), ("foreign", "net"), ("外資", "買賣超"), ("外資", "淨"))
        kt = find_key(x, ("investmenttrust", "difference"), ("trust", "net"), ("投信", "買賣超"), ("投信", "淨"))
        kd = find_key(x, ("dealers", "difference"), ("dealer", "net"), ("自營商", "買賣超"), ("自營", "淨"))
        kdt = find_key(x, ("date",), ("日期",))
        if not kc:
            continue
        out[str(x[kc]).strip()] = [to_num(x.get(kf)) if kf else None,
                                   to_num(x.get(kt)) if kt else None,
                                   to_num(x.get(kd)) if kd else None]
        if kdt and not dt:
            dt = roc_to_ymd(str(x[kdt]))
    return dt, out


def roc_to_ymd(s):
    s = s.replace("/", "").replace("-", "").strip()
    try:
        if len(s) == 7:  # 1151104
            return f"{int(s[:3]) + 1911}{s[3:]}"
        if len(s) == 8:
            return s
    except ValueError:
        pass
    return None


def tw_market_totals(date_str):
    """大盤：上市三大法人金額、融資餘額變化、成交金額"""
    res = {"foreign": None, "trust": None, "dealer": None, "margin_chg": None, "turnover": None}
    try:
        j = twse_json(f"https://www.twse.com.tw/rwd/zh/fund/BFI82U?dayDate={date_str}&type=day&response=json")
        if j.get("stat") == "OK":
            dealer = 0.0
            for row in j["data"]:
                name, diff = str(row[0]), to_num(row[-1])
                if diff is None:
                    continue
                if "外資及陸資" in name:
                    res["foreign"] = diff
                elif name.startswith("投信"):
                    res["trust"] = diff
                elif name.startswith("自營商"):
                    dealer += diff
            res["dealer"] = dealer
    except Exception as e:  # noqa
        log("大盤法人失敗：", e)
    try:
        j = twse_json(f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={date_str}&selectType=MS&response=json")
        tables = j.get("tables") or []
        for t in tables:
            for row in t.get("data") or []:
                if "融資金額" in str(row[0]):
                    flds = t.get("fields") or []
                    i_today = next((i for i, n in enumerate(flds) if "今日餘額" in n), len(row) - 1)
                    i_prev = next((i for i, n in enumerate(flds) if "前日餘額" in n), None)
                    if i_prev is not None:
                        a, b = to_num(row[i_today]), to_num(row[i_prev])
                        if a is not None and b is not None:
                            res["margin_chg"] = (a - b) * 1000  # 仟元 → 元
    except Exception as e:  # noqa
        log("融資失敗：", e)
    try:
        for x in twse_json("https://openapi.twse.com.tw/v1/exchangeReport/FMTQIK")[-1:]:
            res["turnover"] = to_num(x.get(find_key(x, ("tradevalue",), ("成交金額",))))
    except Exception as e:  # noqa
        log("成交金額失敗：", e)
    return res


def recent_weekdays(n, today, hol=None):
    """最近 n 個交易日（跳過週末和證交所休市日）"""
    d, out = today, []
    hol = hol or {}
    while len(out) < n:
        k = d.strftime("%Y%m%d")
        if d.weekday() < 5 and k not in hol:
            out.append(k)
        d -= timedelta(days=1)
    return out


def tw_holidays():
    """證交所休市日（證交所開放 API），快取起來。回傳 {"YYYYMMDD": 名稱}"""
    path = os.path.join(CACHE, "tw_holidays.json")
    cache = load_json(path, {})
    try:
        for x in twse_json("https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"):
            name = str(x.get("Name", ""))
            if "最後交易" in name or "開始交易" in name:   # 這兩種是有交易的日子
                continue
            ymd = roc_to_ymd(str(x.get("Date", "")))
            if ymd:
                cache[ymd] = name
        save_json(path, cache)
    except Exception as e:  # noqa
        log("休市日抓取失敗，改用快取：", e)
    return cache


def tw_official_close():
    """證交所＋櫃買中心的當日收盤（官方資料），用來核對、補上 Yahoo 的台股價格。
    回傳 {代號: ("YYYY-MM-DD", 收盤, 成交股數)}"""
    out = {}
    try:
        for x in twse_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"):
            ymd, c = roc_to_ymd(str(x.get("Date", ""))), to_num(x.get("ClosingPrice"))
            if ymd and c:
                out[str(x.get("Code", "")).strip()] = (f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}", c, to_num(x.get("TradeVolume")) or 0)
    except Exception as e:  # noqa
        log("證交所當日行情失敗：", e)
    try:
        for x in twse_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"):
            ymd, c = roc_to_ymd(str(x.get("Date", ""))), to_num(x.get("Close"))
            if ymd and c:
                out[str(x.get("SecuritiesCompanyCode", "")).strip()] = (f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}", c, to_num(x.get("TradingShares")) or 0)
    except Exception as e:  # noqa
        log("櫃買中心當日行情失敗：", e)
    log(f"交易所當日收盤 {len(out)} 檔")
    return out


def tw_daily_extras(today):
    """每天累積的資料：加權股價報酬指數（含息）、外資產業持股比率；並抓除權息預告"""
    tri_p, qf_p = os.path.join(CACHE, "tw_tri.json"), os.path.join(CACHE, "tw_qfii.json")
    tri, qf, exd = load_json(tri_p, {}), load_json(qf_p, {}), {}
    try:
        for x in twse_json("https://openapi.twse.com.tw/v1/indicesReport/MFI94U"):
            ymd, v = roc_to_ymd(str(x.get("Date", ""))), to_num(x.get("TAIEXTotalReturnIndex"))
            if ymd and v:
                tri[ymd] = v
        save_json(tri_p, {k: tri[k] for k in sorted(tri)[-4000:]})
    except Exception as e:  # noqa
        log("報酬指數失敗：", e)
    try:
        snap = {str(x["IndustryCat"]).strip(): to_num(x.get("Percentage")) for x in twse_json("https://openapi.twse.com.tw/v1/fund/MI_QFIIS_cat")}
        if snap:
            qf[today.strftime("%Y%m%d")] = snap
        save_json(qf_p, {k: qf[k] for k in sorted(qf)[-120:]})
    except Exception as e:  # noqa
        log("外資產業持股失敗：", e)
    try:
        for x in twse_json("https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL"):
            ymd = roc_to_ymd(str(x.get("Date", "")))
            if ymd:
                exd[str(x.get("Code", "")).strip()] = (ymd, str(x.get("Exdividend", "")).strip(), to_num(x.get("CashDividend")))
    except Exception as e:  # noqa
        log("除權息預告失敗：", e)
    log(f"報酬指數 {len(tri)} 天、外資產業持股 {len(qf)} 天、除權息預告 {len(exd)} 檔")
    return tri, qf, exd


def qfii_by_sector(qf):
    """外資持股比率：最新值與跟約 20 個交易日前的差（累積不到 5 天就不算變化）"""
    if not qf:
        return {}
    ds = sorted(qf)
    now, old = qf[ds[-1]], qf[ds[max(0, len(ds) - 21)]] if len(ds) >= 5 else None
    norm = lambda n: str(n).rstrip("業")
    out = {}
    for k, v in now.items():
        if v is None:
            continue
        chg = (v - old[k]) if old and old.get(k) is not None else None
        out[norm(k)] = (v, chg, len(ds))
    return out


def finmind_get(params, timeout=60):
    tok = os.environ.get("FINMIND_TOKEN")
    if tok:
        params = dict(params, token=tok)
    r = requests.get("https://api.finmindtrade.com/api/v4/data", params=params, headers={"User-Agent": "market-radar/1.0"}, timeout=timeout)
    j = r.json()
    if j.get("status") != 200:
        raise RuntimeError(f"FinMind {j.get('status')}：{str(j.get('msg'))[:80]}")
    return j.get("data") or []


def backfill_tw_totals(today, thist):
    """大盤三大法人買賣超：快取不足 25 天時，用 FinMind（不需金鑰）一次補近 60 個交易日"""
    if len(thist) >= 25:
        return thist
    try:
        rows = finmind_get({"dataset": "TaiwanStockTotalInstitutionalInvestors", "start_date": (today - timedelta(days=100)).strftime("%Y-%m-%d")})
        by = {}
        for x in rows:
            by.setdefault(x["date"].replace("-", ""), {})[x["name"]] = (x.get("buy") or 0) - (x.get("sell") or 0)
        added = 0
        for k, b in by.items():
            if k in thist:
                continue
            dealer = (b.get("Dealer_self", 0) + b.get("Dealer_Hedging", 0)) if ("Dealer_self" in b or "Dealer_Hedging" in b) else b.get("Dealer")
            thist[k] = {"foreign": b.get("Foreign_Investor"), "trust": b.get("Investment_Trust"), "dealer": dealer,
                        "margin_chg": None, "turnover": None, "src": "FinMind"}
            added += 1
        log(f"FinMind 補大盤法人 {added} 天")
    except Exception as e:  # noqa
        log("FinMind 大盤法人失敗：", e)
    keep = sorted(thist)[-60:]
    thist = {k: thist[k] for k in keep}
    save_json(os.path.join(CACHE, "tw_totals.json"), thist)
    return thist


def backfill_tpex_insti(hist, uni, today):
    """上櫃個股的法人歷史：櫃買中心開放 API 只給當天，所以用 FinMind 一檔一檔回補。
    需要 FINMIND_TOKEN（免費金鑰）；每次最多 300 檔，每檔 25 天補一次。只補已經有的交易日，避免打斷上市股的連買天數"""
    tok = os.environ.get("FINMIND_TOKEN")
    if not tok or not hist:
        return hist
    path = os.path.join(CACHE, "tw_insti_fm.json")
    done = load_json(path, {})
    cutoff = (today - timedelta(days=25)).strftime("%Y-%m-%d")
    todo = [u["c"] for u in uni if u.get("m") == "上櫃" and not u.get("etf") and done.get(u["c"], "") < cutoff][:300]
    start = (today - timedelta(days=50)).strftime("%Y-%m-%d")
    n = 0
    for c in todo:
        try:
            rows = finmind_get({"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": c, "start_date": start}, timeout=30)
        except Exception as e:  # noqa
            log("FinMind 上櫃法人停止：", e)
            break
        by = {}
        for x in rows:
            v = by.setdefault(x["date"].replace("-", ""), [0.0, 0.0, 0.0])
            net = (x.get("buy") or 0) - (x.get("sell") or 0)
            if x["name"] == "Foreign_Investor":
                v[0] += net
            elif x["name"] == "Investment_Trust":
                v[1] += net
            elif x["name"] in ("Dealer_self", "Dealer_Hedging"):
                v[2] += net
        for k, v in by.items():
            if k in hist:
                hist[k].setdefault(c, v)
        done[c] = today.strftime("%Y-%m-%d")
        n += 1
        time.sleep(0.3)
    save_json(path, done)
    save_json(os.path.join(CACHE, "tw_insti.json"), hist)
    if n:
        log(f"FinMind 回補上櫃法人 {n} 檔")
    return hist


def update_tw_insti(today, hol=None):
    """維護個股法人歷史（最近 30 個交易日），第一次會自動補抓上市過去資料"""
    path = os.path.join(CACHE, "tw_insti.json")
    hist = load_json(path, {})
    tstr = today.strftime("%Y%m%d")
    dates = recent_weekdays(30 if len(hist) < 10 else 3, today, hol)
    for ds in dates:
        if ds in hist and ds != tstr:
            continue
        try:
            got = t86(ds)
            if got:
                hist.setdefault(ds, {}).update(got)
                log(f"  上市法人 {ds}：{len(got)} 檔")
        except Exception as e:  # noqa
            log(f"  上市法人 {ds} 失敗：", e)
        time.sleep(3.5)
    try:
        dt, got = tpex_insti()
        dt = dt or tstr
        if got:
            hist.setdefault(dt, {}).update(got)
            log(f"  上櫃法人 {dt}：{len(got)} 檔")
    except Exception as e:  # noqa
        log("  上櫃法人失敗：", e)
    keep = sorted(hist.keys())[-30:]
    hist = {k: hist[k] for k in keep}
    save_json(path, hist)
    return hist


def update_tw_totals_hist(today, totals):
    path = os.path.join(CACHE, "tw_totals.json")
    hist = load_json(path, {})
    if any(totals.get(k) is not None for k in ("foreign", "trust", "dealer")):
        hist[today.strftime("%Y%m%d")] = totals
    keep = sorted(hist.keys())[-60:]
    hist = {k: hist[k] for k in keep}
    save_json(path, hist)
    return hist


def run_tw(limit=None):
    today = datetime.now(TPE)
    uni = tw_universe()
    if limit:
        uni = [u for u in uni if u["c"] in ("2330", "2317", "2454", "1101", "0050", "0056", "00878", "00679B")] + \
              [u for u in uni if not u.get("etf")][:limit]
        uni = list({u["c"]: u for u in uni}.values())
    yt = {u["c"]: u["c"] + (".TW" if u["m"] == "上市" else ".TWO") for u in uni}
    bench = [("^TWII", "加權指數"), ("0050.TW", "元大台灣50"), ("006208.TW", "富邦台50")]
    tickers = list(yt.values()) + [b[0] for b in bench]
    log(f"下載日線 {len(tickers)} 檔")
    dly = yf_download(tickers, "2y", "1d")
    log("下載月線")
    mon = yf_download(tickers, "20y", "1mo")

    hol = tw_holidays()
    official = tw_official_close()
    tri, qf, exd = tw_daily_extras(today)
    pe = tw_pe()
    rev = tw_revenue()
    fund = update_fundamentals("tw", [yt[u["c"]] for u in uni if not u.get("etf")] + ["0050.TW"], today)
    insti = update_tw_insti(today, hol)
    insti = backfill_tpex_insti(insti, uni, today)
    totals = tw_market_totals(today.strftime("%Y%m%d"))
    thist = update_tw_totals_hist(today, totals)
    thist = backfill_tw_totals(today, thist)
    tdates = sorted(thist.keys())

    # 同族群估值中位數（用證交所的本益比、淨值比，沒有就用 Yahoo）
    def pe_pb(u):
        p, dy, pb = pe.get(u["c"], (None, None, None))
        fd = fund.get(yt[u["c"]], {})
        return (p if p is not None else fd.get("pe")), dy, (pb if pb is not None else fd.get("pb"))
    stocks_only = [u for u in uni if not u.get("etf")]
    pe_med = sector_medians([(u["sec"], pe_pb(u)[0]) for u in stocks_only], "pe")
    pb_med = sector_medians([(u["sec"], pe_pb(u)[2]) for u in stocks_only], "pb")

    idates = sorted(insti.keys())
    bt = BTAgg()
    rows, metrics, series = [], [], {}
    ind_items, tick_sec, tick_w, sec_i5 = [], {}, {}, {}
    flow_agg = FlowAgg()
    px_stat = {}
    for u in uni:
        t = yt[u["c"]]
        is_etf = bool(u.get("etf"))
        dc, dv = get_col(dly, "Close", t), get_col(dly, "Volume", t)
        if dc is None or dv is None:
            continue
        mc, mh, ml = get_col(mon, "Close", t), get_col(mon, "High", t), get_col(mon, "Low", t)
        # 用交易所官方收盤核對 Yahoo：Yahoo 少了最新一天就補上；同一天差超過 2% 就標「價格待確認」
        pxflag, oc = None, official.get(u["c"])
        ydc = dc.dropna()
        if oc and len(ydc):
            od = pd.Timestamp(oc[0])
            if od > ydc.index[-1]:
                dc, dv = dc.copy(), dv.copy()
                dc.loc[od], dv.loc[od] = oc[1], oc[2]
                pxflag = "filled"
            elif od == ydc.index[-1] and abs(float(ydc.iloc[-1]) / oc[1] - 1) > 0.02:
                pxflag = "mismatch"
        px_stat[pxflag or "ok"] = px_stat.get(pxflag or "ok", 0) + (1 if oc else 0)
        m = calc_stock(dc, dv, mc, mh, ml)
        if not m:
            continue
        hl = hist_lights(mc, mh, ml) if mc is not None else None
        if hl is not None and not m["chk"]:
            bt.add("etf" if is_etf else "stock", hl)

        hist_f = [insti[d].get(u["c"], [None] * 3)[0] for d in idates]
        hist_t = [insti[d].get(u["c"], [None] * 3)[1] for d in idates]
        fs, ts = streak(hist_f), streak(hist_t)
        tags = value_tags(m)
        if pxflag == "mismatch":
            tags.append("價格待確認（Yahoo 與交易所不同）")
        ex = exd.get(u["c"])
        if ex and 0 <= (pd.Timestamp(ex[0]) - pd.Timestamp(today.date())).days <= 21:
            kind = {"息": "除息", "權": "除權", "權息": "除權息"}.get(ex[1], "除權息")
            tags.append(f"即將{kind} {ex[0][4:6]}/{ex[0][6:]}" + (f"（現金 {ex[2]:g} 元）" if ex[2] else ""))
        if fs >= 3:
            tags.append(f"外資連買{fs}天")
        if ts >= 3:
            tags.append(f"投信連買{ts}天")
        if m["vr"] and m["vr"] >= 1.5:
            tags.append("量能放大")

        p, dy, pb = pe_pb(u)
        fd = fund.get(t) if not is_etf else None
        ym, ryoy, rcum, rv_cur, rv_prev = rev.get(u["c"], (None,) * 5)
        q, val, warn = None, None, []
        if not is_etf:
            q, warn = quality_score(fd, u["sec"], rcum)
            ff = fund_fields(fd)
            val = valuation_score(p, pb, ff.get("fcfy"), dy, u["sec"], pe_med, pb_med)
            if p and p > 0:
                tags.append(f"本益比{p:.0f}")
            if ryoy is not None and ryoy >= 0.2:
                tags.append(f"月營收年增{round(ryoy * 100)}%")
            ft = fin_trend(fd, ryoy)
            ff.update(ft)
            ind_items.append({"sec": u["sec"], "qs": (fd or {}).get("qs"), "rev_cur": rv_cur, "rev_prev": rv_prev,
                              "fin": ft["fin"], "revg": ryoy})
            tick_sec[t], tick_w[t] = u["sec"], m["tv20"]
        else:
            ff = {}
            if dy:
                tags.append(f"殖利率{dy:.1f}%")
        tags += warn
        lt, light = long_term(m, q, val, is_etf, warn)
        # 近 5 個交易日三大法人合計買賣超金額（股數 × 目前股價）
        i5 = None
        if idates:
            last5 = [insti[d].get(u["c"]) for d in idates[-5:]]
            if any(x for x in last5):
                i5 = sum(sum(v or 0 for v in x) for x in last5 if x) * m["px"]
                if not is_etf:
                    sec_i5[u["sec"]] = sec_i5.get(u["sec"], 0.0) + i5

        has_insti = any(v is not None for v in hist_f[-5:]) if idates else False
        flow_parts = [lin_down(m["vr"], 1.6, 0.7)]
        if has_insti:
            flow_parts += [max(0.0, min(100.0, 50 + fs * 7)), max(0.0, min(100.0, 50 + ts * 9))]
        liq = (m["tv20"] or 0) >= LIQ_MIN["tw"]
        extra = {"mk": u["m"], "fs": fs, "ts": ts, "pe": rnd(p, 1), "dy": rnd(dy, 2), "pb": rnd(pb, 2),
                 "liq": liq, "q": rnd(q, 1), "val": rnd(val, 1), "lt": rnd(lt, 1), "l": light,
                 "ryoy": rnd(ryoy, 3), "rcum": rnd(rcum, 3), "rym": ym, "i5": rnd(i5, 0), **ff}
        if not is_etf:
            fdaily, fsum = stock_flow(dc, get_col(dly, "High", t), get_col(dly, "Low", t), dv, insti, u["c"])
            flow_agg.add(u["sec"], fdaily)
            extra["fl"] = fsum
            mc_ = (fund.get(t) or {}).get("mcap")
            if mc_:
                extra["mcap"] = rnd(mc_, 0)
        if ex:
            extra["exd"] = ex[0]
        if pxflag:
            extra["pxc"] = pxflag
        if is_etf:
            extra["etf"] = True
        if m["chk"]:
            extra["chk"] = True
        rows.append(stock_row(u["c"], u["n"], u["sec"], m, flow_parts, tags, extra))
        if not is_etf:
            metrics.append({"sec": u["sec"], **{k: m[k] for k in ("tv5", "tv20", "tv_prev20", "r1w", "r1w_prev")}})
        series[u["c"]] = series_payload(mc, m["px"], hl) if mc is not None else None

    if len(rows) < 500 and not limit:
        raise RuntimeError(f"台股只算出 {len(rows)} 檔，資料可能沒抓完整，這次不更新網站資料")

    # 族群資金比重 → 把族群分數補進個股資金面
    secs = sector_table(metrics)
    weeks, swk = sector_weekly(dly["Close"], tick_sec, tick_w, vol_df=dly.get("Volume"))
    qfb = qfii_by_sector(qf)
    for s_ in secs:
        s_.update(swk.get(s_["name"], {}))
        s_["i5"] = rnd(sec_i5.get(s_["name"]), 0)
        qv = qfb.get(str(s_["name"]).rstrip("業"))
        if qv:
            s_["qfii"], s_["qfii_chg"], s_["qfii_days"] = rnd(qv[0], 2), rnd(qv[1], 2), qv[2]
    industry = industry_table(ind_items)
    sec_chg = {s["name"]: s["share_chg"] for s in secs}
    for r in rows:
        sc = sec_chg.get(r["sec"])
        if sc is not None and r["f"] is not None:
            bonus = max(-10.0, min(10.0, sc * 1000))  # 比重每增加 0.1 個百分點 +1 分
            r["f"] = rnd(max(0.0, min(100.0, r["f"] + bonus)), 1)

    benches = []
    for t, name in bench:
        dc, dv = get_col(dly, "Close", t), get_col(dly, "Volume", t)
        if dc is None:
            continue
        if dv is None:
            dv = pd.Series(0, index=dc.index)
        mc, mh, ml = get_col(mon, "Close", t), get_col(mon, "High", t), get_col(mon, "Low", t)
        m = calc_stock(dc, dv.fillna(0), mc, mh, ml)
        if m:
            hl = hist_lights(mc, mh, ml) if mc is not None else None
            if hl is not None and t != "^TWII":   # 加權指數是價格指數、不含息，不納入回測
                bt.add("bench", hl)
            code = t.replace(".TW", "")
            bpe = pe.get(code, (None,))[0] if code != "^TWII" else None
            if bpe is None and t in fund:
                bpe = fund[t].get("pe")
            benches.append(bench_row(code, name, m, hl, bpe))
            if t == "^TWII":
                benches[-1]["pi"] = True
            series[code] = series_payload(mc, m["px"], hl) if mc is not None else None
    for code in ("2330",):
        r = next((x for x in rows if x["c"] == code), None)
        if r:
            b = {k: r.get(k) for k in ("c", "n", "px", "ch", "b", "k", "dd", "pos", "v", "pl", "pe")}
            b["l"] = r["pl"]
            benches.append(b)

    def tot_streak(key):
        return streak([thist[d].get(key) for d in tdates])

    signals = sorted(
        [{"c": r["c"], "n": r["n"], "who": "外資", "days": r["fs"]} for r in rows if r["fs"] >= 5] +
        [{"c": r["c"], "n": r["n"], "who": "投信", "days": r["ts"]} for r in rows if r["ts"] >= 5],
        key=lambda x: -x["days"])[:30]

    # 大盤估值：全市場本益比、淨值比中位數、殖利率中位數，並累積歷史算百分位
    pes = [pe_pb(u)[0] for u in stocks_only]
    pbs = [pe_pb(u)[2] for u in stocks_only]
    dys = [pe.get(u["c"], (None, None, None))[1] for u in stocks_only]
    valn = update_val_hist("tw", today, {
        "pe_med": rnd(median_ok([x for x in pes if x and 0 < x < 200]), 2),
        "pb_med": rnd(median_ok([x for x in pbs if x and x > 0]), 2),
        "dy_med": rnd(median_ok([x for x in dys if x is not None]), 2),
        "pe_0050": rnd(fund.get("0050.TW", {}).get("pe"), 2),
    })

    idx = next((b for b in benches if b["c"] == "^TWII"), None)
    out = {
        "market": "tw", "updated": today.strftime("%Y-%m-%d %H:%M"),
        "data_date": dly["Close"].index[-1].strftime("%Y-%m-%d") if not dly["Close"].empty else None,
        "scanned": len(rows),
        "overview": {
            "index": {"name": "加權指數", "px": idx["px"] if idx else None, "ch": idx["ch"] if idx else None},
            "turnover": totals["turnover"],
            "margin_chg": totals["margin_chg"],
            "insti": {k: {"net": totals[k], "streak": tot_streak(k)} for k in ("foreign", "trust", "dealer")},
        },
        "valuation": valn, "backtest": bt.result(), "liq_min": LIQ_MIN["tw"],
        "weeks": weeks, "industry": industry,
        "flow": flow_agg.result(),
        "holidays": sorted(f"{k[:4]}-{k[4:6]}-{k[6:]}" for k in hol if k[:4] >= today.strftime("%Y")),
        "px_check": {"filled": px_stat.get("filled", 0), "mismatch": px_stat.get("mismatch", 0), "ok": px_stat.get("ok", 0)},
        "sectors": secs, "benchmarks": benches, "signals": signals, "stocks": rows,
    }
    finish("tw", out, series)
    log(f"台股完成：{len(rows)} 檔")


# ───────────────────────── 美股 ─────────────────────────

GICS_ZH = {
    "Information Technology": "資訊科技", "Technology": "資訊科技", "Financials": "金融",
    "Health Care": "醫療保健", "Consumer Discretionary": "非必需消費", "Consumer Staples": "必需消費",
    "Industrials": "工業", "Energy": "能源", "Materials": "原物料", "Utilities": "公用事業",
    "Real Estate": "房地產", "Communication Services": "通訊服務", "Communication": "通訊服務",
}
SECTOR_ETF = [("XLK", "科技"), ("SMH", "半導體"), ("XLC", "通訊"), ("XLY", "非必需消費"), ("XLF", "金融"),
              ("XLV", "醫療"), ("XLI", "工業"), ("XLE", "能源"), ("XLB", "原物料"), ("XLP", "必需消費"),
              ("XLU", "公用事業"), ("XLRE", "房地產")]
US_BENCH = [("^GSPC", "S&P 500"), ("^NDX", "那斯達克100"), ("IWM", "美國中小型股"), ("VT", "全球股市")]
# 長線投資常見 ETF（會出現在候選名單，sec 標成 ETF 類別）
US_ETF = [("VOO", "Vanguard S&P 500", "股票ETF"), ("VTI", "Vanguard 全美股市", "股票ETF"),
          ("QQQ", "Invesco 那斯達克100", "股票ETF"), ("VXUS", "Vanguard 美國以外股市", "股票ETF"),
          ("VEA", "Vanguard 已開發市場", "股票ETF"), ("VWO", "Vanguard 新興市場", "股票ETF"),
          ("SCHD", "Schwab 美國高股息", "股票ETF"), ("VIG", "Vanguard 股息成長", "股票ETF"),
          ("VYM", "Vanguard 高股息", "股票ETF"), ("VNQ", "Vanguard 房地產", "股票ETF"),
          ("SMH", "VanEck 半導體", "股票ETF"), ("BND", "Vanguard 全美債券", "債券ETF"),
          ("AGG", "iShares 美國綜合債券", "債券ETF"), ("TLT", "iShares 20年以上美債", "債券ETF"),
          ("IEF", "iShares 7-10年美債", "債券ETF"), ("LQD", "iShares 投資級公司債", "債券ETF"),
          ("GLD", "SPDR 黃金", "商品ETF")]
US_FIX = {"BRKB": "BRK-B", "BFB": "BF-B", "BFA": "BF-A", "LENB": "LEN-B", "HEIA": "HEI-A", "UHALB": "UHAL-B"}


def us_universe():
    """美股清單：S&P 500＋S&P 400 中型股（維基百科，含 GICS 產業），失敗改用 iShares IWB，再失敗用快取"""
    cache = os.path.join(CACHE, "us_universe.json")
    rows = []
    try:
        for url in ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                    "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"):
            r = http_get(url)
            for tb in pd.read_html(io.StringIO(r.text)):
                cols = [str(c) for c in tb.columns]
                if "Symbol" in cols and "Security" in cols and len(tb) > 300:
                    cg = next((c for c in tb.columns if "Sector" in str(c)), None)
                    for _, x in tb.iterrows():
                        sym = str(x["Symbol"]).strip().replace(".", "-")
                        if not sym or sym == "nan":
                            continue
                        sec = GICS_ZH.get(str(x[cg]).strip(), str(x[cg]).strip()) if cg else ""
                        rows.append({"c": sym, "n": str(x["Security"]).strip(), "sec": sec})
                    break
            time.sleep(1)
        rows = list({r["c"]: r for r in rows}.values())
        if len(rows) < 800:
            raise RuntimeError(f"維基百科清單太少（{len(rows)}）")
    except Exception as e:  # noqa
        log("維基百科清單失敗，改用 iShares IWB：", e)
        rows = []
        try:
            r = http_get("https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/"
                         "1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund")
            txt = r.text
            start = txt.find("Ticker,")
            df = pd.read_csv(io.StringIO(txt[start:]))
            if "Asset Class" in df.columns:
                df = df[df["Asset Class"] == "Equity"]
            for _, x in df.iterrows():
                sym = str(x["Ticker"]).strip()
                if not sym or sym in ("-", "nan"):
                    continue
                sym = US_FIX.get(sym, sym.replace(".", "-"))
                rows.append({"c": sym, "n": str(x.get("Name", sym)).title(),
                             "sec": GICS_ZH.get(str(x.get("Sector", "")).strip(), str(x.get("Sector", "")))})
        except Exception as e2:  # noqa
            log("iShares 也失敗：", e2)
    if len(rows) >= 800:
        save_json(cache, rows)
        log(f"美股清單 {len(rows)} 檔")
        return rows
    old = load_json(cache, [])
    log("美股清單改用上次的清單：", len(old))
    if not old:
        raise RuntimeError("沒有美股清單可用")
    return old


def run_us(limit=None):
    now = datetime.now(TPE)
    uni = us_universe()
    if limit:
        uni = [u for u in uni if u["c"] in ("AAPL", "MSFT", "NVDA", "INTC", "JPM")] + uni[:limit]
        uni = list({u["c"]: u for u in uni}.values())
    have = {u["c"] for u in uni}
    uni = uni + [{"c": c, "n": n, "sec": s, "etf": True} for c, n, s in US_ETF if c not in have]
    extra = [b[0] for b in US_BENCH] + [e[0] for e in SECTOR_ETF] + ["^IXIC", "^VIX", "^TNX", "SPY"]
    tickers = [u["c"] for u in uni] + extra
    log(f"下載日線 {len(tickers)} 檔")
    dly = yf_download(tickers, "2y", "1d")
    log("下載月線")
    mon = yf_download([u["c"] for u in uni] + [b[0] for b in US_BENCH], "20y", "1mo")
    fund = update_fundamentals("us", [u["c"] for u in uni if not u.get("etf")] + ["SPY"], now)

    spy = get_col(dly, "Close", "SPY")
    spy_r1m = None
    if spy is not None:
        s = spy.dropna()
        if len(s) >= 22:
            spy_r1m = float(s.iloc[-1] / s.iloc[-22] - 1)

    stocks_only = [u for u in uni if not u.get("etf")]
    pe_med = sector_medians([(u["sec"], fund.get(u["c"], {}).get("pe")) for u in stocks_only], "pe")
    pb_med = sector_medians([(u["sec"], fund.get(u["c"], {}).get("pb")) for u in stocks_only], "pb")

    bt = BTAgg()
    rows, metrics, series = [], [], {}
    ind_items, tick_sec, tick_w = [], {}, {}
    flow_agg = FlowAgg()
    for u in uni:
        t = u["c"]
        is_etf = bool(u.get("etf"))
        dc, dv = get_col(dly, "Close", t), get_col(dly, "Volume", t)
        if dc is None or dv is None:
            continue
        mc, mh, ml = get_col(mon, "Close", t), get_col(mon, "High", t), get_col(mon, "Low", t)
        m = calc_stock(dc, dv, mc, mh, ml)
        if not m:
            continue
        hl = hist_lights(mc, mh, ml) if mc is not None else None
        if hl is not None and not m["chk"]:
            bt.add("etf" if is_etf else "stock", hl)
        rs = (m["r1m"] - spy_r1m) if (m["r1m"] is not None and spy_r1m is not None) else None
        tags = value_tags(m)
        if m["vr"] and m["vr"] >= 1.5:
            tags.append("量能放大")
        if rs is not None and rs >= 0.05:
            tags.append("強於大盤")
        fd = fund.get(t) if not is_etf else None
        q, val, warn, ff = None, None, [], {}
        if not is_etf:
            q, warn = quality_score(fd, u["sec"])
            ff = fund_fields(fd)
            val = valuation_score((fd or {}).get("pe"), (fd or {}).get("pb"), ff.get("fcfy"), None,
                                  u["sec"], pe_med, pb_med)
            if fd and fd.get("pe") and fd["pe"] > 0:
                tags.append(f"本益比{fd['pe']:.0f}")
            ft = fin_trend(fd)
            ff.update(ft)
            ind_items.append({"sec": u["sec"], "qs": (fd or {}).get("qs"), "fin": ft["fin"], "revg": ft["frev"]})
            tick_sec[t], tick_w[t] = u["sec"], m["tv20"]
        tags += warn
        lt, light = long_term(m, q, val, is_etf, warn)
        flow_parts = [lin_down(m["vr"], 1.6, 0.7), lin_down(rs, 0.10, -0.10)]
        liq = (m["tv20"] or 0) >= LIQ_MIN["us"]
        extra = {"rs": rnd(rs, 4), "liq": liq, "q": rnd(q, 1), "val": rnd(val, 1), "lt": rnd(lt, 1), "l": light,
                 "pe": rnd((fd or {}).get("pe"), 1), "pb": rnd((fd or {}).get("pb"), 2), **ff}
        if is_etf:
            extra["etf"] = True
        else:
            fdaily, fsum = stock_flow(dc, get_col(dly, "High", t), get_col(dly, "Low", t), dv)
            flow_agg.add(u["sec"], fdaily)
            extra["fl"] = fsum
            mc_ = (fund.get(t) or {}).get("mcap")
            if mc_:
                extra["mcap"] = rnd(mc_, 0)
        if m["chk"]:
            extra["chk"] = True
        rows.append(stock_row(t, u["n"], u["sec"], m, flow_parts, tags, extra))
        if not is_etf:
            metrics.append({"sec": u["sec"], **{k: m[k] for k in ("tv5", "tv20", "tv_prev20", "r1w", "r1w_prev")}})
        series[t] = series_payload(mc, m["px"], hl) if mc is not None else None

    if len(rows) < 500 and not limit:
        raise RuntimeError(f"美股只算出 {len(rows)} 檔，資料可能沒抓完整，這次不更新網站資料")

    secs = sector_table(metrics)
    weeks, swk = sector_weekly(dly["Close"], tick_sec, tick_w, vol_df=dly.get("Volume"))
    for s_ in secs:
        s_.update(swk.get(s_["name"], {}))
    industry = industry_table(ind_items)
    sec_chg = {s["name"]: s["share_chg"] for s in secs}
    for r in rows:
        sc = sec_chg.get(r["sec"])
        if sc is not None and r["f"] is not None:
            r["f"] = rnd(max(0.0, min(100.0, r["f"] + max(-10.0, min(10.0, sc * 1000)))), 1)

    def last_chg(t):
        s = get_col(dly, "Close", t)
        if s is None:
            return None, None
        s = s.dropna()
        if len(s) < 2:
            return (rnd(s.iloc[-1], 2) if len(s) else None), None
        return rnd(s.iloc[-1], 2), rnd(s.iloc[-1] / s.iloc[-2] - 1, 4)

    etfs = []
    for t, name in SECTOR_ETF:
        dc, dv = get_col(dly, "Close", t), get_col(dly, "Volume", t)
        if dc is None or dv is None:
            continue
        m = calc_stock(dc, dv, None, None, None)
        if m:
            etfs.append({"c": t, "n": name, "r1m": rnd(m["r1m"], 4), "vr": rnd(m["vr"], 2), "ch": rnd(m["ch"], 4)})

    benches = []
    for t, name in US_BENCH:
        dc, dv = get_col(dly, "Close", t), get_col(dly, "Volume", t)
        if dc is None:
            continue
        if dv is None:
            dv = pd.Series(0, index=dc.index)
        mc, mh, ml = get_col(mon, "Close", t), get_col(mon, "High", t), get_col(mon, "Low", t)
        m = calc_stock(dc, dv.fillna(0), mc, mh, ml)
        if m:
            hl = hist_lights(mc, mh, ml) if mc is not None else None
            if hl is not None and t not in ("^GSPC", "^NDX"):   # 價格指數不含息，不納入回測
                bt.add("bench", hl)
            bpe = fund.get("SPY", {}).get("pe") if t == "^GSPC" else None
            benches.append(bench_row(t, name, m, hl, bpe))
            if t in ("^GSPC", "^NDX"):
                benches[-1]["pi"] = True
            if mc is not None:
                series[t] = series_payload(mc, m["px"], hl)

    ov = {}
    for key, t in (("spx", "^GSPC"), ("ixic", "^IXIC"), ("vix", "^VIX"), ("tnx", "^TNX")):
        px, ch = last_chg(t)
        ov[key] = {"px": px, "ch": ch}

    # 大盤估值：S&P 500 本益比、盈餘殖利率、股權風險溢酬（盈餘殖利率 − 10年期美債）
    spe = fund.get("SPY", {}).get("pe")
    ey = (1 / spe) if spe else None
    tnx = ov["tnx"]["px"] / 100 if ov["tnx"]["px"] is not None else None
    pes = [fund.get(u["c"], {}).get("pe") for u in stocks_only]
    valn = update_val_hist("us", now, {
        "pe_spx": rnd(spe, 2), "ey": rnd(ey, 4),
        "erp": rnd(ey - tnx, 4) if ey is not None and tnx is not None else None,
        "pe_med": rnd(median_ok([x for x in pes if x and 0 < x < 300]), 2),
    })

    out = {
        "market": "us", "updated": now.strftime("%Y-%m-%d %H:%M"),
        "data_date": dly["Close"].index[-1].strftime("%Y-%m-%d") if not dly["Close"].empty else None,
        "scanned": len(rows), "overview": ov, "etfs": etfs,
        "valuation": valn, "backtest": bt.result(), "liq_min": LIQ_MIN["us"],
        "weeks": weeks, "industry": industry,
        "flow": flow_agg.result(),
        "sectors": secs, "benchmarks": benches, "stocks": rows,
    }
    finish("us", out, series)
    log(f"美股完成：{len(rows)} 檔")


# ───────────────────────── 總經（戰術層） ─────────────────────────

FRED = [("curve", "T10Y2Y", "10Y−2Y 殖利率差", "%"), ("hy", "BAMLH0A0HYM2", "高收益債信用利差", "%"),
        ("real", "DFII10", "10年期實質利率", "%"), ("infl", "T10YIE", "10年期預期通膨", "%"),
        ("usd", "DTWEXBGS", "美元指數（廣義）", "")]
CYCLE = ["擴張初期", "擴張後期", "緊縮", "衰退"]


def fred_series(sid, start):
    """FRED 資料。有 FRED_API_KEY（免費金鑰）就走官方 API；沒有就用公開 CSV。
    公開 CSV 在 GitHub Actions 的雲端伺服器上常常連不上，所以等待時間設短，失敗就交給備援來源"""
    key = (os.environ.get("FRED_API_KEY") or "").strip()  # 貼 Secret 時常多帶空白或換行，FRED 會回 400
    if key:
        r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                         params={"series_id": sid, "api_key": key, "file_type": "json", "observation_start": start},
                         headers={"User-Agent": "market-radar/1.0"}, timeout=30)
        if r.status_code == 200:
            obs = r.json().get("observations") or []
            s_ = pd.Series({pd.Timestamp(o["date"]): to_num(o.get("value")) for o in obs}, dtype=float).dropna()
            if len(s_):
                return s_.sort_index()
        why = ""
        try:
            why = str(r.json().get("error_message") or "")[:160]  # FRED 的錯誤原因，不含金鑰本身
        except Exception:  # noqa
            pass
        log(f"  FRED API {sid}：HTTP {r.status_code} {why}，改用公開 CSV（金鑰長度 {len(key)}，應為 32）")
    # FRED 會擋偽裝成瀏覽器的 User-Agent（會一直不回應），所以用一般程式名稱
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}"
    last = None
    for i in range(2):
        try:
            r = requests.get(url, headers={"User-Agent": "market-radar/1.0"}, timeout=12)
            if r.status_code == 200:
                break
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa
            last = str(e)
        time.sleep(2)
    else:
        raise RuntimeError(f"FRED {sid} 失敗：{last}")
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = ["d", "v"]
    df["d"] = pd.to_datetime(df["d"])
    df["v"] = pd.to_numeric(df["v"], errors="coerce")
    return df.dropna().set_index("d")["v"]


def treasury_curve(start_year):
    """美國財政部每日殖利率（官網 CSV，不需金鑰）。回傳 (名目 DataFrame, 實質 DataFrame)，欄位是年期"""
    out = {}
    for kind in ("daily_treasury_yield_curve", "daily_treasury_real_yield_curve"):
        frames = []
        for y in range(start_year, datetime.now(TPE).year + 1):
            url = (f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
                   f"{y}/all?type={kind}&field_tdr_date_value={y}&page&_format=csv")
            try:
                r = requests.get(url, headers={"User-Agent": "market-radar/1.0"}, timeout=30)
                if r.status_code == 200 and r.text.startswith("Date"):
                    frames.append(pd.read_csv(io.StringIO(r.text)))
            except Exception as e:  # noqa
                log("  財政部殖利率失敗：", kind, y, e)
            time.sleep(0.5)
        if frames:
            df = pd.concat(frames)
            df["Date"] = pd.to_datetime(df["Date"], format="%m/%d/%Y")
            out[kind] = df.set_index("Date").sort_index().apply(pd.to_numeric, errors="coerce")
    return out.get("daily_treasury_yield_curve"), out.get("daily_treasury_real_yield_curve")


def weekly_payload(s, name, unit, weeks=156):
    w = s.resample("W-FRI").last().dropna().iloc[-weeks:]
    return {"name": name, "unit": unit, "d": [d.strftime("%Y-%m-%d") for d in w.index],
            "v": [rnd(v, 3) for v in w.values], "last": rnd(float(s.iloc[-1]), 3),
            "last_d": s.index[-1].strftime("%Y-%m-%d"),
            "chg13": rnd(float(w.iloc[-1] - w.iloc[-14]), 3) if len(w) > 14 else None}


# 戰術層規則（使用者規則：總經、財報、資金流三者一致才動，只調整核心股債比例 5–10%）
# 以下門檻是 AI 研究後的暫定值，使用者可以改（研究見 CLAUDE.md「AI 研究後的暫定設定」）
TAC = {
    "fin_grow_share": 0.50,   # 營收成長的產業少於 50% → 財報轉弱
    "flow_days": 20,          # 台股外資近 20 個交易日累計（只顯示參考；研究四顯示它沒有預警價值，資金流改看全球風向）
    "step": 5,                # 三條件一致：核心層股票減 5 個百分點，移到中期公債 IEF
    "step_ma52": 10,          # 再加上該市場大盤週線跌破 MA52：減 10 個百分點
}


def tactical_signal(macro_hit, risk_label, r20, idx):
    """三個條件分別判斷，每個市場各自給出建議調整幅度（只做防守，不加碼股票）"""
    macro_on = bool(macro_hit)
    mk = {}
    for m, name in (("tw", "台股"), ("us", "美股")):
        d = load_json(os.path.join(DATA, f"{m}.json"), None)
        fin_on, fin_why, flow_on, flow_why = None, "沒有資料", None, "沒有資料"
        if d and d.get("industry"):
            rows = [r for r in d["industry"]["rows"] if r.get("rev_yoy") is not None]
            up = sum(r.get("up", 0) for r in d["industry"]["rows"])
            dn = sum(r.get("down", 0) for r in d["industry"]["rows"])
            share = sum(1 for r in rows if r["rev_yoy"] > 0) / len(rows) if rows else None
            fin_on = bool((share is not None and share < TAC["fin_grow_share"]) or dn > up)
            fin_why = (f"營收成長的產業 {round(share * 100)}%" if share is not None else "營收資料不足") + f"，個股財報轉強 {up}／轉弱 {dn}"
        if r20 is not None:
            flow_on = risk_label == "偏避險"
            flow_why = f"全球資金風向：景氣循環股減防禦股近 4 週 {r20 * 100:+.1f}%（{risk_label}）"
        if m == "tw":
            th = load_json(os.path.join(CACHE, "tw_totals.json"), {})
            vals = [th[k].get("foreign") for k in sorted(th)[-TAC["flow_days"]:] if th[k].get("foreign") is not None]
            if len(vals) >= 5:
                tot = sum(vals)
                flow_why += f"；外資近 {len(vals)} 個交易日累計{'賣' if tot < 0 else '買'}超 {abs(tot) / 1e8:,.0f} 億（僅供參考，研究顯示沒有預警價值）"
        ix = next((i for i in idx if i["c"] == ("^TWII" if m == "tw" else "^GSPC")), None)
        below = bool(ix and ix["below"])
        on = macro_on and bool(fin_on) and bool(flow_on)
        step = (TAC["step_ma52"] if below else TAC["step"]) if on else 0
        mk[m] = {"name": name, "fin": fin_on, "fin_why": fin_why, "flow": flow_on, "flow_why": flow_why,
                 "below": below, "on": on, "step": step}
    return {"on": any(v["on"] for v in mk.values()), "macro": macro_hit, "macro_on": macro_on, "markets": mk,
            "below": [v["name"] for v in mk.values() if v["below"]], "rule": TAC}


def run_macro():
    """債務週期位置、資金風向、戰術層訊號。規則刻意簡單，網站上會列出判斷依據"""
    now = datetime.now(TPE)
    start = (now - timedelta(days=365 * 4)).strftime("%Y-%m-%d")
    raw, series = {}, {}
    src = {}
    for key, sid, name, unit in FRED:
        try:
            raw[key] = fred_series(sid, start)
            series[key] = weekly_payload(raw[key], name, unit)
            src[key] = "FRED"
        except Exception as e:  # noqa
            log("FRED 失敗：", sid, e)
        time.sleep(1)
    # FRED 連不上時：殖利率曲線、實質利率、預期通膨改用美國財政部資料（數值跟 FRED 同一來源）
    if any(k not in raw for k in ("curve", "real", "infl")):
        nom, real = treasury_curve(int(start[:4]))
        if nom is not None and "curve" not in raw and {"10 Yr", "2 Yr"} <= set(nom.columns):
            raw["curve"] = (nom["10 Yr"] - nom["2 Yr"]).dropna()
            series["curve"] = weekly_payload(raw["curve"], "10Y−2Y 殖利率差", "%"); src["curve"] = "美國財政部"
        if real is not None and "real" not in raw and "10 YR" in real.columns:
            raw["real"] = real["10 YR"].dropna()
            series["real"] = weekly_payload(raw["real"], "10年期實質利率", "%"); src["real"] = "美國財政部"
        if nom is not None and real is not None and "infl" not in raw and "10 YR" in real.columns:
            raw["infl"] = (nom["10 Yr"] - real["10 YR"]).dropna()
            series["infl"] = weekly_payload(raw["infl"], "10年期預期通膨", "%"); src["infl"] = "美國財政部（名目減實質）"
        log("財政部備援：", ", ".join(f"{k}={v}" for k, v in src.items()))
    px = yf_download(["GLD", "^TWII", "^GSPC", "XLK", "XLY", "XLI", "XLF", "XLB", "XLE", "XLP", "XLU", "XLV"],
                     "5y", "1d", chunk=20, pause=1)
    close = px.get("Close", pd.DataFrame())
    if "usd" not in raw:   # 美元：FRED 的廣義美元指數連不上就用 Yahoo 的 DXY（不同指數，但方向一致）
        try:
            dx = yf_download(["DX-Y.NYB"], "5y", "1d", chunk=1, pause=0).get("Close", pd.DataFrame())
            if "DX-Y.NYB" in dx:
                raw["usd"] = dx["DX-Y.NYB"].dropna()
                series["usd"] = weekly_payload(raw["usd"], "美元指數（DXY）", ""); src["usd"] = "Yahoo"
        except Exception as e:  # noqa
            log("美元指數備援失敗：", e)
    if "GLD" in close:
        series["gold"] = weekly_payload(close["GLD"].dropna(), "黃金（GLD）", "")

    # 債務週期：信用利差急升→衰退；殖利率曲線倒掛→緊縮；剛解除倒掛或曲線很平→擴張後期；其他→擴張初期
    why, stage = [], None
    c, h = raw.get("curve"), raw.get("hy")
    if h is None or len(h) <= 70:
        h = None
    if c is not None and len(c) > 300:
        cl = float(c.iloc[-1])
        hl = float(h.iloc[-1]) if h is not None else 0.0
        h3 = float(hl - h.iloc[-64]) if h is not None else None  # 約 3 個月前
        inv24 = bool((c.iloc[-504:] < 0).any())
        inv6 = bool((c.iloc[-126:] < 0).any())
        why.append(f"10Y−2Y 殖利率差 {cl:+.2f}%{'（倒掛中）' if cl < 0 else ''}")
        why.append(f"高收益債利差 {hl:.2f}%，3個月變化 {h3:+.2f}%" if h is not None else "高收益債利差：沒有資料（FRED 連不上），這次只用殖利率曲線判斷")
        if h is not None and (hl >= 6 or h3 >= 1.5):
            stage = 3
            why.append("信用利差偏高或急升，代表企業借錢變難，景氣有衰退壓力")
        elif cl < 0:
            stage = 2
            why.append("殖利率曲線倒掛，央行緊縮的後段")
        elif inv24 or cl < 0.5 or (h is not None and hl >= 4.5):
            stage = 1
            why.append("近兩年內曾倒掛、剛轉正，或曲線仍偏平" if inv24 else "曲線偏平或利差不低")
        else:
            stage = 0
            why.append("曲線陡、利差低，信用寬鬆")
        if "real" in raw:
            why.append(f"實質利率 {float(raw['real'].iloc[-1]):.2f}%（越高代表資金越緊）")
    else:
        inv6 = False
        h3 = None

    # 資金風向：景氣循環類股 vs 防禦類股（美國類股 ETF），近 4 週和 12 週報酬差
    cyc, dfn = ["XLK", "XLY", "XLI", "XLF", "XLB", "XLE"], ["XLP", "XLU", "XLV"]

    def grp_ret(cols, n):
        cs = [x for x in cols if x in close]
        if not cs:
            return None
        sub = close[cs].dropna()
        return float((sub.iloc[-1] / sub.iloc[-1 - n] - 1).mean()) if len(sub) > n else None
    r20 = (grp_ret(cyc, 20) or 0) - (grp_ret(dfn, 20) or 0) if grp_ret(cyc, 20) is not None else None
    r60 = (grp_ret(cyc, 60) or 0) - (grp_ret(dfn, 60) or 0) if grp_ret(cyc, 60) is not None else None
    risk = "中性"
    if r20 is not None:
        if r20 >= 0.02 and (r60 is None or r60 >= 0):
            risk = "偏冒險"
        elif r20 <= -0.02 and (r60 is None or r60 <= 0):
            risk = "偏避險"

    # 大盤 vs 週線年線（MA52）
    idx = []
    for t, name in (("^TWII", "台股加權"), ("^GSPC", "S&P 500")):
        if t not in close:
            continue
        w = close[t].dropna().resample("W-FRI").last().dropna()
        ma = w.rolling(52).mean()
        w2, ma2 = w.iloc[-156:], ma.iloc[-156:]
        idx.append({"c": t, "n": name, "d": [d.strftime("%Y-%m-%d") for d in w2.index],
                    "px": [rnd(v, 1) for v in w2.values], "ma": [rnd(v, 1) for v in ma2.values],
                    "below": bool(w.iloc[-1] < ma.iloc[-1]), "gap": rnd(float(w.iloc[-1] / ma.iloc[-1] - 1), 4)})

    # 戰術層訊號：總經條件（曲線近半年剛解除倒掛，或信用利差 3 個月急升 1.5%）＋ 大盤週線跌破年線
    c_last = float(c.iloc[-1]) if c is not None and len(c) else None
    macro_hit = []
    if c_last is not None and c_last > 0 and inv6:
        macro_hit.append("殖利率曲線近半年由負轉正")
    if h3 is not None and h3 >= 1.5:
        macro_hit.append("信用利差 3 個月急升超過 1.5%")
    below = [i["n"] for i in idx if i["below"]]
    tactical = tactical_signal(macro_hit, risk, r20, idx)

    out = {"updated": now.strftime("%Y-%m-%d %H:%M"), "series": series, "src": src,
           "stage": {"idx": stage, "name": CYCLE[stage] if stage is not None else None, "why": why},
           "risk": {"label": risk, "r20": rnd(r20, 4), "r60": rnd(r60, 4)},
           "tactical": tactical, "idx": idx}
    save_json(os.path.join(DATA, "macro.json"), out)
    log(f"總經完成：{out['stage']['name']}、{risk}、戰術層{'觸發' if tactical['on'] else '未觸發'}")


# ───────────────────────── 核心層與今日判讀（坤寶存摺自己加的） ─────────────────────────
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
    ap.add_argument("--market", choices=["tw", "us", "macro", "core", "summary", "all"], default="all")
    ap.add_argument("--limit", type=int, default=None, help="本機測試用：只跑前 N 檔個股")
    a = ap.parse_args()
    os.makedirs(CACHE, exist_ok=True)
    jobs = {"tw": [lambda: run_tw(a.limit)], "us": [lambda: run_us(a.limit)], "macro": [run_macro], "core": [run_core], "summary": [],
            "all": [lambda: run_tw(a.limit), lambda: run_us(a.limit), run_macro, run_core]}[a.market]
    errors = []
    for j_ in jobs:
        try:
            j_()
        except Exception as e:  # noqa
            log("失敗：", repr(e))
            errors.append(repr(e))
    try:
        run_summary()
    except Exception as e:  # noqa
        log("今日判讀失敗：", repr(e))
    if errors and len(errors) == len(jobs):
        raise SystemExit("全部失敗：" + "；".join(errors))


if __name__ == "__main__":
    main()
