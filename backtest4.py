"""
四道濾網完整回測（衛星層選股）＋ App 的綜合買法

每週五收盤，照「當時」的資料重新選股，只用那一天以前已經公開的資料：
  1. 熱門族群：族群近 4 週相對大盤報酬（成交額加權）排前 1/3 而且是正的
  2. 資金分 ≥ 55：量能（近 5 日／近 60 日成交額）；美股再加相對大盤強弱；再加族群成交比重變化（±10 分）
     ※ 台股的三大法人連買天數沒有 12 年的歷史資料（要逐日抓證交所，太多），台股資金分只用量能和族群資金
  3. 財報轉強：營收年增、毛利率變化、EPS 年增，三項有兩項變好且沒有變差（跟 App 同一個 fin_trend）
     美股：美國證管會 SEC 的 XBRL 財報，用「申報日」判斷當時看不看得到
     台股：公開資訊觀測站的月營收（次月 10 日公布）與季報（5/15、8/14、11/14、隔年 3/31 公布）
  4. 長線買點觸發：回測週 MA52 不破、距 52 週高點 −20% 以上、月線 MACD 黃金交叉（任一個）
  另外排除流動性不足（近 20 日平均成交額 台股 3,000 萬、美股 1,000 萬）

測三件事：
  A. 選股本身：選進名單後 1／3／6／12 個月的報酬，跟同一週「所有流動性夠的股票」和大盤比
  B. App 的買法：通過四道濾網＋價格在買進區（週 MA52 到上方 4%）才買，照點位出場（跟 backtest.py 同一套）
  C. 只看點位、不看濾網（對照組，就是上一份回測）

限制：
  - 股票清單是現在還在的公司，下市的沒算到（倖存者偏差，結果偏樂觀）
  - 收盤價成交，沒算滑價；台股沒有法人歷史
  - 新聞面沒有歷史資料，不在回測內
"""
import argparse
import io
import json
import math
import os
import re
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

import market as M
from backtest import COST, sim_stock, stats

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
# SEC 規定 User-Agent 要有「名稱＋email」，只寫網址會被擋（回 HTML 不是 JSON）；用 GitHub 的公開代信地址，不用私人信箱
SEC_UA = {"User-Agent": "kunbao-ledger research patrick102181@users.noreply.github.com", "Accept-Encoding": "gzip, deflate"}
MOPS = "https://mopsov.twse.com.tw"
CACHE = os.path.join(M.DATA, "cache")
log = M.log


# ───────────────────────── 財報歷史 ─────────────────────────
def _quarters_from_facts(units, kind):
    """SEC 一個科目 → {季末日: (值, 最早申報日)}，含用年報減前三季推出的第 4 季"""
    q, fy = {}, {}
    for x in units:
        if not x.get("start") or not x.get("end") or x.get("val") is None:
            continue
        try:
            s, e, f = pd.Timestamp(x["start"]), pd.Timestamp(x["end"]), pd.Timestamp(x["filed"])
        except Exception:  # noqa
            continue
        d = (e - s).days
        tgt = q if 80 <= d <= 100 else fy if 350 <= d <= 380 else None
        if tgt is None:
            continue
        old = tgt.get(e)
        if old is None or f < old[1]:
            tgt[e] = (float(x["val"]), f, s)
    out = {e: (v, f) for e, (v, f, s) in q.items()}
    for e, (v, f, s) in fy.items():   # 第 4 季＝全年－前三季
        if any(abs((e - k).days) <= 10 for k in out):
            continue
        parts = [out[k] for k in out if s < k < e - pd.Timedelta(days=20)]
        if len(parts) == 3:
            out[e] = (v - sum(p[0] for p in parts), f)
    return out


