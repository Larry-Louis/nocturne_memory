"""
End-to-end integration tests: all MCP tools with namespace isolation.

Test matrix:
  1. Full CRUD flow with two agents (read/create/update/delete/alias/trigger/search)
  2. system://boot isolation
  3. system://index and system://index/<domain> isolation
  4. system://recent isolation
  5. system://glossary cross-check (agent_b must not see agent_a's triggers)
  6. search_memory with domain filter + namespace
  7. Delete cascade (parent delete in ns_a doesn't affect ns_b)
  8. Backward compatibility (default namespace full flow)
"""

import os
import sys
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
os.environ["SKIP_DB_INIT"] = "true"
os.environ["VALID_DOMAINS"] = "core,writer,game,notes,system"
os.environ["CORE_MEMORY_URIS"] = "core://agent,core://my_user"

from db.namespace import set_namespace
from db.database import DatabaseManager
from db.graph import GraphService
from db.search import SearchIndexer
from db.glossary import GlossaryService
import db as db_pkg


@pytest_asyncio.fixture
async def mcp_env():
    """Set up a fresh in-memory DB and patch the global service singletons."""
    _db = DatabaseManager("sqlite+aiosqlite://")
    await _db.init_db()
    _search = SearchIndexer(_db)
    _glossary = GlossaryService(_db, _search)
    _graph = GraphService(_db, _search)

    old = (db_pkg._db_manager, db_pkg._graph_service, db_pkg._search_indexer, db_pkg._glossary_service)
    db_pkg._db_manager = _db
    db_pkg._graph_service = _graph
    db_pkg._search_indexer = _search
    db_pkg._glossary_service = _glossary

    yield

    db_pkg._db_manager, db_pkg._graph_service, db_pkg._search_indexer, db_pkg._glossary_service = old
    await _db.close()


# ====================================================================
# Helper: seed both agents
# ====================================================================

async def _seed_two_agents():
    """Create standard memories for agent_a and agent_b."""
    from mcp_server import remember_child_memory, tag_memory

    set_namespace("agent_a")
    await remember_child_memory(parent_uri="core://", content="I am Agent A's identity.",
                                importance=0, title="agent", when="When asking who I am")
    await remember_child_memory(parent_uri="core://agent", content="Agent A met User on 2026-01-01.",
                                importance=1, title="my_user", when="When talking about my user")
    await remember_child_memory(parent_uri="core://", content="Agent A writer notes.",
                                importance=2, title="notes", when="When discussing writing")
    await tag_memory("core://agent", add=["soul_trigger_a"])

    set_namespace("agent_b")
    await remember_child_memory(parent_uri="core://", content="I am Agent B's identity.",
                                importance=0, title="agent", when="When asking who I am")
    await tag_memory("core://agent", add=["soul_trigger_b"])


# ====================================================================
# 1. Full CRUD + alias + triggers + search
# ====================================================================

@pytest.mark.asyncio
async def test_full_mcp_crud_with_namespace_isolation(mcp_env):
    """All MCP tools work correctly; two agents are fully isolated."""
    from mcp_server import (
        browse_memory, remember_child_memory, edit_memory,
        forget_memory, link_memory, tag_memory, search_memory,
    )

    await _seed_two_agents()

    # --- Read isolation ---
    set_namespace("agent_a")
    assert "Agent A" in await browse_memory("core://agent")
    assert "Agent B" not in await browse_memory("core://agent")

    set_namespace("agent_b")
    assert "Agent B" in await browse_memory("core://agent")
    assert "Agent A" not in await browse_memory("core://agent")

    # --- Update isolation ---
    set_namespace("agent_a")
    result = await edit_memory("core://agent",
                               old_text="I am Agent A's identity.",
                               new_text="I am Agent A's evolved identity.")
    assert "已经改好了" in result
    assert "evolved" in await browse_memory("core://agent")

    set_namespace("agent_b")
    assert "evolved" not in await browse_memory("core://agent")

    # --- Alias isolation ---
    set_namespace("agent_a")
    assert "也能想起" in await link_memory(
        target_uri="core://agent",
        new_uri="writer://agent_copy",
        importance=5,
        when="When mirroring agent identity",
    )

    set_namespace("agent_b")
    alias_b = await browse_memory("writer://agent_copy")
    assert "not found" in alias_b

    # --- Delete isolation ---
    set_namespace("agent_a")
    assert "忘掉了" in await forget_memory("core://agent/my_user")
    deleted = await browse_memory("core://agent/my_user")
    assert "not found" in deleted

    set_namespace("agent_b")
    assert "Agent B" in await browse_memory("core://agent")


