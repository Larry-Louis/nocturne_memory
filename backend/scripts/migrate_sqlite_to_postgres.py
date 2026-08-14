"""
One-shot migration: local SQLite database -> PostgreSQL (Docker deployment).

Reads every business table from the SQLite file and copies it into the
PostgreSQL instance configured in config.json, preserving primary keys and
foreign-key references (memories.id, edges.id, paths.edge_id, ...).

Table insertion order follows FK dependencies:
  presets -> nodes -> memories -> edges -> paths
          -> glossary_keywords -> search_documents -> memory_access_logs

Idempotent: uses ON CONFLICT DO NOTHING (rows already present are skipped).
The only exception is presets, where an existing row with the same name is
updated to match the SQLite version (source of truth).

Usage (inside the backend container):
  docker cp nocturne_data.db backend:/tmp/nocturne_data.db
  docker compose exec backend python scripts/migrate_sqlite_to_postgres.py \
      --sqlite /tmp/nocturne_data.db
"""

import argparse
import asyncio
import datetime
import json
import sys
from pathlib import Path

import asyncpg
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent  # <repo>/backend

sys.path.insert(0, str(ROOT))

from db.models import (  # noqa: E402
    Edge,
    GlossaryKeyword,
    Memory,
    MemoryAccessLog,
    Node,
    Path as PathRow,
    Preset,
    SearchDocument,
)


def _preset_payload(p: Preset) -> dict:
    return {
        "name": p.name,
        "boot_uris": p.boot_uris,
        "path_masks": p.path_masks,
        "is_active": bool(p.is_active),
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


def load_sqlite(sqlite_path: Path) -> dict[str, list]:
    """Read all business tables from the SQLite file via the ORM."""
    engine = create_engine(f"sqlite:///{sqlite_path}")
    with Session(engine) as s:
        return {
            "presets": [_preset_payload(x) for x in s.scalars(select(Preset))],
            "nodes": [
                {
                    "uuid": n.uuid,
                    "created_at": n.created_at,
                    "last_accessed_at": n.last_accessed_at,
                }
                for n in s.scalars(select(Node))
            ],
            "memories": [
                {
                    "id": m.id,
                    "node_uuid": m.node_uuid,
                    "content": m.content,
                    "deprecated": bool(m.deprecated),
                    "migrated_to": m.migrated_to,
                    "created_at": m.created_at,
                    "world_timestamp": m.world_timestamp,
                }
                for m in s.scalars(select(Memory))
            ],
            "edges": [
                {
                    "id": e.id,
                    "parent_uuid": e.parent_uuid,
                    "child_uuid": e.child_uuid,
                    "name": e.name,
                    "priority": e.priority,
                    "disclosure": e.disclosure,
                    "created_at": e.created_at,
                }
                for e in s.scalars(select(Edge))
            ],
            "paths": [
                {
                    "namespace": p.namespace,
                    "domain": p.domain,
                    "path": p.path,
                    "edge_id": p.edge_id,
                    "node_uuid": p.node_uuid,
                    "created_at": p.created_at,
                }
                for p in s.scalars(select(PathRow))
            ],
            "glossary_keywords": [
                {
                    "id": g.id,
                    "keyword": g.keyword,
                    "node_uuid": g.node_uuid,
                    "namespace": g.namespace,
                    "created_at": g.created_at,
                }
                for g in s.scalars(select(GlossaryKeyword))
            ],
            "search_documents": [
                {
                    "namespace": d.namespace,
                    "domain": d.domain,
                    "path": d.path,
                    "node_uuid": d.node_uuid,
                    "memory_id": d.memory_id,
                    "uri": d.uri,
                    "content": d.content,
                    "disclosure": d.disclosure,
                    "search_terms": d.search_terms,
                    "priority": d.priority,
                    "world_timestamp": d.world_timestamp,
                    "updated_at": d.updated_at,
                }
                for d in s.scalars(select(SearchDocument))
            ],
            "memory_access_logs": [
                {
                    "id": l.id,
                    "node_uuid": l.node_uuid,
                    "namespace": l.namespace,
                    "accessed_at": l.accessed_at,
                    "context": l.context,
                }
                for l in s.scalars(select(MemoryAccessLog))
            ],
        }


def _normalize_rows(data: dict[str, list]) -> None:
    """Normalize offset-aware datetimes (historical +00:00 rows) to naive UTC."""
    for rows in data.values():
        for row in rows:
            for key, value in row.items():
                if isinstance(value, datetime.datetime) and value.tzinfo is not None:
                    row[key] = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)


