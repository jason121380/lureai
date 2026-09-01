"""知識庫的兩個大主題：店務營運管理／設計師一對一行銷輔導。

每一塊知識都屬於其中一個主題。資料列可以自己帶 `domain` 欄位，沒帶時
就用分類與來源檔推斷，所以舊資料重建索引後也會自動歸位。
"""

OPERATIONS = "operations"
COACHING = "coaching"
DEFAULT_DOMAIN = OPERATIONS

DOMAIN_LABELS = {
    OPERATIONS: "店務營運管理",
    COACHING: "設計師一對一行銷輔導",
}
# 後台顯示順序，與使用者定義的 1./2. 一致。
DOMAIN_ORDER = (OPERATIONS, COACHING)

# 策展的輔導知識（coach-01~41）不論分類一律屬於輔導主題。
COACHING_SOURCE_FILES = frozenset({"knowledge/designer_coaching_process.md"})

COACHING_CATEGORIES = frozenset({
    "使用範圍", "核心原則", "會前準備", "漏斗診斷", "輔導會議", "優先順序",
    "私訊健檢", "私訊流程", "私訊追蹤", "回覆速度", "社群經營", "內容策略",
    "素材拍攝", "廣告診斷", "數據紀錄", "Google 商家", "售後與回流",
    "方案與價格", "隱私與同意", "輔導溝通", "行動計畫", "追蹤節奏",
    "不確定性", "提問模板", "數位行銷", "話術範本", "關鍵數字", "診斷框架",
})

OPERATIONS_CATEGORIES = frozenset({
    "店務營運", "企業知識", "顧客服務", "人才與管理", "業績管理", "美髮技術",
})


def is_domain(value: str) -> bool:
    return value in DOMAIN_LABELS


def classify(category: str = "", source_file: str = "") -> str:
    """在沒有明確標記時，依來源與分類判斷主題。"""
    if str(source_file or "").strip() in COACHING_SOURCE_FILES:
        return COACHING
    if " ".join(str(category or "").split()) in COACHING_CATEGORIES:
        return COACHING
    return OPERATIONS


def domain_of(row: dict) -> str:
    """資料列自己標的主題優先，其次才推斷。"""
    declared = " ".join(str(row.get("domain", "") or "").split())
    if is_domain(declared):
        return declared
    return classify(row.get("category", ""), row.get("source_file", ""))


def label(domain: str) -> str:
    return DOMAIN_LABELS.get(domain, DOMAIN_LABELS[DEFAULT_DOMAIN])
