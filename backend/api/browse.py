"""
Browse API - Clean URI-based memory navigation

This replaces the old Entity/Relation/Chapter conceptual split with a simple
hierarchical browser. Every path is just a node with content and children.
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
import asyncio
import config
from db import get_graph_service, get_glossary_service, get_db_manager, get_search_indexer, get_preset_service
from db.models import Path as PathModel, Edge as EdgeModel, ROOT_NODE_UUID
from db.namespace import get_namespace
from locales import t
from sqlalchemy import select, distinct
import re

router = APIRouter(prefix="/browse", tags=["browse"])


class NodeUpdate(BaseModel):
    content: str | None = None
    priority: int | None = None
    disclosure: str | None = None
    world_timestamp: str | None = None


class GlossaryAdd(BaseModel):
    keyword: str
    node_uuid: str


class GlossaryRemove(BaseModel):
    keyword: str
    node_uuid: str


class CreateMemoryRequest(BaseModel):
    parent_path: str
    content: str
    priority: int
    disclosure: str
    title: str | None = None
    domain: str = "core"
    world_timestamp: str | None = None


class CreateAliasRequest(BaseModel):
    new_path: str
    target_path: str
    disclosure: str
    new_domain: str = "core"
    target_domain: str = "core"
    priority: int = 0


class RenameNodeRequest(BaseModel):
    path: str
    new_name: str
    domain: str = "core"


class MoveNodeRequest(BaseModel):
    path: str
    target_parent_path: str
    domain: str = "core"
    target_domain: str | None = None  # None = inherit source domain


@router.get("/namespaces")
async def list_namespaces():
    """Return all distinct namespaces that exist in the paths table."""
    db = get_db_manager()
    async with db.session() as session:
        result = await session.execute(
            select(distinct(PathModel.namespace)).order_by(PathModel.namespace)
        )
        return [row[0] for row in result.all()]


@router.get("/domains")
async def list_domains():
    """Return all valid domains, with root-level node counts where applicable."""
    from sqlalchemy import func, distinct

    valid_domains = config.get("valid_domains") or ["core"]

    db = get_db_manager()
    async with db.session() as session:
        result = await session.execute(
            select(
                PathModel.domain,
                func.count(distinct(PathModel.path)).label("node_count"),
            )
            .where(PathModel.namespace == get_namespace())
            .where(~PathModel.path.contains("/"))
            .group_by(PathModel.domain)
            .order_by(PathModel.domain)
        )
        db_counts = {row.domain: row.node_count for row in result.all()}

    domains_to_return = []
    seen = set()
    for d in valid_domains:
        domains_to_return.append({"domain": d, "root_count": db_counts.get(d, 0)})
        seen.add(d)

    for d, count in db_counts.items():
        if d not in seen:
            domains_to_return.append({"domain": d, "root_count": count})
            seen.add(d)

    return domains_to_return


class AddDomainRequest(BaseModel):
    domain: str


@router.post("/domains")
async def add_domain(body: AddDomainRequest):
    domain = body.domain.strip().lower()
    if not re.match(r'^[a-z][a-z0-9_]*$', domain):
        raise HTTPException(status_code=422, detail=t("api.settings.validation_error"))
    if domain == "system":
        raise HTTPException(status_code=400, detail=t("api.settings.system_reserved_error"))

    current = list(config.get("valid_domains") or ["core"])
    if domain not in current:
        current.append(domain)
        config.set_value("valid_domains", current)
        return {"success": True, "domain": domain, "added": True}

    return {"success": True, "domain": domain, "added": False}


@router.delete("/domains/{domain}")
async def remove_domain(domain: str):
    domain = domain.strip().lower()
    if domain in ("core", "system"):
        raise HTTPException(status_code=400, detail=t("api.settings.core_remove_error"))
        
    db = get_db_manager()
    async with db.session() as session:
        result = await session.execute(
            select(PathModel.path)
            .where(PathModel.domain == domain)
            .limit(1)
        )
        if result.first():
            raise HTTPException(
                status_code=409, 
                detail=t("api.settings.domain_in_use_error")
            )

    # Check if any boot URI across ALL presets references this domain
    service = get_preset_service()
    all_presets = await service.list_presets()
    domain_prefix = f"{domain}://"
    referencing = []
    for preset in all_presets:
        for ns, uris in preset["boot_uris"].items():
            if any(u == domain_prefix or u.startswith(domain_prefix) for u in uris):
                label = ns if ns else "(default)"
                referencing.append(f"{preset['name']}:{label}")

    if referencing:
        raise HTTPException(
            status_code=409,
            detail=t("api.settings.domain_boot_uri_conflict").format(
                domain=domain, namespaces=", ".join(referencing)
            ),
        )

    current = list(config.get("valid_domains") or ["core"])
    if domain in current:
        current.remove(domain)
        config.set_value("valid_domains", current)

    return {"success": True, "domain": domain}


@router.get("/node")
async def get_node(
    path: str = Query("", description="URI path like 'nocturne' or 'nocturne/salem'"),
    domain: str = Query("core"),
    nav_only: bool = Query(False, description="Skip expensive processing if only navigating tree")
):
    """
    Get a node's content and its direct children.
    """
    graph = get_graph_service()
    
    if not path:
        memory = await graph.get_memory_by_path("", domain=domain, namespace=get_namespace())
        children_raw = await graph.get_children(
            ROOT_NODE_UUID,
            context_domain=domain,
            context_path=path,
            namespace=get_namespace()
        )
        
        if memory:
            children_raw = [c for c in children_raw if c.get("node_uuid") != memory["node_uuid"]]
        else:
            memory = {
                "content": "",
                "priority": 0,
                "disclosure": None,
                "created_at": None,
                "world_timestamp": None,
                "node_uuid": ROOT_NODE_UUID,
            }
        breadcrumbs = [{"path": "", "label": "root"}]
    else:
        memory = await graph.get_memory_by_path(path, domain=domain, namespace=get_namespace())
        if not memory:
            raise HTTPException(status_code=404, detail=t("api.browse.path_not_found").format(uri=f"{domain}://{path}"))
        
        children_raw = await graph.get_children(
            memory["node_uuid"],
            context_domain=domain,
            context_path=path,
            namespace=get_namespace()
        )
        
        segments = path.split("/")
        breadcrumbs = [{"path": "", "label": "root"}]
        accumulated = ""
        for seg in segments:
            accumulated = f"{accumulated}/{seg}" if accumulated else seg
            breadcrumbs.append({"path": accumulated, "label": seg})
    
    children = [
        {
            "domain": c["domain"],
            "path": c["path"],
            "uri": f"{c['domain']}://{c['path']}",
            "name": c["path"].split("/")[-1],
            "priority": c["priority"],
            "disclosure": c.get("disclosure"),
            "content_snippet": c["content_snippet"],
            "approx_children_count": c.get("approx_children_count", 0)
        }
        for c in children_raw
        if c["domain"] == domain and c["path"] != ""
    ]
    children.sort(key=lambda x: (x["priority"] if x["priority"] is not None else 999, x["path"]))
    
    aliases = []
    if memory.get("node_uuid") and memory["node_uuid"] != ROOT_NODE_UUID:
        async with get_db_manager().session() as session:
            result = await session.execute(
                select(PathModel.domain, PathModel.path)
                .select_from(PathModel)
                .join(EdgeModel, PathModel.edge_id == EdgeModel.id)
                .where(PathModel.namespace == get_namespace())
                .where(EdgeModel.child_uuid == memory["node_uuid"])
            )
            aliases = [f"{row[0]}://{row[1]}" for row in result.all() if not (row[0] == domain and row[1] == path)]
    
    glossary_keywords = []
    glossary_matches = []
    node_uuid = memory.get("node_uuid")

    if not nav_only:
        _glossary = get_glossary_service()
        if node_uuid and node_uuid != ROOT_NODE_UUID:
            glossary_keywords = await _glossary.get_glossary_for_node(node_uuid, namespace=get_namespace())

        if memory.get("content"):
            matches_dict = await _glossary.find_glossary_in_content(memory["content"], namespace=get_namespace())
            if matches_dict:
                glossary_matches = [{"keyword": kw, "nodes": nodes} for kw, nodes in matches_dict.items()]

    # Access count reflects AI-side memory retrieval (MCP reads) only.
    # Human dashboard browsing must NOT inflate the frequency signal.
    access_count = 0
    if node_uuid and node_uuid != ROOT_NODE_UUID:
        from sqlalchemy import func as sa_func
        from db.models import MemoryAccessLog
        async with get_db_manager().session() as session:
            access_count = await session.scalar(
                select(sa_func.count(MemoryAccessLog.id)).where(
                    MemoryAccessLog.node_uuid == node_uuid,
                    MemoryAccessLog.namespace == get_namespace(),
                    MemoryAccessLog.context == "mcp_read",
                )
            ) or 0

    return {
        "node": {
            "path": path,
            "domain": domain,
            "uri": f"{domain}://{path}",
            "name": path.split("/")[-1] if path else "root",
            "content": memory["content"],
            "priority": memory["priority"],
            "disclosure": memory["disclosure"],
            "world_timestamp": memory.get("world_timestamp"),
            "created_at": memory["created_at"],
            "is_virtual": memory.get("created_at") is None,
            "aliases": aliases,
            "node_uuid": node_uuid,
            "access_count": access_count,
            "glossary_keywords": glossary_keywords,
            "glossary_matches": glossary_matches,
        },
        "children": children,
        "breadcrumbs": breadcrumbs
    }


@router.put("/node")
async def update_node(
    body: NodeUpdate,
    path: str = Query(...),
    domain: str = Query("core"),
):
    """Update a node's content."""
    graph = get_graph_service()
    
    memory = await graph.get_memory_by_path(path, domain=domain, namespace=get_namespace())
    if not memory:
        raise HTTPException(status_code=404, detail=t("api.browse.path_not_found").format(uri=f"{domain}://{path}"))
    
    try:
        result = await graph.update_memory(
            path=path,
            domain=domain,
            content=body.content,
            priority=body.priority,
            disclosure=body.disclosure,
            world_timestamp=body.world_timestamp,
            namespace=get_namespace(),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    
    return {"success": True, "memory_id": result["new_memory_id"]}


@router.post("/node")
async def create_node(body: CreateMemoryRequest):
    """Create a new memory node."""
    graph = get_graph_service()

    if not body.disclosure:
        raise HTTPException(status_code=422, detail=t("api.browse.disclosure_empty"))

    if body.title is not None and not re.match(r'^[a-zA-Z0-9_-]+$', body.title):
        raise HTTPException(status_code=422, detail=t("api.browse.invalid_title"))

    try:
        result = await graph.create_memory(
            parent_path=body.parent_path,
            content=body.content,
            priority=body.priority,
            title=body.title,
            disclosure=body.disclosure,
            domain=body.domain,
            world_timestamp=body.world_timestamp,
            namespace=get_namespace(),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return {"success": True, "uri": result["uri"], "memory_id": result["id"]}


@router.post("/alias")
async def create_alias(body: CreateAliasRequest):
    """Create an alias (extra path) for an existing node."""
    graph = get_graph_service()

    if not body.disclosure:
        raise HTTPException(status_code=422, detail=t("api.browse.disclosure_empty"))

    try:
        await graph.add_path(
            new_path=body.new_path,
            target_path=body.target_path,
            new_domain=body.new_domain,
            target_domain=body.target_domain,
            priority=body.priority,
            disclosure=body.disclosure,
            namespace=get_namespace(),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return {"success": True, "uri": f"{body.new_domain}://{body.new_path}"}


async def _move_node(
    graph,
    preset,
    old_path: str,
    old_domain: str,
    new_path: str,
    new_domain: str,
    namespace: str,
) -> dict:
    """Move a node and its whole subtree to a new path.

    Uses add_path's descendant cascade to recreate the subtree under the new
    prefix, then remove_path to clean up the old prefix.  Boot URI entries
    pointing into the old subtree are rewritten to the new location.
    """
    old_uri = f"{old_domain}://{old_path}"
    new_uri = f"{new_domain}://{new_path}"

    if old_uri == new_uri:
        return {"success": True, "old_uri": old_uri, "new_uri": new_uri, "unchanged": True}

    memory = await graph.get_memory_by_path(old_path, domain=old_domain, namespace=namespace)
    if not memory:
        raise ValueError(t("api.browse.path_not_found").format(uri=old_uri))

    # 1. Recreate the subtree under the new path (cascades descendants).
    await graph.add_path(
        new_path=new_path,
        target_path=old_path,
        new_domain=new_domain,
        target_domain=old_domain,
        priority=memory.get("priority", 0),
        disclosure=memory.get("disclosure"),
        namespace=namespace,
    )

    # 2. Remove the old path (and its subtree).  If cleanup fails, roll back
    #    the newly created path so the move is atomic.
    try:
        await graph.remove_path(old_path, old_domain, namespace=namespace)
    except ValueError as e:
        try:
            await graph.remove_path(new_path, new_domain, namespace=namespace)
        except Exception:
            pass
        raise ValueError(t("api.browse.rename_partial_failure").format(error=str(e)))

    # 3. Rewrite boot URI entries that point into the old subtree.
    await preset.rewrite_boot_uri(old_uri, new_uri, namespace)

    return {
        "success": True,
        "old_uri": old_uri,
        "new_uri": new_uri,
        "old_path": old_path,
        "new_path": new_path,
        "new_domain": new_domain,
    }


@router.post("/node/rename")
async def rename_node(body: RenameNodeRequest):
    """Rename a memory by changing the last segment of its URI path.

    The node and its whole subtree are moved to the new path
    (add_path cascade + remove_path cleanup).
    """
    if not re.match(r"^[a-zA-Z0-9_-]+$", body.new_name):
        raise HTTPException(status_code=422, detail=t("api.browse.invalid_name"))

    old_path = body.path
    if "/" in old_path:
        parent = old_path.rsplit("/", 1)[0]
        new_path = f"{parent}/{body.new_name}"
    else:
        new_path = body.new_name

    graph = get_graph_service()
    preset = get_preset_service()
    try:
        result = await _move_node(
            graph,
            preset,
            old_path=old_path,
            old_domain=body.domain,
            new_path=new_path,
            new_domain=body.domain,
            namespace=get_namespace(),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return result


@router.post("/node/move")
async def move_node(body: MoveNodeRequest):
    """Move a node (and its whole subtree) under another parent path.

    The node keeps its own name; only its parent changes.  Cross-domain moves
    are supported via target_domain OR by prefixing target_parent_path with
    "domain://" (e.g. "writer://folder").  An explicit domain:// prefix wins
    over the current domain.
    """
    old_path = body.path
    name = old_path.rsplit("/", 1)[-1]
    raw_parent = body.target_parent_path.strip()

    # Parse an optional "domain://" prefix out of the target parent path.
    # This lets the UI move a node to a different root without the backend
    # silently defaulting to the current domain.  A bare root like "writer://"
    # (no path after the prefix) resolves to the root of that domain.
    target_domain = body.target_domain
    if "://" in raw_parent:
        scheme, _, rest = raw_parent.partition("://")
        if scheme:
            target_domain = scheme
            raw_parent = rest
    target_parent = raw_parent.strip("/")

    if target_parent:
        new_path = f"{target_parent}/{name}"
    else:
        new_path = name

    new_domain = target_domain or body.domain

    graph = get_graph_service()
    preset = get_preset_service()
    try:
        result = await _move_node(
            graph,
            preset,
            old_path=old_path,
            old_domain=body.domain,
            new_path=new_path,
            new_domain=new_domain,
            namespace=get_namespace(),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return result


@router.delete("/node")
async def delete_node(
    path: str = Query(...),
    domain: str = Query("core")
):
    """Delete a path. If it's the last path for a node, the node is archived/orphaned."""
    graph = get_graph_service()
    try:
        await graph.remove_path(path, domain=domain, namespace=get_namespace())
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    
    return {"success": True}


@router.post("/glossary")
async def add_glossary(body: GlossaryAdd):
    """Bind a keyword to a node."""
    glossary = get_glossary_service()
    try:
        await glossary.add_glossary_keyword(body.keyword, body.node_uuid, namespace=get_namespace())
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"success": True}


@router.delete("/glossary")
async def remove_glossary(body: GlossaryRemove):
    """Unbind a keyword from a node."""
    glossary = get_glossary_service()
    try:
        await glossary.remove_glossary_keyword(body.keyword, body.node_uuid, namespace=get_namespace())
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"success": True}


@router.get("/search")
async def search_memories(
    q: str = Query(..., min_length=1, description="Search query"),
    domain: Optional[str] = Query(None, description="Optional domain filter"),
    limit: int = Query(20, ge=1, le=100),
):
    """Search memories across the graph."""
    search = get_search_indexer()
    results = await search.search(q, limit, domain, namespace=get_namespace())
    return results
