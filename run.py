#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path

from app.ingest import ingest_jsonl
from app.server import AppContext, create_server
from app.storage import KnowledgeStore


PROJECT_ROOT = Path(__file__).resolve().parent


def default_paths(root: Path = PROJECT_ROOT) -> dict[str, Path]:
    configured_knowledge = os.environ.get("KNOWLEDGE_JSONL")
    bundled_knowledge = root / "knowledge" / "active_customer_service.jsonl"
    sibling_knowledge = root.parent / "張副總知識庫大腦-v3" / "rag" / "active_customer_service.jsonl"
    knowledge = Path(configured_knowledge) if configured_knowledge else (
        bundled_knowledge if bundled_knowledge.is_file() else sibling_knowledge
    )
    database = Path(os.environ.get("APP_DB_PATH", root / "data" / "knowledge.db"))
    return {"knowledge": knowledge, "database": database}


def load_settings(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def reindex(root: Path = PROJECT_ROOT) -> dict:
    paths = default_paths(root)
    if not paths["knowledge"].is_file():
        raise FileNotFoundError(f"找不到客服知識檔：{paths['knowledge']}")
    store = KnowledgeStore(paths["database"])
    try:
        report = ingest_jsonl(store, paths["knowledge"])
        return {
            "imported": report.imported,
            "rejected": report.rejected,
            "errors": report.errors,
            "database": str(paths["database"]),
            "knowledge": str(paths["knowledge"]),
        }
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="張副總 AI 客服 RAG")
    parser.add_argument("--host", default=os.getenv("APP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("APP_PORT", "8765")))
    parser.add_argument("--reindex-only", action="store_true")
    args = parser.parse_args(argv)

    if args.reindex_only:
        try:
            print(json.dumps(reindex(), ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1

    paths = default_paths()
    if not paths["knowledge"].is_file():
        print(f"找不到客服知識檔：{paths['knowledge']}", file=sys.stderr)
        return 1
    settings = load_settings(PROJECT_ROOT / "config" / "settings.json")
    admin_token = os.getenv("ADMIN_TOKEN", "local-admin")
    context = AppContext.create(
        db_path=paths["database"],
        knowledge_path=paths["knowledge"],
        static_dir=PROJECT_ROOT / "static",
        admin_token=admin_token,
        policy_path=PROJECT_ROOT / "config" / "customer_policy.md",
        minimum_score=float(settings["retrieval"]["minimum_score"]),
        top_k=int(settings["retrieval"]["top_k"]),
    )
    server = create_server(args.host, args.port, context)
    print(f"張副總 AI 客服：http://{args.host}:{server.server_port}")
    print(f"管理後台：http://{args.host}:{server.server_port}/admin.html")
    print("本機預設管理權杖：local-admin（可用 ADMIN_TOKEN 變更）")
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
