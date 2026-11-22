"""高手語錄：把 Trader-Archives（MIT，梭哈.AI 整理）的推文挑出跟長期定投有關的部分，轉繁體、分情境，輸出 data/traders.json。

用法：python traders.py <Trader-Archives 的本機路徑>
只挑跟「長期持有、定期定額、大跌心態、倉位風控、美股、BTC 大週期」有關的分類；
Meme、合約、套利、做市、空投這些短線玩法整個不收，免得跟坤寶的策略打架。
"""
import json, re, sys, glob, os, collections
from opencc import OpenCC

cc = OpenCC("s2twp")
SRC = sys.argv[1]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "traders.json")

# 交易員：顯示名、X 帳號、要收的分類（分類名的子字串）
TRADERS = {
    "kuigas": ("丰密 KuiGas", "KuiGas", ["长期主义", "大周期配置"]),
    "coolish": ("Wei Paul", "coolish", ["杠铃", "宏观周期"]),
    "0xcryptowizard": ("0xWizard", "0xcryptowizard", ["四步核心", "人性洞察", "周期与撤退"]),
    "xiaomu": ("川沐 Trumoo", "xiaomustock", ["美股 AI", "加密大周期", "仓位风控", "选股框架"]),
    "wallstreet0name": ("華爾街沒有名字", "WallStreet0Name", ["美股周期", "比特币大底", "止损风控", "踏空焦虑"]),
    "michael-liu": ("憨厚的麥總", "Michael_Liu93", ["大周期与逃顶", "交易系统建立"]),
    "thankucrypto": ("熬鷹資本", "thankUcrypto", ["心态", "仓位管理", "止损与出金"]),
    "ztzzbtc": ("ZTZZ", "ZTZZBTC", ["心智修炼", "仓位系统"]),
    "web3feng": ("風無向", "Web3Feng", ["宏观趋势", "负债心态"]),
    "0xsun": ("0xSun", "0xSunNFT", ["十年自省"]),
    "agintender": ("danny", "agintender", ["交易纪律"]),
    "0xmagnolia": ("magnolia", "0xmagnolia", ["蓝筹抄底"]),
}

# 情境（繁體關鍵字比對）。順序就是 App 上按鈕的順序。
SITU = [
    ("drop", "大跌時", r"熊市|暴跌|大跌|下跌|恐慌|抄底|底部|大底|回撤|腰斬|割肉|崩盤|血洗|熊"),
    ("dca", "定投", r"定投|定期|囤幣|長期持有|拿住|拿得住|長期主義|慢就是快|持有|複利|躺平"),
    ("btc", "比特幣", r"BTC|比特幣|大餅|\bbtc\b|減半"),
    ("us", "美股", r"美股|納指|納斯達克|標普|輝達|英偉達|半導體|科技股|ETF|QQQ|SPY|台積電|NVDA"),
    ("risk", "倉位風控", r"倉位|風控|停損|止損|現金流|槓桿|爆倉|分散|配置|出金|梭哈"),
    ("mind", "心態", r"心態|耐心|紀律|貪婪|恐懼|情緒|焦慮|踏空|FOMO|認知|人性|後悔"),
    ("cycle", "週期", r"週期|牛市|宏觀|降息|升息|流動性|美聯儲|聯準會"),
]
# 心法（站方整理的思維模型）手挑跟長期定投、大跌心態、資產安全有關的
KEEP_MODELS = {  # 標題關鍵字 → 放在哪些情境（站方自動標的不準，手動指定）
    "四步穿越牛熊": ["drop", "dca", "mind", "cycle"], "敦刻爾克撤退": ["cycle", "risk"],
    "人生底線思維": ["drop", "risk", "mind"], "做時代的股東": ["us", "dca"],
    "技術分析祛魅": ["mind", "risk"], "長期複利的身心": ["mind"],
    "熊市的兩種逼籌": ["drop", "btc", "mind"], "戰術上的勤奮": ["dca", "mind"],
    "抄底看無人問津": ["drop", "cycle"], "Beta，錯當成": ["risk", "cycle", "mind"],
    "資產安全底線": ["btc", "risk"],
}
NOISE = re.compile(r"空投|擼毛|土狗|Meme|meme|MEME|撸|打新|互動拿|抽獎|轉發|關注我|返傭|邀請碼|開單|做空|空單|多單|起飛|必漲|發財|暴富|百倍|十倍|千倍|翻倍|拉盤|帶單|做多|開多|開空|平倉|上倉位|合約|GMGN|Bot|十倍股|\$[A-Z]{2,}")