def sec_fund(tickers):
    """美股：{ticker: [[季末, 申報日, 營收, 毛利, EPS], ...]}，快取在 cache/bt_sec.json"""
    path = os.path.join(CACHE, "bt_sec.json")
    cache = M.load_json(path, {})
    try:
        rr = requests.get("https://www.sec.gov/files/company_tickers.json", headers=SEC_UA, timeout=40)
        if rr.status_code != 200:
            raise RuntimeError(f"HTTP {rr.status_code} {rr.text[:120]}")
        cik = {v["ticker"].upper().replace(".", "-"): v["cik_str"] for v in rr.json().values()}
    except Exception as e:  # noqa
        log("SEC 代號表失敗：", e)
        return {t: cache[t] for t in tickers if t in cache}
    todo = [t for t in tickers if t not in cache and t in cik]
    log(f"SEC 財報：快取 {len(cache)}，這次抓 {len(todo)}")
    fails = 0
    for i, t in enumerate(todo):
        try:
            r = requests.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik[t]):010d}.json", headers=SEC_UA, timeout=40)
            if r.status_code != 200:
                fails += 1
                if fails >= 30 and i < 40:
                    log("SEC 一直拒絕：", r.status_code, r.text[:200])
                    break
                continue
            g = r.json().get("facts", {}).get("us-gaap", {})

            def pick(tags, unit):
                best = {}
                for tg in tags:
                    u = g.get(tg, {}).get("units", {}).get(unit)
                    if u:
                        qq = _quarters_from_facts(u, tg)
                        for k, v in qq.items():
                            best.setdefault(k, v)
                return best
            rev = pick(["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
                        "RevenueFromContractWithCustomerIncludingAssessedTax", "RevenuesNetOfInterestExpense"], "USD")
            gp = pick(["GrossProfit"], "USD")
            eps = pick(["EarningsPerShareDiluted", "EarningsPerShareBasic"], "USD/shares")
            ends = sorted(set(rev) | set(eps))
            rows = []
            for e in ends:
                fs = [x[1] for x in (rev.get(e), gp.get(e), eps.get(e)) if x]
                rows.append([e.strftime("%Y-%m-%d"), max(fs).strftime("%Y-%m-%d"),
                             rev[e][0] if e in rev else None, gp[e][0] if e in gp else None, eps[e][0] if e in eps else None])
            cache[t] = rows
        except Exception as e:  # noqa
            fails += 1
        time.sleep(0.15)
        if i % 100 == 99:
            log(f"  SEC {i + 1}/{len(todo)}")
            M.save_json(path, cache)
    M.save_json(path, cache)
    return {t: cache[t] for t in tickers if t in cache}


def _mops_get(url, data=None, tries=3):
    for i in range(tries):
        try:
            r = (requests.post(url, data=data, headers=UA, timeout=60) if data else requests.get(url, headers=UA, timeout=60))
            r.encoding = "utf-8" if data else "big5"
            txt = r.text
            if "FOR SECURITY REASONS" in txt or "查詢過於頻繁" in txt or "Overrun" in txt:
                log("  公開資訊觀測站要求放慢，等一下")
                time.sleep(30 * (i + 1))
                continue
            return txt
        except Exception as e:  # noqa
            log("  觀測站連線失敗", e)
            time.sleep(10 * (i + 1))
    return None


def _num(x):
    try:
        s = str(x).replace(",", "").strip()
        return float(s) if s not in ("", "--", "-", "nan") else None
    except Exception:  # noqa
        return None


def tw_monthly(start_year):
    """台股月營收年增率：{代號: {"YYYY-MM": 年增率}}，快取在 cache/bt_tw_rev.json"""
    path = os.path.join(CACHE, "bt_tw_rev2.json")  # 舊檔表頭沒對到、整份是空的
    cache = M.load_json(path, {})
    now = datetime.now(M.TPE)
    got = 0
    for y in range(start_year, now.year + 1):
        for m in range(1, 13):
            key = f"{y}-{m:02d}"
            if (y, m) >= (now.year, now.month) or (key in cache.get("_done", []) and (y, m) < (now.year, now.month - 2)):
                continue
            got0 = got
            for typ in ("sii", "otc"):
                html = _mops_get(f"{MOPS}/nas/t21/{typ}/t21sc03_{y - 1911}_{m}_0.html")
                if not html:
                    continue
                try:
                    for tb in pd.read_html(io.StringIO(html)):
                        tb.columns = [re.sub(r"\s+", "", "".join(str(c) for c in (col if isinstance(col, tuple) else (col,)))) for col in tb.columns]  # 表頭有 <br>，pandas 會變成「公司 代號」
                        cc = next((c for c in tb.columns if "公司代號" in c), None)
                        cy = next((c for c in tb.columns if "去年同月增減" in c), None)
                        if not cc or not cy:
                            continue
                        for _, x in tb.iterrows():
                            code = str(x[cc]).strip()
                            v = _num(x[cy])
                            if re.fullmatch(r"\d{4}", code) and v is not None:
                                cache.setdefault(code, {})[key] = v / 100
                                got += 1
                except Exception as e:  # noqa
                    log("  月營收解析失敗", key, typ, e)
                time.sleep(2.5)
            if got > got0:  # 有抓到才算完成，下次才不會跳過
                cache.setdefault("_done", []).append(key)
            else:
                log("  月營收沒抓到", key)
        log(f"  月營收 {y} 年完成")
        M.save_json(path, cache)
    log(f"台股月營收：這次新增 {got} 筆")
    return cache


