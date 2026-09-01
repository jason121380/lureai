# STYLE.md — 視覺規範

全站採 ChatGPT 式中性視覺語言。單一樣式表 `static/app.css`，所有顏色一律使用 `:root` design token，禁止新增硬編色碼。

## 色彩 tokens

| Token | 值 | 用途 |
| --- | --- | --- |
| `--ink` | `#0d0d0d` | 主文字、主要按鈕底色 |
| `--ink-soft` | `#5d5d5d` | 次要文字 |
| `--muted` | `#8f8f8f` | 弱化文字、佔位 |
| `--canvas` / `--surface` | `#ffffff` | 頁面與卡片底 |
| `--sidebar` | `#f9f9f9` | 側欄底、表頭底 |
| `--line` | `#ececec` | 一般邊框、分隔線 |
| `--line-strong` | `#d9d9d9` | 輸入框邊框 |
| `--hover` | `#ececec` | hover 底色 |
| `--bubble` | `#f4f4f4` | 使用者訊息氣泡、淡底 |
| `--ok` / `--ok-soft` | `#10a37f` / `#e6f4ef` | 正常狀態徽章 |
| `--warning` / `--warning-soft` | `#d92d20` / `#fdecea` | 錯誤、轉人工 |
| `--caution` / `--caution-soft` | `#b54708` / `#fff3e0` | 警告 |

語意色（ok/warning/caution）只用於狀態徽章與健康檢查，介面主體保持無彩度。

## 圓角

- 按鈕、徽章、建議 chip：`--radius-pill`（999px 藥丸）
- 輸入框、select、toast：`--radius-sm`（12px）
- 卡片、表格外框：`--radius`（16px）
- 聊天輸入列：28px；使用者訊息氣泡：24px

## 元件規則

- **主要按鈕** `.command-button`：黑底白字藥丸，hover `#3d3d3d`，不變色系。
- **確認彈窗的兩顆按鈕**（如切換語氣）：同形狀同尺寸的藥丸，只有填色不同——確認是黑底白字、取消是白底黑框黑字。選擇器要比 `.command-button` 更明確（`.tone-confirm-actions .tone-ok`），否則圓角與高度會被它蓋掉。
- **輸入框** focus：邊框變 `--ink` + `box-shadow 0 0 0 1px var(--ink)`（不用彩色光暈）。
- **聊天訊息**：使用者靠右、`--bubble` 氣泡、最大寬 78%；助理靠左純文字無框無頭像、字級 15px、行高 1.8。
- **引用**：藥丸按鈕 + 黑色圓形編號；內文中的 `[n]` 是灰底小圓鈕 `.cite-ref`，點擊開來源抽屜。
- **關聯問題** `.followup-button`：白底細框藥丸、`corner-down-right` icon，直向排列在最新回覆下方，點擊即送出。
- **對話標題**：topbar 顯示 AI 產生的標題＋鉛筆行內編輯（`.title-input`）。
- **登入頁**：白底、無卡片框、置中，logo 置中、標題 28px、輸入框 48px、黑色藥丸登入鈕。
- **側欄**：`--sidebar` 底、10px 圓角 hover 項目；用量面板是獨立白色卡片（12px 圓角）與帳號列分離。
- 字體：系統字堆疊（含 PingFang TC / Noto Sans TC）；數字用 SFMono/Consolas。

## 禁止事項

- 不出現連線狀態指示、應用名稱副標等裝飾性雜訊。
- 不新增彩色品牌 accent 到互動元件（logo 本身除外）。
- 深色模式尚未支援；新增顏色一律先加 token。

## 後台（比照前台的同一套語言）

