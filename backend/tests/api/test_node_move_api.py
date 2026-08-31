"""Regression tests: node move & rename via API (subtree cascade move)."""
import pytest


async def _seed_subtree(api_client):
    """Create target parent + a subtree with one child."""
    for payload in [
        {"parent_path": "", "content": "Target parent", "priority": 1,
         "disclosure": "x", "title": "target_parent", "domain": "core"},
        {"parent_path": "", "content": "Move root", "priority": 1,
         "disclosure": "x", "title": "move_me", "domain": "core"},
        {"parent_path": "move_me", "content": "Move child", "priority": 1,
         "disclosure": "x", "title": "child", "domain": "core"},
        {"parent_path": "move_me/child", "content": "Move grandchild", "priority": 1,
         "disclosure": "x", "title": "grand", "domain": "core"},
    ]:
        r = await api_client.post("/browse/node", json=payload)
        assert r.status_code == 200, r.text


async def _path_exists(api_client, path, domain="core"):
    r = await api_client.get("/browse/node", params={"path": path, "domain": domain})
    return r.status_code == 200


@pytest.mark.asyncio
async def test_move_node_moves_whole_subtree(api_client):
    await _seed_subtree(api_client)

    r = await api_client.post(
        "/browse/node/move",
        json={"path": "move_me", "domain": "core", "target_parent_path": "target_parent"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["new_path"] == "target_parent/move_me"

    # Old subtree gone
    assert not await _path_exists(api_client, "move_me")
    assert not await _path_exists(api_client, "move_me/child")
    assert not await _path_exists(api_client, "move_me/child/grand")

    # New subtree fully present
    assert await _path_exists(api_client, "target_parent/move_me")
    assert await _path_exists(api_client, "target_parent/move_me/child")
    assert await _path_exists(api_client, "target_parent/move_me/child/grand")

    # Content preserved
    r = await api_client.get(
        "/browse/node", params={"path": "target_parent/move_me/child", "domain": "core"}
    )
    assert r.json()["node"]["content"] == "Move child"


@pytest.mark.asyncio
async def test_rename_node_changes_last_segment_and_moves_subtree(api_client):
    await _seed_subtree(api_client)

    r = await api_client.post(
        "/browse/node/rename",
        json={"path": "move_me", "new_name": "renamed_me", "domain": "core"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["new_path"] == "renamed_me"

    assert not await _path_exists(api_client, "move_me")
    assert not await _path_exists(api_client, "move_me/child")
    assert await _path_exists(api_client, "renamed_me")
    assert await _path_exists(api_client, "renamed_me/child")
    assert await _path_exists(api_client, "renamed_me/child/grand")


@pytest.mark.asyncio
async def test_move_node_cross_domain(api_client):
    await _seed_subtree(api_client)
    # Cross-domain move requires the target parent to exist in the target domain.
    r = await api_client.post(
        "/browse/node",
        json={"parent_path": "", "content": "Writer target parent", "priority": 1,
              "disclosure": "x", "title": "target_parent", "domain": "writer"},
    )
    assert r.status_code == 200, r.text

    r = await api_client.post(
        "/browse/node/move",
        json={"path": "move_me", "domain": "core", "target_parent_path": "target_parent",
              "target_domain": "writer"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["new_domain"] == "writer"
    assert r.json()["new_path"] == "target_parent/move_me"

    assert not await _path_exists(api_client, "move_me")
    assert await _path_exists(api_client, "target_parent/move_me", domain="writer")
    assert await _path_exists(api_client, "target_parent/move_me/child", domain="writer")


@pytest.mark.asyncio
async def test_move_node_cross_domain_via_uri_prefix(api_client):
    """UI sends target_parent_path with a 'domain://' prefix (e.g. 'writer://folder').

    Regression: backend used to treat the whole string as a path while keeping
    the *current* domain, so moving to another root silently produced
    'core://writer://folder/...' instead of crossing domains.
    """
    await _seed_subtree(api_client)
    r = await api_client.post(
        "/browse/node",
        json={"parent_path": "", "content": "Writer target parent", "priority": 1,
              "disclosure": "x", "title": "target_parent", "domain": "writer"},
    )
    assert r.status_code == 200, r.text

    r = await api_client.post(
        "/browse/node/move",
        json={"path": "move_me", "domain": "core",
              "target_parent_path": "writer://target_parent"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["new_domain"] == "writer"
    assert r.json()["new_path"] == "target_parent/move_me"

    assert not await _path_exists(api_client, "move_me")
    assert await _path_exists(api_client, "target_parent/move_me", domain="writer")
    assert await _path_exists(api_client, "target_parent/move_me/child", domain="writer")


@pytest.mark.asyncio
async def test_move_node_to_bare_domain_root_via_uri_prefix(api_client):
    """Bare 'domain://' prefix (no path after it) moves the node to that
    domain's root.

    Regression: 'projects://'.strip('/') produced 'projects:', which then no
    longer contained '://', so the domain prefix was dropped and the backend
    tried to resolve 'core://projects:' -> 422 'Parent does not exist'.
    """
    await _seed_subtree(api_client)

    r = await api_client.post(
        "/browse/node/move",
        json={"path": "move_me", "domain": "core", "target_parent_path": "projects://"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["new_domain"] == "projects"
    assert r.json()["new_path"] == "move_me"

    assert not await _path_exists(api_client, "move_me")
    assert await _path_exists(api_client, "move_me", domain="projects")
    assert await _path_exists(api_client, "move_me/child", domain="projects")