# ====================================================================
# 2. system://boot isolation
# ====================================================================

@pytest.mark.asyncio
async def test_system_boot_isolation(mcp_env):
    """system://boot loads core memories from the current namespace only."""
    from mcp_server import browse_memory

    await _seed_two_agents()

    set_namespace("agent_a")
    boot_a = await browse_memory("system://boot")
    assert "Agent A" in boot_a
    assert "Agent B" not in boot_a

    set_namespace("agent_b")
    boot_b = await browse_memory("system://boot")
    assert "Agent B" in boot_b
    assert "Agent A" not in boot_b


@pytest.mark.asyncio
async def test_system_boot_per_namespace_uris(mcp_env):
    """Per-namespace CORE_MEMORY_URIS override the global fallback."""
    from mcp_server import browse_memory

    await _seed_two_agents()

    # agent_a: boot only core://agent (skip core://my_user)
    # agent_b: boot only core://agent (its own override)
    os.environ["CORE_MEMORY_URIS__agent_a"] = "core://agent"
    os.environ["CORE_MEMORY_URIS__agent_b"] = "core://agent"
    try:
        set_namespace("agent_a")
        boot_a = await browse_memory("system://boot")
        assert "Agent A" in boot_a
        # core://my_user is in the global fallback but NOT in agent_a's override
        assert "met User" not in boot_a

        set_namespace("agent_b")
        boot_b = await browse_memory("system://boot")
        assert "Agent B" in boot_b
        assert "Agent A" not in boot_b
    finally:
        os.environ.pop("CORE_MEMORY_URIS__agent_a", None)
        os.environ.pop("CORE_MEMORY_URIS__agent_b", None)


@pytest.mark.asyncio
async def test_system_boot_per_namespace_empty_override(mcp_env):
    """An explicit empty boot-URI override for a namespace should NOT fall back to the global list."""
    from mcp_server import browse_memory
    from db import get_preset_service

    preset = get_preset_service()
    # Mock a global list
    await preset.set_boot_uris("", ["core://global"])
    # Mock an explicit empty list for agent_c
    await preset.set_boot_uris("agent_c", [])

    set_namespace("agent_c")
    boot_c = await browse_memory("system://boot")
    # Should NOT load global
    assert "core://global" not in boot_c
    assert "载入状态: 0/0" in boot_c


# ====================================================================
# 3. system://index and system://index/<domain>
# ====================================================================

@pytest.mark.asyncio
async def test_system_index_isolation(mcp_env):
    """system://index shows only the current namespace's memory tree."""
    from mcp_server import browse_memory

    await _seed_two_agents()

    set_namespace("agent_a")
    index_a = await browse_memory("system://index/core")
    assert "my_user" in index_a
    assert "notes" in index_a

    set_namespace("agent_b")
    index_b = await browse_memory("system://index/core")
    assert "my_user" not in index_b
    assert "notes" not in index_b


@pytest.mark.asyncio
async def test_system_index_domain_isolation(mcp_env):
    """system://index/<domain> only shows paths in the requested domain within the namespace."""
    from mcp_server import browse_memory, remember_child_memory

    set_namespace("agent_a")
    await remember_child_memory(parent_uri="core://", content="A core data", importance=0,
                                title="a_core", when="When reviewing agent A core index")
    await remember_child_memory(parent_uri="writer://", content="A writer data", importance=0,
                                title="a_writer", when="When reviewing agent A writer index")

    set_namespace("agent_b")
    await remember_child_memory(parent_uri="core://", content="B core data", importance=0,
                                title="b_core", when="When reviewing agent B core index")

    set_namespace("agent_a")
    index_core = await browse_memory("system://index/core")
    assert "a_core" in index_core
    assert "a_writer" not in index_core
    assert "b_core" not in index_core

    set_namespace("agent_b")
    index_core_b = await browse_memory("system://index/core")
    assert "b_core" in index_core_b
    assert "a_core" not in index_core_b


# ====================================================================
# 4. system://recent isolation
# ====================================================================

@pytest.mark.asyncio
async def test_system_recent_isolation(mcp_env):
    """system://recent shows only the current namespace's recent memories."""
    from mcp_server import browse_memory

    await _seed_two_agents()

    set_namespace("agent_a")
    recent_a = await browse_memory("system://recent")
    assert "core://agent" in recent_a
    assert "my_user" in recent_a

    set_namespace("agent_b")
    recent_b = await browse_memory("system://recent")
    assert "my_user" not in recent_b