def tw_quarterly(start_year):
    """台股季報：{代號: [[季別 YYYY-Qn, 營收, 毛利, EPS], ...]}（單季，已從累計數換算），快取在 cache/bt_tw_q.json"""
    path = os.path.join(CACHE, "bt_tw_q2.json")
    cache = M.load_json(path, {})
    cum = cache.get("_cum", {})
    now = datetime.now(M.TPE)
    for y in range(start_year, now.year + 1):
        for s in range(1, 5):
            key = f"{y}-Q{s}"
            if key in cache.get("_done", []):
                continue
            if datetime(y, [5, 8, 11, 12][s - 1], 20, tzinfo=M.TPE) + (timedelta(days=110) if s == 4 else timedelta(0)) > now:
                continue
            for typ in ("sii", "otc"):
                html = _mops_get(f"{MOPS}/mops/web/ajax_t163sb04",
                                 {"encodeURIComponent": 1, "step": 1, "firstin": 1, "off": 1, "isQuery": "Y",
                                  "TYPEK": typ, "year": str(y - 1911), "season": f"{s:02d}"})
                if not html:
                    continue
                try:
                    for tb in pd.read_html(io.StringIO(html)):
                        tb.columns = [re.sub(r"\s+", "", str(c)) for c in tb.columns]  # 「公司<br>代號」→「公司代號」
                        cols = list(tb.columns)
                        cc = next((c for c in cols if "公司代號" in c), None)
                        cr = next((c for c in cols if c.startswith("營業收入")), None) or next((c for c in cols if "收益" in c and "合計" in c), None)
                        cg = next((c for c in cols if "營業毛利" in c), None)
                        ce = next((c for c in cols if "基本每股盈餘" in c), None)
                        if not cc or not ce:
                            continue
                        for _, x in tb.iterrows():
                            code = str(x[cc]).strip()
                            if re.fullmatch(r"\d{4}", code):
                                cum.setdefault(code, {})[key] = [_num(x[cr]) if cr else None, _num(x[cg]) if cg else None, _num(x[ce])]
                except Exception as e:  # noqa
                    log("  季報解析失敗", key, typ, e)
                time.sleep(4)
            n = sum(1 for d in cum.values() if key in d)
            if n:
                cache.setdefault("_done", []).append(key)
            log(f"  季報 {key} {'完成 ' + str(n) + ' 家' if n else '沒抓到'}")
            cache["_cum"] = cum
            M.save_json(path, cache)
    # 累計數（年初到該季）換成單季
    out = {}
    for code, d in cum.items():
        rows = []
        for key in sorted(d):
            y, s = int(key[:4]), int(key[-1])
            cur = d[key]
            prev = d.get(f"{y}-Q{s - 1}") if s > 1 else None
            if s > 1 and prev is None:
                continue
            q = [(c - p) if (c is not None and p is not None) else (c if s == 1 else None) for c, p in zip(cur, prev or [0, 0, 0])]
            rows.append([key] + q)
        out[code] = rows
    return out


def tw_q_avail(key):
    """台股季報最晚公布日"""
    y, s = int(key[:4]), int(key[-1])
    return pd.Timestamp({1: f"{y}-05-15", 2: f"{y}-08-14", 3: f"{y}-11-14", 4: f"{y + 1}-03-31"}[s])


def fin_trend(qs, rev_yoy=None):
    """跟 App（market.py 的 fin_trend）同一套：qs＝[[年月, 營收, 毛利, 淨利, EPS], ...]（舊到新）"""
    qs = [q for q in (qs or []) if q[1]]
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
    if len(signs) < 2:
        return None
    up, dn = signs.count(1), signs.count(-1)
    return "up" if up >= 2 and dn == 0 else "down" if dn >= 2 else "flat"


# ───────────────────────── 每週選股 ─────────────────────────
def lin_down(x, good, bad):
    return M.lin_down(x, good, bad)


