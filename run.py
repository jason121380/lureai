#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path

from app.ingest import ingest_jsonl
from app.policy import SENSITIVE_TOPICS
from app.server import AppContext, create_server
from app.storage import KnowledgeStore


PROJECT_ROOT = Path(__file__).resolve().parent

PROFILES = {
    "customer_service": {
        "knowledge_file": "active_customer_service.jsonl",
        "database_file": "knowledge.db",
        "policy_file": "customer_policy.md",
        "access_level": "customer_service",
        "app_name": "張副總 AI 客服",
        "assistant_name": "AI 客服",
        "welcome_prompts": (
            "顧客不滿意怎麼處理？",
            "臉型可以直接決定髮型嗎？",
            "預約需要提供什麼資訊？",
            "染髮多少錢？",
        ),
        "blocked_topics": SENSITIVE_TOPICS,
        "fallback_message": "目前知識庫沒有足夠且已核准的資料，我幫您轉由專人確認。",
    },
    "designer_coach": {
        "knowledge_file": "designer_coaching_process.jsonl",
        "database_file": "designer_coach.db",
        "policy_file": "designer_coach_policy.md",
        "access_level": "internal_coaching",
        "app_name": "設計師 1 對 1 AI 輔導",
        "assistant_name": "AI 輔導教練",
        "welcome_prompts": (
            "設計師私訊很多但預約很少，先查什麼？",
            "幫我安排一次 1 對 1 輔導流程",
            "如何健檢設計師的私訊回覆？",
            "社群停更時要怎麼排優先順序？",
        ),
        "blocked_topics": {
            key: SENSITIVE_TOPICS[key]
            for key in ("personal_or_payment", "health_or_medical", "legal_refund_or_compensation", "labor_hr")
        },
        "fallback_message": "目前內部知識庫沒有足夠且已核准的資料，請補充數據或交由輔導主管確認。",
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
    profile_name = profile or os.environ.get("APP_PROFILE", "customer_service")
    profile_config = load_profile(profile_name)
    configured_knowledge = os.environ.get("KNOWLEDGE_JSONL")
    bundled_knowledge = root / "knowledge" / profile_config["knowledge_file"]
    private_full_knowledge = root / "private_sources" / "full" / "rag" / f"{profile_name}_full.jsonl"
    sibling_knowledge = root.parent / "張副總知識庫大腦-v3" / "rag" / "active_customer_service.jsonl"
    if configured_knowledge:
        knowledge = Path(configured_knowledge)
    elif private_full_knowledge.is_file():
        knowledge = private_full_knowledge
    elif bundled_knowledge.is_file() or profile_name != "customer_service":
        knowledge = bundled_knowledge
    else:
        knowledge = sibling_knowledge
    database = Path(os.environ.get("APP_DB_PATH", root / "data" / profile_config["database_file"]))
    return {"knowledge": knowledge, "database": database}


def load_settings(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def default_port() -> int:
    return int(os.getenv("APP_PORT") or os.getenv("PORT") or "8765")


def admin_token_for_host(host: str) -> str:
    configured = os.getenv("ADMIN_TOKEN", "").strip()
    if configured:
        return configured
    if str(host).strip().lower() in {"127.0.0.1", "localhost", "::1"}:
        return "local-admin"
    raise ValueError("正式環境必須設定 ADMIN_TOKEN")


def reindex(root: Path = PROJECT_ROOT, profile: str = "customer_service") -> dict:
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
    parser = argparse.ArgumentParser(description="張副總 AI 客服 RAG")
    parser.add_argument(
        "--profile",
        choices=tuple(PROFILES),
        default=os.getenv("APP_PROFILE", "customer_service"),
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
    server = create_server(args.host, args.port, context)
    print(f"{profile['app_name']}：http://{args.host}:{server.server_port}")
    print(f"管理後台：http://{args.host}:{server.server_port}/admin.html")
    if admin_token == "local-admin":
        print("本機預設管理權杖：local-admin（正式部署必須設定 ADMIN_TOKEN）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服務")
    finally:
        server.server_close()
        context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