async def migrate(data: dict[str, list], database_url: str) -> None:
    conn = await asyncpg.connect(database_url)
    try:
        async with conn.transaction():
            for p in data["presets"]:
                await conn.execute(
                    """
                    INSERT INTO presets (name, boot_uris, path_masks, is_active, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (name) DO UPDATE SET
                        boot_uris = EXCLUDED.boot_uris,
                        path_masks = EXCLUDED.path_masks,
                        is_active = EXCLUDED.is_active,
                        updated_at = EXCLUDED.updated_at
                    """,
                    p["name"], p["boot_uris"], p["path_masks"], p["is_active"],
                    p["created_at"], p["updated_at"],
                )

            for n in data["nodes"]:
                await conn.execute(
                    """
                    INSERT INTO nodes (uuid, created_at, last_accessed_at)
                    VALUES ($1, $2, $3) ON CONFLICT (uuid) DO NOTHING
                    """,
                    n["uuid"], n["created_at"], n["last_accessed_at"],
                )

            for m in data["memories"]:
                await conn.execute(
                    """
                    INSERT INTO memories (id, node_uuid, content, deprecated, migrated_to, created_at, world_timestamp)
                    VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (id) DO NOTHING
                    """,
                    m["id"], m["node_uuid"], m["content"], m["deprecated"],
                    m["migrated_to"], m["created_at"], m["world_timestamp"],
                )

            for e in data["edges"]:
                await conn.execute(
                    """
                    INSERT INTO edges (id, parent_uuid, child_uuid, name, priority, disclosure, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (id) DO NOTHING
                    """,
                    e["id"], e["parent_uuid"], e["child_uuid"], e["name"],
                    e["priority"], e["disclosure"], e["created_at"],
                )

            for p in data["paths"]:
                await conn.execute(
                    """
                    INSERT INTO paths (namespace, domain, path, edge_id, node_uuid, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING
                    """,
                    p["namespace"], p["domain"], p["path"], p["edge_id"],
                    p["node_uuid"], p["created_at"],
                )

            for g in data["glossary_keywords"]:
                await conn.execute(
                    """
                    INSERT INTO glossary_keywords (id, keyword, node_uuid, namespace, created_at)
                    VALUES ($1, $2, $3, $4, $5) ON CONFLICT (id) DO NOTHING
                    """,
                    g["id"], g["keyword"], g["node_uuid"], g["namespace"], g["created_at"],
                )

            for d in data["search_documents"]:
                await conn.execute(
                    """
                    INSERT INTO search_documents
                        (namespace, domain, path, node_uuid, memory_id, uri, content,
                         disclosure, search_terms, priority, world_timestamp, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    ON CONFLICT DO NOTHING
                    """,
                    d["namespace"], d["domain"], d["path"], d["node_uuid"],
                    d["memory_id"], d["uri"], d["content"], d["disclosure"],
                    d["search_terms"], d["priority"], d["world_timestamp"], d["updated_at"],
                )

            for l in data["memory_access_logs"]:
                await conn.execute(
                    """
                    INSERT INTO memory_access_logs (id, node_uuid, namespace, accessed_at, context)
                    VALUES ($1, $2, $3, $4, $5) ON CONFLICT (id) DO NOTHING
                    """,
                    l["id"], l["node_uuid"], l["namespace"], l["accessed_at"], l["context"],
                )
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, required=True,
                        help="Path to the SQLite database file (e.g. /tmp/nocturne_data.db)")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json",
                        help="config.json with the target database_url (default: <repo>/config.json)")
    args = parser.parse_args()

    if not args.sqlite.exists():
        sys.exit(f"SQLite file not found: {args.sqlite}")

    config = json.loads(args.config.read_text(encoding="utf-8"))
    database_url = config.get("database_url")
    if not database_url:
        sys.exit(f"config.json at {args.config} has no database_url")
    if database_url.startswith("postgresql+asyncpg://"):
        database_url = "postgresql://" + database_url.removeprefix("postgresql+asyncpg://")
    # 'ssl=disable' is a SQLAlchemy-dialect query param; asyncpg defaults to no TLS.
    database_url = database_url.split("?", 1)[0]

    data = load_sqlite(args.sqlite)
    _normalize_rows(data)
    print("Source rows:")
    for table, rows in data.items():
        print(f"  {table}: {len(rows)}")

    asyncio.run(migrate(data, database_url))
    print("Migration complete.")


if __name__ == "__main__":
    main()