def weekly_signals(mk, uni, tick, d, fund_tw_rev=None, fund_tw_q=None, fund_us=None, idx_col=None):
    C, V, Hh = d["Close"], d.get("Volume"), d.get("High")
    cols = [tick[u["c"]] for u in uni if tick[u["c"]] in C.columns]
    C = C[cols].astype(float)
    V = (V[cols] if V is not None else pd.DataFrame(0, index=C.index, columns=cols)).reindex(C.index).fillna(0).astype(float)
    TV = C * V
    tv5, tv20, tv60 = TV.rolling(5).mean(), TV.rolling(20).mean(), TV.rolling(60).mean()
    tvp20 = TV.shift(5).rolling(20).mean()
    hi250 = C.rolling(250, min_periods=200).max()
    wk_day = C.index.to_series().resample("W-FRI").last().dropna()
    days = list(wk_day.values)
    WC = C.reindex(days)
    WC.index = wk_day.index
    WR = WC.pct_change(fill_method=None)
    wma = WC.rolling(52).mean()
    wpos = WC / wma - 1
    wup = wma > wma.shift(4)
    # 月線 MACD 黃金交叉（只用收完的月份）
    MCm = C.resample("ME").last()
    dif = MCm.ewm(span=12, adjust=False).mean() - MCm.ewm(span=26, adjust=False).mean()
    hist = dif - dif.ewm(span=9, adjust=False).mean()
    macx = (hist > 0) & ((hist.shift(1) <= 0) | (hist.shift(2) <= 0))
    macx[MCm.notna().cumsum() < 40] = False
    r1m = C / C.shift(21) - 1
    spy = None
    if mk == "us" and idx_col is not None:
        spy = idx_col / idx_col.shift(21) - 1
    sec = pd.Series({tick[u["c"]]: u["sec"] for u in uni if tick[u["c"]] in cols})
    code_of = {tick[u["c"]]: u["c"] for u in uni}

    picks, elig = {}, {}
    for wi in range(60, len(WC)):
        wd = WC.index[wi]
        day = pd.Timestamp(wk_day.iloc[wi])
        w20 = tv20.loc[day].clip(lower=0).fillna(0)
        liq = tv20.loc[day] >= M.LIQ_MIN[mk]
        # 族群近 4 週相對強弱
        win = WR.iloc[wi - 3:wi + 1]
        ok = win.notna().all() & (w20 > 0)
        if ok.sum() < 30:
            continue
        num = (win.fillna(0) * w20).sum(axis=1)
        mkt = num / (win.notna() * w20).sum(axis=1).replace(0, np.nan)
        mkt4 = float((1 + mkt).prod() - 1)
        rs4 = {}
        sh_chg = {}
        t5, tp = tv5.loc[day], tvp20.loc[day]
        T5, TP = t5[ok].sum(), tp[ok].sum()
        for name, cs in sec[ok[ok].index].groupby(sec[ok[ok].index]):
            idx = cs.index
            if len(idx) < 3:
                continue
            ww = w20[idx]
            sr = (win[idx].fillna(0) * ww).sum(axis=1) / (win[idx].notna() * ww).sum(axis=1).replace(0, np.nan)
            if sr.notna().all():
                rs4[name] = float((1 + sr).prod() - 1) - mkt4
            if T5 and TP:
                sh_chg[name] = t5[idx].sum() / T5 - tp[idx].sum() / TP
        ranked = sorted(rs4.items(), key=lambda x: -x[1])
        hot = {n for n, v in ranked[:max(1, math.ceil(len(ranked) / 3))] if v > 0}
        if not hot:
            continue
        # 每檔（向量化）
        mo = (wd.to_period("M") - 1).to_timestamp("M")   # 上個收完的月
        mm = macx.loc[:mo]
        mrow = mm.iloc[-1].reindex(cols).fillna(False).values.astype(bool) if len(mm) else np.zeros(len(cols), bool)
        wcr = WC.iloc[wi].values
        lq = liq.reindex(cols).fillna(False).values.astype(bool) & ~np.isnan(wcr)
        el = [t for t, ok_ in zip(cols, lq) if ok_]
        sec_arr = sec.reindex(cols).values
        hotm = np.array([x in hot for x in sec_arr]) & lq

        def ld(x, good, bad):
            x = np.asarray(x, dtype=float)
            v = np.clip((x - bad) / (good - bad), 0, 1) * 100
            return np.where(np.isnan(x), np.nan, v)
        vr = (tv5.loc[day] / tv60.loc[day]).reindex(cols).values
        parts = [ld(vr, 1.6, 0.7)]
        if mk == "us":
            sp = float(spy.loc[day]) if spy is not None and day in spy.index and pd.notna(spy.loc[day]) else np.nan
            parts.append(ld(r1m.loc[day].reindex(cols).values - sp, 0.10, -0.10))
        f = np.nanmean(np.vstack(parts), axis=0) if len(parts) > 1 else parts[0]
        bonus = np.array([max(-10.0, min(10.0, sh_chg[x] * 1000)) if x in sh_chg and sh_chg[x] == sh_chg[x] else 0.0 for x in sec_arr])
        f = np.clip(f + bonus, 0, 100)
        p_ = wpos.iloc[wi].reindex(cols).values
        up_ = wup.iloc[wi].reindex(cols).fillna(False).values.astype(bool)
        dd = (C.loc[day] / hi250.loc[day] - 1).reindex(cols).values
        trig = ((p_ >= 0) & (p_ <= 0.04) & up_) | (dd <= -0.20) | mrow
        cand = hotm & (np.nan_to_num(f, nan=0) >= 55) & trig
        pk = [t for t, c_ in zip(cols, cand) if c_ and fin_at(mk, code_of[t], t, day, fund_tw_rev, fund_tw_q, fund_us) == "up"]
        picks[wd] = pk
        elig[wd] = el
    return picks, elig, WC, wk_day