def tc(s):
    return cc.convert(s or "").strip()

def clip(s, n):
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"\*\*|__|^#+\s*|^>\s*", "", s, flags=re.M).strip()   # 長文的 markdown 記號拿掉
    s = re.sub(r"(?<=[\s：:])>\s+", "", s)   # 句中的引用記號
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"

items, models, stat = [], [], collections.Counter()
for key, (name, handle, doms) in TRADERS.items():
    d = os.path.join(SRC, key, "data")
    f = glob.glob(os.path.join(d, "*_featured.json"))
    if not f:
        print("找不到", key); continue
    for x in json.load(open(f[0], encoding="utf-8")):
        dn = x.get("domainName") or ""
        if not any(k in dn for k in doms):
            continue
        if x.get("qualityTier") != "S":
            continue
        role = x.get("roleOrder")
        if role not in (1, 3):  # 只收「核心原則」和「踩坑／復盤教訓」，盤中喊單與實盤案例不收
            continue
        raw = x.get("text") or ""
        if len(raw) < 40 or raw.lstrip().startswith("@"):  # 回覆別人的推文沒有上下文，不收
            continue
        t = tc(raw)
        if NOISE.search(t):
            continue
        note = tc(x.get("shortNote") or "")
        head = t[:120]
        # 情境要「真的在講這件事」：摘要或開頭就提到，或全文提到至少兩次；只在角落順口帶過的不算
        tags = [k for k, _, rx in SITU if re.search(rx, note) or re.search(rx, head) or len(re.findall(rx, t)) >= 2]
        if not tags:
            continue
        main = [k for k, _, rx in SITU if re.search(rx, note)]   # 站方摘要就在講這件事的，App 排前面
        items.append({
            "w": key, "d": (x.get("date") or "")[:10], "s": x.get("qualityScore") or 0,
            "g": tags, "n": note, "t": clip(t, 420),
            "u": x.get("tweetUrl") or "", "k": "原則" if role == 1 else "踩坑", "p": main,
        })
        stat[key] += 1
    m = glob.glob(os.path.join(d, "*_mental_models.json"))
    if m:
        for x in json.load(open(m[0], encoding="utf-8")):
            title = tc(x.get("title"))
            hit = [v for k, v in KEEP_MODELS.items() if k in title]
            if not hit:
                continue
            q = tc(x.get("quote")); body = q + tc(x.get("summary")) + "".join(tc(r) for r in x.get("rules") or [])
            tags = hit[0]
            models.append({
                "w": key, "title": title, "q": clip(q, 200), "sum": tc(x.get("summary")),
                "rules": [tc(r) for r in (x.get("rules") or [])][:3], "g": tags,
            })

items.sort(key=lambda x: (-x["s"], x["d"]), reverse=False)
out = {
    "generated": __import__("datetime").date.today().isoformat(),
    "source": "https://github.com/suoha888/Trader-Archives",
    "credit": "梭哈.AI（@SUOHA_AI）整理，MIT 授權",
    "who": {k: {"n": v[0], "x": v[1]} for k, v in TRADERS.items()},
    "situ": [[k, lab] for k, lab, _ in SITU],
    "models": models,
    "items": items,
}
os.makedirs(os.path.dirname(OUT), exist_ok=True)
json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
print("推文", len(items), "心法", len(models), "大小", os.path.getsize(OUT) // 1024, "KB")
print(dict(stat))
print({lab: sum(1 for i in items if k in i["g"]) for k, lab, _ in SITU})
