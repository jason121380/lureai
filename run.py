#!/usr/bin/env python3
import argparse
import json
import os
import secrets
import sys
from pathlib import Path

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
        # 開場題庫：每次進到空白對話會隨機挑三題，全部都答得出來
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
        ),
        "blocked_topics": {
            key: SENSITIVE_TOPICS[key]
            for key in ("personal_or_payment", "health_or_medical", "legal_refund_or_compensation", "labor_hr")
        },
        "fallback_message": "這題我手上沒有夠明確的資料，換個問法可能就有了——你可以直接點下面的題目，或告訴我你目前的數字（私訊數、預約數、到店數）。",
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
    )
    # Postgres 持久化（不掛 Volume）：開機還原上一份快照，之後定期備份。
    replica = PostgresReplica.from_env()
    restored = False
    if replica.configured and not replica.enabled:
        print("[boot] 偵測到 Postgres 連線設定，但缺少 psycopg 套件，持久化停用", file=sys.stderr, flush=True)
    if replica.enabled:
        try:
            restored = replica.restore(context.store)
        except Exception as exc:  # noqa: BLE001 - 還原失敗要照常開站，只是資料是新的
            print(f"[pg] restore failed: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr, flush=True)
        if restored:
            # 快照會整批取代帳號表；環境變數指定的第一個帳號若不在快照裡要補回來。
            username = os.getenv("USER_USERNAME", "").strip()
            password = os.getenv("USER_PASSWORD", "")
            if username and password:
                try:
                    context.auth.ensure_bootstrap_user(username, password, os.getenv("USER_ROLE"))
                except ValueError:
                    pass
        replica.start(context.store)
    # 後台「系統健康」要看得到持久化狀態，不用翻 log。
    context.replica = replica
    context.restored_from_replica = restored

    # 開機資訊要留在 Log 裡：卡在哪一步、索引有沒有進去，才查得出來。
    print(
        f"[boot] profile={args.profile} chunks={context.store.count_chunks()} "
        f"knowledge={paths['knowledge'].name} db={paths['database']} "
        f"model={'on' if context.service.answerer.model_enabled else 'off'} "
        f"persistence={'postgres' if replica.enabled else 'sqlite-only'}"
        f"{' restored' if restored else ''}",
        flush=True,
    )
    server = create_server(args.host, args.port, context)
    print(f"{profile['app_name']}：http://{args.host}:{server.server_port}", flush=True)
    print(f"管理後台：http://{args.host}:{server.server_port}/admin.html")
    if admin_token == "local-admin":
        print("本機預設管理權杖：local-admin（正式部署必須設定 ADMIN_TOKEN）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服務")
    finally:
        server.server_close()
        replica.stop(context.store)
        context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
