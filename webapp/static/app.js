/* Rendering logic adapted from design/preprocessing-pipeline-mock.html and
 * scripts/build_prototype.py's CHECKS_JS/renderTable/renderJson patches —
 * the same interaction pattern (tabs, region rows, checks strip, page-frame
 * region overlay), just driven by one `doc` object that arrives from a real
 * POST /api/analyze response instead of a hardcoded DOCS array or a value
 * baked in at generation time. This is exactly the swap ADR 0003 named as
 * the future step ("a change to how the DOCS array gets populated, not a
 * rewrite of the interaction logic itself").
 */

let doc = null;
let activeTab = 'text';
let activeRegionId = null;
let reconActiveSheet = null;

function escapeHtml(str){
  return String(str).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}

function setLiveStatus(state, label){
  const row = document.getElementById('statusRow');
  row.hidden = false;
  const el = document.getElementById('liveStatus');
  el.className = 'live-status' + (state ? ' ' + state : '');
  document.getElementById('liveStatusText').textContent = label;
}

function showError(message){
  const banner = document.getElementById('errorBanner');
  banner.hidden = false;
  banner.textContent = message;
  document.getElementById('result').hidden = true;
}

function clearError(){
  const banner = document.getElementById('errorBanner');
  banner.hidden = true;
  banner.textContent = '';
}

async function handleFile(file){
  clearError();
  document.getElementById('result').hidden = true;
  document.getElementById('statusFile').textContent = file.name;
  setLiveStatus('running', 'uploading and processing — real OCR/CV work, this can take a few seconds…');

  const formData = new FormData();
  formData.append('file', file);

  let resp, body;
  try{
    resp = await fetch('/api/analyze', { method:'POST', body: formData });
    body = await resp.json();
  }catch(err){
    setLiveStatus('error', 'request failed');
    showError('Could not reach the local server: ' + err.message);
    return;
  }

  if(!resp.ok || !body.ok){
    setLiveStatus('error', 'failed — see message below');
    showError(body.error || ('Request failed with status ' + resp.status));
    return;
  }

  setLiveStatus('done', 'done');
  doc = body.doc;
  activeRegionId = null;
  reconActiveSheet = null;
  renderResult();
}

function renderResult(){
  document.getElementById('result').hidden = false;
  document.getElementById('resultTitle').textContent = doc.title;
  document.getElementById('resultMeta').textContent = doc.pageLabel + ' · ' + doc.meta;
  document.getElementById('resultAdapter').textContent = doc.adapter;

  renderConfidenceStrip();
  renderChecks();
  renderPreview(true);
  renderActiveTab();
  renderRecon();
  resetAskAnswer();
}

/* ---------- Ask a question (real Groq-backed task-accuracy Q&A) ----------
 * Reuses qa/groq_qa.py via POST /api/ask — no Groq-calling logic here, this
 * is just the form/fetch/render plumbing. GET /api/ask/status is checked
 * once on load so the box shows an honest disabled state (never a request
 * that's silently doomed to fail) when GROQ_API_KEY isn't configured.
 */
let askAvailable = false;

async function initAsk(){
  const sub = document.getElementById('askSub');
  const input = document.getElementById('askInput');
  const submit = document.getElementById('askSubmit');
  try{
    const resp = await fetch('/api/ask/status');
    const body = await resp.json();
    askAvailable = !!body.available;
    sub.textContent = body.detail;
  }catch(err){
    askAvailable = false;
    sub.textContent = 'Could not reach the local server to check Groq configuration: ' + err.message;
  }
  input.disabled = !askAvailable;
  submit.disabled = !askAvailable;
  if(askAvailable){
    sub.textContent = 'Ask a real question about the artifact above — answered by Groq from its structured representation.';
  }

  document.getElementById('askForm').addEventListener('submit', async e => {
    e.preventDefault();
    if(!askAvailable || !doc) return;
    const question = input.value.trim();
    if(!question) return;

    const answerBox = document.getElementById('askAnswer');
    answerBox.hidden = false;
    answerBox.className = 'ask-answer pending';
    answerBox.textContent = 'Asking Groq…';
    submit.disabled = true;

    try{
      const resp = await fetch('/api/ask', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({question, artifact: doc.realArtifact}),
      });
      const body = await resp.json();
      if(!resp.ok || !body.ok){
        answerBox.className = 'ask-answer error';
        answerBox.textContent = body.error || ('Request failed with status ' + resp.status);
      } else {
        answerBox.className = 'ask-answer';
        answerBox.textContent = body.answer;
      }
    }catch(err){
      answerBox.className = 'ask-answer error';
      answerBox.textContent = 'Could not reach the local server: ' + err.message;
    } finally {
      submit.disabled = !askAvailable;
    }
  });
}

