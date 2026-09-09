# ruff: noqa: E501
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Assuming networkx is used, but graphify isn't needed here.
# Note: the original functions expect `graph`, which is a networkx graph.
from .config import _VIZ_DRILLDOWN_CAP, _VIZ_MARKER, _VIZ_PHYSICS_LIMIT


def _graphify_imports() -> dict[str, Any] | None:
    """Import graphify modules lazily. Returns None with a clear error if unavailable.

    ``GRAPHIFY_OUT`` (read once at graphify import time) is pinned to a RELATIVE
    ``.ai/codegraph`` so that, combined with the ``cache_root=<root>`` passed to
    ``detect``/``extract`` in :func:`build_graph`, graphify's own cache lands in
    ``<root>/.ai/codegraph/cache/`` instead of a stray ``graphify-out/`` at the
    project root. An explicitly-set env override is respected (setdefault).
    ``GRAPHIFY_NO_BACKUP=1`` disables graphify's dated backup snapshot dirs (a
    semantic/curated-only side-effect — harmless belt-and-suspenders for the
    AST-only path, which never writes a semantic marker).
    """
    # Must be set BEFORE any ``from graphify...`` import below (read-once).
    os.environ.setdefault("GRAPHIFY_OUT", ".ai/codegraph")
    os.environ.setdefault("GRAPHIFY_NO_BACKUP", "1")
    try:
        from graphify.build import build_from_json
        from graphify.cluster import cluster
        from graphify.detect import detect
        from graphify.export import to_html, to_json
        from graphify.extract import extract
    except ImportError:  # pragma: no cover - defensive (graphifyy is a core dep)
        return None
    return {
        "detect": detect,
        "extract": extract,
        "build_from_json": build_from_json,
        "cluster": cluster,
        "to_json": to_json,
        "to_html": to_html,
    }


# Colour palette for the injected member-level views (mirrors graphify's
# community colours closely enough for visual distinction).
_VIZ_PALETTE = [
    "#4E79A7",
    "#F28E2B",
    "#E15759",
    "#76B7B2",
    "#59A14F",
    "#EDC948",
    "#B07AA1",
    "#FF9DA7",
    "#9C755F",
    "#BAB0AC",
    "#86BCB6",
    "#D37295",
]


def _viz_js_safe(obj: Any) -> str:
    """JSON-encode for embedding inside a <script> tag (no </script> breakout)."""
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def _community_labels(graph, communities: dict[int, list[str]], top_k: int = 3, max_len: int = 48) -> dict[int, str]:
    """Non-LLM heuristic community labels (for the HTML legend + drill-down).

    Ranks each community's members by degree and joins the top labels (e.g.
    ``"apiHandler, helper"``), falling back to ``"Community N"``. Mirrors
    graphify's CLI label generation but stays AST-only (no LLM pass). Without
    these, graphify's HTML legend renders empty (just "Select All").
    """
    degree = dict(graph.degree())
    labels: dict[int, str] = {}
    for cid, members in communities.items():
        ranked = sorted(members, key=lambda m: degree.get(m, 0), reverse=True)
        names: list[str] = []
        seen: set[str] = set()
        for mid in ranked:
            data = graph.nodes.get(mid, {}) or {}
            label = str(data.get("label") or mid).strip().strip("()")
            if not label or label.lower() in seen:
                continue
            seen.add(label.lower())
            names.append(label)
            if len(names) >= top_k:
                break
        label = ", ".join(names)
        if len(label) > max_len:
            label = label[: max_len - 1] + "…"
        labels[int(cid)] = label or f"Community {cid}"
    return labels


