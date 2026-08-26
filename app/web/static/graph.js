(function () {
  // Kept in sync by hand with the .tag[data-relation="..."] rules in style.css -- that file is
  // the source of truth for these colors (same "kept in sync by hand" convention already used
  // for RELATION_TYPES between app/db/models.py and its alembic migrations).
  const RELATION_COLORS = {
    related_to: "#6b7280",
    same_as: "#16a34a",
    follow_up_of: "#2563eb",
    mentions: "#9ca3af",
    supersedes: "#ea580c",
    causes: "#dc2626",
    fixes: "#059669",
    contradicts: "#db2777",
  };

  const SVG_NS = "http://www.w3.org/2000/svg";
  const svg = document.getElementById("relation-graph");
  if (!svg) return;

  function layout(nodes, edges, width, height) {
    const positions = new Map();
    nodes.forEach((n, i) => {
      const angle = (2 * Math.PI * i) / Math.max(nodes.length, 1);
      const radius = Math.min(width, height) / 3;
      positions.set(n.id, {
        x: width / 2 + Math.cos(angle) * radius,
        y: height / 2 + Math.sin(angle) * radius,
      });
    });

    // Small fixed-iteration force-directed layout (repulsion + spring + centering). Vanilla
    // JS instead of a vendored graph library -- at realistic scale (dozens of nodes/edges per
    // project) this is plenty, and avoids reconciling a library's own theming with this
    // project's light/dark CSS custom properties for no real benefit at this size.
    const REPULSION = 8000;
    const SPRING_LENGTH = 140;
    const SPRING_K = 0.02;
    const CENTER_K = 0.01;
    const MAX_STEP = 20;
    const ITERATIONS = 250;

    for (let iter = 0; iter < ITERATIONS; iter++) {
      const forces = new Map();
      nodes.forEach((n) => forces.set(n.id, { x: 0, y: 0 }));

      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const a = positions.get(nodes[i].id);
          const b = positions.get(nodes[j].id);
          const dx = a.x - b.x;
          const dy = a.y - b.y;
          const distSq = dx * dx + dy * dy || 0.01;
          const dist = Math.sqrt(distSq);
          const force = REPULSION / distSq;
          const fx = (dx / dist) * force;
          const fy = (dy / dist) * force;
          forces.get(nodes[i].id).x += fx;
          forces.get(nodes[i].id).y += fy;
          forces.get(nodes[j].id).x -= fx;
          forces.get(nodes[j].id).y -= fy;
        }
      }

      edges.forEach((e) => {
        const a = positions.get(e.from);
        const b = positions.get(e.to);
        if (!a || !b) return;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const displacement = (dist - SPRING_LENGTH) * SPRING_K;
        const fx = (dx / dist) * displacement;
        const fy = (dy / dist) * displacement;
        forces.get(e.from).x += fx;
        forces.get(e.from).y += fy;
        forces.get(e.to).x -= fx;
        forces.get(e.to).y -= fy;
      });

      nodes.forEach((n) => {
        const pos = positions.get(n.id);
        const f = forces.get(n.id);
        f.x += (width / 2 - pos.x) * CENTER_K;
        f.y += (height / 2 - pos.y) * CENTER_K;
        pos.x += Math.max(-MAX_STEP, Math.min(MAX_STEP, f.x));
        pos.y += Math.max(-MAX_STEP, Math.min(MAX_STEP, f.y));
        pos.x = Math.max(30, Math.min(width - 30, pos.x));
        pos.y = Math.max(30, Math.min(height - 30, pos.y));
      });
    }

    return positions;
  }

  function showMessage(width, height, text) {
    const el = document.createElementNS(SVG_NS, "text");
    el.setAttribute("x", width / 2);
    el.setAttribute("y", height / 2);
    el.setAttribute("text-anchor", "middle");
    el.setAttribute("class", "graph-empty");
    el.textContent = text;
    svg.appendChild(el);
  }

  // Fixed logical coordinate system for the layout math -- the SVG scales this viewBox to
  // whatever pixel box CSS actually gives #relation-graph (default preserveAspectRatio
  // "xMidYMid meet"), so this doesn't need to match the rendered size and isn't at the mercy
  // of getBoundingClientRect() being called before the page has finished laying out.
  const LOGICAL_WIDTH = 1000;
  const LOGICAL_HEIGHT = 600;

  function render(nodes, edges) {
    const width = LOGICAL_WIDTH;
    const height = LOGICAL_HEIGHT;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

    if (nodes.length === 0) {
      showMessage(width, height, "Noch keine Einträge in diesem Projekt.");
      return;
    }

    const positions = layout(nodes, edges, width, height);

    edges.forEach((e) => {
      const a = positions.get(e.from);
      const b = positions.get(e.to);
      if (!a || !b) return;
      const line = document.createElementNS(SVG_NS, "line");
      line.setAttribute("x1", a.x);
      line.setAttribute("y1", a.y);
      line.setAttribute("x2", b.x);
      line.setAttribute("y2", b.y);
      line.setAttribute("stroke", RELATION_COLORS[e.relation_type] || "#6b7280");
      line.setAttribute("stroke-width", "1.5");
      line.setAttribute("opacity", "0.7");
      const title = document.createElementNS(SVG_NS, "title");
      title.textContent = e.note ? `${e.relation_type}: ${e.note}` : e.relation_type;
      line.appendChild(title);
      svg.appendChild(line);
    });

    nodes.forEach((n) => {
      const pos = positions.get(n.id);
      const g = document.createElementNS(SVG_NS, "g");
      g.setAttribute("class", "graph-node" + (n.status === "veraltet" ? " veraltet" : ""));
      g.setAttribute("transform", `translate(${pos.x}, ${pos.y})`);
      g.addEventListener("click", () => {
        window.location.href = `/entries/${n.id}`;
      });

      const circle = document.createElementNS(SVG_NS, "circle");
      circle.setAttribute("r", "8");
      g.appendChild(circle);

      const label = document.createElementNS(SVG_NS, "text");
      label.setAttribute("x", "12");
      label.setAttribute("y", "4");
      label.textContent = n.title.length > 30 ? n.title.slice(0, 29) + "…" : n.title;
      g.appendChild(label);

      const titleEl = document.createElementNS(SVG_NS, "title");
      titleEl.textContent = n.title;
      g.appendChild(titleEl);

      svg.appendChild(g);
    });
  }

  const url = svg.dataset.graphUrl;
  fetch(url)
    .then((r) => r.json())
    .then((data) => render(data.nodes || [], data.edges || []))
    .catch(() => {
      svg.setAttribute("viewBox", `0 0 ${LOGICAL_WIDTH} ${LOGICAL_HEIGHT}`);
      showMessage(LOGICAL_WIDTH, LOGICAL_HEIGHT, "Graph konnte nicht geladen werden.");
    });
})();