function resetAskAnswer(){
  const answerBox = document.getElementById('askAnswer');
  answerBox.hidden = true;
  answerBox.textContent = '';
  document.getElementById('askInput').value = '';
}

function renderConfidenceStrip(){
  const strip = document.getElementById('confidenceStrip');
  const tiers = {};
  doc.regions.forEach(r => { tiers[r.confidence] = (tiers[r.confidence] || 0) + 1; });
  const confs = Object.keys(tiers).map(Number).sort((a,b) => b - a);
  strip.innerHTML = confs.map(c => {
    const cls = c < 0.85 ? 'low' : 'high';
    return '<span class="conf-tier ' + cls + '">' + Math.round(c*100) + '% · ' + tiers[c] + ' region' + (tiers[c] !== 1 ? 's' : '') + '</span>';
  }).join('');
}

function renderChecks(){
  const strip = document.getElementById('checksStrip');
  if(!doc.checks || !doc.checks.length){ strip.innerHTML = ''; return; }
  strip.innerHTML = '<span class="checks-strip-label">Validation checks — artifact-level, independent of per-region confidence</span>' +
    doc.checks.map(c => {
      const cls = c.passed ? 'pass' : 'fail';
      const mark = c.passed ? '✓' : '✕';
      return '<div class="check-badge ' + cls + '"><span class="mark">' + mark + '</span>' +
        '<div class="body"><strong>' + escapeHtml(c.name) + '</strong><span>' + escapeHtml(c.detail) + '</span></div></div>';
    }).join('');
}

