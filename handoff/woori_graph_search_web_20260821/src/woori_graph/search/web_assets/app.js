(() => {
  "use strict";

  const elements = {
    form: document.querySelector("#search-form"),
    query: document.querySelector("#query"),
    button: document.querySelector("#search-button"),
    status: document.querySelector("#search-status"),
    empty: document.querySelector("#empty-state"),
    result: document.querySelector("#result"),
    answer: document.querySelector("#answer"),
    graph: document.querySelector("#network"),
    graphSummary: document.querySelector("#graph-summary"),
    selection: document.querySelector("#selection-detail"),
    reset: document.querySelector("#reset-selection"),
    evidenceSummary: document.querySelector("#evidence-summary"),
    evidenceList: document.querySelector("#evidence-list"),
    traversalRule: document.querySelector("#traversal-rule"),
    adaptation: document.querySelector("#adaptation-status"),
    metrics: document.querySelector("#search-metrics"),
    hopBody: document.querySelector("#hop-table-body"),
    entityCount: document.querySelector("#entity-candidate-count"),
    entityBody: document.querySelector("#entity-candidate-body"),
    relationCount: document.querySelector("#relation-candidate-count"),
    relationBody: document.querySelector("#relation-candidate-body"),
    finalSelection: document.querySelector("#final-selection"),
    duration: document.querySelector("#duration"),
    pathCount: document.querySelector("#path-count"),
    documentCount: document.querySelector("#document-count"),
    loadingTemplate: document.querySelector("#loading-template"),
  };

  let currentPayload = null;
  let nodeElements = new Map();
  let edgeElements = new Map();
  let labelElements = new Map();

  elements.form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const query = elements.query.value.trim();
    if (query.length < 2) {
      setStatus("질문을 두 글자 이상 입력해주세요.", "error");
      elements.query.focus();
      return;
    }
    await runSearch(query);
  });
  elements.reset.addEventListener("click", resetSelection);

  async function runSearch(query) {
    setLoading(true);
    try {
      const response = await fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "검색 요청에 실패했습니다.");
      currentPayload = payload;
      renderResult(payload);
      setStatus(
        `탐색 완료 · ${formatNumber(payload.stats.duration_seconds, 3)}초 · ${payload.counts.paths}개 경로`,
        "success",
      );
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "검색 요청에 실패했습니다.", "error");
    } finally {
      setLoading(false);
    }
  }

  function setLoading(loading) {
    elements.button.disabled = loading;
    elements.button.textContent = loading ? "탐색 중" : "탐색";
    if (loading) {
      elements.status.className = "search-status";
      elements.status.replaceChildren(elements.loadingTemplate.content.cloneNode(true));
    }
  }

  function setStatus(message, type = "") {
    elements.status.className = `search-status${type ? ` ${type}` : ""}`;
    elements.status.textContent = message;
  }

  function renderResult(payload) {
    elements.empty.hidden = true;
    elements.result.hidden = false;
    elements.answer.textContent = payload.answer;
    elements.graphSummary.textContent = `최대 ${payload.stats.max_hops_reached}-hop · 노드 ${payload.counts.nodes}개 · 관계 ${payload.counts.edges}개`;
    elements.duration.textContent = `탐색 시간 ${formatNumber(payload.stats.duration_seconds, 3)}초`;
    elements.pathCount.textContent = `최종 경로 ${payload.counts.paths}개`;
    elements.documentCount.textContent = `근거 문서 ${payload.counts.documents}개`;
    renderGraph(payload.graph);
    renderEvidence(payload.evidence);
    renderDiagnostics(payload);
    resetSelection();
  }

  function renderGraph(graph) {
    const svg = elements.graph;
    svg.replaceChildren();
    nodeElements = new Map();
    edgeElements = new Map();
    labelElements = new Map();
    const ns = "http://www.w3.org/2000/svg";
    const width = 1000;
    const height = 620;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

    const defs = createSvg("defs");
    const marker = createSvg("marker", {
      id: "arrow", viewBox: "0 0 10 10", refX: "9", refY: "5",
      markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse",
    });
    marker.append(createSvg("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "context-stroke" }));
    defs.append(marker);
    svg.append(defs);

    const positions = layoutNodes(graph.nodes, width, height);
    const edgeLayer = createSvg("g");
    const labelLayer = createSvg("g");
    const nodeLayer = createSvg("g");
    svg.append(edgeLayer, labelLayer, nodeLayer);

    const parallels = new Map();
    for (const edge of graph.edges) {
      const key = [edge.source, edge.target].sort().join("|");
      if (!parallels.has(key)) parallels.set(key, []);
      parallels.get(key).push(edge.id);
    }

    for (const edge of graph.edges) {
      const start = positions.get(edge.source);
      const end = positions.get(edge.target);
      if (!start || !end) continue;
      const key = [edge.source, edge.target].sort().join("|");
      const group = parallels.get(key);
      const offset = (group.indexOf(edge.id) - (group.length - 1) / 2) * 17;
      const geometry = edgeGeometry(start, end, offset);
      const hit = createSvg("path", { d: geometry.path, class: "graph-edge-hit" });
      const line = createSvg("path", {
        d: geometry.path,
        class: `graph-edge${edge.polarity === "NEGATIVE" ? " negative" : ""}`,
      });
      const label = createSvg("text", {
        x: geometry.labelX,
        y: geometry.labelY,
        class: "graph-edge-label",
      });
      label.textContent = edge.relation;
      hit.addEventListener("click", () => selectEdge(edge));
      edgeLayer.append(hit, line);
      labelLayer.append(label);
      edgeElements.set(edge.id, line);
      labelElements.set(edge.id, label);
    }

    for (const node of graph.nodes) {
      const point = positions.get(node.id);
      if (!point) continue;
      const group = createSvg("g", {
        class: `graph-node${node.seed ? " seed" : ""}`,
        transform: `translate(${point.x},${point.y})`,
        tabindex: "0",
        role: "button",
        "aria-label": `${node.name} 엔티티`,
      });
      group.append(createSvg("rect", { x: "-76", y: "-27", width: "152", height: "54", rx: "9" }));
      const name = createSvg("text", { y: "-2", class: "name" });
      name.textContent = truncate(node.name, 18);
      const type = createSvg("text", { y: "15", class: "type" });
      type.textContent = node.type;
      group.append(name, type);
      group.addEventListener("click", () => selectNode(node));
      group.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") selectNode(node);
      });
      nodeLayer.append(group);
      nodeElements.set(node.id, group);
    }

    function createSvg(tag, attributes = {}) {
      const element = document.createElementNS(ns, tag);
      for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
      return element;
    }
  }

  function layoutNodes(nodes, width, height) {
    const levels = new Map();
    for (const node of nodes) {
      const level = Math.max(0, Math.min(3, Number(node.level) || 0));
      if (!levels.has(level)) levels.set(level, []);
      levels.get(level).push(node);
    }
    const presentLevels = [...levels.keys()].sort((a, b) => a - b);
    const maxLevel = Math.max(1, ...presentLevels);
    const positions = new Map();
    for (const level of presentLevels) {
      const levelNodes = levels.get(level).sort((a, b) => a.name.localeCompare(b.name, "ko"));
      const x = 110 + (width - 220) * (level / maxLevel);
      levelNodes.forEach((node, index) => {
        const y = 70 + (height - 140) * ((index + 1) / (levelNodes.length + 1));
        positions.set(node.id, { x, y });
      });
    }
    return positions;
  }

  function edgeGeometry(start, end, offset) {
    const dx = end.x - start.x;
    const dy = end.y - start.y;
    const length = Math.max(1, Math.hypot(dx, dy));
    const ux = dx / length;
    const uy = dy / length;
    const px = -uy;
    const py = ux;
    const source = { x: start.x + ux * 78, y: start.y + uy * 30 };
    const target = { x: end.x - ux * 82, y: end.y - uy * 30 };
    const control = {
      x: (source.x + target.x) / 2 + px * offset,
      y: (source.y + target.y) / 2 + py * offset,
    };
    return {
      path: `M ${source.x} ${source.y} Q ${control.x} ${control.y} ${target.x} ${target.y}`,
      labelX: .25 * source.x + .5 * control.x + .25 * target.x,
      labelY: .25 * source.y + .5 * control.y + .25 * target.y - 8,
    };
  }

  function resetSelection() {
    for (const element of nodeElements.values()) element.classList.remove("active", "dim");
    for (const element of edgeElements.values()) element.classList.remove("dim");
    for (const element of labelElements.values()) element.classList.remove("dim");
    if (!currentPayload) return;
    elements.selection.textContent = "노드 또는 관계를 선택하면 연결 정보와 해당 근거를 확인할 수 있습니다.";
    renderEvidence(currentPayload.evidence);
  }

  function selectNode(node) {
    resetVisualState();
    const incident = currentPayload.graph.edges.filter((edge) => edge.source === node.id || edge.target === node.id);
    const related = new Set([node.id]);
    for (const edge of incident) { related.add(edge.source); related.add(edge.target); }
    for (const [id, element] of nodeElements) {
      if (!related.has(id)) element.classList.add("dim");
    }
    const incidentIds = new Set(incident.map((edge) => edge.id));
    dimUnselectedEdges(incidentIds);
    nodeElements.get(node.id)?.classList.add("active");
    elements.selection.textContent = `${node.name} · ${node.type} · 직접 연결 ${incident.length}개`;
    const relationIds = new Set(incident.map((edge) => edge.id));
    renderEvidence(currentPayload.evidence.filter((item) => relationIds.has(item.relation_id)));
  }

  function selectEdge(edge) {
    resetVisualState();
    nodeElements.get(edge.source)?.classList.add("active");
    nodeElements.get(edge.target)?.classList.add("active");
    dimUnselectedEdges(new Set([edge.id]));
    elements.selection.textContent = `${edge.source_name} — ${edge.relation} → ${edge.target_name} · 전체 근거 ${edge.evidence_count}건`;
    renderEvidence(currentPayload.evidence.filter((item) => item.relation_id === edge.id));
  }

  function resetVisualState() {
    for (const element of nodeElements.values()) element.classList.remove("active", "dim");
    for (const element of edgeElements.values()) element.classList.remove("dim");
    for (const element of labelElements.values()) element.classList.remove("dim");
  }

  function dimUnselectedEdges(selectedIds) {
    for (const [id, element] of edgeElements) if (!selectedIds.has(id)) element.classList.add("dim");
    for (const [id, element] of labelElements) if (!selectedIds.has(id)) element.classList.add("dim");
  }

  function renderEvidence(evidence) {
    elements.evidenceList.replaceChildren();
    elements.evidenceSummary.textContent = `${evidence.length}건 표시`;
    if (!evidence.length) {
      const empty = document.createElement("li");
      empty.textContent = "선택한 범위에 표시할 근거가 없습니다.";
      elements.evidenceList.append(empty);
      return;
    }
    for (const item of evidence) {
      const row = document.createElement("li");
      const title = document.createElement("strong");
      title.textContent = item.document_title;
      const citation = document.createElement("span");
      citation.className = "citation";
      citation.textContent = sourceReference(item.source_ref);
      const raw = document.createElement("span");
      raw.className = "raw";
      raw.textContent = `${item.raw_subject} — ${item.raw_predicate} → ${item.raw_object}`;
      row.append(title, citation, raw);
      elements.evidenceList.append(row);
    }
  }

  function renderDiagnostics(payload) {
    const plan = payload.search_plan;
    elements.traversalRule.textContent = plan.traversal.rule;
    const adaptations = plan.final_selection.adaptations;
    elements.adaptation.textContent = adaptations.length ? `시간 축소 적용 ${adaptations.length}건` : "시간 축소 없음";
    elements.adaptation.classList.toggle("reduced", adaptations.length > 0);

    const metricValues = [
      ["엔티티 Top-K", plan.candidate_retrieval.entity_top_k],
      ["릴레이션 Top-K", plan.candidate_retrieval.relation_top_k],
      ["엔티티별 인접 관계", plan.traversal.max_neighbors_per_entity ?? "—"],
      ["최종 경로", plan.final_selection.max_paths],
    ];
    elements.metrics.replaceChildren();
    for (const [label, value] of metricValues) {
      const wrapper = document.createElement("div");
      const term = document.createElement("dt");
      const description = document.createElement("dd");
      term.textContent = label;
      description.textContent = String(value);
      wrapper.append(term, description);
      elements.metrics.append(wrapper);
    }

    elements.hopBody.replaceChildren();
    for (const hop of plan.traversal.hops) {
      const row = document.createElement("tr");
      const values = [
        `${hop.hop}-hop`,
        `${formatNumber(hop.frontier_entities)}개 엔티티`,
        `${formatNumber(hop.fetched_edges)}개 / 상한 ${formatNumber(hop.query_edge_limit)}`,
        `${formatNumber(hop.neighbor_limit_per_entity)}개`,
        `${formatNumber(hop.generated_paths)}개`,
        `${formatNumber(hop.retained_paths)}개 / beam ${formatNumber(hop.beam_width)}`,
      ];
      for (const value of values) {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.append(cell);
      }
      elements.hopBody.append(row);
    }

    const entities = payload.selected_candidates.entities;
    elements.entityCount.textContent = `${entities.length}개`;
    elements.entityBody.replaceChildren();
    for (const entity of entities) {
      elements.entityBody.append(candidateRow([
        entity.rank,
        entity.canonical_name,
        entity.entity_type,
        formatNumber(entity.score, 6),
        matchSources(entity.matched_by),
      ]));
    }

    const relations = payload.selected_candidates.relations;
    elements.relationCount.textContent = `${relations.length}개`;
    elements.relationBody.replaceChildren();
    for (const relation of relations) {
      elements.relationBody.append(candidateRow([
        relation.rank,
        `${relation.source_name} — ${relation.relation_type_name} → ${relation.target_name}`,
        formatNumber(relation.score, 6),
        matchSources(relation.matched_by),
      ]));
    }

    const final = plan.final_selection;
    elements.finalSelection.replaceChildren(
      summaryItem("중복 제거 경로", final.deduplicated_paths),
      summaryItem("근거 결합 대상", final.evidence_path_pool),
      summaryItem("최종 반환", `${final.returned_paths} / ${final.max_paths}`),
      summaryItem("관계별 근거", final.evidence_per_relation ?? "—"),
      summaryItem("전체 제한시간", final.timeout_seconds ? `${final.timeout_seconds}초` : "—"),
    );
  }

  function candidateRow(values) {
    const row = document.createElement("tr");
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = String(value);
      if (index === values.length - 2) cell.className = "score";
      if (index === values.length - 1) cell.className = "match";
      row.append(cell);
    });
    return row;
  }

  function summaryItem(label, value) {
    const item = document.createElement("span");
    const strong = document.createElement("strong");
    strong.textContent = `${label} `;
    item.append(strong, String(value));
    return item;
  }

  function sourceReference(reference) {
    const values = [];
    if (reference?.article) values.push(reference.article);
    if (reference?.paragraph !== null && reference?.paragraph !== undefined && reference.paragraph !== "") values.push(`제${reference.paragraph}항`);
    if (Array.isArray(reference?.item_path) && reference.item_path.length) values.push(reference.item_path.join("-"));
    return values.join(" ") || "조문 위치 없음";
  }

  function matchSources(values) {
    const names = { keyword: "키워드", vector: "벡터", relation_endpoint: "관계 종점" };
    return (values || []).map((value) => names[value] || value).join("+") || "—";
  }

  function formatNumber(value, fractionDigits = 0) {
    return new Intl.NumberFormat("ko-KR", {
      minimumFractionDigits: fractionDigits,
      maximumFractionDigits: fractionDigits,
    }).format(Number(value));
  }

  function truncate(value, length) {
    const text = String(value);
    return text.length > length ? `${text.slice(0, length - 1)}…` : text;
  }
})();
