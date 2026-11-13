"""
回測 App 裡的點位規則（跟 index.html 的 recLv／addLv 用同一套數字）

一、個股點位（衛星層）
  買進：週收盤在週 MA52 到上方 4% 之間，而且週 MA52 比 4 週前高（往上彎）
        近 20 日平均成交額要過門檻（台股 3,000 萬台幣／美股 1,000 萬美元）
  出場線：週收盤跌破「當週的週 MA52 × 0.97」就用那週收盤價賣
  目標：進場當下的 52 週高點、年線 × 1.3，只留比買進區上緣高 5% 以上的；
        兩個都有就各賣一半，只有一個就全賣；日線最高價碰到就用目標價成交
  成本：台股買 0.1425%、賣 0.4425%（含證交稅）；美股兩邊各 0.05%
  比較：同一段持有期間的大盤（台股 ^TWII、美股 ^GSPC）

二、ETF／比特幣補倉點（核心層）
  每月第一個交易日扣 1 期；ETF 跌到年線、年線 -10%、年線 -20% 各多扣 1 期
  比特幣用 200 日線、×0.8、×0.6
  比較「只定期定額」跟「定期定額＋補倉」的年化報酬（XIRR）

限制：
  1. 只測點位規則，四道濾網裡的「熱門族群、資金流、財報轉強」沒有歷史資料，沒辦法一起測
  2. 股票清單是「現在還在」的公司，下市的沒算進去，結果會偏樂觀（倖存者偏差）
  3. 用收盤價成交、沒算滑價
"""
import argparse
import json
import math
import os
from datetime import datetime

import numpy as np
import pandas as pd

import market as M

COST = {"tw": (0.001425, 0.004425), "us": (0.0005, 0.0005)}
INDEX = {"tw": "^TWII", "us": "^GSPC"}


def sim_stock(dc, dh, dv, liq_min):
    """單一檔：回傳交易清單"""
    dc = dc.dropna()
    if len(dc) < 400:
        return []
    dh = (dh if dh is not None else dc).reindex(dc.index).fillna(dc)
    dv = (dv if dv is not None else pd.Series(0, index=dc.index)).reindex(dc.index).fillna(0)
    tv20 = (dc * dv).rolling(20).mean()
    ma240 = dc.rolling(240).mean()
    hi250 = dc.rolling(250).max()
    wk = dc.resample("W-FRI").last().dropna()
    wma = wk.rolling(52).mean()
    wup = wma > wma.shift(4)
    # 每週對應到的最後一個交易日
    wk_day = dc.index.to_series().resample("W-FRI").last().dropna()
    trades, pos = [], None
    days = dc.index
    for i in range(57, len(wk)):
        wd = wk.index[i]
        day = wk_day.get(wd)
        if day is None or pd.isna(day):
            continue
        c, ma = float(wk.iloc[i]), float(wma.iloc[i])
        if not (ma > 0):
            continue
        if pos is not None:
            # 這週每天先看有沒有碰到目標
            prev_day = pos["last_day"]
            seg = dh[(days > prev_day) & (days <= day)]
            for d_, h in seg.items():
                while pos["tg"] and h >= pos["tg"][0] and pos["left"] > 1e-9:
                    t = pos["tg"].pop(0)
                    part = 0.5 if (pos["n_tg"] == 2 and len(pos["tg"]) == 1) else pos["left"]   # 兩個目標：第一個賣一半
                    pos["fills"].append((part, t, "目標"))
                    pos["left"] -= part
                    pos["exit_day"] = d_
                if pos["left"] <= 1e-9:
                    break
            pos["last_day"] = day
            if pos["left"] > 1e-9 and c < ma * 0.97:
                pos["fills"].append((pos["left"], c, "出場線"))
                pos["left"] = 0
                pos["exit_day"] = day
            if pos["left"] <= 1e-9:
                trades.append(close_pos(pos))
                pos = None
            continue
        if not (0 <= c / ma - 1 <= 0.04 and bool(wup.iloc[i])):
            continue
        if not (tv20.get(day, 0) >= liq_min):
            continue
        b_hi, stop = ma * 1.04, ma * 0.97
        hi, y = float(hi250.get(day, np.nan)), float(ma240.get(day, np.nan))
        tg = sorted(x for x in (hi if hi > b_hi * 1.05 else None, y * 1.3 if y == y else None)
                    if x is not None and x > b_hi * 1.05)
        rr = (tg[0] - c) / (c - stop) if tg and c > stop else None
        pos = {"in_day": day, "px": c, "tg": list(tg), "n_tg": len(tg), "left": 1.0, "fills": [], "last_day": day,
               "rr": rr, "exit_day": None}
    if pos is not None:
        pos["fills"].append((pos["left"], float(dc.iloc[-1]), "還沒出場"))
        pos["left"] = 0
        pos["exit_day"] = dc.index[-1]
        trades.append(close_pos(pos))
    return trades


