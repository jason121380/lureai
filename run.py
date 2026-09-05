#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import threading
import secrets
import sys
from pathlib import Path

# 版本檢查要在 import app 之前：程式碼用了 3.10 的型別語法，舊直譯器
# （例如 macOS 內建的 3.9）會在 import 階段就丟一句看不懂的 TypeError，
# 使用者根本不知道是版本問題。
if sys.version_info < (3, 10):
    sys.exit(
        f"lure ai 需要 Python 3.10 以上（目前是 {sys.version.split()[0]}）。"
        "macOS 可用 python3.12 指令或從 python.org 安裝新版。"
    )

from app.ingest import ingest_jsonl
from app.policy import SENSITIVE_TOPICS
from app.replica import PostgresReplica
from app.server import AppContext, create_server
from app.storage import KnowledgeStore


PROJECT_ROOT = Path(__file__).resolve().parent

PROFILES = {
    "designer_coach": {
        "knowledge_file": "designer_coaching_process.jsonl",
        "database_file": "designer_coach.db",
        "policy_file": "designer_coach_policy.md",
        "access_level": "internal_coaching",
        "app_name": "LUREAI 你的智慧大腦中心",
        "assistant_name": "AI 輔導教練",
        # 開場題庫：100 題，每次進到空白對話隨機挑五題，全部都答得出來
        # （tests/test_welcome_prompts.py 會逐題驗證）。
        "welcome_prompts": (
            "我的私訊很多，但預約很少，該先查什麼？",
            "我的廣告成效變差，要先看哪些數字？",
            "客人問完價格就已讀不回，我該怎麼接？",
            "我很久沒發作品了，要從哪裡開始補？",
            "我的回覆是不是太長了？",
            "私訊要多久回才算及格？",
            "回覆要不要加 emoji？",
            "二選一要怎麼問客人？",
            "客人說太貴了，我要怎麼接？",
            "客人說再想想，我該怎麼追？",
            "報價前我要先問哪些事？",
            "我要怎麼幫自己的對話做健檢？",
            "廣告一天要投多少錢才夠？",
            "廣告受眾我要怎麼設？",
            "有私訊卻沒預約，是哪裡出問題？",
            "廣告花的錢有沒有回本，要怎麼算？",
            "我的版面第一屏要放什麼？",
            "一週要發幾則作品才夠？",
            "限時動態我可以發什麼？",
            "短影音的腳本要怎麼寫？",
            "作品照怎麼拍顏色才會準？",
            "我要怎麼請客人幫我留評論？",
            "客人不回來，我要怎麼把他找回來？",
            "我想提高客單價，要從哪裡下手？",
            "健檢要抽哪 20 則對話來看？",
            "20 則對話記完之後我要看什麼？",
            "私訊要怎麼講才有親切感？",
            "客人覺得我不夠專業，我要改什麼？",
            "客人不照我的選項回，我要怎麼接？",
            "第一則回覆有沒有模板可以用？",
            "已讀不回要隔多久再追？",
            "有沒有可以直接成交的句型？",
            "健檢完我該先改哪一件事？",
            "預存回覆要先準備哪幾句？",
            "客人從限動私訊我，要怎麼承接？",
            "新客跟舊客的私訊要怎麼分？",
            "客人問我會不會傷髮質，我怎麼回？",
            "客人約了沒來，我要怎麼處理？",
            "我不敢拒絕客人怎麼辦？",
            "我最近很累，要先處理什麼？",
            "廣告目標要選哪一種？",
            "素材要準備幾組、多久換一次？",
            "廣告文案要怎麼寫？",
            "廣告要看的五個數字是什麼？",
            "廣告什麼時候該停掉？",
            "停掉的廣告要怎麼重新開跑？",
            "廣告客人都不回流，怎麼辦？",
            "廣告和自然貼文要怎麼分工？",
            "哪些做法最浪費廣告預算？",
            "淡季旺季的廣告要怎麼配？",
            "沒有預算的人要怎麼開始？",
            "我為什麼每天都做到很晚？",
            "精選動態要分哪幾類？",
            "作品要怎麼排才不會擠在一起？",
            "停更很久了要怎麼復健？",
            "貼文文案要怎麼寫？",
            "拍作品照要注意哪些基本條件？",
            "前後對比要怎麼拍才有說服力？",
            "修圖可以修到什麼程度？",
            "拍客人的照片要怎麼取得同意？",
            "舊客見證要怎麼用？",
            "我要怎麼找內容題材？",
            "社群要看哪幾個數字？",
            "怎麼判斷一則貼文有沒有效？",
            "我的版面要怎麼做一次總體檢？",
            "我完全沒有數據要怎麼開始？",
            "我的問題卡在漏斗的哪一段？",
            "目標要怎麼訂才追得動？",
            "每週追蹤要追什麼？",
            "上週沒做到要怎麼檢討？",
            "時間有限我要先做哪一項？",
            "輔導紀錄表要有哪些欄位？",
            "會談要怎麼收尾？",
            "資料不足的時候你會怎麼回我？",
            "會跟我走的客人要怎麼估？",
            "開店的固定成本有哪些？",
            "現在還不適合開店的訊號有哪些？",
            "先不開店我可以做什麼？",
            "排班要怎麼排才不會一直延後？",
            "客人遲到我要怎麼處理？",
            "臨時要加的客人我要怎麼回？",
            "我想多休一天但怕收入掉？",
            "我覺得自己不適合這行怎麼辦？",
            "業績沒達標我要先檢查什麼？",
            "客人歸屬要怎麼定才清楚？",
            "我要怎麼跟店長談？",
            "什麼時候可以漲價？",
            "漲價要怎麼跟客人說？",
            "訂金的訊息要怎麼寫？",
            "我要不要花錢去上課？",
            "我不會賣產品要怎麼開口？",
            "客人進門的接待要怎麼做？",
            "服務前的諮詢要問什麼？",
            "客資料要怎麼建、怎麼用？",
            "燙染後幾天要關懷客人？",
            "業績掉了要先看哪裡？",
            "促銷活動要怎麼企劃？",
            "新人前七天要怎麼帶？",
            "客訴要怎麼處理？",
            "Google 評論要怎麼經營？",
        ),
        "blocked_topics": {
            key: SENSITIVE_TOPICS[key]
            for key in ("personal_or_payment", "health_or_medical", "legal_refund_or_compensation", "labor_hr")
        },
        # 查不到資料時的說法：短、口語、不用破折號，直接把球丟回去。
        "fallback_message": "我目前沒有資料 比較難幫你評估\n還是你可以貼給我一下你的數據",
    },
}