- **內容寬度**：`.admin-section` 用 `width: min(var(--admin-content), 100%)` 置中（1120px）。放到 1440px 以上滿版時，健康檢查右側的數值會被推到離標籤一千多 px 遠，等於看不懂。
- **字級下限**：主體 13px、次要說明 12px、單位與 mono 數值 11px，**不要再出現 9~10px**。區塊標題 20px、卡片標題 15px、小標 15px。
- **語意色只給狀態**：`待整理` 這種計數只有 `> 0` 才上 `--warning`（`.alert-stat[data-alert="true"]`）；`0` 塗紅會讓人以為出事了。
- **少畫框**：重複出現幾十次的小格子（分類卡）用 `--bubble` 淡底，不要每格都描 `--line`，否則框線比內容還搶眼。
- **健康檢查列**：兩欄（狀態＋說明 / 數值），數值欄 `max-width: 460px`；耗時跟狀態徽章放同一行，才看得出來是誰的耗時。手機版數值欄換到第二列靠左。
- **同類欄位要等寬**：整列都是下拉時用 `.editor-row.even-row`（`repeat(3, 1fr)`）；`2fr 1.2fr 1fr` 那種只給「標題／分類／主題」這類不同性質的欄位。
- 表單欄位最小高度 44px、圓角 `--radius-sm`，focus 一樣是 `--ink` 邊框＋1px 陰影，跟前台輸入框同一組手勢。
- **規則多的頁面用「左選單／右內容」**：AI 模型校調有 51 條規則，排成一長串捲不完，改成左側 208px 分組選單（sticky，帶每組條數與改過幾條）＋右側只顯示該組。手機版選單改成可橫向捲的藥丸列。
- **側欄底部的返回鍵**：分隔線用 `::before` 畫在項目上方 18px 處，項目本身維持 10px 圓角 hover。直接在項目上加 `border-top` 會讓線緊貼文字、下緣又頂到視窗，看起來很擠。

## 回答排版

- AI 回答一律「一句結論 + 條列行動」，條列以真正的 `<ul>/<ol>` 呈現（`.message-text.rich`），不是純文字換行。
- 條列間距 6px、區塊間距 10px；項目符號用次要色，避免搶走文字。
- 使用者訊息維持 `white-space: pre-wrap`，只有 AI 回答切換成 `normal`。

## 空白頁排版（標題／輸入框／建議題目）

- 三者的位置全部由 `:root` 的 `--welcome-top`、`--welcome-title-h`、`--welcome-gap`、`--welcome-composer-h`、`--topbar-h` 推算，**不要讓標題用 `vh`、輸入框用 `%` 各算各的**：兩套單位在矮視窗會交叉，標題會壓到輸入框上（實測 1440×700 時重疊 5px）。
- 輸入框在空白頁是絕對定位，`top = topbar + welcome-top + 標題高 + 間距`；建議題目的 `margin-top = 間距 × 2 + 輸入框高`，所以輸入框正好浮在標題與題目中間。
- `.welcome h2` 要給固定 `height`／`line-height`（等於 `--welcome-title-h`），推算才會準。
- 手機版只覆寫這組變數（含含安全區的 `--topbar-h`），不要再寫個別的 `padding-top`／`top`。

## 版面骨架（不要改回 auto）

- `.app-shell` 必須同時寫 `grid-template-rows: minmax(0, 1fr)` 與 `height: 100%`。留成隱含的 `auto` row 時，側欄對話一多就會把 grid 撐超過視窗，`<html>` 開始整頁捲動，`position: relative` 的 `.topbar` 會直接被捲出畫面。
- `.sidebar` 要有 `min-height: 0`，`.conversation-list` 才接得到捲動。
- `html` 與 `body` 都要 `overflow: hidden`：只設 body 仍會整頁捲。

## 手機版選單

- 側欄在手機版開成「左側 80% 寬」的純白抽屜（`--surface`＋右側 `--line` 細邊），不壓暗背景、不加陰影：任何壓暗或陰影都會在狀態列下緣形成交界。
- 右邊露出的 20% 用透明的 `drawer-overlay.clear` 接住點擊來關閉；來源抽屜仍用原本壓暗的 overlay。
- 也可用右上角 X 或 Escape 關閉。

## PWA 淺色鎖定

- `theme-color` meta 三份（light media／dark media／無 media）全部 `#ffffff`，讓 PWA 狀態列與頁面同色、無分割線。
- 搭配 `<meta name="color-scheme" content="light">` 與 CSS `:root { color-scheme: light; }`，系統深色模式也不變色。
- iOS 在「加入主畫面」當下快取 theme-color：改了 meta 之後，既有使用者要刪掉圖示重新加入才會生效。