function renderPreview(runScan){
  const frame = document.getElementById('pageFrame');
  frame.querySelectorAll('.region, .preview-image, .preview-page-label').forEach(el => el.remove());

  // Real pixels from the file the user actually uploaded (doc.previewImages
  // — one entry for a diagram, one per page for a document), not just the
  // abstract confidence-colored boxes below. A spreadsheet has no bitmap of
  // itself; previewImages is null for that kind and the grid-bg fallback
  // (an approximation of "a spreadsheet," not the literal file) still runs.
  const images = doc.previewImages || [];
  const hasImages = images.length > 0;
  frame.classList.toggle('grid-bg', doc.kind === 'spreadsheet' && !hasImages);

  const sub = document.getElementById('previewSub');
  if(hasImages){
    sub.textContent = '— the actual uploaded file, with detected regions overlaid';
  } else if(doc.kind === 'spreadsheet'){
    sub.textContent = '— cell grid reconstructed from parsed values (no bitmap original to preview)';
  } else {
    sub.textContent = '— region boxes by type/confidence (no image preview available for this file)';
  }

  // A single image (a diagram, or a one-page document) gets the frame's own
  // aspect ratio so it isn't squished into the fixed portrait default.
  // Multiple document pages keep that fixed frame and stack inside it by
  // percentage — the exact layout doc.regions[].box already assumes for a
  // multi-page document (each page gets an equal vertical band; see
  // scripts/build_prototype.py's _compute_boxes_document), so page images
  // are placed at the same per-page band their regions' boxes already use,
  // not independently sized — otherwise a region's overlay box would land
  // on the wrong page's image.
  frame.style.aspectRatio = (images.length === 1) ? (images[0].width + ' / ' + images[0].height) : '';

  const bandHeight = hasImages ? 100 / images.length : 0;
  images.forEach((img, i) => {
    const imgEl = document.createElement('img');
    imgEl.className = 'preview-image';
    imgEl.src = img.dataUrl;
    imgEl.alt = img.pageLabel || 'Uploaded file preview';
    imgEl.style.top = (i * bandHeight) + '%';
    imgEl.style.height = bandHeight + '%';
    frame.appendChild(imgEl);
    if(img.pageLabel && images.length > 1){
      const label = document.createElement('div');
      label.className = 'preview-page-label';
      label.style.top = (i * bandHeight) + '%';
      label.textContent = img.pageLabel;
      frame.appendChild(label);
    }
  });

  const noPreview = document.getElementById('noPreview');
  const hasBoxes = doc.regions.some(r => r.box);
  noPreview.hidden = hasImages || hasBoxes;

  doc.regions.forEach(r => {
    if(!r.box) return;
    const el = document.createElement('div');
    let cls = 'region';
    if(r.confidence < 0.85) cls += ' low';
    if(r.id === activeRegionId) cls += ' active';
    if(r.shape) cls += ' shape-' + r.shape;
    el.className = cls;
    el.style.top = r.box.top + '%';
    el.style.left = r.box.left + '%';
    el.style.width = r.box.width + '%';
    el.style.height = r.box.height + '%';
    el.title = r.typeLabel;
    el.setAttribute('role', 'button');
    el.setAttribute('tabindex', '0');
    el.setAttribute('aria-label', r.typeLabel + (r.text ? ': ' + r.text : ' (non-text region)'));
    const tag = document.createElement('span');
    tag.className = 'region-label';
    tag.textContent = r.typeLabel;
    el.appendChild(tag);
    el.addEventListener('click', () => selectRegion(r.id));
    el.addEventListener('keydown', e => { if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); selectRegion(r.id); } });
    frame.appendChild(el);
  });

  if(runScan){
    const line = document.getElementById('scanline');
    line.classList.remove('run');
    void line.offsetWidth;
    line.classList.add('run');
  }
}

/* ---------- Reconstruction view ----------
 * A genuine visual redraw per artifact kind, built entirely from data
 * already present in the artifact JSON (region id/type/text/shape/
 * relationships, plus the same re-derived `box` percentages the abstract
 * preview above already uses) — no new adapter/backend work. This is
 * additive: the abstract box-overlay preview above stays exactly as it was,
 * for confidence/type inspection; this view answers "what did the pipeline
 * think this file actually looks like".
 */
function renderRecon(){
  const view = document.getElementById('reconView');
  if(!doc){ view.innerHTML = ''; return; }
  if(doc.kind === 'spreadsheet'){
    view.innerHTML = reconSpreadsheetHtml(doc.realArtifact.regions);
  } else if(doc.kind === 'diagram'){
    view.innerHTML = reconDiagramHtml(doc.regions);
  } else if(doc.kind === 'document'){
    view.innerHTML = reconDocumentHtml(doc.realArtifact.regions);
  } else {
    view.innerHTML = '<div class="recon-empty">No reconstruction view is defined for artifact kind "' + escapeHtml(doc.kind) + '".</div>';
  }
  bindReconEvents();
}

function bindReconEvents(){
  document.querySelectorAll('.recon-sheet-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      reconActiveSheet = btn.dataset.sheet;
      renderRecon();
    });
  });
  // Diagram reconstruction: clicking a shape (or its invisible wider hit
  // target, .rc-hit, for thin edge lines) highlights the same region
  // everywhere else in the page — the abstract preview overlay and the
  // structured tab both already key off activeRegionId.
  document.querySelectorAll('.recon-diagram [data-region-id]').forEach(el => {
    el.setAttribute('tabindex', '0');
    el.addEventListener('click', () => selectRegion(el.dataset.regionId));
    el.addEventListener('keydown', e => {
      if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); selectRegion(el.dataset.regionId); }
    });
  });
}

/* -- spreadsheet: a real HTML grid, cells placed by parsing each region's
 * id ("Sheet1!C5") back into a row/column, exactly the coordinate the
 * spreadsheet adapter encoded it from. -- */
