"""Tests for the galaxy UI route and GET /v1/knowledge/graph."""

import json
import sqlite3

import pytest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openjarvis.server.galaxy import build_knowledge_graph, galaxy_router
except ImportError:
    galaxy_router = None

pytestmark = pytest.mark.skipif(
    galaxy_router is None,
    reason="fastapi not installed (requires server extra)",
)


def _make_stores(tmp_path):
    traces = sqlite3.connect(tmp_path / "traces.db")
    traces.executescript(
        """
        CREATE TABLE traces (trace_id TEXT, query TEXT, result TEXT, outcome TEXT,
                             started_at REAL, total_tokens INTEGER);
        CREATE TABLE trace_steps (trace_id TEXT, step_type TEXT, input TEXT);
        """
    )
    traces.executemany(
        "INSERT INTO traces VALUES (?, ?, ?, 'success', ?, 100)",
        [
            ("t1", "Qual a capital da Austrália?", "Canberra é a capital", 1.0),
            ("t2", "Quantos habitantes tem Canberra?", "Canberra tem 540 mil", 2.0),
        ],
    )
    traces.execute(
        "INSERT INTO trace_steps VALUES ('t1', 'tool_call', ?)",
        (json.dumps({"tool": "web_search"}),),
    )
    traces.commit()
    traces.close()

    mem = sqlite3.connect(tmp_path / "memory.db")
    mem.execute(
        "CREATE TABLE documents (id TEXT, content TEXT, source TEXT, created_at REAL)"
    )
    mem.execute(
        "INSERT INTO documents VALUES ('d1', 'Notas sobre Canberra', 'notas.md', 3.0)"
    )
    mem.commit()
    mem.close()

    (tmp_path / "memory_facts.jsonl").write_text(
        json.dumps({"text": "O usuário gosta de café", "created_at": 4.0}) + "\n",
        encoding="utf-8",
    )


def test_graph_links_knowledge(tmp_path):
    _make_stores(tmp_path)
    graph = build_knowledge_graph(config_dir=tmp_path, tools=["web_search"])
    ids = {n["id"] for n in graph["nodes"]}
    expected = {
        "core",
        "trace:t1",
        "trace:t2",
        "memory:d1",
        "fact:0",
        "tool:web_search",
    }
    assert expected <= ids
    assert {"hub:conversations", "hub:memory", "hub:facts", "hub:tools"} <= ids
    assert "hub:skills" not in ids  # empty categories get no hub

    pairs = {(e["source"], e["target"], e["kind"]) for e in graph["edges"]}
    assert ("core", "hub:tools", "structure") in pairs
    assert ("tool:web_search", "trace:t1", "used") in pairs
    # Both conversations mention Canberra -> lexical similarity edge
    assert any(
        {s, t} == {"trace:t1", "trace:t2"} and k == "similar" for s, t, k in pairs
    )
    for e in graph["edges"]:
        assert e["source"] in ids and e["target"] in ids


def test_graph_with_no_stores(tmp_path):
    graph = build_knowledge_graph(config_dir=tmp_path)
    assert [n["id"] for n in graph["nodes"]] == ["core"]
    assert graph["edges"] == []


def test_routes(tmp_path, monkeypatch):
    _make_stores(tmp_path)
    monkeypatch.setattr("openjarvis.server.galaxy._config_dir", lambda: tmp_path)
    app = FastAPI()
    app.include_router(galaxy_router)
    client = TestClient(app)

    page = client.get("/galaxy")
    assert page.status_code == 200
    assert "three" in page.text and "/v1/knowledge/graph" in page.text

    data = client.get("/v1/knowledge/graph").json()
    assert any(n["id"] == "trace:t1" for n in data["nodes"])


def test_vendored_assets_served_without_traversal():
    app = FastAPI()
    app.include_router(galaxy_router)
    client = TestClient(app)

    js = client.get("/galaxy/assets/three/three.module.min.js")
    assert js.status_code == 200
    addon = client.get("/galaxy/assets/three/addons/controls/OrbitControls.js")
    assert addon.status_code == 200
    assert client.get("/galaxy/assets/../galaxy.py").status_code == 404
    assert client.get("/galaxy/assets/%2e%2e/galaxy.py").status_code == 404


