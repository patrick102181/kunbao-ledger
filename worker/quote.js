/*
 * 坤寶存摺：盤中報價轉接（Cloudflare Worker，免費方案每天 10 萬次請求）
 *
 * 證交所、Yahoo 不讓網頁直接抓，所以經過這支自己的 Worker 轉一手。
 *   GET /quote?s=TW:0050,TW:00991A,US:RKLB
 *   → { t: "抓取時間", q: { "TW:0050": { p, prev, ch, t, src }, ... }, miss: [...] }
 *
 * 台股：先抓證交所 MIS（即時），抓不到的改 Yahoo（延遲約 20 分）
 * 美股：Yahoo（含盤前、盤後）
 * 只接受下面 ALLOW 列的網址呼叫；換網址的話改這裡就好
 */
const ALLOW = ["https://patrick102181.github.io", "http://localhost", "http://127.0.0.1"];
const UA = { "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36" };
const memo = new Map();   // 同一台 Worker 15 秒內同樣的查詢直接回快取，避免被來源擋

const num = (v) => { const x = parseFloat(String(v ?? "").replace(/,/g, "")); return Number.isFinite(x) && x > 0 ? x : null; };

export default {
  async fetch(req) {
    const origin = req.headers.get("Origin") || "";
    const ok = ALLOW.some((a) => origin === a || origin.startsWith(a + ":"));
    const cors = ok ? { "Access-Control-Allow-Origin": origin, "Vary": "Origin" } : {};
    const json = (o, status = 200) => new Response(JSON.stringify(o), { status, headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", ...cors } });
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: { ...cors, "Access-Control-Allow-Methods": "GET", "Access-Control-Max-Age": "86400" } });
    if (!ok) return json({ error: "這個網址不能用：只接受坤寶存摺呼叫" }, 403);
    const url = new URL(req.url);
    if (url.pathname === "/" || url.pathname === "/ping") return json({ ok: true, name: "kunbao-quote" });
    if (url.pathname !== "/quote") return json({ error: "找不到" }, 404);
    const keys = [...new Set((url.searchParams.get("s") || "").split(",").map((x) => x.trim().toUpperCase()).filter((x) => /^(TW|US):[A-Z0-9.\-]{1,12}$/.test(x)))].slice(0, 40);
    if (!keys.length) return json({ error: "沒有代號" }, 400);
    const ck = keys.join(","), hit = memo.get(ck);
    if (hit && Date.now() - hit.at < 15000) return json(hit.body);
    const q = {};
    const tw = keys.filter((k) => k.startsWith("TW:")).map((k) => k.slice(3));
    const us = keys.filter((k) => k.startsWith("US:")).map((k) => k.slice(3));
    if (tw.length) Object.assign(q, await twMis(tw));
    const need = [...tw.filter((c) => !q["TW:" + c]).flatMap((c) => [["TW:" + c, c + ".TW"], ["TW:" + c, c + ".TWO"]]), ...us.map((c) => ["US:" + c, c])];
    const got = await Promise.all(need.map(([k, y]) => yahoo(y).then((r) => [k, r]).catch(() => [k, null])));
    for (const [k, r] of got) if (r && !q[k]) q[k] = r;
    const body = { t: new Date().toISOString(), q, miss: keys.filter((k) => !q[k]) };
    memo.set(ck, { at: Date.now(), body });
    if (memo.size > 200) memo.delete(memo.keys().next().value);
    return json(body);
  },
};

// 證交所 MIS：上市、上櫃都問一次，只會回有的那個
export async function twMis(codes) {
  const out = {};
  for (let i = 0; i < codes.length; i += 20) {
    const ex = codes.slice(i, i + 20).flatMap((c) => [`tse_${c.toLowerCase()}.tw`, `otc_${c.toLowerCase()}.tw`]).join("|");
    try {
      const r = await fetch(`https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=${encodeURIComponent(ex)}&json=1&delay=0&_=${Date.now()}`,
        { headers: { ...UA, "Referer": "https://mis.twse.com.tw/stock/index.jsp" } });
      if (!r.ok) continue;
      const j = await r.json();
      for (const m of j.msgArray || []) {
        // z＝最新成交價；這 5 秒內沒成交時是「-」，改用最佳買價，再不行用開盤價、昨收
        const prev = num(m.y), p = num(m.z) ?? num(String(m.b || "").split("_")[0]) ?? num(m.o) ?? prev;
        if (!p || !m.c) continue;
        const d = String(m.d || ""), t = String(m.t || "");
        out["TW:" + String(m.c).toUpperCase()] = { p, prev, ch: prev ? p / prev - 1 : null, t: d.length === 8 ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)} ${t.slice(0, 5)}` : t.slice(0, 5), src: "證交所即時" };
      }
    } catch (e) { /* 被擋就交給 Yahoo */ }
  }
  return out;
}

// Yahoo：1 分鐘線、含盤前盤後，取最後一筆有成交的價格
export async function yahoo(sym) {
  const r = await fetch(`https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(sym)}?interval=1m&range=1d&includePrePost=true`, { headers: UA });
  if (!r.ok) return null;
  const res = (await r.json())?.chart?.result?.[0];
  if (!res || !res.meta) return null;
  const m = res.meta, ts = res.timestamp || [], cl = res.indicators?.quote?.[0]?.close || [];
  let p = null, at = null;
  for (let i = cl.length - 1; i >= 0; i--) if (num(cl[i])) { p = num(cl[i]); at = ts[i]; break; }
  if (!p) { p = num(m.regularMarketPrice); at = m.regularMarketTime; }
  if (!p) return null;
  const prev = num(m.chartPreviousClose ?? m.previousClose), reg = m.currentTradingPeriod?.regular;
  const tag = !reg || !at ? "" : at < reg.start ? "盤前" : at >= reg.end ? "盤後" : "盤中";
  const d = at ? new Date(at * 1000 + 8 * 3600e3).toISOString() : "";   // 一律顯示台灣時間
  return { p, prev, ch: prev ? p / prev - 1 : null, t: d ? `${d.slice(0, 10)} ${d.slice(11, 16)}` : "", src: sym.endsWith(".TW") || sym.endsWith(".TWO") ? "Yahoo（延遲約 20 分）" : `Yahoo ${tag}`.trim() };
}
