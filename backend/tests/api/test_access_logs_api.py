"""Regression tests: access-log frequency tracking (per-node query counts).

Semantics:
- Only AI-side reads (context="mcp_read") count toward access_count.
- Human dashboard browsing (GET /browse/node) must NOT inflate the signal.
"""
import asyncio

import pytest


async def _create_node(api_client, title, content="Access log test"):
    r = await api_client.post(
        "/browse/node",
        json={"parent_path": "", "content": content, "priority": 1,
              "disclosure": "x", "title": title, "domain": "core"},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _node_uuid(graph_service, uri):
    """Resolve a node's uuid from its URI via the graph service."""
    from db.namespace import get_namespace
    domain, path = uri.split("://", 1)
    memory = await graph_service.get_memory_by_path(path, domain=domain, namespace=get_namespace())
    assert memory is not None, f"node not found: {uri}"
    return memory["node_uuid"]


async def _simulate_mcp_read(graph_service, node_uuid, times=1):
    """Bypass HTTP: record access exactly as the MCP read path does."""
    from db.namespace import get_namespace
    for _ in range(times):
        await graph_service.log_access(node_uuid, namespace=get_namespace(), context="mcp_read")


@pytest.mark.asyncio
async def test_dashboard_browsing_does_not_inflate_access_count(api_client, graph_service):
    """API reads must not count toward frequency — only MCP reads do."""
    node = await _create_node(api_client, "browse_only_node")
    path = node["uri"].split("://")[1]

    # Browse 3 times via the Dashboard API (human).
    for _ in range(3):
        r = await api_client.get("/browse/node", params={"path": path, "domain": "core"})
        assert r.status_code == 200, r.text
    await asyncio.sleep(0.3)

    # Node detail shows access_count that excludes human browsing.
    r = await api_client.get("/browse/node", params={"path": path, "domain": "core"})
    assert r.status_code == 200, r.text
    assert r.json()["node"]["access_count"] == 0, r.json()["node"]["access_count"]

    # ranked must not include the node at all.
    r = await api_client.get("/maintenance/access-logs/ranked")
    assert r.status_code == 200, r.text
    assert not any(node["uri"] in e["uris"] for e in r.json())


@pytest.mark.asyncio
async def test_mcp_reads_rank_by_frequency(api_client, graph_service):
    hot = await _create_node(api_client, "hot_node")
    cold = await _create_node(api_client, "cold_node")

    # Simulate MCP reads: hot 3x, cold 1x.
    await _simulate_mcp_read(graph_service, await _node_uuid(graph_service, hot["uri"]), times=3)
    await _simulate_mcp_read(graph_service, await _node_uuid(graph_service, cold["uri"]), times=1)

    r = await api_client.get("/maintenance/access-logs/ranked")
    assert r.status_code == 200, r.text
    ranked = r.json()

    by_uri = {}
    for entry in ranked:
        for uri in entry["uris"]:
            by_uri[uri] = entry

    hot_entry = by_uri.get(hot["uri"])
    cold_entry = by_uri.get(cold["uri"])
    assert hot_entry is not None, f"hot node missing from ranked: {ranked}"
    assert cold_entry is not None, f"cold node missing from ranked: {ranked}"
    assert hot_entry["access_count"] == 3, hot_entry
    assert cold_entry["access_count"] == 1, cold_entry

    hot_index = next(i for i, e in enumerate(ranked) if e["node_uuid"] == hot_entry["node_uuid"])
    cold_index = next(i for i, e in enumerate(ranked) if e["node_uuid"] == cold_entry["node_uuid"])
    assert hot_index < cold_index

    # Node detail carries the MCP-only access count.
    hot_path = hot["uri"].split("://")[1]
    r = await api_client.get("/browse/node", params={"path": hot_path, "domain": "core"})
    assert r.status_code == 200, r.text
    assert r.json()["node"]["access_count"] == 3


@pytest.mark.asyncio
async def test_include_human_opt_in(api_client, graph_service):
    """include_human=true adds human browsing events to the ranked signal.

    New dashboard browsing no longer writes api_read logs (human browsing
    must not inflate importance), so the human signal only exists when such
    logs were written by older versions. This test simulates that legacy case.
    """
    from db.namespace import get_namespace
    node = await _create_node(api_client, "human_read_node")

    # One MCP read + two legacy human-browsing logs (as old versions wrote).
    await _simulate_mcp_read(graph_service, await _node_uuid(graph_service, node["uri"]), times=1)
    uuid = await _node_uuid(graph_service, node["uri"])
    for _ in range(2):
        await graph_service.log_access(uuid, namespace=get_namespace(), context="api_read")

    # Default: only the MCP read counts.
    r = await api_client.get("/maintenance/access-logs/ranked")
    assert r.status_code == 200, r.text
    default_entry = next((e for e in r.json() if node["uri"] in e["uris"]), None)
    assert default_entry is not None
    assert default_entry["access_count"] == 1

    # include_human=true: legacy human logs now count too.
    r = await api_client.get("/maintenance/access-logs/ranked", params={"include_human": "true"})
    assert r.status_code == 200, r.text
    human_entry = next((e for e in r.json() if node["uri"] in e["uris"]), None)
    assert human_entry is not None
    assert human_entry["access_count"] == 3


@pytest.mark.asyncio
async def test_ranked_access_logs_days_filter(api_client, graph_service):
    node = await _create_node(api_client, "days_filter_node")
    await _simulate_mcp_read(graph_service, await _node_uuid(graph_service, node["uri"]), times=1)

    r = await api_client.get("/maintenance/access-logs/ranked", params={"days": 365})
    assert r.status_code == 200, r.text
    assert any(node["uri"] in e["uris"] for e in r.json())

    r = await api_client.get("/maintenance/access-logs/ranked", params={"days": 1})
    assert r.status_code == 200, r.text
    assert any(node["uri"] in e["uris"] for e in r.json())