/* Semantic graph view — every adapter's output is regions + relationships
 * (formula-ref, cross-sheet-ref, sums-line-items, connects-to), so one
 * renderer shows the *structure* nexDoc kept for any artifact type.
 *
 *  - Diagram "edge" regions are collapsed into direct links between the nodes
 *    they connect (that is what a diagram edge means), and diagrams keep their
 *    real on-page layout; everything else gets a force-directed layout.
 *  - Hover dims everything but a node's neighborhood; click selects the
 *    region (synced with the source pane + stream); nodes are draggable.
 *  - toMermaid() exports the same graph as a Mermaid flowchart.
 */
window.NX = window.NX || {};
NX.graph = (function(){
  'use strict';
  const SVGNS = 'http://www.w3.org/2000/svg';
  const PALETTE = ['#4ee0b5', '#7dd3fc', '#fcd34d', '#c4b5fd', '#ff8d5c', '#f9a8d4', '#a3e635', '#94a3b8'];
  const LINK_COLORS = { 'connects-to':'#9fb3ad', 'formula-ref':'#7dd3fc', 'cross-sheet-ref':'#ff8d5c', 'sums-line-items':'#fcd34d' };

  function el(name, attrs, parent){
    const n = document.createElementNS(SVGNS, name);
    if(attrs) for(const k in attrs) n.setAttribute(k, attrs[k]);
    if(parent) parent.appendChild(n);
    return n;
  }
  function hashStr(s){ let h = 0; for(let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return Math.abs(h); }
  function colorFor(group){ return PALETTE[hashStr(group) % PALETTE.length]; }
  function linkColor(type){ return LINK_COLORS[type] || PALETTE[hashStr(type) % PALETTE.length]; }
  function trunc(s, n){ s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

  function labelFor(r, kind){
    if(kind === 'spreadsheet'){
      const ref = r.id.includes('!') ? r.id.slice(r.id.indexOf('!') + 1) : r.id;
      return r.type === 'formula-cell' ? ref + ' ƒ' : ref;
    }
    if(r.text) return trunc(r.text, 22);
    if(r.shape) return r.shape.replace(/^node-/, '');
    return r.id;
  }
  function groupFor(r, kind){
    if(r.type === 'node' && r.shape) return r.shape;
    return r.type;
  }

  /* ---- model ---- */
  function buildModel(regions, kind){
    const byId = new Map(regions.map(r => [r.id, r]));
    const nodes = [];
    regions.forEach(r => {
      if(r.type === 'edge' || r.type === 'swimlane') return;
      nodes.push({ id:r.id, region:r, label:labelFor(r, kind), group:groupFor(r, kind), deg:0 });
    });
    const nodeIds = new Set(nodes.map(n => n.id));
    const links = [];
    regions.forEach(r => {
      const rels = r.relationships || [];
      if(r.type === 'edge'){
        const ends = rels.filter(x => x.type === 'connects-to').map(x => x.target);
        if(ends.length >= 2 && nodeIds.has(ends[0]) && nodeIds.has(ends[1])){
          links.push({ source:ends[0], target:ends[1], type:'connects-to', label:r.text || '', regionId:r.id, confidence:r.confidence });
        }
        return;
      }
      rels.forEach(rel => {
        if(nodeIds.has(rel.target) && nodeIds.has(r.id)) links.push({ source:r.id, target:rel.target, type:rel.type, label:'', regionId:r.id, confidence:r.confidence });
      });
    });
    const idx = new Map(nodes.map(n => [n.id, n]));
    links.forEach(l => { idx.get(l.source).deg++; idx.get(l.target).deg++; });
    return { nodes, links, kind };
  }

  /* ---- layout ---- */
  function seeded(seed){ let s = seed >>> 0 || 1; return () => { s ^= s << 13; s >>>= 0; s ^= s >> 17; s ^= s << 5; s >>>= 0; return s / 4294967296; }; }

  function forceLayout(nodes, links, W, H){
    const n = nodes.length; if(!n) return;
    const rnd = seeded(7919 + n);
    const k = Math.sqrt((W * H) / n) * 0.62;
    nodes.forEach((nd, i) => {
      const a = (i / n) * Math.PI * 2;
      nd.x = W / 2 + Math.cos(a) * W * 0.28 + (rnd() - .5) * 30;
      nd.y = H / 2 + Math.sin(a) * H * 0.28 + (rnd() - .5) * 30;
    });
    const idx = new Map(nodes.map((nd, i) => [nd.id, i]));
    const edges = links.map(l => [idx.get(l.source), idx.get(l.target)]);
    const iters = n > 160 ? 140 : 240;
    let temp = W / 8;
    const dx = new Float32Array(n), dy = new Float32Array(n);
    for(let it = 0; it < iters; it++){
      dx.fill(0); dy.fill(0);
      for(let i = 0; i < n; i++){
        for(let j = i + 1; j < n; j++){
          let ux = nodes[i].x - nodes[j].x, uy = nodes[i].y - nodes[j].y;
          let d2 = ux * ux + uy * uy; if(d2 < 0.01){ ux = rnd() - .5; uy = rnd() - .5; d2 = 0.25; }
          const d = Math.sqrt(d2), f = (k * k) / d;
          ux /= d; uy /= d;
          dx[i] += ux * f; dy[i] += uy * f; dx[j] -= ux * f; dy[j] -= uy * f;
        }
      }
      for(const [a, b] of edges){
        const ux = nodes[a].x - nodes[b].x, uy = nodes[a].y - nodes[b].y;
        const d = Math.sqrt(ux * ux + uy * uy) || 0.01, f = (d * d) / k;
        dx[a] -= (ux / d) * f; dy[a] -= (uy / d) * f; dx[b] += (ux / d) * f; dy[b] += (uy / d) * f;
      }
      for(let i = 0; i < n; i++){
        dx[i] += (W / 2 - nodes[i].x) * 0.04; dy[i] += (H / 2 - nodes[i].y) * 0.04;   // gentle gravity
        const d = Math.sqrt(dx[i] * dx[i] + dy[i] * dy[i]) || 0.01, m = Math.min(d, temp);
        nodes[i].x += (dx[i] / d) * m; nodes[i].y += (dy[i] / d) * m;
      }
      temp *= 0.975;
    }
    fit(nodes, W, H, 46);
  }

  function fit(nodes, W, H, pad){
    if(!nodes.length) return;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    nodes.forEach(n => { x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y); x1 = Math.max(x1, n.x); y1 = Math.max(y1, n.y); });
    const sw = Math.max(x1 - x0, 1), sh = Math.max(y1 - y0, 1);
    const s = Math.min((W - 2 * pad) / sw, (H - 2 * pad) / sh);
    const ox = (W - sw * s) / 2, oy = (H - sh * s) / 2;
    nodes.forEach(n => { n.x = ox + (n.x - x0) * s; n.y = oy + (n.y - y0) * s; });
  }

  function boxLayout(nodes, W, H){
    nodes.forEach(n => {
      const b = n.region.box;
      n.x = ((b.left + b.width / 2) / 100) * W;
      n.y = ((b.top + b.height / 2) / 100) * H;
    });
  }

  function radius(n){ return Math.min(17, 6 + n.deg * 1.5); }

  /* ---- render ---- */
  function render(host, model, opts){
    opts = opts || {};
    host.innerHTML = '';
    const showIsolated = !!opts.showIsolated;
    const aspect = opts.aspect && opts.aspect > 0 ? opts.aspect : 1.55;
    const W = 800, H = Math.max(340, Math.min(760, Math.round(W / aspect)));

    let nodes = model.nodes.filter(n => showIsolated || n.deg > 0);
    if(!nodes.length) nodes = model.nodes.slice();     // nothing is linked: show what exists rather than a blank canvas
    const nodeSet = new Set(nodes.map(n => n.id));
    const links = model.links.filter(l => nodeSet.has(l.source) && nodeSet.has(l.target));
    if(!nodes.length){
      host.innerHTML = '<div class="skel">No relationships to graph in this artifact.</div>';
      return { setActive(){}, setHover(){}, destroy(){} };
    }

    const useBoxes = model.kind === 'diagram' && nodes.every(n => n.region.box);
    nodes.forEach(n => { n.x = n.y = 0; });
    if(useBoxes){ boxLayout(nodes, W, H); } else { forceLayout(nodes, links, W, H); }
    const byId = new Map(nodes.map(n => [n.id, n]));

    const svg = el('svg', { viewBox:`0 0 ${W} ${H}`, class:'graph-svg', role:'img', 'aria-label':'Semantic graph of regions and their relationships' });
    const defs = el('defs', null, svg);
    const types = [...new Set(links.map(l => l.type))];
    types.forEach(t => {
      const m = el('marker', { id:'ga-' + hashStr(t), viewBox:'0 0 10 10', refX:'8', refY:'5', markerWidth:'7', markerHeight:'7', orient:'auto-start-reverse' }, defs);
      el('path', { d:'M0,0 L10,5 L0,10 z', fill:linkColor(t) }, m);
    });
    const gl = el('g', null, svg), gn = el('g', null, svg);

    const adj = new Map(nodes.map(n => [n.id, new Set()]));
    links.forEach(l => { adj.get(l.source).add(l.target); adj.get(l.target).add(l.source); });

    const linkEls = links.map((l, i) => {
      const p = el('path', { class:'gl draw flowing', stroke:linkColor(l.type), 'marker-end':`url(#ga-${hashStr(l.type)})`, 'data-region':l.regionId, pathLength:'1' }, gl);
      p.style.setProperty('--ld', Math.min(i * 22, 900) + 'ms');
      p.addEventListener('animationend', e => { if(e.animationName === 'draw'){ p.classList.remove('draw'); p.removeAttribute('pathLength'); } });
      const title = el('title', null, p); title.textContent = l.type + (l.label ? ' · ' + l.label : '');
      return p;
    });

    function placeLink(i){
      const l = links[i], a = byId.get(l.source), b = byId.get(l.target), p = linkEls[i];
      const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1;
      const ux = dx / d, uy = dy / d;
      const ra = radius(a) + 2, rb = radius(b) + 5;
      const x1 = a.x + ux * ra, y1 = a.y + uy * ra, x2 = b.x - ux * rb, y2 = b.y - uy * rb;
      const bend = Math.min(0.16, 26 / d) * d;
      const cx = (x1 + x2) / 2 - uy * bend, cy = (y1 + y2) / 2 + ux * bend;
      p.setAttribute('d', `M${x1.toFixed(1)},${y1.toFixed(1)} Q${cx.toFixed(1)},${cy.toFixed(1)} ${x2.toFixed(1)},${y2.toFixed(1)}`);
    }

    const nodeEls = new Map();
    nodes.forEach((n, i) => {
      const g = el('g', { class:'gn' + (n.deg >= 4 || nodes.length <= 14 ? ' hub' : ''), 'data-id':n.id, tabindex:'0', role:'button', 'aria-label':n.label }, gn);
      // outer <g> carries the position (transform attribute); the inner <g>
      // carries the entrance animation, so a CSS transform animation can't
      // clobber the translate.
      const inner = el('g', { class:'gi enter' }, g);
      inner.style.setProperty('--nd', Math.min(i * 18, 700) + 'ms');
      const low = n.region.confidence < 0.85;
      const col = colorFor(n.group);
      el('circle', { class:'halo', r:radius(n) + 5, fill:col, opacity:'0.14' }, inner);
      el('circle', { class:'core', r:radius(n), fill:col, 'fill-opacity':'0.9', stroke:low ? '#ff8d5c' : 'rgba(255,255,255,.55)', 'stroke-width':low ? 2.4 : 1.2 }, inner);
      const t = el('text', { y:radius(n) + 13, 'text-anchor':'middle' }, inner); t.textContent = n.label;
      nodeEls.set(n.id, g);
      n._g = g;
      g.setAttribute('transform', `translate(${n.x.toFixed(1)} ${n.y.toFixed(1)})`);
    });
    links.forEach((_, i) => placeLink(i));

    function clearHl(){
      svg.classList.remove('dim');
      nodeEls.forEach(g => g.classList.remove('hl'));
      linkEls.forEach(p => p.classList.remove('hl'));
    }
    function hl(id){
      clearHl(); if(!id || !byId.has(id)) return;
      svg.classList.add('dim');
      nodeEls.get(id).classList.add('hl');
      adj.get(id).forEach(nid => nodeEls.get(nid).classList.add('hl'));
      links.forEach((l, i) => { if(l.source === id || l.target === id) linkEls[i].classList.add('hl'); });
    }

    let activeId = opts.activeId || null;
    function setActive(id){
      activeId = id;
      nodeEls.forEach((g, nid) => g.classList.toggle('active', nid === id));
      if(id) hl(id); else clearHl();
    }

    // pointer: hover highlight, click select, drag to move
    let drag = null;
    nodeEls.forEach((g, id) => {
      g.addEventListener('pointerenter', () => { if(!drag){ hl(id); opts.onHover && opts.onHover(id); } });
      g.addEventListener('pointerleave', () => { if(!drag){ activeId ? hl(activeId) : clearHl(); opts.onHover && opts.onHover(null); } });
      g.addEventListener('keydown', e => { if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); opts.onSelect && opts.onSelect(id); } });
      g.addEventListener('pointerdown', e => {
        const n = byId.get(id), pt = toSvg(e);
        drag = { id, n, ox:pt.x - n.x, oy:pt.y - n.y, moved:false, sx:e.clientX, sy:e.clientY };
        g.setPointerCapture(e.pointerId);
      });
      g.addEventListener('pointermove', e => {
        if(!drag || drag.id !== id) return;
        if(!drag.moved && Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) < 4) return;
        drag.moved = true;
        const pt = toSvg(e), n = drag.n;
        n.x = Math.max(10, Math.min(W - 10, pt.x - drag.ox)); n.y = Math.max(10, Math.min(H - 10, pt.y - drag.oy));
        g.setAttribute('transform', `translate(${n.x.toFixed(1)} ${n.y.toFixed(1)})`);
        links.forEach((l, i) => { if(l.source === id || l.target === id) placeLink(i); });
      });
      g.addEventListener('pointerup', e => {
        if(drag && drag.id === id && !drag.moved) opts.onSelect && opts.onSelect(id);
        drag = null;
        try { g.releasePointerCapture(e.pointerId); } catch (err) {}
      });
    });
    function toSvg(e){
      const r = svg.getBoundingClientRect();
      return { x:(e.clientX - r.left) / r.width * W, y:(e.clientY - r.top) / r.height * H };
    }

    host.appendChild(svg);
    if(activeId) setActive(activeId);

    // legend: node groups and relationship types actually present
    const legend = document.createElement('div'); legend.className = 'graph-legend';
    const groups = [...new Set(nodes.map(n => n.group))].slice(0, 9);
    groups.forEach(gp => { const s = document.createElement('span'); s.innerHTML = `<i style="background:${colorFor(gp)}"></i>${escapeHtml(gp)}`; legend.appendChild(s); });
    types.forEach(t => { const s = document.createElement('span'); s.innerHTML = `<i class="ln" style="background:${linkColor(t)}"></i>${escapeHtml(t)}`; legend.appendChild(s); });
    host.appendChild(legend);

    return {
      setActive,
      setHover(id){ if(id) hl(id); else activeId ? hl(activeId) : clearHl(); },
      counts(){ return { nodes:nodes.length, links:links.length }; },
      destroy(){ host.innerHTML = ''; },
    };
  }

  function escapeHtml(s){ return String(s).replace(/[&<>"]/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c])); }

  /* ---- Mermaid export ---- */
  function toMermaid(model){
    const ids = new Map(model.nodes.map((n, i) => [n.id, 'n' + (i + 1)]));
    const esc = s => String(s == null ? '' : s).replace(/"/g, '#quot;').replace(/[\r\n]+/g, ' ').trim();
    const lines = ['flowchart TD'];
    model.nodes.forEach(n => {
      const id = ids.get(n.id), label = esc(n.label || n.id), shape = n.region.shape || '';
      if(shape === 'node-decision') lines.push(`  ${id}{"${label}"}`);
      else if(shape === 'node-initial') lines.push(`  ${id}(("${label === 'initial' ? 'start' : label}"))`);
      else if(shape === 'node-final') lines.push(`  ${id}((("${label === 'final' ? 'end' : label}")))`);
      else if(shape === 'node-circle') lines.push(`  ${id}(("${label}"))`);
      else if(shape === 'node-activity') lines.push(`  ${id}("${label}")`);
      else if(shape === 'fork-join') lines.push(`  ${id}[["${label === 'fork-join' ? 'fork / join' : label}"]]`);
      else lines.push(`  ${id}["${label}"]`);
    });
    model.links.forEach(l => {
      const a = ids.get(l.source), b = ids.get(l.target);
      if(!a || !b) return;
      if(l.type === 'connects-to') lines.push(l.label ? `  ${a} -->|"${esc(l.label)}"| ${b}` : `  ${a} --> ${b}`);
      else lines.push(`  ${a} -. "${esc(l.type)}" .-> ${b}`);
    });
    return lines.join('\n');
  }

  return { buildModel, render, toMermaid };
})();
