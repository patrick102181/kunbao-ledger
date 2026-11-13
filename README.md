# 坤寶存摺：架設教學

記帳、股票交易、推薦追蹤都在同一個網頁 App。App 本身放在 GitHub Pages，帳本資料放在你自己的**私人** repo，換手機、換電腦都會同步。

整個架設大約 10 分鐘，只要做一次。

---

## 第 1 步：放上 App（公開 repo）

1. 登入 GitHub，右上角「＋」→ **New repository**
2. Repository name 填 `kunbao-ledger`，選 **Public**
   - 免費帳號的 GitHub Pages 只能用公開 repo。這裡只放程式，帳本資料不在這裡，公開沒關係。
3. 按 **Create repository**
4. 在 repo 頁面按 **uploading an existing file**，把 `index.html` 拖進去，按 **Commit changes**
5. 到 repo 的 **Settings → Pages**
   - Source 選 **Deploy from a branch**
   - Branch 選 **main**、資料夾選 **/(root)**，按 **Save**
6. 等一到兩分鐘，App 網址會是：`https://你的帳號.github.io/kunbao-ledger/`

## 第 2 步：建立放帳本的私人 repo

1. 再按一次「＋」→ **New repository**
2. Repository name 填 `kunbao-ledger-data`，**一定要選 Private**
3. 勾選 **Add a README file**，按 **Create repository**

App 第一次同步時會自動在這個 repo 建立 `ledger.json`，你不用自己建。

## 第 3 步：產生只能動這個 repo 的金鑰

1. 右上角頭像 → **Settings** → 左側最下面 **Developer settings**
2. **Personal access tokens → Fine-grained tokens → Generate new token**
3. 設定：
   - **Token name**：`坤寶存摺`
   - **Expiration**：選最長的期限，到期前 GitHub 會寄信提醒，到時再產生一把新的
   - **Repository access**：選 **Only select repositories**，只勾 `kunbao-ledger-data`
   - **Permissions → Repository permissions**：**Contents** 和 **Actions** 都改成 **Read and write**（其他項目都不用動）
4. 按 **Generate token**，把 `github_pat_` 開頭的那串複製起來
   - 這串只會顯示一次。忘記了就刪掉重新產生一把。

## 第 4 步：在 App 裡連上同步

1. 打開 `https://你的帳號.github.io/kunbao-ledger/`
2. 底部切到 **設定**，在「雲端同步」填三欄：
   - GitHub 帳號
   - 資料 repo 名稱：`kunbao-ledger-data`
   - 存取金鑰：剛剛複製的 token
3. 按 **儲存並同步**，右上角變成綠色的「已同步」就完成了

**每台新裝置第一次打開時都要填一次這三欄。** 填完之後，所有帳都會從 GitHub 拉下來。

## 第 5 步：放到 iPhone 主畫面

用 Safari 打開 App 網址 → 下方分享鈕 → **加入主畫面**。之後點圖示打開，看起來就跟一般 App 一樣。

---

## App 裡有什麼

| 分頁 | 功能 |
|---|---|
| 記帳 | 支出、收入、帳戶、備註；月份切換；分類預算條；每日支出圖；一鍵記上房租、薪水這類固定收支；緊急預備金進度；搜尋明細 |
| 投資 | 台股、美股、加密貨幣的買進、賣出、股利；平均成本、已實現與未實現損益（美股、加密貨幣含匯率變化）；配置跟目標比，偏離超過 5 個百分點會提醒再平衡；定期定額計畫一鍵帶入股數 |
| 推薦 | 今日判讀（債務週期、資金風向、戰術層）、大盤與 ETF 長線燈號、衛星層四道濾網候選；你自己的推薦紀錄（誰推薦、當時價格、推薦後漲跌）；從推薦直接記成交易 |
| 雨林箱 | 記帳養成遊戲：記帳＝幫雨林箱噴水，累積露水升級，解鎖苔蘚、蕨類、鼠婦、馬陸、古巴青銅蜘蛛；連續記帳、任務、徽章 |
| 設定 | 同步、預算、目標配置、固定收支、定期定額、分類、市場資料位置、自動抓價與 AI 狀態、備份匯出與匯入 |

**現價怎麼來的**（優先順序由上到下）：
1. 你在持股手動填的價格
2. 加密貨幣：App 直接抓 Binance 即時價
3. 資料 repo 每個交易日自動抓的持股收盤價（`prices.json`）
4. App repo 每天自己掃的市場資料（`data/tw.json`、`data/us.json`）

