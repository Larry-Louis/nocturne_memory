from fastapi import APIRouter, HTTPException
from db import get_graph_service
from db.models import MemoryAccessLog
from db.namespace import get_namespace
from namespace_middleware import _validate_namespace
from locales import t
from sqlalchemy import select, func, delete
from datetime import datetime, timedelta
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


@router.get("/orphans")
async def get_orphans():
    """
    Get all orphan memories (all have deprecated=True).
    
    - deprecated: old versions created by update_memory (migrated_to is set)
    - orphaned: node lost all paths, auto-deprecated (migrated_to is NULL)
    
    Includes migration target paths for deprecated memories so the human can see
    where the memory used to live without clicking into each one.
    """
    graph = get_graph_service()
    return await graph.get_all_orphan_memories()


@router.get("/orphans/{memory_id}")
async def get_orphan_detail(memory_id: int):
    """
    Get full detail of an orphan memory, including migration target's
    full content for diff comparison.
    """
    graph = get_graph_service()
    detail = await graph.get_orphan_detail(memory_id)
    if not detail:
        raise HTTPException(status_code=404, detail=t("api.maintenance.memory_not_found").format(memory_id=memory_id))
    return detail


@router.delete("/orphans/{memory_id}")
async def delete_orphan(memory_id: int):
    """
    Permanently delete an orphan memory.
    This action is irreversible. Repairs the version chain if applicable.
    
    Safety: requires deprecated=True; active memories are never deleted.
    """
    graph = get_graph_service()
    try:
        result = await graph.permanently_delete_memory(memory_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/access-logs/stats")
async def get_access_log_stats():
    """
    Get global stats for access logs (total count, oldest record date).
    """
    graph = get_graph_service()

    async with graph.session() as session:
        count = await session.scalar(
            select(func.count(MemoryAccessLog.id))
        )
        oldest = await session.scalar(
            select(func.min(MemoryAccessLog.accessed_at))
        )

    return {
        "count": count or 0,
        "oldest": oldest.isoformat() if oldest else None
    }


class ClearLogsRequest(BaseModel):
    keep_days: Optional[int] = None

@router.delete("/access-logs")
async def clear_access_logs(req: ClearLogsRequest):
    """
    Clear access logs globally. If keep_days is provided, deletes logs older than X days.
    If keep_days is 0 or None, deletes all logs.
    """
    graph = get_graph_service()

    async with graph.session() as session:
        stmt = delete(MemoryAccessLog)

        if req.keep_days and req.keep_days > 0:
            cutoff = datetime.now() - timedelta(days=req.keep_days)
            stmt = stmt.where(MemoryAccessLog.accessed_at < cutoff)

        result = await session.execute(stmt)
        return {"deleted": result.rowcount}


@router.get("/access-logs/ranked")
async def get_ranked_access_logs(
    days: Optional[int] = None,
    limit: int = 50,
    namespace: Optional[str] = None,
    include_human: bool = False,
):
    """
    Aggregate access counts per node, ordered by frequency (high-traffic first).

    - days: only count accesses within the last N days (None = all time).
    - limit: max number of nodes to return (default 50).
    - namespace: restrict to a namespace (None = current namespace).
    - include_human: by default only AI-side reads (context=mcp_read) are
      counted so dashboard browsing doesn't skew importance; set true to
      include human reads too.

    Returns list of {node_uuid, access_count, last_accessed_at, uris: [...]},
    with each node's current URI paths (best path first).
    """
    from db.models import Path as PathModel, Edge as EdgeModel, Node as NodeModel

    graph = get_graph_service()
    target_namespace = (namespace if namespace is not None else get_namespace()).strip()

    filters = [MemoryAccessLog.namespace == target_namespace]
    if not include_human:
        filters.append(MemoryAccessLog.context == "mcp_read")
    if days is not None and days > 0:
        cutoff = datetime.now() - timedelta(days=days)
        filters.append(MemoryAccessLog.accessed_at >= cutoff)

    async with graph.session() as session:
        count_stmt = (
            select(
                MemoryAccessLog.node_uuid,
                func.count(MemoryAccessLog.id).label("access_count"),
                func.max(MemoryAccessLog.accessed_at).label("last_accessed_at"),
            )
            .where(*filters)
            .group_by(MemoryAccessLog.node_uuid)
            .order_by(func.count(MemoryAccessLog.id).desc())
            .limit(limit)
        )
        rows = (await session.execute(count_stmt)).all()

        node_uuids = [row.node_uuid for row in rows]
        uris_by_node: dict = {uid: [] for uid in node_uuids}
        if node_uuids:
            path_stmt = (
                select(
                    PathModel.node_uuid,
                    PathModel.domain,
                    PathModel.path,
                )
                .select_from(PathModel)
                .join(EdgeModel, PathModel.edge_id == EdgeModel.id)
                .where(PathModel.namespace == target_namespace)
                .where(EdgeModel.child_uuid.in_(node_uuids))
            )
            path_rows = (await session.execute(path_stmt)).all()
            for node_uuid, domain, path in path_rows:
                uris_by_node.setdefault(node_uuid, []).append(f"{domain}://{path}")

        result = []
        for row in rows:
            result.append({
                "node_uuid": row.node_uuid,
                "access_count": row.access_count,
                "last_accessed_at": row.last_accessed_at.isoformat() if row.last_accessed_at else None,
                "uris": uris_by_node.get(row.node_uuid, []),
            })
        return result


class RestoreOrphanRequest(BaseModel):
    new_domain: str = "core"
    new_path: str
    priority: int = 0
    disclosure: Optional[str] = None
    namespace: Optional[str] = None


@router.post("/orphans/{memory_id}/restore")
async def restore_orphan(memory_id: int, req: RestoreOrphanRequest):
    """
    Restore an orphan or deprecated memory by assigning a new path and
    activating it (deprecated=False).
    """
    graph = get_graph_service()
    target_namespace = (req.namespace if req.namespace is not None else get_namespace()).strip()
    if err := _validate_namespace(target_namespace):
        raise HTTPException(status_code=422, detail=err)
    try:
        result = await graph.restore_orphan_memory(
            memory_id=memory_id,
            new_path=req.new_path,
            new_domain=req.new_domain,
            priority=req.priority,
            disclosure=req.disclosure,
            namespace=target_namespace,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