def close_pos(p):
    out = sum(w * px for w, px, _ in p["fills"])
    why = [k for _, _, k in p["fills"]]
    return {"in": p["in_day"], "out": p["exit_day"], "px": p["px"], "gross": out / p["px"] - 1, "rr": p["rr"],
            "t1": "目標" in why, "stop": why[-1] == "出場線", "open": why[-1] == "還沒出場"}


def stats(tr, cost):
    tr = [t for t in tr if not t["open"]]
    if not tr:
        return None
    buy, sell = cost
    r = np.array([(1 + t["gross"]) * (1 - sell) / (1 + buy) - 1 for t in tr])
    ex = np.array([t["ret"] - t["idx"] for t in tr if t.get("idx") is not None])
    hold = np.array([(t["out"] - t["in"]).days / 7 for t in tr])
    for t, x in zip(tr, r):
        t["ret"] = x
    order = sorted(tr, key=lambda t: t["out"])
    streak = best = 0
    for t in order:
        streak = streak + 1 if t["ret"] <= 0 else 0
        best = max(best, streak)
    win, loss = r[r > 0], r[r <= 0]
    yrs = {}
    for t in tr:
        yrs.setdefault(t["in"].year, []).append(t["ret"])
    return {
        "n": int(len(r)), "win": round(float((r > 0).mean()), 4), "avg": round(float(r.mean()), 4),
        "med": round(float(np.median(r)), 4), "avg_win": round(float(win.mean()), 4) if len(win) else None,
        "avg_loss": round(float(loss.mean()), 4) if len(loss) else None,
        "payoff": round(float(win.mean() / -loss.mean()), 2) if len(win) and len(loss) and loss.mean() < 0 else None,
        "hold_wk": round(float(np.median(hold)), 1), "t1_rate": round(float(np.mean([t["t1"] for t in tr])), 4),
        "stop_rate": round(float(np.mean([t["stop"] for t in tr])), 4), "max_losing_streak": int(best),
        "worst": round(float(r.min()), 4), "best": round(float(r.max()), 4),
        "excess_vs_index": round(float(ex.mean()), 4) if len(ex) else None,
        "beat_index": round(float((ex > 0).mean()), 4) if len(ex) else None,
        "by_year": {str(y): {"n": len(v), "win": round(float(np.mean(np.array(v) > 0)), 3), "avg": round(float(np.mean(v)), 4)}
                    for y, v in sorted(yrs.items())},
    }


def run_market(mk, limit=None, period="12y"):
    uni = M.tw_universe() if mk == "tw" else M.us_universe()
    uni = [u for u in uni if not u.get("etf")]
    if limit:
        uni = uni[:limit]
    if mk == "tw":
        tick = {u["c"]: u["c"] + (".TW" if u["m"] == "上市" else ".TWO") for u in uni}
    else:
        tick = {u["c"]: u["c"] for u in uni}
    tks = list(tick.values()) + [INDEX[mk]]
    M.log(f"{mk} 回測：下載 {len(tks)} 檔日線（{period}）")
    d = M.yf_download(tks, period, "1d")
    idx = M.col(d, "Close", INDEX[mk])
    trades = []
    for u in uni:
        t = tick[u["c"]]
        dc = M.col(d, "Close", t)
        if dc is None:
            continue
        for tr in sim_stock(dc, M.col(d, "High", t), M.col(d, "Volume", t), M.LIQ_MIN[mk]):
            tr["c"] = u["c"]
            tr["n"] = u.get("n", "")
            if idx is not None and not tr["open"]:
                try:
                    a = idx.asof(tr["in"]); b = idx.asof(tr["out"])
                    tr["idx_gross"] = float(b / a - 1) if a and b else None
                except Exception:  # noqa
                    tr["idx_gross"] = None
            trades.append(tr)
    closed = [t for t in trades if not t["open"]]
    buy, sell = COST[mk]
    for t in closed:
        t["ret"] = (1 + t["gross"]) * (1 - sell) / (1 + buy) - 1
        t["idx"] = t.get("idx_gross")
    def era(a, b):
        return stats([t for t in closed if pd.Timestamp(a) <= t["in"] < pd.Timestamp(b)], COST[mk])
    res = {"universe": len(uni), "all": stats(closed, COST[mk]),
           "eras": {"2000-2003 網路泡沫": era("2000-01-01", "2003-04-01"), "2007-2009 金融海嘯": era("2007-07-01", "2009-04-01"),
                    "2020 疫情崩跌": era("2020-01-01", "2020-04-01"), "2022 升息熊市": era("2022-01-01", "2022-11-01")},
           "rr2": stats([t for t in closed if t["rr"] is not None and t["rr"] >= 2], COST[mk]),
           "open_now": len([t for t in trades if t["open"]])}
    recent = sorted(closed, key=lambda t: t["out"])[-15:]
    res["recent"] = [{"c": t["c"], "n": t["n"], "in": str(t["in"].date()), "out": str(t["out"].date()),
                      "ret": round(t["ret"], 4), "why": "目標" if t["t1"] and not t["stop"] else "出場線" if t["stop"] else "目標"}
                     for t in recent]
    M.log(f"{mk} 回測完成：{len(closed)} 筆")
    return res