## 市場資料：自己從源頭抓（App repo 的 GitHub Actions）

`kunbao-ledger` 裡的 `market.py` 由「每日更新市場資料」（`.github/workflows/market.yml`）在台灣時間週一到週五 22:10（台股）、隔天 06:30（美股＋總經＋核心層）自動執行，產出 `data/` 資料夾：

| 檔案 | 內容 | 資料來源 |
|---|---|---|
| tw.json | 上市櫃股票與 ETF 的長線燈號、四道濾網候選、族群強弱 | 證交所 ISIN 清單、證交所／櫃買中心 OpenAPI（本益比、月營收、收盤）、Yahoo Finance |
| us.json | S&P 500＋S&P 400＋常見 ETF 的同上資料 | 維基百科成分股、Yahoo Finance |
| macro.json | 債務週期、資金風向、大盤 vs 週線 MA52、戰術層 | FRED（連不上改用美國財政部）、Yahoo 類股 ETF |
| core.json | 全天候核心層的固定比例與風險平價比例、美元匯率 | Yahoo Finance |
| summary.json | 今日判讀（規則組句） | 上面四個檔 |

**策略規則參考市場雷達（market-radar）**，資料不讀它算好的結果：
- 長線燈號：年線乖離、月 KD、距 5 年高點、5 年位置，各換成 0～100 分取平均；65 分以上便宜、35 分以下偏貴。個股再加估值 30%、品質 30%，品質太差或虧損亮「基本面弱」
- 衛星層四道濾網：近 4 週強於大盤的前三分之一族群、資金分 55 以上、財報轉強、長線買點觸發（回測週 MA52 不破、距 52 週高點 −20%、月線 MACD 黃金交叉），排除流動性不足與 ETF
- 戰術層：總經、財報、資金流三個條件都轉弱才把核心層股票減 5 個百分點；大盤週線跌破 MA52 再減到 10 個百分點；只做防守

**第一次架好後**：到 `kunbao-ledger` 的 Actions →「每日更新市場資料」→ Run workflow 跑一次（約 30～60 分鐘）。基本面資料每次最多更新 700 檔，前幾天會慢慢補齊，在那之前個股的估值與品質分數比較少。公開 repo 的 Actions 不限分鐘數；私人 repo 每月只有 2000 分鐘，所以全市場掃描放在公開的 App repo，帳本仍在私人 repo。

選填：到 fred.stlouisfed.org 申請免費 API 金鑰，放到 `kunbao-ledger` 的 Secrets，名稱 `FRED_API_KEY`，信用利差才抓得到。

## 資料 repo 的自動化

`kunbao-ledger-data` 裡有兩支 GitHub Actions：

| workflow | 什麼時候跑 | 做什麼 |
|---|---|---|
| 更新持股現價（prices.yml） | 台灣時間週一到週五 22:15、隔天 06:35，或按 App 的「更新現價」 | 讀 ledger.json 裡的持股、推薦、定期定額，抓收盤價寫進 prices.json |
| AI 助手（ai.yml） | 按 App 的「幫我健檢」「讓 AI 整理」 | 用 Claude 寫健檢、整理推薦，結果寫進 ai.json |

**AI 要先設定一次 Claude 金鑰：** 到 console.anthropic.com 建一把 API 金鑰 → 資料 repo 的 **Settings → Secrets and variables → Actions → New repository secret**，名稱填 `ANTHROPIC_API_KEY`。金鑰只存在 GitHub，不會出現在手機或瀏覽器裡。建議在 console 設每月花費上限。

## 安全須知

- **GitHub token 只存在每台裝置自己的瀏覽器裡**，不會上傳。Claude 金鑰只放在 GitHub Secrets。
- **iPhone 主畫面的 App 和 Safari 不共用瀏覽器資料**，加到主畫面後，要從主畫面打開再填一次 token。
- **token 的權限一定要只開 `kunbao-ledger-data` 這一個 repo。** 同一個 `你的帳號.github.io` 網域底下的網頁，讀得到彼此存在瀏覽器裡的東西，所以權限開得越小越安全。
- **備份**：設定頁可以下載完整 JSON 備份和 CSV。GitHub 也會保留 `ledger.json` 的每一次修改紀錄，改錯了可以從 repo 的 History 找回舊版本。