def fin_at(mk, code, t, day, rev, q, us):
    if mk == "us":
        rows = (us or {}).get(t)
        if not rows:
            return None
        av = [r for r in rows if pd.Timestamp(r[1]) <= day]
        qs = [[r[0][:7], r[2], r[3], None, r[4]] for r in av][-6:]
        return fin_trend(qs)
    rows = (q or {}).get(code) or []
    av = [r for r in rows if tw_q_avail(r[0]) <= day]
    qs = [[r[0], r[1], r[2], None, r[3]] for r in av][-6:]
    ry = None
    mrev = (rev or {}).get(code) or {}
    if mrev:
        # 次月 10 日公布：今天看得到的最新月份
        lastm = (day - pd.Timedelta(days=10)).to_period("M") - 1
        for k in range(3):
            key = str(lastm - k)
            if key in mrev:
                ry = mrev[key]
                break
    return fin_trend(qs, ry)


# ───────────────────────── 評估 ─────────────────────────
def forward(picks, elig, WC, idx_w, horizons=(4, 13, 26, 52)):
    """名單第一次出現那週買進，持有 N 週的報酬；同一週所有流動性夠的股票平均當對照"""
    rows, base = {h: [] for h in horizons}, {h: [] for h in horizons}
    prev = set()
    wks = list(WC.index)
    pos = {w: i for i, w in enumerate(wks)}
    for wd in sorted(picks):
        cur = set(picks[wd])
        new = cur - prev
        prev = cur
        i = pos[wd]
        for h in horizons:
            if i + h >= len(wks):
                continue
            fw = WC.iloc[i + h] / WC.iloc[i] - 1
            ix = float(idx_w.iloc[i + h] / idx_w.iloc[i] - 1) if idx_w is not None else None
            e = fw[elig[wd]].dropna()
            if len(e):
                base[h].append(float(e.mean()))
            for t in new:
                v = fw.get(t)
                if pd.notna(v):
                    rows[h].append((wd.year, float(v), ix, float(e.mean()) if len(e) else None))
    out = {}
    for h in horizons:
        r = rows[h]
        if not r:
            continue
        v = np.array([x[1] for x in r])
        ex_i = np.array([x[1] - x[2] for x in r if x[2] is not None])
        ex_b = np.array([x[1] - x[3] for x in r if x[3] is not None])
        yrs = {}
        for y, val, _, b in r:
            yrs.setdefault(y, []).append(val - (b or 0))
        out[f"{h}w"] = {"n": len(v), "avg": round(float(v.mean()), 4), "med": round(float(np.median(v)), 4),
                        "win": round(float((v > 0).mean()), 4),
                        "vs_index": round(float(ex_i.mean()), 4) if len(ex_i) else None,
                        "beat_index": round(float((ex_i > 0).mean()), 4) if len(ex_i) else None,
                        "vs_all_stocks": round(float(ex_b.mean()), 4) if len(ex_b) else None,
                        "beat_all_stocks": round(float((ex_b > 0).mean()), 4) if len(ex_b) else None,
                        "all_stocks_avg": round(float(np.mean(base[h])), 4) if base[h] else None,
                        "by_year_vs_all": {str(y): round(float(np.mean(a)), 4) for y, a in sorted(yrs.items())}}
    return out