def load_profile(name: str) -> dict:
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"未知的知識 profile：{name}") from exc


def default_paths(
    root: Path = PROJECT_ROOT,
    profile: str | None = None,
) -> dict[str, Path]:
    profile_name = profile or os.environ.get("APP_PROFILE", "designer_coach")
    profile_config = load_profile(profile_name)
    configured_knowledge = os.environ.get("KNOWLEDGE_JSONL")
    bundled_knowledge = root / "knowledge" / profile_config["knowledge_file"]
    private_full_knowledge = root / "private_sources" / "full" / "rag" / f"{profile_name}_full.jsonl"
    if configured_knowledge:
        knowledge = Path(configured_knowledge)
    elif private_full_knowledge.is_file():
        knowledge = private_full_knowledge
    else:
        knowledge = bundled_knowledge
    database = Path(os.environ.get("APP_DB_PATH", root / "data" / profile_config["database_file"]))
    return {"knowledge": knowledge, "database": database}


def load_settings(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def default_port() -> int:
    return int(os.getenv("APP_PORT") or os.getenv("PORT") or "8765")


def admin_token_for_host(host: str) -> str:
    """管理 API 的權杖。沒設定時不讓整個服務停擺——後台本來就走帳號登入。"""
    configured = os.getenv("ADMIN_TOKEN", "").strip()
    if configured:
        return configured
    if str(host).strip().lower() in {"127.0.0.1", "localhost", "::1"}:
        return "local-admin"
    # 產生一個沒有人知道的權杖：等於關掉 header 這條路，但聊天與後台
    # （管理者帳號登入）照常運作，不會因為少一個環境變數就整站打不開。
    print(
        "[boot] 未設定 ADMIN_TOKEN，已改用隨機權杖；管理 API 請改用管理者帳號登入",
        file=sys.stderr,
        flush=True,
    )
    return secrets.token_urlsafe(32)


def bot_token() -> str:
    """lurebot 呼叫大腦用的服務權杖。沒設定就等於關掉 /api/bot/*。"""
    return os.getenv("BOT_API_TOKEN", "").strip()


def reindex(root: Path = PROJECT_ROOT, profile: str = "designer_coach") -> dict:
    profile_config = load_profile(profile)
    paths = default_paths(root, profile=profile)
    if not paths["knowledge"].is_file():
        raise FileNotFoundError(f"找不到客服知識檔：{paths['knowledge']}")
    store = KnowledgeStore(paths["database"])
    try:
        report = ingest_jsonl(
            store,
            paths["knowledge"],
            expected_access_level=profile_config["access_level"],
        )
        return {
            "imported": report.imported,
            "rejected": report.rejected,
            "errors": report.errors,
            "database": str(paths["database"]),
            "knowledge": str(paths["knowledge"]),
            "profile": profile,
        }
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="lure ai 輔導大腦 RAG")
    parser.add_argument(
        "--profile",
        choices=tuple(PROFILES),
        default=os.getenv("APP_PROFILE", "designer_coach"),
    )
    parser.add_argument("--host", default=os.getenv("APP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=default_port())
    parser.add_argument("--reindex-only", action="store_true")
    args = parser.parse_args(argv)

    if args.reindex_only:
        try:
            print(json.dumps(reindex(profile=args.profile), ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1

    profile = load_profile(args.profile)
    paths = default_paths(profile=args.profile)
    if not paths["knowledge"].is_file():
        print(f"找不到客服知識檔：{paths['knowledge']}", file=sys.stderr)
        return 1
    settings = load_settings(PROJECT_ROOT / "config" / "settings.json")
    try:
        admin_token = admin_token_for_host(args.host)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    context = AppContext.create(
        db_path=paths["database"],
        knowledge_path=paths["knowledge"],
        static_dir=PROJECT_ROOT / "static",
        admin_token=admin_token,
        bot_token=bot_token(),
        policy_path=PROJECT_ROOT / "config" / profile["policy_file"],
        minimum_score=float(settings["retrieval"]["minimum_score"]),
        top_k=int(settings["retrieval"]["top_k"]),
        profile=args.profile,
        access_level=profile["access_level"],
        app_name=profile["app_name"],
        assistant_name=profile["assistant_name"],
        welcome_prompts=profile["welcome_prompts"],
        blocked_topics=profile["blocked_topics"],
        fallback_message=profile["fallback_message"],
        defer_bootstrap=True,
    )
    # Postgres 持久化（不掛 Volume）：開機還原上一份快照，之後定期備份。
    replica = PostgresReplica.from_env()
    restored = False
    if replica.configured and not replica.enabled:
        context.close()
        raise RuntimeError("已設定 Postgres 但缺少 psycopg；停止啟動以保護資料")
    try:
        if replica.enabled:
            restored = replica.restore(context.store)
        context.initialize_accounts()
        if replica.enabled:
            replica.start(context.store)
    except Exception:
        try:
            replica.stop()
        finally:
            context.close()
        raise
    # 後台「系統健康」要看得到持久化狀態，不用翻 log。
    context.replica = replica
    context.restored_from_replica = restored

    # 開機資訊要留在 Log 裡：卡在哪一步、索引有沒有進去，才查得出來。
    print(
        f"[boot] profile={args.profile} chunks={context.store.count_chunks()} "
        f"knowledge={paths['knowledge'].name} db={paths['database']} "
        f"model={'on' if context.service.answerer.model_enabled else 'off'} "
        f"bot_api={'on' if context.bot_token else 'off'} "
        f"persistence={'postgres' if replica.enabled else 'sqlite-only'}"
        f"{' restored' if restored else ''}",
        flush=True,
    )
    try:
        server = create_server(args.host, args.port, context)
    except Exception:
        try:
            replica.stop(context.store)
        finally:
            context.close()
        raise
    print(f"{profile['app_name']}：http://{args.host}:{server.server_port}", flush=True)
    print(f"管理後台：http://{args.host}:{server.server_port}/admin.html")
    if admin_token == "local-admin":
        print("本機預設管理權杖：local-admin（正式部署必須設定 ADMIN_TOKEN）")
    def request_shutdown(*_args):
        threading.Thread(target=server.shutdown, daemon=True).start()

    replica.on_writer_lost = request_shutdown
    previous_sigterm = signal.signal(signal.SIGTERM, request_shutdown)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服務")
    finally:
        try:
            server.server_close()
        except TimeoutError:
            # Remaining workers may mutate SQLite. Exit without closing it or publishing
            # a misleading final snapshot; process exit releases the PostgreSQL session.
            print("[shutdown] 工作逾時，保留上次快照並停止程序", file=sys.stderr, flush=True)
            os._exit(1)
        try:
            replica.stop(context.store)
        finally:
            context.close()
            signal.signal(signal.SIGTERM, previous_sigterm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