def test_classify_media_url():
    from openjarvis.server.galaxy import classify_media_url

    yt = classify_media_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3")
    assert yt["type"] == "youtube" and yt["id"] == "dQw4w9WgXcQ"
    assert classify_media_url("https://youtu.be/dQw4w9WgXcQ")["id"] == "dQw4w9WgXcQ"
    assert classify_media_url("https://vimeo.com/76979871")["type"] == "vimeo"
    img = "https://upload.wikimedia.org/a/Tour_Eiffel.JPG"
    assert classify_media_url(img)["type"] == "image"
    assert classify_media_url("https://cdn.example.com/clip.mp4?x=1")["type"] == "video"
    assert classify_media_url("https://example.com/artigo") is None


class _FakeResponse:
    def __init__(self, url, ctype, body=b""):
        self.url = url
        self.status_code = 200
        self.headers = {"content-type": ctype}
        self.encoding = "utf-8"
        self.extensions = {}
        self._body = body

    def iter_bytes(self):
        yield self._body

    def close(self):
        pass


def test_resolve_media_reads_open_graph(monkeypatch):
    from openjarvis.server import galaxy

    page = b"""<html><head>
      <meta property="og:title" content="Torre Eiffel &amp; Paris">
      <meta content="/img/torre.jpg" property="og:image">
    </head></html>"""
    monkeypatch.setattr(
        galaxy,
        "_fetch",
        lambda url: _FakeResponse(url, "text/html; charset=utf-8", page),
    )
    galaxy._media_cache.clear()
    res = galaxy.resolve_media("https://example.com/artigo")
    assert res["type"] == "image"
    assert res["src"] == "https://example.com/img/torre.jpg"
    assert res["title"] == "Torre Eiffel & Paris"


def test_resolve_media_prefers_video(monkeypatch):
    from openjarvis.server import galaxy

    page = b"""<meta property="og:image" content="https://x.com/thumb.jpg">
      <meta property="og:video:url" content="https://www.youtube.com/embed/dQw4w9WgXcQ">"""
    monkeypatch.setattr(
        galaxy, "_fetch", lambda url: _FakeResponse(url, "text/html", page)
    )
    galaxy._media_cache.clear()
    res = galaxy.resolve_media("https://example.com/video-page")
    assert res["type"] == "youtube" and res["id"] == "dQw4w9WgXcQ"


def test_resolve_media_rejects_non_http():
    from openjarvis.server.galaxy import resolve_media

    with pytest.raises(ValueError):
        resolve_media("file:///etc/passwd")


def test_media_resolve_route_blocks_internal_hosts():
    app = FastAPI()
    app.include_router(galaxy_router)
    client = TestClient(app)
    resp = client.get("/v1/media/resolve", params={"url": "http://127.0.0.1:8001/x"})
    assert resp.status_code == 400
    assert client.get("/v1/media/resolve", params={"url": "ftp://x"}).status_code == 400


def test_galaxy_csp_survives_security_middleware():
    from openjarvis.server.galaxy import GALAXY_CSP
    from openjarvis.server.middleware import (
        SECURITY_HEADERS,
        create_security_middleware,
    )

    app = FastAPI()
    app.add_middleware(create_security_middleware())
    app.include_router(galaxy_router)

    @app.get("/plain")
    def plain():
        return {}

    client = TestClient(app)
    assert client.get("/galaxy").headers["content-security-policy"] == GALAXY_CSP
    assert (
        client.get("/plain").headers["content-security-policy"]
        == SECURITY_HEADERS["Content-Security-Policy"]
    )


def test_resolve_media_fetches_wiki_file_pages(monkeypatch):
    from openjarvis.server import galaxy

    page = b'<meta property="og:image" content="https://upload.wikimedia.org/x.jpg">'
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return _FakeResponse(url, "text/html", page)

    monkeypatch.setattr(galaxy, "_fetch", fake_fetch)
    galaxy._media_cache.clear()
    url = "https://commons.wikimedia.org/wiki/File:Torre.jpg"
    res = galaxy.resolve_media(url)
    assert fetched == [url]
    assert res == {
        "type": "image",
        "src": "https://upload.wikimedia.org/x.jpg",
        "page": url,
    }


def test_classify_music_urls():
    from openjarvis.server.galaxy import GALAXY_CSP, classify_media_url

    sc = classify_media_url("https://soundcloud.com/artista/faixa-1")
    assert sc["type"] == "soundcloud"
    assert classify_media_url("https://cdn.example.com/som.mp3")["type"] == "audio"
    assert classify_media_url("https://soundcloud.com/artista") is None
    assert "https://w.soundcloud.com" in GALAXY_CSP
