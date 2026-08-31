"""Regression test: glossary keyword removal via API (bug: AttributeError -> 500).

Covers https://github.com/Dataojitori/nocturne_memory - "Initial local version"
regression where browse.py called GlossaryService.remove_keyword() which does not
exist (real method: remove_glossary_keyword), returning 500 on the dashboard.
"""
import pytest


async def _seed_node(api_client):
    r = await api_client.post(
        "/browse/node",
        json={
            "parent_path": "",
            "content": "test glossary removal",
            "priority": 1,
            "disclosure": "x",
            "title": "glossary_test",
            "domain": "core",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    node_path = data["uri"].split("://")[1]
    r = await api_client.get("/browse/node", params={"path": node_path, "domain": "core"})
    assert r.status_code == 200, r.text
    return r.json()["node"]["node_uuid"], node_path


@pytest.mark.asyncio
async def test_api_glossary_add_and_remove(api_client):
    uid, node_path = await _seed_node(api_client)

    # Add keyword
    r = await api_client.post(
        "/browse/glossary", json={"keyword": "glossary_test_kw", "node_uuid": uid}
    )
    assert r.status_code == 200, r.text

    r = await api_client.get("/browse/node", params={"path": node_path, "domain": "core"})
    assert "glossary_test_kw" in r.json()["node"].get("glossary_keywords", [])

    # Remove keyword - the reported buggy path (was 500 AttributeError)
    r = await api_client.request(
        "DELETE",
        "/browse/glossary",
        json={"keyword": "glossary_test_kw", "node_uuid": uid},
    )
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"

    r = await api_client.get("/browse/node", params={"path": node_path, "domain": "core"})
    assert "glossary_test_kw" not in r.json()["node"].get("glossary_keywords", [])