# ====================================================================
# 5. system://glossary cross-check
# ====================================================================

@pytest.mark.asyncio
async def test_system_glossary_isolation(mcp_env):
    """Each agent's glossary only contains its own triggers, not the other's."""
    from mcp_server import browse_memory

    await _seed_two_agents()

    set_namespace("agent_a")
    glossary_a = await browse_memory("system://glossary")
    assert "soul_trigger_a" in glossary_a
    assert "soul_trigger_b" not in glossary_a

    set_namespace("agent_b")
    glossary_b = await browse_memory("system://glossary")
    assert "soul_trigger_b" in glossary_b
    assert "soul_trigger_a" not in glossary_b


# ====================================================================
# 6. search_memory with domain filter + namespace
# ====================================================================

@pytest.mark.asyncio
async def test_search_memory_domain_filter_isolation(mcp_env):
    """search_memory(domain=...) respects both domain and namespace."""
    from mcp_server import search_memory

    await _seed_two_agents()

    set_namespace("agent_a")
    result = await search_memory("identity", domain="core")
    assert "Agent A" in result
    assert "Agent B" not in result

    # agent_a has no writer:// content matching "identity"
    result_writer = await search_memory("identity", domain="writer")
    assert "Agent A" not in result_writer

    set_namespace("agent_b")
    result_b = await search_memory("identity", domain="core")
    assert "Agent B" in result_b
    assert "Agent A" not in result_b


# ====================================================================
# 7. Delete cascade isolation
# ====================================================================

@pytest.mark.asyncio
async def test_delete_cascade_isolation(mcp_env):
    """Deleting a parent in agent_a does not affect agent_b's subtree."""
    from mcp_server import remember_child_memory, forget_memory, browse_memory

    for ns in ("agent_a", "agent_b"):
        set_namespace(ns)
        await remember_child_memory(parent_uri="core://", content=f"Parent {ns}",
                                    importance=0, title="parent", when=f"When reviewing parent memory in {ns}")
        await remember_child_memory(parent_uri="core://parent", content=f"Child {ns}",
                                    importance=1, title="child", when=f"When reviewing child memory in {ns}")

    set_namespace("agent_a")
    await forget_memory("core://parent/child")
    await forget_memory("core://parent")

    deleted_parent = await browse_memory("core://parent")
    assert "not found" in deleted_parent

    set_namespace("agent_b")
    assert "Parent agent_b" in await browse_memory("core://parent")
    assert "Child agent_b" in await browse_memory("core://parent/child")


# ====================================================================
# 8. Backward compatibility — default namespace
# ====================================================================

@pytest.mark.asyncio
async def test_default_namespace_mcp_full_flow(mcp_env):
    """When no namespace is configured (empty string), all MCP tools work as before."""
    from mcp_server import (
        browse_memory, remember_child_memory, edit_memory,
        forget_memory, link_memory, tag_memory, search_memory,
    )

    set_namespace("")

    # Create
    assert "记住了：" in await remember_child_memory(
        parent_uri="core://", content="Default agent identity.", importance=0,
        title="agent", when="When asking who I am")
    assert "记住了：" in await remember_child_memory(
        parent_uri="core://agent", content="Default user info.", importance=1,
        title="my_user", when="When talking about user")

    # Read
    content = await browse_memory("core://agent")
    assert "Default agent identity" in content

    # Boot
    boot = await browse_memory("system://boot")
    assert "Default agent identity" in boot

    # Index
    index = await browse_memory("system://index/core")
    assert "agent" in index
    assert "my_user" in index

    # Recent
    recent = await browse_memory("system://recent")
    assert "core://agent" in recent

    # Update
    assert "已经改好了" in await edit_memory(
        "core://agent", old_text="Default agent identity.",
        new_text="Updated default agent identity.")

    # Search
    results = await search_memory("identity")
    assert "Updated default agent" in results

    # Alias
    assert "也能想起" in await link_memory(
        target_uri="core://agent",
        new_uri="writer://agent_ref",
        importance=5,
        when="When mirroring default agent identity",
    )

    alias_content = await browse_memory("writer://agent_ref")
    assert "Updated default agent" in alias_content

    # Triggers
    assert "加上了" in await tag_memory("core://agent", add=["default_trigger"])
    glossary = await browse_memory("system://glossary")
    assert "default_trigger" in glossary

    # Delete
    assert "忘掉了" in await forget_memory("core://agent/my_user")
    deleted = await browse_memory("core://agent/my_user")
    assert "not found" in deleted

    # Parent still alive
    assert "Updated default agent" in await browse_memory("core://agent")
