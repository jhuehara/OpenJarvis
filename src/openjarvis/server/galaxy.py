"""Galaxy route — holographic knowledge-universe UI and its graph endpoint.

``GET /galaxy`` serves a self-contained three.js page that chats with the
agent and renders everything Jarvis knows as a holographic sphere.
``GET /v1/knowledge/graph`` feeds it: conversations (traces), stored memory
documents, extracted facts, tools, skills and knowledge-graph entities become
nodes; edges come from explicit relations (KG, tool usage, shared source)
plus lexical similarity between node texts.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import logging
import re
import sqlite3
import threading
import urllib.parse
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from openjarvis.security.ssrf import check_ssrf

logger = logging.getLogger(__name__)

galaxy_router = APIRouter()

_PAGE_PATH = Path(__file__).with_name("galaxy.html")
# Vendored three.js (r170) so the page works offline and under the server CSP
_ASSETS_DIR = Path(__file__).with_name("galaxy_assets")

CORE_ID = "core"

# Category hubs orbiting the core: id -> (label, kind)
_HUBS: Dict[str, str] = {
    "hub:conversations": "Conversas",
    "hub:memory": "Memória",
    "hub:facts": "Fatos",
    "hub:tools": "Ferramentas",
    "hub:skills": "Habilidades",
    "hub:entities": "Conhecimento",
}

_STOPWORDS = frozenset(
    """
    a o e é de da do das dos em no na nos nas um uma uns umas que se por para
    com como mais mas ou ao aos à às isso isto esse essa este esta ele ela eles
    elas eu você voce vocês nós meu minha seu sua são ser foi era tem ter há
    muito pouco sobre entre quando onde qual quais quem porque pois também só
    já não sim me te lhe nos vos the and for with that this from are was were
    have has had you your our their them they what which who whom how why can
    could would should will shall into about just only also than then there
    here some any all each other such very more most much many does did done
    """.split()
)

_WORD_RE = re.compile(r"[a-zà-ÿ0-9]{4,}")


def _keywords(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS}


def _short(text: str, n: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _config_dir() -> Path:
    from openjarvis.core.config import get_config_dir

    return Path(get_config_dir())


def _read_rows(db: Path, sql: str, params: Iterable[Any] = ()) -> List[tuple]:
    """Read-only query; a missing DB or table yields no rows."""
    if not db.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            return conn.execute(sql, tuple(params)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.debug("galaxy: cannot read %s: %s", db, exc)
        return []


def _enabled_tools(config: Any) -> List[str]:
    enabled = getattr(getattr(config, "tools", None), "enabled", "") or ""
    if isinstance(enabled, str):
        enabled = enabled.split(",")
    return [t.strip() for t in enabled if t and t.strip()]


def build_knowledge_graph(
    *,
    config_dir: Path,
    trace_db: Optional[Path] = None,
    tools: Iterable[str] = (),
    skills: Iterable[str] = (),
    limit: int = 400,
) -> Dict[str, Any]:
    """Assemble ``{nodes, edges}`` from the local knowledge stores."""
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: Dict[Tuple[str, str], Dict[str, Any]] = {}
    texts: Dict[str, str] = {}

    def add_node(nid: str, label: str, kind: str, group: str, **extra: Any) -> None:
        nodes[nid] = {
            "id": nid,
            "label": label,
            "kind": kind,
            "group": group,
            "weight": 1.0,
            **extra,
        }

    def add_edge(a: str, b: str, kind: str, weight: float = 1.0) -> None:
        if a == b or a not in nodes or b not in nodes:
            return
        key = (a, b) if a < b else (b, a)
        if key not in edges or edges[key]["weight"] < weight:
            edges[key] = {
                "source": key[0],
                "target": key[1],
                "kind": kind,
                "weight": round(weight, 3),
            }

    add_node(CORE_ID, "Jarvis", "core", CORE_ID)

    # ---- Conversations (traces) ----------------------------------------
    trace_db = trace_db or (config_dir / "traces.db")
    trace_rows = _read_rows(
        trace_db,
        "SELECT trace_id, query, result, outcome, started_at, total_tokens "
        "FROM traces ORDER BY started_at DESC LIMIT ?",
        (limit,),
    )
    tool_use: Dict[str, set[str]] = defaultdict(set)
    for trace_id, step_input in _read_rows(
        trace_db,
        "SELECT trace_id, input FROM trace_steps WHERE step_type = 'tool_call'",
    ):
        try:
            data = json.loads(step_input or "{}")
        except ValueError:
            continue
        name = data.get("tool") or data.get("name") or data.get("tool_name")
        if name:
            tool_use[trace_id].add(str(name))
    for trace_id, query, result, outcome, started, tokens in trace_rows:
        nid = f"trace:{trace_id}"
        add_node(
            nid,
            _short(query or "(sem pergunta)"),
            "conversation",
            "hub:conversations",
            detail=_short(result or "", 400),
            outcome=outcome,
            created_at=started,
            weight=1.0 + min((tokens or 0) / 2000.0, 2.0),
        )
        texts[nid] = f"{query} {result}"

    # ---- Memory documents ----------------------------------------------
    by_source: Dict[str, List[str]] = defaultdict(list)
    for doc_id, content, source, created in _read_rows(
        config_dir / "memory.db",
        "SELECT id, content, source, created_at FROM documents "
        "WHERE source IS NULL OR source != ? "
        "ORDER BY created_at DESC LIMIT ?",
        (ACTIVITY_SOURCE, limit),
    ):
        nid = f"memory:{doc_id}"
        label = Path(source).name if source else _short(content or "", 60)
        add_node(
            nid,
            label or "memória",
            "memory",
            "hub:memory",
            detail=_short(content or "", 400),
            source=source or "",
            created_at=created,
        )
        texts[nid] = f"{source} {content}"
        if source:
            by_source[source].append(nid)

    # ---- Extracted facts -----------------------------------------------
    facts_path = config_dir / "memory_facts.jsonl"
    if facts_path.is_file():
        try:
            lines = facts_path.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            lines = []
        for i, line in enumerate(lines):
            try:
                fact = json.loads(line)
            except ValueError:
                continue
            text = str(fact.get("text", "")).strip()
            if not text:
                continue
            nid = f"fact:{i}"
            add_node(
                nid,
                _short(text, 60),
                "fact",
                "hub:facts",
                detail=text,
                created_at=fact.get("created_at"),
            )
            texts[nid] = text

    # ---- Knowledge-graph entities & relations --------------------------
    kg_db = config_dir / "knowledge_graph.db"
    for eid, etype, name, props in _read_rows(
        kg_db,
        "SELECT entity_id, entity_type, name, properties FROM entities LIMIT ?",
        (limit,),
    ):
        nid = f"entity:{eid}"
        add_node(
            nid,
            name or eid,
            "entity",
            "hub:entities",
            detail=f"{etype}: {props}" if props and props != "{}" else etype,
        )
        texts[nid] = f"{name} {etype}"

    # ---- Tools & skills ------------------------------------------------
    for tool in tools:
        nid = f"tool:{tool}"
        add_node(nid, tool, "tool", "hub:tools")
        texts[nid] = tool.replace("_", " ")
    for skill in skills:
        nid = f"skill:{skill}"
        add_node(nid, skill, "skill", "hub:skills")
        texts[nid] = skill.replace("_", " ").replace("-", " ")

    # ---- Hubs (only those with members) and structural edges -----------
    members: Dict[str, List[str]] = defaultdict(list)
    for nid, node in list(nodes.items()):
        if node["kind"] != "core":
            members[node["group"]].append(nid)
    for hub, label in _HUBS.items():
        if members.get(hub):
            add_node(
                hub,
                label,
                "hub",
                hub,
                weight=2.0 + len(members[hub]) ** 0.5,
                count=len(members[hub]),
            )
            add_edge(CORE_ID, hub, "structure", 1.0)
            for nid in members[hub]:
                add_edge(hub, nid, "member", 0.6)

    for rel_src, rel_dst, rel_type, weight in _read_rows(
        kg_db, "SELECT source_id, target_id, relation_type, weight FROM relations"
    ):
        add_edge(
            f"entity:{rel_src}",
            f"entity:{rel_dst}",
            rel_type or "relation",
            float(weight or 1.0),
        )
    for trace_id, used in tool_use.items():
        for tool in used:
            add_edge(f"trace:{trace_id}", f"tool:{tool}", "used", 0.9)
    for group in by_source.values():
        for a, b in zip(group, group[1:]):
            add_edge(a, b, "same_source", 0.8)

    # ---- Lexical similarity: each node links to its closest neighbours --
    kw = {nid: _keywords(t) for nid, t in texts.items()}
    kw = {nid: k for nid, k in kw.items() if k}
    index: Dict[str, List[str]] = defaultdict(list)
    for nid, words in kw.items():
        for w in words:
            index[w].append(nid)
    for nid, words in kw.items():
        overlap: Dict[str, int] = defaultdict(int)
        for w in words:
            peers = index[w]
            if len(peers) > 40:  # too common to be meaningful
                continue
            for other in peers:
                if other != nid:
                    overlap[other] += 1
        scored = sorted(
            ((c / len(words | kw[o]), o) for o, c in overlap.items()), reverse=True
        )[:3]
        for score, other in scored:
            if score >= 0.12:
                add_edge(nid, other, "similar", score)

    # Degree feeds star brightness
    degree: Dict[str, int] = defaultdict(int)
    for e in edges.values():
        degree[e["source"]] += 1
        degree[e["target"]] += 1
    for nid, node in nodes.items():
        node["degree"] = degree[nid]
    nodes[CORE_ID]["weight"] = 6.0

    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


# The page shows images/videos Jarvis finds on the web, so it needs a wider
# CSP than the server default (the security middleware keeps a CSP that a
# route already set). Scripts stay same-origin; only media and a few video
# players are allowed from outside.
GALAXY_CSP = (
    "default-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "img-src 'self' data: blob: https: http:; "
    "media-src 'self' blob: https: http:; "
    "frame-src https://www.youtube-nocookie.com https://www.youtube.com "
    "https://player.vimeo.com https://w.soundcloud.com"
)


@galaxy_router.get("/galaxy", response_class=HTMLResponse)
async def galaxy_page():
    """Serve the holographic knowledge-universe UI."""
    return HTMLResponse(
        content=_PAGE_PATH.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache", "Content-Security-Policy": GALAXY_CSP},
    )


@galaxy_router.get("/galaxy/assets/{asset_path:path}")
async def galaxy_asset(asset_path: str):
    """Serve the page's vendored scripts."""
    root = _ASSETS_DIR.resolve()
    candidate = (root / asset_path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(candidate, headers={"Cache-Control": "max-age=86400"})


@galaxy_router.get("/v1/knowledge/graph")
def knowledge_graph(request: Request, limit: int = 400):
    """Nodes and edges of everything Jarvis knows, for the galaxy view."""
    config = getattr(request.app.state, "config", None)
    trace_store = getattr(request.app.state, "trace_store", None)
    trace_db = getattr(trace_store, "_db_path", None)
    try:
        from openjarvis.core.registry import SkillRegistry

        skills = sorted(SkillRegistry.keys())
    except Exception:
        skills = []
    return build_knowledge_graph(
        config_dir=_config_dir(),
        trace_db=Path(trace_db) if trace_db else None,
        tools=_enabled_tools(config),
        skills=skills,
        limit=max(1, min(limit, 2000)),
    )


# ---- Media resolver ---------------------------------------------------------

_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg", ".bmp")
_VIDEO_EXT = (".mp4", ".webm", ".mov", ".m4v", ".ogv")
_AUDIO_EXT = (".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac")
_YOUTUBE_RE = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/|v/)"
    r"|youtu\.be/)([A-Za-z0-9_-]{11})"
)
_VIMEO_RE = re.compile(r"vimeo\.com/(?:video/)?(\d+)")
_SOUNDCLOUD_RE = re.compile(r"^https?://(?:www\.|m\.)?soundcloud\.com/[^/?#]+/[^?#]+")
_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([a-zA-Z:_-]+)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""")
_MAX_PAGE_BYTES = 1_500_000
_MAX_REDIRECTS = 4
# Identified UA: Wikimedia and others throttle anonymous browser look-alikes
_UA = (
    "OpenJarvis/1.0 (+https://github.com/open-jarvis/OpenJarvis; media preview) "
    "Mozilla/5.0 (compatible)"
)
_media_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()


def classify_media_url(url: str) -> Optional[Dict[str, Any]]:
    """Recognise a media URL without touching the network."""
    m = _YOUTUBE_RE.search(url)
    if m:
        return {"type": "youtube", "id": m.group(1), "src": url}
    m = _VIMEO_RE.search(url)
    if m:
        return {"type": "vimeo", "id": m.group(1), "src": url}
    if _SOUNDCLOUD_RE.match(url):
        return {"type": "soundcloud", "src": url}
    path = urllib.parse.urlparse(url).path.lower()
    if path.endswith(_IMAGE_EXT):
        return {"type": "image", "src": url}
    if path.endswith(_VIDEO_EXT):
        return {"type": "video", "src": url}
    if path.endswith(_AUDIO_EXT):
        return {"type": "audio", "src": url}
    return None


def _meta_tags(page: str) -> Dict[str, str]:
    """``property``/``name`` -> ``content`` for the page's <meta> tags."""
    tags: Dict[str, str] = {}
    for tag in _META_RE.findall(page):
        attrs = {k.lower(): v.strip("'\"") for k, v in _ATTR_RE.findall(tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key and "content" in attrs and key not in tags:
            tags[key] = html.unescape(attrs["content"]).strip()
    return tags


def _fetch(url: str) -> "Any":
    """GET with an SSRF check on every hop; returns the streamed response."""
    import httpx

    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        error = check_ssrf(current)
        if error:
            raise ValueError(f"blocked: {error}")
        req = httpx.Request("GET", current, headers={"User-Agent": _UA})
        client = httpx.Client(timeout=8.0, follow_redirects=False)
        resp = client.send(req, stream=True)
        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get(
            "location"
        ):
            current = urllib.parse.urljoin(str(resp.url), resp.headers["location"])
            resp.close()
            client.close()
            continue
        resp.extensions["client"] = client
        return resp
    raise ValueError("too many redirects")


def resolve_media(url: str) -> Dict[str, Any]:
    """Find the image or video behind ``url`` (a direct file or a web page)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("only http(s) URLs are supported")
    known = classify_media_url(url)
    # Players are safe to trust by URL; a ".jpg" URL may still be an HTML page
    # (e.g. commons.wikimedia.org/wiki/File:X.jpg), so those get fetched.
    if known and known["type"] in ("youtube", "vimeo", "soundcloud"):
        return {**known, "page": url}
    if url in _media_cache:
        _media_cache.move_to_end(url)
        return _media_cache[url]

    resp = _fetch(url)
    try:
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")
        final = str(resp.url)
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype.startswith("image/"):
            result: Dict[str, Any] = {"type": "image", "src": final}
        elif ctype.startswith("video/"):
            result = {"type": "video", "src": final}
        elif ctype.startswith("audio/"):
            result = {"type": "audio", "src": final}
        elif "html" in ctype or not ctype:
            body = b""
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) >= _MAX_PAGE_BYTES:
                    break
            meta = _meta_tags(body.decode(resp.encoding or "utf-8", "replace"))
            title = meta.get("og:title") or meta.get("twitter:title") or ""
            result = {"type": "none"}
            for key in (
                "og:video:secure_url",
                "og:video:url",
                "og:video",
                "twitter:player",
            ):
                if meta.get(key):
                    src = urllib.parse.urljoin(final, meta[key])
                    result = classify_media_url(src) or {"type": "video", "src": src}
                    if result["type"] != "image":
                        break
            if result["type"] == "none":
                for key in (
                    "og:image:secure_url",
                    "og:image:url",
                    "og:image",
                    "twitter:image",
                    "twitter:image:src",
                ):
                    if meta.get(key):
                        src = urllib.parse.urljoin(final, meta[key])
                        result = {"type": "image", "src": src}
                        break
            if title:
                result["title"] = title
        else:
            result = {"type": "none"}
    finally:
        resp.close()
        client = resp.extensions.get("client")
        if client is not None:
            client.close()

    result["page"] = url
    if result["type"] == "none":
        return result  # not cached: the page may just have been rate-limited
    _media_cache[url] = result
    if len(_media_cache) > 256:
        _media_cache.popitem(last=False)
    return result


@galaxy_router.get("/v1/media/resolve")
def media_resolve(url: str):
    """Resolve a link from Jarvis into something the galaxy media box can show."""
    try:
        return resolve_media(url.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # network errors, timeouts, bad pages
        logger.debug("media resolve failed for %s: %s", url, exc)
        raise HTTPException(status_code=502, detail="Could not load that link") from exc


# ---- Persona and activity memory ---------------------------------------------

# Memory-store source tag for the "everything Jarvis did" log. These entries
# are retrieved into later prompts by the server's context injection; the
# graph skips them because each exchange already is a conversation star.
ACTIVITY_SOURCE = "jarvis:atividade"
_LOCAL_TZ = _dt.timezone(_dt.timedelta(hours=-3), "BRT")
_TAG_RE = re.compile(r"\[\[\s*([a-zà-ÿ]+)\s*:\s*([^\]]*?)\s*\]\]", re.IGNORECASE)


def _local_now() -> _dt.datetime:
    try:
        from zoneinfo import ZoneInfo

        return _dt.datetime.now(ZoneInfo("America/Sao_Paulo"))
    except Exception:
        return _dt.datetime.now(_LOCAL_TZ)


def summarize_reply(text: str) -> str:
    """Reply text without the page's [[...]] tags, noting any media shown."""
    shown = []

    def _tag(m: "re.Match[str]") -> str:
        key = m.group(1).lower()
        if key.startswith(("imag", "foto", "vid", "víd", "mus", "mús", "aud", "áud")):
            kind = (
                "imagem"
                if key.startswith(("imag", "foto"))
                else "música"
                if key.startswith(("mus", "mús", "aud", "áud"))
                else "vídeo"
            )
            caption = m.group(2).split("|", 1)[1].strip() if "|" in m.group(2) else ""
            shown.append(f"{kind} “{caption}”" if caption else kind)
        return ""

    clean = " ".join(_TAG_RE.sub(_tag, text or "").split())
    if shown:
        clean += f" [mostrou: {', '.join(shown)}]"
    return clean.strip()


def _recent_activity(trace_db: Path, limit: int = 12) -> str:
    rows = _read_rows(
        trace_db,
        "SELECT query, result, started_at FROM traces "
        "WHERE query != '' ORDER BY started_at DESC LIMIT ?",
        (limit,),
    )
    lines = []
    for query, result, started in reversed(rows):
        when = (
            _dt.datetime.fromtimestamp(started, _local_now().tzinfo).strftime(
                "%d/%m %H:%M"
            )
            if started
            else "?"
        )
        lines.append(
            f"- {when} — Usuário: {_short(query or '', 140)} → "
            f"Jarvis: {_short(summarize_reply(result or ''), 180)}"
        )
    return "\n".join(lines)


@galaxy_router.get("/v1/galaxy/persona")
def galaxy_persona(request: Request):
    """The identity prompt (SOUL.md + MEMORY.md + USER.md) for the galaxy chat.

    The server only adds this prompt when a request has no system message of
    its own; the galaxy page always sends one (emotion/media/voice rules), so
    it fetches the persona here and puts both in a single system message.
    """
    from openjarvis.prompt.builder import SystemPromptBuilder

    cfg = getattr(request.app.state, "config", None)
    if cfg is None:
        from openjarvis.core.config import load_config

        cfg = load_config()
    try:
        prompt = SystemPromptBuilder(
            agent_template=cfg.agent.default_system_prompt or "",
            memory_files_config=getattr(cfg, "memory_files", None),
            system_prompt_config=getattr(cfg, "system_prompt", None),
        ).build()
    except Exception as exc:
        logger.warning("galaxy persona prompt failed: %s", exc)
        prompt = ""
    trace_store = getattr(request.app.state, "trace_store", None)
    trace_db = getattr(trace_store, "_db_path", None)
    recent = _recent_activity(
        Path(trace_db) if trace_db else _config_dir() / "traces.db"
    )
    if recent:
        prompt += (
            "\n\n## Suas atividades recentes (horário de Brasília)\n"
            "Use para lembrar do que já conversaram e fizeram juntos.\n" + recent
        )
    runner = getattr(request.app.state, "devtasks", None)
    if runner is not None:
        from openjarvis.devtasks.context import projects_context

        projects = runner.registry.list()
        prompt += "\n\n" + delegation_rules(projects)
        status = projects_context(projects)
        if status:
            prompt += (
                "\n\n" + status + "\nQuando uma pergunta exigir ler o código de um "
                "projeto, a interface pode te dar acesso de leitura a ele (ferramentas "
                "Read, Grep e Glob); use-as antes de responder sobre o código."
            )
    return {"prompt": prompt}


def delegation_rules(projects: Iterable[Any]) -> str:
    """How Jarvis delegates work to coding agents (the page parses the tags)."""
    listed = (
        "\n".join(
            f"- {p.name}" + (f": {p.description}" if p.description else "")
            for p in projects
        )
        or "- (nenhum projeto cadastrado ainda)"
    )
    rules = (
        "Quando o usuário pedir algo que exige mexer em código, revisar código ou uma "
        "pesquisa aprofundada, não faça você mesmo: diga em uma frase o que o agente "
        "vai fazer e coloque no fim da resposta exatamente uma linha "
        "[[tarefa: tipo | projeto | título curto | pedido]] — tipo é codigo, revisao "
        "ou pesquisa (pesquisa pode ficar sem projeto); o título tem até 6 palavras. "
        "Escreva o pedido completo e "
        "autossuficiente para o agente, com o contexto da conversa. A interface pede "
        "a confirmação; não pergunte de novo. Agentes de código trabalham numa branch "
        "nova e o push/PR só acontece depois da aprovação do usuário. Se o projeto não "
        "estiver cadastrado, diga que ele precisa ser cadastrado primeiro.\n"
        "Quando ele pedir para ver o que está sendo executado ou o que os agentes "
        "estão fazendo, responda com um resumo de uma frase e inclua "
        "[[abrir: atividades]] (ou [[abrir: atividades | projeto]] para focar num "
        "agente)."
    )
    return (
        "## Agentes de IA que trabalham para você\n"
        "Você pode delegar trabalho a agentes de IA (Claude Code) que rodam em "
        f"segundo plano. Projetos cadastrados:\n{listed}\n{rules}"
    )


class RememberRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    answer: str = Field(default="", max_length=20000)


@galaxy_router.post("/v1/galaxy/remember")
def galaxy_remember(req: RememberRequest, request: Request):
    """Log one exchange in the searchable memory so Jarvis can recall it later."""
    backend = getattr(request.app.state, "memory_backend", None)
    if backend is None:
        try:
            from openjarvis.tools.storage.sqlite import SQLiteMemory

            backend = SQLiteMemory()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Memory unavailable") from exc
    now = _local_now()
    content = (
        f"Em {now.strftime('%d/%m/%Y %H:%M')}, o usuário pediu: "
        f"{' '.join(req.question.split())} — Jarvis respondeu: "
        f"{summarize_reply(req.answer)}"
    )
    doc_id = backend.store(
        content,
        source=ACTIVITY_SOURCE,
        metadata={"kind": "activity", "at": now.isoformat()},
    )
    return {"id": doc_id}


# ---- Neural voices (Kokoro) --------------------------------------------------

# Brazilian Portuguese Kokoro voices; much more natural than the browser's
# Eloquence voices. Ordered as shown in the voice picker.
NEURAL_VOICES = [
    {"id": "pm_alex", "name": "Alex", "gender": "male", "lang": "pt-BR"},
    {"id": "pm_santa", "name": "Santa", "gender": "male", "lang": "pt-BR"},
    {"id": "pf_dora", "name": "Dora", "gender": "female", "lang": "pt-BR"},
]
_NEURAL_IDS = {v["id"] for v in NEURAL_VOICES}
_tts_backend: Any = None
_tts_lock = threading.Lock()


def _kokoro() -> Any:
    """Lazily build (and cache) the Kokoro backend; the model loads on first use."""
    global _tts_backend
    with _tts_lock:
        if _tts_backend is None:
            from openjarvis.speech.kokoro_tts import KokoroTTSBackend

            _tts_backend = KokoroTTSBackend()
        return _tts_backend


def _neural_available() -> bool:
    try:
        import kokoro  # noqa: F401
        import soundfile  # noqa: F401
    except Exception:
        return False
    return True


def _warm_up() -> None:
    try:
        _kokoro().synthesize("Olá.", voice_id=NEURAL_VOICES[0]["id"])
    except Exception as exc:  # warm-up is best effort
        logger.debug("kokoro warm-up failed: %s", exc)


class SynthesizeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=800)
    voice_id: str = NEURAL_VOICES[0]["id"]
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


@galaxy_router.get("/v1/speech/voices")
def speech_voices():
    """Neural voices the galaxy page can use; also warms the model up."""
    available = _neural_available()
    if available and _tts_backend is None:
        threading.Thread(target=_warm_up, daemon=True).start()
    return {"available": available, "voices": NEURAL_VOICES if available else []}


@galaxy_router.post("/v1/speech/synthesize")
def speech_synthesize(req: SynthesizeRequest):
    """Text -> WAV with a local Kokoro voice (one sentence at a time)."""
    if req.voice_id not in _NEURAL_IDS:
        raise HTTPException(status_code=400, detail="Unknown voice")
    if not _neural_available():
        raise HTTPException(status_code=501, detail="Kokoro is not installed")
    try:
        result = _kokoro().synthesize(
            req.text, voice_id=req.voice_id, speed=req.speed, output_format="wav"
        )
    except Exception as exc:
        logger.warning("kokoro synthesis failed: %s", exc)
        raise HTTPException(status_code=500, detail="Speech synthesis failed") from exc
    return Response(
        content=result.audio,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store"},
    )


__all__ = [
    "ACTIVITY_SOURCE",
    "NEURAL_VOICES",
    "build_knowledge_graph",
    "classify_media_url",
    "galaxy_router",
    "resolve_media",
    "summarize_reply",
]