def xirr(flows):
    """flows: [(日期, 金額)]，投入是負的"""
    if not flows:
        return None
    t0 = flows[0][0]

    def npv(r):
        return sum(v / (1 + r) ** ((d - t0).days / 365.25) for d, v in flows)
    lo, hi = -0.99, 5.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def dca_test(name, dc, base_n, ks):
    dc = dc.dropna()
    base = dc.rolling(base_n).mean()
    months = dc.groupby([dc.index.year, dc.index.month]).head(1)
    months = months[base.reindex(months.index).notna()]
    if len(months) < 24:
        return None
    last_d, last_p = dc.index[-1], float(dc.iloc[-1])
    plain, plus, extra = [], [], []
    u_plain = u_plus = u_extra = 0.0
    extra_cnt = 0
    for d, p in months.items():
        p = float(p)
        b = float(base[d])
        n = sum(1 for k in ks if p <= b * k)
        plain.append((d, -1.0)); u_plain += 1 / p
        plus.append((d, -1.0 - n)); u_plus += (1 + n) / p
        if n:
            extra.append((d, -float(n))); u_extra += n / p; extra_cnt += n
    r = lambda fl, u: xirr(fl + [(last_d, u * last_p)])  # noqa
    return {"name": name, "from": str(months.index[0].date()), "months": len(months), "extra_buys": extra_cnt,
            "plain_xirr": rnd4(r(plain, u_plain)), "plus_xirr": rnd4(r(plus, u_plus)),
            "extra_only_xirr": rnd4(r(extra, u_extra)) if extra else None,
            "plain_multiple": round(u_plain * last_p / len(months), 3),
            "plus_multiple": round(u_plus * last_p / (len(months) + extra_cnt), 3)}


def rnd4(x):
    return round(x, 4) if x is not None and x == x else None


def run_core():
    d = M.yf_download(["0050.TW", "VT", "BTC-USD"], "max", "1d", chunk=3)
    out = []
    for t, name, n, ks in (("0050.TW", "0050 元大台灣50", 240, (1, 0.9, 0.8)), ("VT", "VT 全球股市", 240, (1, 0.9, 0.8)),
                           ("BTC-USD", "比特幣", 200, (1, 0.8, 0.6))):
        dc = M.col(d, "Close", t)
        if dc is not None:
            x = dca_test(name, dc, n, ks)
            if x:
                out.append(x)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--market", default="all", choices=["all", "tw", "us", "core"])
    ap.add_argument("--period", default="12y", help="12y 或 max（能抓多久就抓多久，美股約到 1990 年代、台股約到 2000 年）")
    a = ap.parse_args()
    path = os.path.join(M.DATA, "backtest.json")
    out = M.load_json(path, {}) or {}
    out["generated"] = datetime.now(M.TPE).strftime("%Y-%m-%d %H:%M")
    out["rules"] = __doc__.strip()
    for mk in (["tw", "us"] if a.market == "all" else [a.market] if a.market in ("tw", "us") else []):
        key = mk if a.period == "12y" else f"{mk}_{a.period}"
        try:
            out[key] = run_market(mk, a.limit, a.period)
        except Exception as e:  # noqa
            M.log(mk, "回測失敗", repr(e))
            out[key] = {"error": repr(e)}
    if a.market in ("all", "core") and a.period == "12y":
        try:
            out["core"] = run_core()
        except Exception as e:  # noqa
            out["core"] = {"error": repr(e)}
    M.save_json(path, out)
    print(json.dumps({k: v for k, v in out.items() if k != "rules"}, ensure_ascii=False)[:60000])


if __name__ == "__main__":
    main()
