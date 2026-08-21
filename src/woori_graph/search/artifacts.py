"""Versioned search outputs, grounded answer Markdown, and portable graph HTML."""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import SearchPipelineConfig
from .models import SearchResult


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def default_search_run_id(query: str) -> str:
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:10]
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"query-{timestamp}-{digest}"


def write_search_artifacts(
    result: SearchResult,
    *,
    config: SearchPipelineConfig,
    config_path: Path,
    embedding_model: str,
    embedding_dimension: int,
    run_id: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    run_id = run_id or default_search_run_id(result.query)
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError(f"invalid search run ID: {run_id!r}")
    run_dir = config.artifact_root / run_id
    work_dir = run_dir / "work"
    final_dir = run_dir / "final"
    work_dir.mkdir(parents=True, exist_ok=True)
    final_dir.mkdir(parents=True, exist_ok=True)

    candidates = {
        "query": result.query,
        "entities": [item.to_dict() for item in result.entity_candidates],
        "relations": [item.to_dict() for item in result.relation_candidates],
    }
    paths = {
        "query": result.query,
        "stats": result.stats.to_dict(),
        "paths": [item.to_dict() for item in result.paths],
    }
    graph = build_graph_payload(result)
    _write_json(work_dir / "01_candidates.json", candidates, overwrite=overwrite)
    _write_json(work_dir / "02_ranked_paths.json", paths, overwrite=overwrite)
    _write_json(final_dir / "query_result.json", result.to_dict(), overwrite=overwrite)
    _write_text(final_dir / "answer.md", result.answer + "\n", overwrite=overwrite)
    _write_json(final_dir / "graph.json", graph, overwrite=overwrite)
    _write_text(
        final_dir / "graph.html",
        render_graph_html(result, graph),
        overwrite=overwrite,
    )

    manifest_path = final_dir / "search_manifest.json"
    generated_files = sorted(
        path
        for path in run_dir.rglob("*")
        if path.is_file() and path != manifest_path
    )
    resolved_config = config_path.resolve()
    manifest = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "query": result.query,
        "embedding": {
            "model": embedding_model,
            "dimension": embedding_dimension,
        },
        "answer": result.answer_metadata.to_dict(),
        "search": {
            "max_hops": config.max_hops,
            "entity_top_k": config.entity_top_k,
            "relation_top_k": config.relation_top_k,
            "max_neighbors_per_entity": config.max_neighbors_per_entity,
            "path_beam_width": config.path_beam_width,
            "max_paths": config.max_paths,
            "timeout_seconds": config.timeout_seconds,
        },
        "stats": result.stats.to_dict(),
        "config": {
            "path": portable_manifest_path(config_path),
            "sha256": _sha256(resolved_config),
        },
        "counts": {
            "entity_candidates": len(result.entity_candidates),
            "relation_candidates": len(result.relation_candidates),
            "paths": len(result.paths),
            "nodes": len(graph["nodes"]),
            "edges": len(graph["edges"]),
        },
        "files": [
            {
                "path": path.relative_to(run_dir).as_posix(),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in generated_files
        ],
        "passed": bool(result.paths) and not result.stats.timed_out,
    }
    _write_json(manifest_path, manifest, overwrite=overwrite)
    return manifest


def build_graph_payload(result: SearchResult, *, path_limit: int = 20) -> dict[str, Any]:
    candidate_by_id = {item.entity_id: item for item in result.entity_candidates}
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}

    def ensure_node(
        identifier: str,
        name: str,
        *,
        entity_type: str | None = None,
        candidate: bool = False,
        candidate_rank: int | None = None,
        selected: bool = False,
        level: int = 0,
    ) -> None:
        entity_candidate = candidate_by_id.get(identifier)
        current = nodes.get(identifier)
        if current is None:
            nodes[identifier] = {
                "id": identifier,
                "name": name,
                "type": entity_type or (
                    entity_candidate.entity_type if entity_candidate else "OTHER"
                ),
                "seed": entity_candidate is not None,
                "candidate": candidate,
                "candidate_rank": candidate_rank,
                "candidate_score": (
                    round(entity_candidate.score, 8) if entity_candidate else 0.0
                ),
                "selected": selected,
                "level": level,
            }
            return
        current["candidate"] = current["candidate"] or candidate
        current["selected"] = current["selected"] or selected
        if candidate_rank is not None and (
            current["candidate_rank"] is None
            or candidate_rank < current["candidate_rank"]
        ):
            current["candidate_rank"] = candidate_rank
        if selected:
            current["level"] = min(current["level"], level)

    for rank, candidate in enumerate(result.entity_candidates, start=1):
        ensure_node(
            candidate.entity_id,
            candidate.canonical_name,
            entity_type=candidate.entity_type,
            candidate=True,
            candidate_rank=rank,
        )

    for rank, candidate in enumerate(result.relation_candidates, start=1):
        ensure_node(
            candidate.source_entity_id,
            candidate.source_name,
            candidate=True,
        )
        ensure_node(
            candidate.target_entity_id,
            candidate.target_name,
            candidate=True,
        )
        edges.setdefault(
            candidate.relation_id,
            {
                "id": candidate.relation_id,
                "source": candidate.source_entity_id,
                "source_name": candidate.source_name,
                "target": candidate.target_entity_id,
                "target_name": candidate.target_name,
                "relation": candidate.relation_type_name,
                "polarity": None,
                "evidence_count": 0,
                "documents": [],
                "evidence": [],
                "candidate": True,
                "candidate_rank": rank,
                "candidate_score": round(candidate.score, 8),
                "selected": False,
            },
        )

    for path in result.paths[:path_limit]:
        for level, (identifier, name) in enumerate(
            zip(path.traversed_entity_ids, path.traversed_entity_names, strict=True)
        ):
            ensure_node(identifier, name, selected=True, level=level)
        for edge in path.edges:
            current = edges.get(edge.relation_id)
            candidate_metadata = current or {}
            edges[edge.relation_id] = {
                "id": edge.relation_id,
                "source": edge.source_entity_id,
                "source_name": edge.source_name,
                "target": edge.target_entity_id,
                "target_name": edge.target_name,
                "relation": edge.relation_type_name,
                "polarity": edge.polarity,
                "evidence_count": edge.evidence_count,
                "documents": sorted({item.document_title for item in edge.evidences}),
                "evidence": [item.to_dict() for item in edge.evidences],
                "candidate": bool(candidate_metadata.get("candidate", False)),
                "candidate_rank": candidate_metadata.get("candidate_rank"),
                "candidate_score": candidate_metadata.get("candidate_score", 0.0),
                "selected": True,
            }
    selected_node_count = sum(item["selected"] for item in nodes.values())
    selected_edge_count = sum(item["selected"] for item in edges.values())
    return {
        "query": result.query,
        "path_limit": path_limit,
        "nodes": sorted(
            nodes.values(),
            key=lambda item: (
                not item["selected"],
                item["candidate_rank"] is None,
                item["candidate_rank"] or 0,
                item["name"],
                item["id"],
            ),
        ),
        "edges": sorted(
            edges.values(),
            key=lambda item: (
                not item["selected"],
                item["candidate_rank"] is None,
                item["candidate_rank"] or 0,
                item["id"],
            ),
        ),
        "selected_counts": {"nodes": selected_node_count, "edges": selected_edge_count},
        "context_counts": {
            "nodes": len(nodes) - selected_node_count,
            "edges": len(edges) - selected_edge_count,
        },
    }


