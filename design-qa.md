> **已封存（2026-08-31 的一次性紀錄）。**
> 這份是當初對照 ChatGPT 介面截圖做的設計驗收報告，**內容已經不反映現況**：
> 裡面提到的兩段式模式切換（`.mode-switch`）已改成右上角語氣標示、產品名已改為 lure ai、
> 引用的 `qa/*.png` 截圖也不在倉庫裡（`qa/` 目前只留驗證用的 JSON）。
> **現行的視覺規範看 `STYLE.md`**；這份只留作當時的決策紀錄。

---

**Design QA**

- Source visual truth: user-provided ChatGPT UI screenshot, supplied as `Screenshot 2026-08-31 at 1.05.17 PM.png` in the conversation (2760 x 1758 px).
- Implementation screenshots: `qa/chatgpt-style-empty-desktop-v2.png`, `qa/chatgpt-style-empty-mobile-v2.png`, and `qa/chatgpt-style-source-desktop-v3.png`.
- Desktop viewport: 1440 x 900 CSS px, device scale factor 1.
- Mobile viewport: 390 x 844 CSS px, device scale factor 1.
- State: empty chat, grounded answer, citation drawer, and mobile navigation.
- Density normalization: the source is a high-density macOS screenshot; comparisons used normalized proportions against its approximate 1380 x 879 CSS-pixel layout.

**Full-View Comparison**

The implementation matches the source's core composition: a light left rail, centered mode control, large unframed workspace, centered prompt, pill composer, muted suggestions, and persistent account footer. Domain-specific labels and the RAG send control are intentional product differences.

**Focused Region Comparison**

- Sidebar: width ratio, active navigation treatment, icon scale, recent list, and account footer align with the reference.
- Header: centered two-option segmented control and compact right-side status retain the reference hierarchy.
- Empty state: heading, composer, and three suggestion rows follow the reference spacing after the second pass.
- Conversation state: citations and source drawer remain usable without changing the reference-inspired primary layout.

**Comparison History**

- Pass 1 found a P1 overlap between the empty-state heading and composer and a P2 extra desktop close control.
- Fixes: moved the heading upward, tightened suggestion spacing, and restored desktop-only control visibility rules.
- Pass 2 evidence: `qa/chatgpt-style-empty-desktop-v2.png` and `qa/chatgpt-style-empty-mobile-v2.png` show separated content, correct controls, and no horizontal overflow.

**Fidelity Surfaces**

- Fonts and typography: system UI fonts, restrained weights, and zero letter spacing match the source character.
- Spacing and layout rhythm: major-region proportions and vertical ordering match; mobile uses a compact responsive equivalent.
- Colors and visual tokens: white canvas, light gray rail, neutral controls, and restrained semantic green/red states.
- Image quality and assets: the reference uses interface icons rather than content imagery; Lucide icons render sharply at CSS scale.
- Copy and content: ChatGPT labels are adapted to Hair Brain, RAG retrieval, answer audit, and designer coaching workflows.

**Interaction Checks**

- Prompt suggestion and grounded answer flow tested.
- Citation drawer open state tested.
- Mobile sidebar open state tested.
- Browser console errors checked: none.
- Horizontal overflow checked at both viewports: none.

**Findings**

No actionable P0, P1, or P2 differences remain. Domain labels and the black send action are intentional functional adaptations.

final result: passed