def app_rule(mk, picks, WC, d, tick_codes, idx_col):
    """通過四道濾網的那幾週才允許進場，其他照點位規則（backtest.sim_stock）"""
    allow = {}
    for wd, pk in picks.items():
        for t in pk:
            allow.setdefault(t, set()).add(wd)
    trades = []
    for t, wks in allow.items():
        dc = M.col(d, "Close", t)
        if dc is None:
            continue
        for tr in sim_stock(dc, M.col(d, "High", t), M.col(d, "Volume", t), M.LIQ_MIN[mk], allow=wks):
            if idx_col is not None and not tr["open"]:
                a, b = idx_col.asof(tr["in"]), idx_col.asof(tr["out"])
                tr["idx"] = float(b / a - 1) if a and b else None
            trades.append(tr)
    closed = [t for t in trades if not t["open"]]
    buy, sell = COST[mk]
    for t in closed:
        t["ret"] = (1 + t["gross"]) * (1 - sell) / (1 + buy) - 1
    return stats(closed, COST[mk]), len(trades) - len(closed)


def run(mk, years, limit=None):
    uni = [u for u in (M.tw_universe() if mk == "tw" else M.us_universe()) if not u.get("etf")]
    if limit:
        uni = uni[:limit]
    tick = {u["c"]: (u["c"] + (".TW" if u.get("m") == "上市" else ".TWO")) if mk == "tw" else u["c"] for u in uni}
    idx_t = "^TWII" if mk == "tw" else "^GSPC"
    extra = [idx_t] + (["SPY"] if mk == "us" else [])
    log(f"{mk}：下載 {len(tick)} 檔 {years} 年日線")
    d = M.yf_download(list(tick.values()) + extra, f"{years}y", "1d")
    idx_col = M.col(d, "Close", idx_t)
    start = datetime.now(M.TPE).year - years
    if mk == "us":
        fu = sec_fund(list(tick.values()))
        rev = q = None
        cover = sum(1 for t in tick.values() if fu.get(t))
    else:
        fu = None
        rev = tw_monthly(start - 1)
        q = tw_quarterly(start - 1)
        cover = sum(1 for c in tick if (q or {}).get(c))
    log(f"{mk}：有財報歷史的 {cover}／{len(tick)} 檔")
    picks, elig, WC, wk_day = weekly_signals(mk, uni, tick, d, rev, q, fu, M.col(d, "Close", "SPY") if mk == "us" else None)
    idx_w = idx_col.reindex(list(wk_day.values)) if idx_col is not None else None
    if idx_w is not None:
        idx_w.index = wk_day.index
        idx_w = idx_w.reindex(WC.index)
    n_weeks = len(picks)
    sizes = [len(v) for v in picks.values()]
    fw = forward(picks, elig, WC, idx_w)
    rule, still = app_rule(mk, picks, WC, d, tick, idx_col)
    recent = {str(k.date()): [t.replace(".TWO", "").replace(".TW", "") for t in v] for k, v in list(picks.items())[-8:]}
    log(f"{mk}：{n_weeks} 週，平均每週 {np.mean(sizes) if sizes else 0:.1f} 檔")
    return {"universe": len(tick), "fund_cover": cover, "weeks": n_weeks,
            "picks_per_week": round(float(np.mean(sizes)), 2) if sizes else 0,
            "weeks_with_picks": round(float(np.mean([s > 0 for s in sizes])), 3) if sizes else 0,
            "forward": fw, "app_rule": rule, "app_rule_open": still, "recent_picks": recent}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="all", choices=["all", "tw", "us"])
    ap.add_argument("--years", type=int, default=12)
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    path = os.path.join(M.DATA, "backtest4.json")
    out = M.load_json(path, {}) or {}
    out["generated"] = datetime.now(M.TPE).strftime("%Y-%m-%d %H:%M")
    out["rules"] = __doc__.strip()
    for mk in (["tw", "us"] if a.market == "all" else [a.market]):
        try:
            out[mk] = run(mk, a.years, a.limit)
        except Exception as e:  # noqa
            import traceback
            traceback.print_exc()
            out[mk] = {"error": repr(e)}
        M.save_json(path, out)
    print(json.dumps({k: v for k, v in out.items() if k != "rules"}, ensure_ascii=False)[:60000])


if __name__ == "__main__":
    main()