function colLettersToIndex(letters){
  let n = 0;
  for(const ch of letters.toUpperCase()) n = n * 26 + (ch.charCodeAt(0) - 64);
  return n;
}
function colIndexToLetters(n){
  let s = '';
  while(n > 0){
    const rem = (n - 1) % 26;
    s = String.fromCharCode(65 + rem) + s;
    n = Math.floor((n - 1) / 26);
  }
  return s;
}

function reconSpreadsheetHtml(regions){
  const sheets = new Map();
  regions.forEach(r => {
    const bang = r.id.indexOf('!');
    if(bang === -1) return;
    const sheet = r.id.slice(0, bang);
    const ref = r.id.slice(bang + 1);
    const m = ref.match(/^([A-Za-z]+)(\d+)$/);
    if(!m) return;
    const col = colLettersToIndex(m[1]);
    const row = parseInt(m[2], 10);
    if(!sheets.has(sheet)) sheets.set(sheet, { cells: new Map(), maxRow: 0, maxCol: 0 });
    const s = sheets.get(sheet);
    s.cells.set(row + ':' + col, r);
    s.maxRow = Math.max(s.maxRow, row);
    s.maxCol = Math.max(s.maxCol, col);
  });
  if(!sheets.size) return '<div class="recon-empty">No cell regions with a recognizable sheet!ref id to reconstruct.</div>';

  const sheetNames = [...sheets.keys()];
  if(!sheetNames.includes(reconActiveSheet)) reconActiveSheet = sheetNames[0];

  const tabsHtml = sheetNames.length > 1
    ? '<div class="recon-sheet-tabs" role="tablist" aria-label="Sheet">' + sheetNames.map(name =>
        '<button type="button" class="recon-sheet-tab" role="tab" aria-selected="' + (name === reconActiveSheet ? 'true' : 'false') +
        '" data-sheet="' + escapeHtml(name) + '">' + escapeHtml(name) + '</button>'
      ).join('') + '</div>'
    : '';

  const s = sheets.get(reconActiveSheet);
  const rowsHtml = [];
  for(let row = 1; row <= s.maxRow; row++){
    let titleCell = null;
    const rowCells = [];
    for(let col = 1; col <= s.maxCol; col++){
      const region = s.cells.get(row + ':' + col) || null;
      if(region && region.type === 'title' && !titleCell) titleCell = region;
      rowCells.push(region);
    }
    if(titleCell){
      rowsHtml.push('<tr><td class="row-head">' + row + '</td><td class="cell-title" colspan="' + s.maxCol + '">' +
        escapeHtml(titleCell.text || '') + '</td></tr>');
      continue;
    }
    rowsHtml.push('<tr><td class="row-head">' + row + '</td>' + rowCells.map(reconSpreadsheetCellHtml).join('') + '</tr>');
  }

  const colHeads = '<th class="corner"></th>' +
    Array.from({ length: s.maxCol }, (_, i) => '<th class="col-head">' + colIndexToLetters(i + 1) + '</th>').join('');

  return tabsHtml +
    '<div class="recon-grid-wrap"><table class="recon-grid"><thead><tr>' + colHeads + '</tr></thead><tbody>' +
    rowsHtml.join('') + '</tbody></table></div>';
}

function reconSpreadsheetCellHtml(region){
  if(!region) return '<td class="cell-empty"></td>';
  const lowCls = region.confidence < 0.85 ? ' cell-low' : '';
  let cls = '', content = escapeHtml(region.text || ''), titleAttr = '';
  if(region.type === 'header'){
    cls = 'cell-header';
  } else if(region.type === 'formula-cell'){
    const text = region.text || '';
    const isUnresolved = text.includes('unresolved');
    const value = text.includes(' → ') ? text.split(' → ', 2)[1] : text;
    cls = isUnresolved ? 'cell-formula-bad' : 'cell-formula';
    content = escapeHtml(isUnresolved ? '#REF!' : value);
    titleAttr = ' title="' + escapeHtml(text) + '"';
  } else if(region.type === 'value'){
    cls = 'cell-value';
  }
  return '<td class="' + cls + lowCls + '"' + titleAttr + '>' + content + '</td>';
}