def render_graph_html(result: SearchResult, graph: dict[str, Any]) -> str:
    payload = json.dumps(graph, ensure_ascii=False, separators=(",", ":")).replace(
        "</", "<\\/"
    )
    title = html.escape(result.query)
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,">
<title>{title} - graph search</title>
<style>
:root {{ color-scheme: light dark; --bg:#f7f8fa; --surface:#fff; --text:#18202b; --muted:#667085; --line:#98a2b3; --accent:#175cd3; --seed:#d1e9ff; --node:#eef2f6; --border:#d0d5dd; }}
@media (prefers-color-scheme:dark) {{ :root {{ --bg:#101828; --surface:#1d2939; --text:#f2f4f7; --muted:#98a2b3; --line:#667085; --accent:#84adff; --seed:#194185; --node:#344054; --border:#475467; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:20px; background:var(--bg); color:var(--text); font-family:system-ui,-apple-system,"Noto Sans KR",sans-serif; }}
main {{ max-width:1400px; margin:auto; }}
h1 {{ margin:0 0 8px; font-size:22px; font-weight:600; }}
.summary {{ margin:0 0 16px; color:var(--muted); }}
.layout {{ display:grid; grid-template-columns:minmax(0,1fr) 320px; gap:16px; align-items:start; }}
.graph {{ background:var(--surface); border:1px solid var(--border); border-radius:10px; overflow:hidden; }}
svg {{ display:block; width:100%; min-height:680px; }}
.side {{ background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:16px; min-height:180px; }}
.side h2 {{ margin:0 0 10px; font-size:16px; }}
.side p,.side li {{ font-size:14px; line-height:1.5; overflow-wrap:anywhere; }}
.side ul {{ padding-left:18px; }}
.node rect {{ fill:#b6c0cf; stroke:#94a3b8; stroke-width:1.2; }}
.node.selected rect {{ fill:#e7f8f6; stroke:#0f9fa7; stroke-width:2.4; }}
.node.seed rect {{ stroke:#2563eb; }}
.node text {{ fill:var(--text); font-size:12px; pointer-events:none; }}
.node:not(.selected) text {{ display:none; }}
.node {{ cursor:pointer; }}
.edge {{ fill:none; marker-end:url(#arrow); }}
.edge.candidate {{ stroke:#c5ceda; stroke-width:1.1; opacity:.82; }}
.edge.selected {{ stroke:#2563eb; stroke-width:2.6; }}
.edge-hit {{ stroke:transparent; stroke-width:14; fill:none; cursor:pointer; pointer-events:stroke; }}
.edge-label {{ fill:var(--muted); font-size:11px; text-anchor:middle; paint-order:stroke; stroke:var(--surface); stroke-width:4px; stroke-linejoin:round; pointer-events:none; }}
.edge-label.candidate {{ display:none; }}
.dim {{ opacity:.14; }}
.active rect {{ stroke:var(--accent); stroke-width:3; }}
@media (max-width:850px) {{ body {{ padding:10px; }} .layout {{ grid-template-columns:1fr; }} svg {{ min-height:560px; }} }}
</style>
</head>
<body>
<main>
  <h1>{title}</h1>
  <p class="summary">최대 {result.stats.max_hops_reached}-hop · 노드 {len(graph['nodes'])}개 · 관계 {len(graph['edges'])}개</p>
  <div class="layout">
    <div class="graph"><svg id="network" role="img" aria-label="질문 관련 법령 관계 그래프"></svg></div>
    <aside class="side" id="detail"><h2>그래프 탐색</h2><p>노드 또는 관계를 선택하면 연결과 근거 문서를 확인할 수 있습니다.</p></aside>
  </div>
</main>
<script id="graph-data" type="application/json">{payload}</script>
<script>
(() => {{
  const data=JSON.parse(document.getElementById('graph-data').textContent);
  const svg=document.getElementById('network');
  const detail=document.getElementById('detail');
  const ns='http://www.w3.org/2000/svg';
  const compact=window.innerWidth<700, width=compact?360:1100, height=compact?620:720, margin=36;
  const positions=new Map();
  const seededUnit=(value,offset=0)=>{{ let hash=2166136261+offset; for(let i=0;i<value.length;i+=1){{ hash^=value.charCodeAt(i); hash=Math.imul(hash,16777619); }} return ((hash>>>0)%10000)/10000; }};
  for(const node of data.nodes){{ positions.set(node.id,{{ x:margin+seededUnit(node.id,11)*(width-margin*2), y:margin+seededUnit(node.id,97)*(height-margin*2), vx:0, vy:0 }}); }}
  for(let iteration=0;iteration<180;iteration+=1){{
    const force=new Map(data.nodes.map(node=>[node.id,{{x:0,y:0}}]));
    for(let left=0;left<data.nodes.length;left+=1){{ for(let right=left+1;right<data.nodes.length;right+=1){{ const a=positions.get(data.nodes[left].id), b=positions.get(data.nodes[right].id), dx=b.x-a.x, dy=b.y-a.y, distance=Math.max(1,Math.hypot(dx,dy)), magnitude=5200/(distance*distance), x=dx/distance*magnitude, y=dy/distance*magnitude; force.get(data.nodes[left].id).x-=x; force.get(data.nodes[left].id).y-=y; force.get(data.nodes[right].id).x+=x; force.get(data.nodes[right].id).y+=y; }} }}
    for(const edge of data.edges){{ const a=positions.get(edge.source), b=positions.get(edge.target); if(!a||!b)continue; const dx=b.x-a.x, dy=b.y-a.y, distance=Math.max(1,Math.hypot(dx,dy)), magnitude=(distance-(edge.selected?128:96))*.032, x=dx/distance*magnitude, y=dy/distance*magnitude; force.get(edge.source).x+=x; force.get(edge.source).y+=y; force.get(edge.target).x-=x; force.get(edge.target).y-=y; }}
    for(const node of data.nodes){{ const point=positions.get(node.id), applied=force.get(node.id); point.vx=(point.vx+applied.x)*.76; point.vy=(point.vy+applied.y)*.76; point.x=Math.max(margin,Math.min(width-margin,point.x+point.vx)); point.y=Math.max(margin,Math.min(height-margin,point.y+point.vy)); }}
  }}
  svg.setAttribute('viewBox',`0 0 ${{width}} ${{height}}`);
  svg.style.height=compact?`${{height}}px`:`${{Math.max(680,Math.round(svg.parentElement.clientWidth*height/width))}}px`;
  const defs=document.createElementNS(ns,'defs');
  defs.innerHTML='<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke"/></marker>';
  svg.appendChild(defs);
  const edgeLayer=document.createElementNS(ns,'g'), labelLayer=document.createElementNS(ns,'g'), nodeLayer=document.createElementNS(ns,'g');
  svg.append(edgeLayer,labelLayer,nodeLayer);
  const edgeElements=new Map(), nodeElements=new Map();
  const parallelGroups=new Map();
  for(const edge of data.edges){{ const key=[edge.source,edge.target].sort().join('|'); if(!parallelGroups.has(key))parallelGroups.set(key,[]); parallelGroups.get(key).push(edge.id); }}
  for(const edge of data.edges){{
    const a=positions.get(edge.source), b=positions.get(edge.target); if(!a||!b)continue;
    const key=[edge.source,edge.target].sort().join('|'), group=parallelGroups.get(key), parallelIndex=group.indexOf(edge.id), offset=(parallelIndex-(group.length-1)/2)*18;
    const line=document.createElementNS(ns,'path');
    const dx=b.x-a.x, dy=b.y-a.y, distance=Math.max(1,Math.hypot(dx,dy)), normalX=-dy/distance, normalY=dx/distance, startX=a.x+dx/distance*12, startY=a.y+dy/distance*12, endX=b.x-dx/distance*12, endY=b.y-dy/distance*12, controlX=(a.x+b.x)/2+normalX*offset, controlY=(a.y+b.y)/2+normalY*offset;
    const pathData=`M ${{startX}} ${{startY}} Q ${{controlX}} ${{controlY}} ${{endX}} ${{endY}}`;
    const hit=document.createElementNS(ns,'path'); hit.setAttribute('d',pathData); hit.setAttribute('class','edge-hit'); hit.addEventListener('click',()=>showEdge(edge)); edgeLayer.appendChild(hit);
    line.setAttribute('d',pathData);
    line.setAttribute('class',`edge${{edge.selected?' selected':' candidate'}}`); line.dataset.id=edge.id; edgeLayer.appendChild(line); edgeElements.set(edge.id,line);
    const label=document.createElementNS(ns,'text'); label.setAttribute('class',`edge-label${{edge.selected?' selected':' candidate'}}`); label.setAttribute('x',controlX); label.setAttribute('y',controlY-8); label.textContent=edge.relation; labelLayer.appendChild(label);
  }}
  for(const node of data.nodes){{
    const p=positions.get(node.id); const group=document.createElementNS(ns,'g'); group.setAttribute('class',`node${{node.seed?' seed':''}}${{node.candidate?' candidate':''}}${{node.selected?' selected':''}}`); group.setAttribute('transform',`translate(${{p.x}},${{p.y}})`); group.dataset.id=node.id;
    const rect=document.createElementNS(ns,'rect'); rect.setAttribute('x','-7'); rect.setAttribute('y','-7'); rect.setAttribute('width','14'); rect.setAttribute('height','14'); rect.setAttribute('rx','7');
    const text=document.createElementNS(ns,'text'); text.setAttribute('text-anchor','middle'); text.setAttribute('y','4'); text.textContent=node.name.length>22?node.name.slice(0,21)+'…':node.name;
    group.append(rect,text); group.addEventListener('click',()=>showNode(node)); nodeLayer.appendChild(group); nodeElements.set(node.id,group);
  }}
  function reset(){{ for(const el of nodeElements.values())el.classList.remove('dim','active'); for(const el of edgeElements.values())el.classList.remove('dim'); }}
  function showNode(node){{ reset(); const incident=data.edges.filter(e=>e.source===node.id||e.target===node.id); const related=new Set([node.id]); for(const e of incident){{related.add(e.source);related.add(e.target);}} for(const [id,el] of nodeElements){{if(!related.has(id))el.classList.add('dim');}} for(const [id,el] of edgeElements){{if(!incident.some(e=>e.id===id))el.classList.add('dim');}} nodeElements.get(node.id).classList.add('active'); detail.innerHTML=`<h2>${{escapeHtml(node.name)}}</h2><p>타입: ${{escapeHtml(node.type)}} · 직접 연결 ${{incident.length}}개</p><ul>${{incident.slice(0,12).map(e=>`<li>${{escapeHtml(e.source_name)}} — ${{escapeHtml(e.relation)}} → ${{escapeHtml(e.target_name)}}</li>`).join('')}}</ul>`; }}
  function showEdge(edge){{ reset(); nodeElements.get(edge.source)?.classList.add('active'); nodeElements.get(edge.target)?.classList.add('active'); for(const [id,el] of edgeElements){{if(id!==edge.id)el.classList.add('dim');}} const docs=edge.documents.length?edge.documents.map(d=>`<li>${{escapeHtml(d)}}</li>`).join(''):'<li>표시할 근거 없음</li>'; detail.innerHTML=`<h2>${{escapeHtml(edge.relation)}}</h2><p>${{escapeHtml(edge.source_name)}} → ${{escapeHtml(edge.target_name)}}</p><p>근거 ${{edge.evidence_count}}건</p><ul>${{docs}}</ul>`; }}
  function escapeHtml(value){{ return String(value).replace(/[&<>"']/g,ch=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch])); }}
}})();
</script>
</body>
</html>
"""


def _write_json(path: Path, payload: Any, *, overwrite: bool) -> None:
    _write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        overwrite=overwrite,
    )


def _write_text(path: Path, value: str, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing search artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def portable_manifest_path(path: Path) -> str:
    """Return a non-secret, machine-independent config reference for manifests."""

    candidate = Path(path)
    if not candidate.is_absolute() and ".." not in candidate.parts:
        return candidate.as_posix()
    return candidate.name