def _drilldown_data(
    graph,
    communities: dict[int, list[str]],
    cap: int | None = None,
    community_labels: dict[int, str] | None = None,
) -> dict:
    """Build community → member rows for the aggregated view's drill-down.

    Returns ``{str(cid): {"label", "count", "members": [[id, label, source_file], ...]}}``
    — the exact data the injected layer needs to expand a community node into
    its member nodes. Bounded by ``cap`` per community to keep the embedded
    payload sane on huge graphs.
    """
    cap = _VIZ_DRILLDOWN_CAP if cap is None else cap
    labels = community_labels or {}
    out: dict[str, Any] = {}
    for cid, members in communities.items():
        rows: list[list[str]] = []
        for mid in members:
            if len(rows) >= cap:
                break
            data = graph.nodes.get(mid, {}) or {}
            rows.append(
                [
                    str(mid),
                    str(data.get("label") or mid),
                    str(data.get("source_file") or ""),
                ]
            )
        out[str(cid)] = {
            "label": labels.get(int(cid), f"Community {cid}"),
            "count": len(members),
            "members": rows,
        }
    return out


def _full_member_view(
    graph,
    communities: dict[int, list[str]],
    community_labels: dict[int, str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Full node/edge data for the aggregated view's member-level exploration.

    graphify's over-limit ``to_html`` embeds ONLY the community meta-graph in
    ``graph.html`` — the member nodes are not present, so drill-down cannot
    rebuild a member subgraph from the page alone. We embed the FULL member
    node/edge list alongside the aggregated view (same shape as graphify's
    RAW_NODES/RAW_EDGES) so the injected layer can swap views. Payload is the
    size of graph.json — fine for a local file.
    """
    labels = community_labels or {}
    node_community = {nid: cid for cid, members in communities.items() for nid in members}
    degree = dict(graph.degree())
    max_deg = max(degree.values(), default=1) or 1
    nodes: list[dict[str, Any]] = []
    for node_id, data in graph.nodes(data=True):
        data = data or {}
        cid = node_community.get(node_id, 0)
        label = str(data.get("label") or node_id)
        deg = degree.get(node_id, 1)
        nodes.append(
            {
                "id": str(node_id),
                "label": label,
                "color": _VIZ_PALETTE[cid % len(_VIZ_PALETTE)],
                "size": round(10 + 30 * (deg / max_deg), 1),
                "font": {"size": 12 if deg >= max_deg * 0.15 else 0, "color": "#ffffff"},
                "title": label,
                "community": cid,
                "community_name": labels.get(int(cid), f"Community {cid}"),
                "source_file": str(data.get("source_file") or ""),
                "file_type": str(data.get("file_type") or ""),
                "degree": deg,
            }
        )
    edges: list[dict[str, Any]] = []
    for u, v, edata in graph.edges(data=True):
        extracted = edata.get("confidence") == "EXTRACTED"
        edges.append(
            {
                "from": str(u),
                "to": str(v),
                "title": str(edata.get("relation") or ""),
                "dashes": not extracted,
                "width": 2 if extracted else 1,
                "color": {"opacity": 0.7 if extracted else 0.35},
            }
        )
    return nodes, edges


def _viz_injection_fragment(
    drilldown: dict,
    master_nodes: list[dict] | None = None,
    master_edges: list[dict] | None = None,
) -> str:
    """Self-contained <style>+<script> that adds filtering + drill-down to a
    graphify-generated graph.html.

    Reuses the globals graphify's exporter leaves in page scope (RAW_NODES,
    RAW_EDGES, nodesDS, edgesDS, network, LEGEND) — no forking of graphify.
    Keeps the SAME DataSet instances (mutated via clear()/add()) so graphify's
    own showInfo/focusNode/community-toggle handlers keep working.

    ``master_nodes/master_edges`` (aggregated view only) carry the FULL member
    node/edge data so the filter bar + drill-down can rebuild a real member
    subgraph (graphify's over-limit HTML embeds only the community meta-graph).
    When absent, the page's RAW_NODES/RAW_EDGES ARE the master dataset.
    """
    drill_json = _viz_js_safe(drilldown)
    master_nodes_json = _viz_js_safe(master_nodes or [])
    master_edges_json = _viz_js_safe(master_edges or [])
    return f"""<!-- {_VIZ_MARKER}: enhanced filter + drill-down layer (self-contained) -->
<style>
  #viz-controls {{ padding: 8px 12px; border-bottom: 1px solid #2a2a4e; font-size: 12px; display: flex; flex-direction: column; gap: 8px; background: #14142a; }}
  #viz-controls .vhead {{ display: flex; align-items: center; justify-content: space-between; }}
  #viz-controls .vhead b {{ color: #aaa; font-size: 11px; text-transform: uppercase; letter-spacing: .05em; }}
  #viz-controls .vbody {{ display: flex; flex-direction: column; gap: 8px; }}
  #viz-controls.collapsed .vbody {{ display: none; }}
  #viz-controls .vrow {{ display: flex; align-items: center; gap: 8px; }}
  #viz-controls input[type=text] {{ width: 100%; background: #0f0f1a; border: 1px solid #3a3a5e; color: #e0e0e0; padding: 6px 8px; border-radius: 5px; font-size: 12px; outline: none; box-sizing: border-box; }}
  #viz-controls input[type=text]:focus {{ border-color: #4E79A7; }}
  #viz-controls input[type=range] {{ flex: 1; accent-color: #4E79A7; }}
  #viz-controls label {{ color: #aaa; display: flex; align-items: center; gap: 5px; white-space: nowrap; }}
  #viz-controls button {{ background: #2a2a4e; color: #e0e0e0; border: 1px solid #3a3a5e; border-radius: 5px; padding: 4px 10px; font-size: 12px; cursor: pointer; }}
  #viz-controls button:hover {{ background: #3a3a5e; }}
  #viz-controls .vstat {{ color: #888; font-size: 11px; }}
  #viz-controls .vwarn {{ color: #d9a13b; font-size: 11px; line-height: 1.5; }}
  #viz-drill {{ border-top: 1px solid #2a2a4e; max-height: 42%; overflow-y: auto; padding: 8px 12px; background: #14142a; display: flex; flex-direction: column; gap: 6px; }}
  #viz-drill h4 {{ font-size: 12px; color: #aaa; margin: 0; text-transform: uppercase; letter-spacing: .05em; }}
  #viz-drill input[type=text] {{ width: 100%; background: #0f0f1a; border: 1px solid #3a3a5e; color: #e0e0e0; padding: 5px 8px; border-radius: 5px; font-size: 12px; outline: none; box-sizing: border-box; }}
  #viz-drill .drow {{ padding: 3px 6px; border-radius: 3px; cursor: pointer; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  #viz-drill .drow:hover {{ background: #2a2a4e; }}
  #viz-drill .dpath {{ color: #666; font-size: 10px; margin-left: 6px; }}
  /* Node Info box: graphify's CSS leaves #info-panel as `flex: 0 1 auto;
     overflow: visible`, so in the fixed-height sidebar it gets squeezed and its
     content spills over the Communities legend below. Fix: never shrink the
     panel below its content (flex 0 0 auto) and cap it so the box scrolls
     internally — long neighbor lists no longer overlap or push the legend,
     which keeps the remaining sidebar height. */
  #info-panel {{ flex: 0 0 auto; max-height: 240px; overflow-y: auto; }}
  /* Draggable splitter between Node Info and Communities: drag to give either
     pane more room; double-click resets to the automatic height. */
  #viz-splitter {{ flex: 0 0 auto; height: 7px; cursor: row-resize; background: #1a1a2e; border-top: 1px solid #2a2a4e; border-bottom: 1px solid #2a2a4e; }}
  #viz-splitter:hover, #viz-splitter.dragging {{ background: #3a3a5e; }}
  body.viz-resizing {{ cursor: row-resize; user-select: none; }}
</style>
<script>
(function () {{
  if (typeof RAW_NODES === 'undefined' || typeof RAW_EDGES === 'undefined' || typeof network === 'undefined') return;
  var DRILLDOWN = {drill_json};
  var PHYSICS_LIMIT = {_VIZ_PHYSICS_LIMIT};
  var FILTER_HOPS = 2;
  var FOCUSED_ID = null;

  // Master dataset = full member data (aggregated view) or the page's RAW
  // (full view). RAW_NODES/RAW_EDGES always hold the INITIAL rendered view
  // (meta-graph for aggregated), so Reset restores it.
  var MASTER_NODES = {master_nodes_json};
  var MASTER_EDGES = {master_edges_json};
  var nodes = MASTER_NODES.length ? MASTER_NODES : RAW_NODES;
  var edges = MASTER_EDGES.length ? MASTER_EDGES : RAW_EDGES;
  var TOTAL_NODES = nodes.length;
  var TOTAL_EDGES = edges.length;

  function esc(s) {{ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }}

  // Adjacency from the master dataset so focus mode uses the FULL graph (not
  // the current, possibly filtered, view).
  var ADJ = {{}};
  edges.forEach(function (e) {{ var a = String(e.from), b = String(e.to); (ADJ[a] = ADJ[a] || []).push(b); (ADJ[b] = ADJ[b] || []).push(a); }});
  function neighborsOf(id) {{ return ADJ[id] || []; }}

  var wrap = document.getElementById('search-wrap');
  if (!wrap) return;
  var ctl = document.createElement('div');
  ctl.id = 'viz-controls';
  ctl.innerHTML =
    '<div class="vhead"><b>Filters</b><button id="f-toggle" title="Show / hide the filter bar">−</button></div>' +
    '<div class="vbody">' +
      '<div class="vrow"><input id="f-path" type="text" placeholder="Filter by path (e.g. src/components)…" autocomplete="off"></div>' +
      '<div class="vrow"><input id="f-deg" type="range" min="0" max="1" value="0"><label>min degree <b id="f-deg-v">0</b></label></div>' +
      '<div class="vrow"><label><input id="f-focus" type="checkbox"> Focus 2-hop</label><span style="flex:1"></span><button id="f-stab" title="Run the layout on the visible nodes">Stabilize</button></div>' +
      '<div class="vrow"><button id="f-apply" style="flex:1">Apply filters</button><button id="f-reset">Reset</button></div>' +
      '<div class="vstat" id="f-stats"></div>' +
      '<div class="vwarn" id="f-warn" style="display:none"></div>' +
    '</div>';
  wrap.after(ctl);

  // Filter bar show/hide toggle — collapse it to free sidebar space.
  document.getElementById('f-toggle').addEventListener('click', function () {{
    ctl.classList.toggle('collapsed');
    this.textContent = ctl.classList.contains('collapsed') ? '+' : '−';
  }});

  // Draggable splitter between Node Info and Communities.
  var infoPanel = document.getElementById('info-panel');
  var legendWrap = document.getElementById('legend-wrap');
  if (infoPanel && legendWrap) {{
    var splitter = document.createElement('div');
    splitter.id = 'viz-splitter';
    splitter.title = 'Drag to resize · double-click to reset';
    infoPanel.after(splitter);
    var dragging = false, startY = 0, startH = 0;
    splitter.addEventListener('mousedown', function (e) {{
      dragging = true; startY = e.clientY;
      startH = infoPanel.getBoundingClientRect().height;
      splitter.classList.add('dragging');
      document.body.classList.add('viz-resizing');
      if (e.preventDefault) e.preventDefault();
    }});
    window.addEventListener('mousemove', function (e) {{
      if (!dragging) return;
      var top = infoPanel.getBoundingClientRect().top;
      var max = legendWrap.getBoundingClientRect().bottom - top - 60;
      var h = Math.min(Math.max(startH + (e.clientY - startY), 60), Math.max(max, 60));
      infoPanel.style.height = h + 'px';
      infoPanel.style.maxHeight = 'none';
    }});
    window.addEventListener('mouseup', function () {{
      if (!dragging) return;
      dragging = false;
      splitter.classList.remove('dragging');
      document.body.classList.remove('viz-resizing');
    }});
    splitter.addEventListener('dblclick', function () {{
      infoPanel.style.height = '';
      infoPanel.style.maxHeight = '';
    }});
  }}

  var maxDeg = 0;
  nodes.forEach(function (n) {{ if ((n.degree || 0) > maxDeg) maxDeg = n.degree || 0; }});
  document.getElementById('f-deg').max = Math.max(1, maxDeg);

  function nodeObj(n) {{
    return {{ id: n.id, label: n.label, color: n.color, size: n.size, font: n.font, title: n.title, _community: n.community, _community_name: n.community_name, _source_file: n.source_file, _file_type: n.file_type, _degree: n.degree }};
  }}
  function edgeObj(e, i) {{
    return {{ id: i, from: e.from, to: e.to, label: '', title: e.title, dashes: e.dashes, width: e.width, color: e.color, arrows: {{ to: {{ enabled: true, scaleFactor: 0.5 }} }} }};
  }}
  function statsLine(vn, ve) {{
    var nc = (typeof LEGEND !== 'undefined' && LEGEND) ? LEGEND.length : 0;
    document.getElementById('f-stats').textContent = vn.length + ' / ' + TOTAL_NODES + ' nodes · ' + ve.length + ' edges · ' + nc + ' communities';
  }}
  function showWarn(msg) {{ var w = document.getElementById('f-warn'); if (msg) {{ w.textContent = msg; w.style.display = 'block'; }} else {{ w.style.display = 'none'; }} }}

  function computeVisible() {{
    var q = document.getElementById('f-path').value.trim().toLowerCase();
    var minDeg = parseInt(document.getElementById('f-deg').value, 10) || 0;
    var focusOn = document.getElementById('f-focus').checked;
    var vis = {{}};
    for (var i = 0; i < nodes.length; i++) {{
      var n = nodes[i], id = String(n.id);
      if ((n.degree || 0) < minDeg) continue;
      if (q && (String(n.source_file || '').toLowerCase().indexOf(q) === -1)) continue;
      vis[id] = true;
    }}
    if (focusOn && FOCUSED_ID) {{
      var keep = {{}}; keep[FOCUSED_ID] = true;
      var frontier = [FOCUSED_ID];
      for (var h = 0; h < FILTER_HOPS; h++) {{
        var next = [];
        frontier.forEach(function (f) {{
          neighborsOf(f).forEach(function (nb) {{ if (!keep[nb]) {{ keep[nb] = true; next.push(nb); }} }});
        }});
        frontier = next;
      }}
      var merged = {{}};
      for (var k in vis) {{ if (keep[k]) merged[k] = true; }}
      vis = merged;
    }}
    return vis;
  }}

  function setView(vn, ve) {{
    nodesDS.clear(); edgesDS.clear();
    nodesDS.add(vn.map(nodeObj));
    edgesDS.add(ve.map(edgeObj));
    network.setData({{ nodes: nodesDS, edges: edgesDS }});
    if (vn.length > 0 && vn.length <= PHYSICS_LIMIT) {{
      network.setOptions({{ physics: {{ enabled: true, solver: 'forceAtlas2Based', forceAtlas2Based: {{ gravitationalConstant: -60, centralGravity: 0.005, springLength: 120, springConstant: 0.08, damping: 0.4, avoidOverlap: 0.8 }}, stabilization: {{ iterations: 200, fit: true }} }} }});
      network.stabilize(200, function () {{ network.setOptions({{ physics: {{ enabled: false }} }}); }});
    }} else {{
      network.setOptions({{ physics: {{ enabled: false }} }});
    }}
    statsLine(vn, ve);
    showWarn(vn.length > PHYSICS_LIMIT ? 'Too many nodes for physics (' + vn.length + '). Narrow with filters, then Stabilize.' : '');
  }}

  function applyFilters() {{
    var vis = computeVisible();
    var vn = [], ve = [];
    nodes.forEach(function (n) {{ if (vis[String(n.id)]) vn.push(n); }});
    edges.forEach(function (e) {{ if (vis[String(e.from)] && vis[String(e.to)]) ve.push(e); }});
    setView(vn, ve);
  }}

  function hideDrill() {{ var d = document.getElementById('viz-drill'); if (d) d.parentNode.removeChild(d); }}

  function showDrill(cid) {{
    hideDrill();
    var d = DRILLDOWN[cid];
    var panel = document.createElement('div');
    panel.id = 'viz-drill';
    var total = d.count, rows = d.members;
    panel.innerHTML =
      '<h4>' + esc(d.label) + ' · ' + total + ' members</h4>' +
      '<div class="vrow"><button id="d-load" style="flex:1">Load members into graph</button><button id="d-close">Close</button></div>' +
      (rows.length < total ? '<div class="vwarn">Showing first ' + rows.length + ' of ' + total + ' members — use the path filter to narrow.</div>' : '') +
      '<input id="d-q" type="text" placeholder="Filter members…" autocomplete="off">' +
      '<div id="d-list" style="margin-top:2px"></div>';
    document.getElementById('sidebar').appendChild(panel);
    function render(q) {{
      q = (q || '').toLowerCase();
      var items = rows.filter(function (r) {{ return !q || r[1].toLowerCase().indexOf(q) !== -1 || (r[2] || '').toLowerCase().indexOf(q) !== -1; }});
      document.getElementById('d-list').innerHTML = items.slice(0, 200).map(function (r) {{
        return '<div class="drow" data-nid="' + esc(r[0]) + '"><span>' + esc(r[1]) + '</span><span class="dpath">' + esc(r[2] || '') + '</span></div>';
      }}).join('');
    }}
    render('');
    document.getElementById('d-q').addEventListener('input', function () {{ render(this.value); }});
    document.getElementById('d-close').addEventListener('click', hideDrill);
    document.getElementById('d-list').addEventListener('click', function (e) {{
      var el = e.target.closest('.drow'); if (!el) return;
      var nid = el.dataset.nid;
      if (nodesDS.get(nid)) {{ network.focus(nid, {{ scale: 1.5, animation: true }}); network.selectNodes([nid]); }}
    }});
    document.getElementById('d-load').addEventListener('click', function () {{
      var keep = {{}}; rows.forEach(function (r) {{ keep[String(r[0])] = true; }});
      var vn = nodes.filter(function (n) {{ return keep[String(n.id)]; }});
      var ve = edges.filter(function (e) {{ return keep[String(e.from)] && keep[String(e.to)]; }});
      setView(vn, ve);
      hideDrill();
      showWarn('Community loaded: ' + vn.length + ' nodes. Reset to return to the overview.');
    }});
  }}

  function bindEvents() {{
    document.getElementById('f-apply').addEventListener('click', applyFilters);
    document.getElementById('f-reset').addEventListener('click', function () {{
      document.getElementById('f-path').value = '';
      document.getElementById('f-deg').value = '0';
      document.getElementById('f-deg-v').textContent = '0';
      document.getElementById('f-focus').checked = false;
      FOCUSED_ID = null;
      hideDrill();
      // Restore the initial rendered view (meta-graph for aggregated, full
      // node view for full renders).
      setView(RAW_NODES, RAW_EDGES);
    }});
    document.getElementById('f-deg').addEventListener('input', function () {{ document.getElementById('f-deg-v').textContent = this.value; }});
    document.getElementById('f-path').addEventListener('keydown', function (e) {{ if (e.key === 'Enter') applyFilters(); }});
    document.getElementById('f-stab').addEventListener('click', function () {{
      var cur = nodesDS.length;
      if (cur === 0 || cur > PHYSICS_LIMIT) {{ showWarn('Cannot stabilize ' + cur + ' nodes — narrow the view first.'); return; }}
      network.setOptions({{ physics: {{ enabled: true, stabilization: {{ iterations: 200, fit: true }} }} }});
      network.stabilize(200, function () {{ network.setOptions({{ physics: {{ enabled: false }} }}); }});
      showWarn('');
    }});
    network.on('click', function (params) {{
      if (params.nodes && params.nodes.length) {{ FOCUSED_ID = String(params.nodes[0]); if (document.getElementById('f-focus').checked) applyFilters(); }}
    }});
    network.on('selectNode', function (params) {{ if (params.nodes && params.nodes.length) FOCUSED_ID = String(params.nodes[0]); }});
    if (Object.keys(DRILLDOWN).length) {{
      network.on('click', function (params) {{
        if (params.nodes && params.nodes.length) {{ var id = String(params.nodes[0]); if (DRILLDOWN[id]) showDrill(id); }}
      }});
    }}
  }}

  statsLine(RAW_NODES, RAW_EDGES);
  if (RAW_NODES.length > PHYSICS_LIMIT) {{
    // Halt the initial forceAtlas2 stabilization before it freezes the browser.
    if (network.stopSimulation) network.stopSimulation();
    network.setOptions({{ physics: {{ enabled: false }} }});
    showWarn('Large graph (' + RAW_NODES.length + ' nodes): physics disabled. Filter by path / degree / focus, then Stabilize.');
  }}
  bindEvents();
}})();
</script>"""


def _inject_large_viz(html_path: Path, drilldown: dict, master: tuple | None = None) -> None:
    """Append the enhanced filter/drill-down layer to a generated graph.html.

    Idempotent (guarded by the ``_VIZ_MARKER`` comment). Self-contained — the
    injected fragment reuses graphify's page-scope globals, so no forking of
    graphify is required and the structural graph.json stays authoritative.
    ``master`` = ``(nodes, edges)`` full member data to embed alongside an
    aggregated view (so drill-down/filter can rebuild a real member subgraph).
    """
    if not html_path.is_file():
        return
    text = html_path.read_text(encoding="utf-8")
    if f"<!-- {_VIZ_MARKER}" in text:
        return
    master_nodes = master[0] if master else None
    master_edges = master[1] if master else None
    frag = _viz_injection_fragment(drilldown, master_nodes=master_nodes, master_edges=master_edges)
    if "</body>" in text:
        text = text.replace("</body>", frag + "\n</body>", 1)
    else:
        text += frag
    html_path.write_text(text, encoding="utf-8")


def _html_export(graph, communities: dict[int, list[str]], output_path: Path, node_limit: int) -> str:
    """graphify's ``to_html`` + our large-viz enhancement layer.

    Returns the html status string the build callers report: ``"full"``,
    ``"aggregated"``, or ``"skipped (...)"``. The enhanced filter layer is
    injected into every rendered HTML. For the over-limit aggregated view the
    full member node/edge data is embedded alongside the community meta-graph
    so drill-down can expand a community into its member subgraph.

    Heuristic (non-LLM) community labels are passed to ``to_html`` so the
    Communities legend actually lists per-community toggles instead of only
    "Select All".
    """
    g = _graphify_imports()
    if g is None:
        return "skipped (graphifyy not installed)"
    community_labels = _community_labels(graph, communities)
    try:
        g["to_html"](
            graph,
            communities,
            str(output_path),
            node_limit=node_limit,
            community_labels=community_labels,
        )
    except ValueError as e:  # non-fatal: structural graph.json + manifest still land
        return f"skipped ({e})"
    aggregated = graph.number_of_nodes() > node_limit
    if output_path.is_file():
        master: tuple | None = None
        if aggregated:
            drilldown = _drilldown_data(graph, communities, community_labels=community_labels)
            master = _full_member_view(graph, communities, community_labels=community_labels)
        else:
            drilldown = {}
        try:
            _inject_large_viz(output_path, drilldown, master=master)
        except Exception:  # pragma: no cover - non-fatal (base viz already written)
            pass
    return "aggregated" if aggregated else "full"