/* -- diagram: a real SVG redraw from each node's re-derived pixel box
 * (percent of the source image) and each edge's `relationships`
 * (connects-to: [from, to]) — real geometry and real topology, not just
 * "some shapes were found". The common schema doesn't carry the source
 * image's own pixel dimensions (position is a UI concern, not semantic
 * content — see adapters/diagram/adapter.py), so a fixed 4:3 landscape
 * viewBox is used as a reasonable default aspect for diagrams generally;
 * box positions/sizes are real either way, only the overall aspect can be
 * slightly off for an unusually tall/wide source image. -- */
function reconDiagramHtml(regions){
  const DIAGRAM_VIEW_W = 400 / 3; // 4:3 landscape default, view height fixed at 100
  const sx = v => v * (DIAGRAM_VIEW_W / 100);

  const nodes = regions.filter(r => r.type === 'node' && r.box);
  const edges = regions.filter(r => r.type === 'edge');
  if(!nodes.length) return '<div class="recon-empty">No node geometry available to reconstruct — see the structured tab for the raw region list.</div>';

  const svgBox = {};
  const nodeById = {};
  nodes.forEach(n => {
    svgBox[n.id] = { left: sx(n.box.left), top: n.box.top, width: sx(n.box.width), height: n.box.height };
    nodeById[n.id] = n;
  });

  function center(b){ return { x: b.left + b.width / 2, y: b.top + b.height / 2 }; }

  function clipToBox(from, to, box){
    const dx = to.x - from.x, dy = to.y - from.y;
    const hx = box.width / 2, hy = box.height / 2;
    const tx = dx !== 0 ? hx / Math.abs(dx) : Infinity;
    const ty = dy !== 0 ? hy / Math.abs(dy) : Infinity;
    const t = Math.min(tx, ty, 1);
    return { x: from.x + t * dx, y: from.y + t * dy };
  }

  function shortLabel(text){
    if(!text) return null;
    const trimmed = text.trim();
    return trimmed.length > 18 ? trimmed.slice(0, 17) + '…' : trimmed;
  }

  let shapesSvg = '';
  nodes.forEach(n => {
    const box = svgBox[n.id];
    const c = center(box);
    const lowCls = n.confidence < 0.85 ? ' low' : '';
    const activeCls = n.id === activeRegionId ? ' active' : '';
    const idAttr = ' data-region-id="' + escapeHtml(n.id) + '"';
    if(n.shape === 'node-decision'){
      const hx = box.width / 2, hy = box.height / 2;
      const pts = [[c.x, c.y - hy], [c.x + hx, c.y], [c.x, c.y + hy], [c.x - hx, c.y]]
        .map(p => p[0].toFixed(2) + ',' + p[1].toFixed(2)).join(' ');
      shapesSvg += '<polygon class="rc-node' + lowCls + activeCls + '"' + idAttr + ' points="' + pts + '"/>';
    } else if(n.shape === 'node-circle'){
      shapesSvg += '<ellipse class="rc-node' + lowCls + activeCls + '"' + idAttr + ' cx="' + c.x.toFixed(2) + '" cy="' + c.y.toFixed(2) +
        '" rx="' + (box.width / 2).toFixed(2) + '" ry="' + (box.height / 2).toFixed(2) + '"/>';
    } else {
      shapesSvg += '<rect class="rc-node' + lowCls + activeCls + '"' + idAttr + ' x="' + box.left.toFixed(2) + '" y="' + box.top.toFixed(2) +
        '" width="' + box.width.toFixed(2) + '" height="' + box.height.toFixed(2) + '" rx="1.4"/>';
    }
    const label = shortLabel(n.text);
    if(label){
      const titleTag = label !== n.text.trim() ? '<title>' + escapeHtml(n.text) + '</title>' : '';
      shapesSvg += '<text class="rc-label" x="' + c.x.toFixed(2) + '" y="' + c.y.toFixed(2) + '" font-size="3.4">' +
        titleTag + escapeHtml(label) + '</text>';
    }
  });

  let edgesSvg = '';
  edges.forEach(e => {
    const targets = (e.relationships || []).filter(r => r.type === 'connects-to').map(r => r.target);
    if(targets.length < 2) return;
    const fromBox = svgBox[targets[0]], toBox = svgBox[targets[1]];
    if(!fromBox || !toBox) return;
    const fromC = center(fromBox), toC = center(toBox);
    const start = clipToBox(fromC, toC, fromBox);
    const end = clipToBox(toC, fromC, toBox);
    const lowCls = e.confidence < 0.85 ? ' low' : '';
    const activeCls = e.id === activeRegionId ? ' active' : '';
    const idAttr = ' data-region-id="' + escapeHtml(e.id) + '"';
    const marker = e.confidence < 0.85 ? 'rc-arrow-low' : (e.id === activeRegionId ? 'rc-arrow-active' : 'rc-arrow');
    const coords = ' x1="' + start.x.toFixed(2) + '" y1="' + start.y.toFixed(2) + '" x2="' + end.x.toFixed(2) + '" y2="' + end.y.toFixed(2) + '"';
    // A wide, invisible line drawn first (so it never blocks the visible
    // stroke) makes the thin real edge line actually clickable/hoverable —
    // a 1.8px stroke is otherwise a frustratingly small hit target.
    edgesSvg += '<line class="rc-hit"' + idAttr + coords + '/>';
    edgesSvg += '<line class="rc-edge' + lowCls + activeCls + '"' + idAttr + coords + ' marker-end="url(#' + marker + ')"/>';
    const label = shortLabel(e.text);
    if(label){
      const mx = (start.x + end.x) / 2, my = (start.y + end.y) / 2;
      const w = Math.max(8, label.length * 1.9);
      edgesSvg += '<rect class="rc-edge-label-bg" x="' + (mx - w / 2).toFixed(2) + '" y="' + (my - 2.6).toFixed(2) +
        '" width="' + w.toFixed(2) + '" height="4.4"/>';
      edgesSvg += '<text class="rc-edge-label" x="' + mx.toFixed(2) + '" y="' + (my + 1).toFixed(2) + '">' +
        escapeHtml(label) + '</text>';
    }
  });

  const defs = '<defs>' +
    '<marker id="rc-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="2" markerHeight="2" orient="auto">' +
    '<path d="M0,0 L10,5 L0,10 z" fill="var(--ink-soft)"/></marker>' +
    '<marker id="rc-arrow-low" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="2" markerHeight="2" orient="auto">' +
    '<path d="M0,0 L10,5 L0,10 z" fill="var(--flag)"/></marker>' +
    '<marker id="rc-arrow-active" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="2" markerHeight="2" orient="auto">' +
    '<path d="M0,0 L10,5 L0,10 z" fill="var(--accent)"/></marker>' +
    '</defs>';

  const legend = '<div class="recon-legend">' +
    '<span><span class="swatch" style="background:var(--accent-soft);border:1.5px solid var(--accent)"></span>high confidence</span>' +
    '<span><span class="swatch" style="background:var(--flag-soft);border:1.5px solid var(--flag)"></span>low confidence · review</span>' +
    '<span>click a shape to see it in the structured tab</span>' +
    '</div>';

  return '<div class="recon-diagram" style="aspect-ratio:' + DIAGRAM_VIEW_W + '/100">' +
    '<svg viewBox="0 0 ' + DIAGRAM_VIEW_W.toFixed(2) + ' 100" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">' +
    defs + edgesSvg + shapesSvg + '</svg></div>' + legend;
}

