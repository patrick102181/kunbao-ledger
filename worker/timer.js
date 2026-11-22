// 坤寶存摺：準時鬧鐘（Cloudflare Worker，免費方案）
//
// GitHub 自己的排程常晚 4～8 小時才跑，這支 Worker 每 5 分鐘醒來一次，照下面的時間表（台灣時間）
// 準時去叫 GitHub 跑對應的工作。GitHub 原本的排程留著當備用：新聞、市場資料都會自己跳過重複的。
//
// 要設定的東西（Cloudflare 這支 Worker 的 Settings）：
//   1. Variables and Secrets：新增 Secret，名稱 GH_TOKEN，值是 GitHub 的 Fine-grained token
//      （只勾 kunbao-ledger、kunbao-ledger-data 兩個 repo，權限 Actions 選 Read and write）
//   2. Triggers → Cron Triggers：新增 */5 * * * *
//
// 打開 Worker 網址可以看時間表；網址後面加 /test 會叫 GitHub 跑一次「高手語錄」，用來確認金鑰有效（10 分鐘內只能按一次）。
const OWNER = "patrick102181";
const APP = "kunbao-ledger", DATA = "kunbao-ledger-data";
const WEEKDAY = [1, 2, 3, 4, 5], TUE_SAT = [2, 3, 4, 5, 6], EVERYDAY = [0, 1, 2, 3, 4, 5, 6];
const range = (from, to, stepMin) => {   // "09:15" 到 "13:45" 每 stepMin 分鐘
  const toM = (s) => +s.slice(0, 2) * 60 + +s.slice(3), out = [];
  for (let m = toM(from); m <= toM(to); m += stepMin) out.push(String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0"));
  return out;
};

// [名稱, repo, 工作檔, 參數, 星期幾（台灣，0=週日）, 時間（台灣，要是 5 的倍數）]
const JOBS = [
  ["台股市場資料", APP, "market.yml", { market: "tw" }, WEEKDAY, ["19:05", "22:15"]],         // 三大法人公布後；月營收公布後再一次
  ["美股市場資料", APP, "market.yml", { market: "us" }, TUE_SAT, ["05:35", "07:45"]],         // 美股收盤後
  ["新聞", DATA, "news.yml", { auto: "1" }, WEEKDAY, ["07:40", "21:00"]],                     // auto=1：這個時段跑過就跳過
  ["持股現價（台股）", DATA, "prices.yml", {}, WEEKDAY, [...range("09:15", "13:45", 30), "14:40", "22:15"]],
  ["持股現價（美股）", DATA, "prices.yml", {}, WEEKDAY, range("17:50", "23:50", 60)],
  ["持股現價（美股深夜）", DATA, "prices.yml", {}, TUE_SAT, [...range("00:50", "07:50", 60), "08:50"]],
  ["高手語錄", APP, "traders.yml", {}, EVERYDAY, ["08:15"]],
];
const DAYS = "日一二三四五六";

function taipei(ms) {   // 回傳台灣時間的星期與 "HH:MM"（以 5 分鐘為單位對齊）
  const d = new Date(Math.round(ms / 300000) * 300000 + 8 * 3600e3);
  return { dow: d.getUTCDay(), hm: String(d.getUTCHours()).padStart(2, "0") + ":" + String(d.getUTCMinutes()).padStart(2, "0") };
}
export function due(ms) { const { dow, hm } = taipei(ms); return JOBS.filter(([, , , , days, times]) => days.includes(dow) && times.includes(hm)); }

async function dispatch(env, [name, repo, wf, inputs]) {
  const r = await fetch(`https://api.github.com/repos/${OWNER}/${repo}/actions/workflows/${wf}/dispatches`, {
    method: "POST",
    headers: { Authorization: "Bearer " + env.GH_TOKEN, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "kunbao-timer", "Content-Type": "application/json" },
    body: JSON.stringify({ ref: "main", inputs }),
  });
  const ok = r.status === 204;
  const msg = ok ? "已送出" : `失敗 ${r.status}：${(await r.text()).slice(0, 200)}`;
  console.log(`${name}（${repo}/${wf}）${msg}`);
  return { name, ok, msg };
}

let lastTest = 0;
export default {
  async scheduled(event, env, ctx) {
    const jobs = due(event.scheduledTime);
    if (!jobs.length) return;
    if (!env.GH_TOKEN) { console.log("還沒設定 GH_TOKEN"); return; }
    ctx.waitUntil(Promise.all(jobs.map((j) => dispatch(env, j))));
  },
  async fetch(req, env) {
    const url = new URL(req.url);
    const text = (s, status = 200) => new Response(s, { status, headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" } });
    if (url.pathname === "/test") {
      if (!env.GH_TOKEN) return text("還沒設定 GH_TOKEN：到 Settings → Variables and Secrets 新增", 500);
      if (Date.now() - lastTest < 600e3) return text("10 分鐘內按過了，等一下再試", 429);
      lastTest = Date.now();
      const r = await dispatch(env, JOBS.find((j) => j[2] === "traders.yml"));
      return text(r.ok ? "成功：金鑰有效，GitHub 已開始跑「高手語錄」，約 1 分鐘後到 GitHub 的 Actions 頁看得到" : "失敗：" + r.msg, r.ok ? 200 : 500);
    }
    const now = taipei(Date.now());
    const lines = JOBS.map(([n, repo, wf, , days, times]) => `${n}（${wf}）\n  星期${days.map((d) => DAYS[d]).join("")}：${times.join(" ")}`);
    return text(`坤寶存摺準時鬧鐘\n現在台灣時間：星期${DAYS[now.dow]} ${now.hm}\n金鑰：${env.GH_TOKEN ? "已設定" : "還沒設定"}\n\n${lines.join("\n\n")}\n\n網址後面加 /test 可以測試金鑰`);
  },
};
