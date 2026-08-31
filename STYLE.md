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