/* -- document: regions replayed in their real reading order (the order the
 * adapter yields them, page then in-page order — no reordering here), laid
 * out as a page with type-specific styling instead of raw tab rows. -- */
function reconDocumentHtml(regions){
  if(!regions.length) return '<div class="recon-empty">No regions to reconstruct.</div>';

  const pages = [];
  let current = null;
  regions.forEach(r => {
    const m = r.id.match(/^page(\d+)!/);
    const pageNo = m ? m[1] : null;
    if(!current || current.pageNo !== pageNo){
      current = { pageNo, regions: [] };
      pages.push(current);
    }
    current.regions.push(r);
  });

  return '<div class="recon-doc">' + pages.map(p =>
    '<div class="recon-page">' +
    (p.pageNo ? '<div class="recon-page-label">Page ' + escapeHtml(p.pageNo) + '</div>' : '') +
    p.regions.map(reconDocBlockHtml).join('') +
    '</div>'
  ).join('') + '</div>';
}

function reconDocBlockHtml(r){
  const lowCls = r.confidence < 0.85 ? ' low' : '';
  const typeClass = 'recon-block-' + r.type;
  if(r.type === 'scanned-page-unprocessed'){
    return '<div class="recon-block ' + typeClass + lowCls + '">Scanned page — OCR engine unavailable, this page could not be processed.</div>';
  }
  const text = r.text ? escapeHtml(r.text) : '<span class="native">(non-text region)</span>';
  return '<div class="recon-block ' + typeClass + lowCls + '">' + text + '</div>';
}

/* ---------- Downloads ----------
 * schema.json / text.txt / structured.json / table.json — all four are
 * already fully available client-side once /api/analyze returns (the same
 * shapes run_adapter.py writes to disk: to_json/to_text/to_structured/
 * to_table over the real artifact), so these are built as Blobs in the
 * browser rather than adding a new backend endpoint per format.
 */
function buildTextOutput(artifact){
  return artifact.regions.map(r => r.text ? r.text : '[' + r.type + ' — excluded, non-text]').join('\n');
}
function buildStructuredOutput(artifact){
  return JSON.stringify(artifact.regions, null, 2);
}
function buildTableOutput(){
  // Matches adapters.common.outputs.to_table's exact shape (the same
  // object run_adapter.py serializes to table.json): empty_reason is only
  // present at all when there's nothing tabular to show.
  const out = { columns: doc.tableColumns, rows: doc.table };
  if(doc.tableEmptyReason) out.empty_reason = doc.tableEmptyReason;
  return JSON.stringify(out, null, 2);
}
function buildSchemaOutput(artifact){
  return JSON.stringify(artifact, null, 2);
}

function downloadBlob(filename, content, mime){
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function initDownloadMenu(){
  const toggle = document.getElementById('downloadToggle');
  const list = document.getElementById('downloadList');

  toggle.addEventListener('click', e => {
    e.stopPropagation();
    const willShow = list.hidden;
    list.hidden = !willShow;
    toggle.setAttribute('aria-expanded', String(willShow));
  });
  list.addEventListener('click', e => e.stopPropagation());
  document.addEventListener('click', () => {
    list.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
  });

  list.querySelectorAll('button[data-format]').forEach(btn => {
    btn.addEventListener('click', () => {
      if(!doc) return;
      const stem = doc.id || 'output';
      const fmt = btn.dataset.format;
      let filename, content, mime = 'application/json';
      if(fmt === 'schema'){
        filename = stem + '.schema.json';
        content = buildSchemaOutput(doc.realArtifact);
      } else if(fmt === 'text'){
        filename = stem + '.text.txt';
        content = buildTextOutput(doc.realArtifact);
        mime = 'text/plain';
      } else if(fmt === 'structured'){
        filename = stem + '.structured.json';
        content = buildStructuredOutput(doc.realArtifact);
      } else if(fmt === 'table'){
        filename = stem + '.table.json';
        content = buildTableOutput();
      } else {
        return;
      }
      downloadBlob(filename, content, mime);
      list.hidden = true;
      toggle.setAttribute('aria-expanded', 'false');
    });
  });
}

function selectRegion(id){
  activeRegionId = (activeRegionId === id) ? null : id;
  renderPreview(false);
  renderActiveTab();
  if(doc.kind === 'diagram') renderRecon();
}

function confBadge(c){
  const pct = Math.round(c * 100) + '%';
  const cls = c < 0.85 ? 'conf-low' : 'conf-high';
  return '<span class="badge ' + cls + '">' + pct + (c < 0.85 ? ' · review' : '') + '</span>';
}

function renderText(){
  const panel = document.getElementById('panel-text');
  const lines = doc.regions.map(r => r.text ? r.text : '[' + r.typeLabel + ' — excluded, non-text]').join('\n');
  panel.innerHTML = '<span class="muted">' + doc.pageLabel + ' · flattened for search indexing</span>\n\n' + escapeHtml(lines);
}

function renderStructured(){
  const panel = document.getElementById('panel-structured');
  panel.innerHTML = '';
  doc.regions.forEach(r => {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'region-row' + (r.confidence < 0.85 ? ' low' : '') + (r.id === activeRegionId ? ' active' : '');
    const langBadge = r.lang ? '<span class="badge lang">' + r.lang + '</span>' : '';
    row.innerHTML =
      '<div class="row-text">' + (r.text ? escapeHtml(r.text) : '<span class="native">non-text region</span>') +
      '<span class="native">' + r.typeLabel + '</span></div>' +
      '<div class="badge-col">' + langBadge + confBadge(r.confidence) + '</div>';
    row.addEventListener('click', () => selectRegion(r.id));
    panel.appendChild(row);
  });
}

function renderJson(){
  const panel = document.getElementById('panel-json');
  let text = JSON.stringify(doc.realArtifact, null, 2);
  text = escapeHtml(text)
    .replace(/"([a-zA-Z_]+)":/g, '<span class="k">"$1"</span>:')
    .replace(/: "([^"]*)"/g, ': <span class="s">"$1"</span>');
  panel.innerHTML = text;
}

function renderTable(){
  const panel = document.getElementById('panel-table');
  if(!doc.table || !doc.table.length){
    panel.innerHTML = '<div class="empty-state">' + (doc.tableEmptyReason || 'No tabular data in this artifact.') + '<br>Table export stays empty rather than guessing.</div>';
    return;
  }
  const cols = doc.tableColumns;
  const rows = doc.table.map(row => '<tr>' + cols.map(c => {
    let v = row[c];
    if(c === 'confidence') v = Math.round(v*100) + '%';
    if(v === null || v === undefined) v = '—';
    return '<td>' + escapeHtml(String(v)) + '</td>';
  }).join('') + '</tr>').join('');
  panel.innerHTML = '<table class="extract-table"><thead><tr>'+cols.map(h=>'<th>'+h+'</th>').join('')+'</tr></thead><tbody>'+rows+'</tbody></table>';
}

function renderActiveTab(){
  renderText();
  renderStructured();
  renderJson();
  renderTable();
}

function initTabs(){
  const buttons = document.querySelectorAll('.tab-btn');
  buttons.forEach(btn => {
    btn.addEventListener('click', () => {
      activeTab = btn.dataset.tab;
      buttons.forEach(b => b.setAttribute('aria-selected', b === btn ? 'true' : 'false'));
      ['text','structured','json','table'].forEach(name => {
        document.getElementById('panel-' + name).hidden = (name !== activeTab);
      });
    });
  });
}

function initUpload(){
  const dropzone = document.getElementById('dropzone');
  const input = document.getElementById('fileInput');

  dropzone.addEventListener('click', () => input.click());
  dropzone.addEventListener('keydown', e => {
    if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); input.click(); }
  });
  input.addEventListener('change', () => {
    if(input.files[0]) handleFile(input.files[0]);
  });

  ['dragenter','dragover'].forEach(evt => {
    dropzone.addEventListener(evt, e => {
      e.preventDefault(); e.stopPropagation();
      dropzone.classList.add('drag-over');
    });
  });
  ['dragleave','drop'].forEach(evt => {
    dropzone.addEventListener(evt, e => {
      e.preventDefault(); e.stopPropagation();
      dropzone.classList.remove('drag-over');
    });
  });
  dropzone.addEventListener('drop', e => {
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    if(file) handleFile(file);
  });
}

initTabs();
initUpload();
initAsk();
initDownloadMenu();
