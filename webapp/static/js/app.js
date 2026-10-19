/* nexDoc workspace.
 *
 * Flow: a file goes to POST /api/analyze/stream, which returns NDJSON events
 * (webapp/engine.py): start → sniffed → preview → geometry (diagrams) →
 * region* → done → xray. Events are queued and replayed through a small
 * pacing pump so that even a burst (cached result, a spreadsheet) animates
 * visibly instead of snapping in, while a genuinely slow stream (OCR) just
 * flows at the speed it arrives.
 *
 * The two panes are linked: hovering/selecting a region in the source
 * preview, the live stream, the reconstruction, or the graph highlights it
 * in all of them. Everything shown is real adapter output.
 */
(function(){
'use strict';

const $ = id => document.getElementById(id);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const LOW = 0.85;
const isLow = r => r.confidence < LOW;
function esc(str){ return String(str == null ? '' : str).replace(/[&<>"]/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c])); }

const S = {
  runToken:0, queue:[], pumping:false,
  kind:null, filename:'', regions:[], byId:new Map(), geometry:{}, images:[], doc:null,
  xray:null, xrayState:'idle', timings:null, phase:'idle', startedAt:0, timer:null,
  activeId:null, hoverId:null, mode:'source', zoom:1, tab:'stream', showLabels:false, showIsolated:false,
  hosts:new Map(), boxEls:new Map(), rowEls:new Map(), reconSheet:null, graphCtl:null, graphModel:null, graphDirty:true,
  sawResult:false, abort:null,
};

/* ============================ toast ============================ */
let toastTimer = null;
function toast(msg){
  const t = $('toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 2200);
}

/* ============================ engine chip ============================ */
async function pollEngine(){
  let next = 4000;
  try{
    const j = await (await fetch('/api/engine')).json();
    const chip = $('engineChip'), o = j.ocr;
    chip.dataset.state = o.state;
    chip.title = o.detail || 'The OCR model loads once in the background when the server starts';
    if(o.state === 'warming'){ $('engineText').textContent = 'OCR engine · warming up' + (o.elapsedSeconds != null ? ' · ' + Math.round(o.elapsedSeconds) + 's' : '…'); next = 1200; }
    else if(o.state === 'ready'){ $('engineText').textContent = 'OCR engine · ready' + (o.loadSeconds ? ' · loaded in ' + o.loadSeconds + 's' : ''); next = 0; }
    else if(o.state === 'unavailable'){ $('engineText').textContent = 'OCR engine · unavailable — shapes & spreadsheets still work'; next = 0; }
    else { $('engineText').textContent = 'OCR engine · starting…'; next = 1200; }
  }catch(err){ $('engineText').textContent = 'OCR engine · server unreachable'; }
  if(next) setTimeout(pollEngine, next);
}

/* ============================ samples ============================ */
async function initSamples(){
  const row = $('sampleRow');
  try{
    const { samples } = await (await fetch('/api/samples')).json();
    row.innerHTML = '';
    samples.forEach(s => {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'sample'; b.dataset.kind = s.kind; b.title = s.blurb;
      b.innerHTML = '<span class="sk"></span>' + esc(s.label) + '<small>' + esc(s.filename.split('.').pop()) + '</small>';
      b.addEventListener('click', async () => {
        b.classList.add('loading');
        try{
          const blob = await (await fetch('/api/samples/' + encodeURIComponent(s.id))).blob();
          await handleFile(new File([blob], s.filename, { type: blob.type || 'application/octet-stream' }));
        }finally{ b.classList.remove('loading'); }
      });
      row.appendChild(b);
    });
  }catch(err){ row.textContent = 'Samples unavailable.'; }
}

/* ============================ upload ============================ */
function initUpload(){
  const dz = $('dropzone'), input = $('fileInput');
  dz.addEventListener('click', () => input.click());
  dz.addEventListener('keydown', e => { if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); input.click(); } });
  input.addEventListener('change', () => { if(input.files[0]) handleFile(input.files[0]); input.value = ''; });

  // Accept a drop anywhere on the page (otherwise the browser would navigate to the file).
  let depth = 0;
  window.addEventListener('dragenter', e => { e.preventDefault(); depth++; dz.classList.add('drag-over'); });
  window.addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if(!depth) dz.classList.remove('drag-over'); });
  window.addEventListener('dragover', e => e.preventDefault());
  window.addEventListener('drop', e => {
    e.preventDefault(); depth = 0; dz.classList.remove('drag-over');
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if(f) handleFile(f);
  });
}

function showError(msg){
  const b = $('errorBanner'); b.hidden = false; b.textContent = msg;
}
function clearError(){ const b = $('errorBanner'); b.hidden = true; b.textContent = ''; }

async function handleFile(file){
  const token = ++S.runToken;
  if(S.abort) S.abort.abort();             // stop reading the previous file's stream
  S.abort = new AbortController();
  resetRun(file.name);
  const form = new FormData(); form.append('file', file);
  let resp;
  try{
    resp = await fetch('/api/analyze/stream', { method:'POST', body:form, signal:S.abort.signal });
  }catch(err){
    if(err.name === 'AbortError') return;
    return failRun(token, 'Could not reach the local server: ' + err.message);
  }
  if(!resp.ok || !resp.body){
    let msg = 'Request failed with status ' + resp.status;
    try{ const j = await resp.json(); if(j.error) msg = j.error; }catch(e){}
    return failRun(token, msg);
  }
  const reader = resp.body.getReader(), dec = new TextDecoder();
  let buf = '';
  try{
    for(;;){
      const { value, done } = await reader.read();
      if(done) break;
      buf += dec.decode(value, { stream:true });
      let i;
      while((i = buf.indexOf('\n')) >= 0){
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        if(line.trim()) enqueue(token, JSON.parse(line));
      }
    }
    if(buf.trim()) enqueue(token, JSON.parse(buf));
  }catch(err){
    if(err.name === 'AbortError') return;
    failRun(token, 'The result stream was interrupted: ' + err.message);
  }
}

function failRun(token, msg){
  if(token !== S.runToken) return;
  enqueue(token, { event:'error', message:msg });
}

/* ============================ event pump ============================ */
function enqueue(token, ev){ S.queue.push({ token, ev }); if(!S.pumping) pump(); }
async function pump(){
  S.pumping = true;
  while(S.queue.length){
    const { token, ev } = S.queue.shift();
    if(token !== S.runToken) continue;
    try{ handleEvent(ev); }catch(err){ console.error('event handler failed', ev.event, err); }
    if(ev.event === 'region'){
      // A burst drains fast (<~1.3s total); a slow trickle isn't held back.
      await sleep(Math.max(6, Math.min(70, 1400 / (S.queue.length + 8))));
    } else if(ev.event === 'preview' || ev.event === 'geometry'){
      await sleep(120);
    }
  }
  S.pumping = false;
}

function handleEvent(ev){
  switch(ev.event){
    case 'start':
      setStep('sniff', 'active');
      break;
    case 'note':
      $('streamHead').dataset.note = ev.message;
      document.querySelector('#tracker li[data-step="extract"] em').textContent = ev.message;
      renderStreamHead();
      break;
    case 'queued':
      $('streamHead').dataset.note = 'waiting for another analysis to finish…';
      renderStreamHead();
      break;
    case 'sniffed':
      S.kind = ev.kind;
      setStep('sniff', 'done', ev.ms); setStep('route', 'done', 0); setStep('extract', 'active');
      $('resultAdapter').textContent = ev.kind + ' adapter';
      $('resultMeta').textContent = 'routed by content sniffing · streaming…';
      document.body.dataset.kind = ev.kind;
      break;
    case 'preview':
      S.images = ev.images || [];
      buildSourceFrame();
      break;
    case 'geometry':
      S.geometry = ev.boxes || {};
      addGhosts();
      break;
    case 'region': addRegion(ev); break;
    case 'done': finishRun(ev); break;
    case 'xray':
      S.xray = ev.xray; S.xrayState = ev.xray ? 'ready' : 'unavailable';
      renderXray();
      break;
    case 'error':
      stopTimer();
      S.phase = 'error';
      const active = document.querySelector('#tracker li.active');
      if(active) active.className = 'error';
      showError(ev.message);
      $('scanline').classList.remove('run');
      $('streamHead').dataset.note = '';
      renderStreamHead();
      break;
  }
}

/* ============================ run lifecycle ============================ */
function resetRun(name){
  stopTimer();
  Object.assign(S, { kind:null, filename:name, regions:[], byId:new Map(), geometry:{}, images:[], doc:null, xray:null, xrayState:'pending',
    timings:null, phase:'extracting', activeId:null, hoverId:null, graphCtl:null, graphModel:null, graphDirty:true, reconSheet:null });
  S.hosts = new Map(); S.boxEls = new Map(); S.rowEls = new Map(); S.queue = [];
  clearError();
  $('tracker').hidden = false;
  document.querySelectorAll('#tracker li').forEach(li => { li.className = ''; li.querySelector('.ms').textContent = ''; });
  document.querySelector('#tracker li[data-step="extract"] em').textContent = 'native structure';
  $('streamHead').dataset.note = '';
  $('tracker').style.setProperty('--prog', 0);
  $('result').hidden = false;
  $('resultTitle').textContent = name.replace(/\.[^.]+$/, '');
  $('resultMeta').textContent = 'uploading…';
  $('resultAdapter').textContent = '…';
  $('resultTimings').hidden = true;
  $('confidenceStrip').innerHTML = ''; $('checksStrip').innerHTML = '';
  $('srcFile').textContent = name;
  $('feed').innerHTML = '';
  $('streamCount').textContent = '0';
  ['panel-graph', 'panel-xray', 'panel-table', 'panel-json', 'panel-text'].forEach(id => { $(id).innerHTML = ''; });
  $('reconView').innerHTML = '';
  $('pageFrame').querySelectorAll('[data-dyn]').forEach(n => n.remove());
  $('pageFrame').className = 'page-frame';
  $('pageFrame').style.aspectRatio = '';
  $('noPreview').hidden = true;
  setMode('source', true);
  resetAsk();
  S.startedAt = performance.now();
  S.timer = setInterval(renderStreamHead, 120);
  renderStreamHead();
  renderXray();
  if(!S.sawResult){ S.sawResult = true; }
  $('tracker').scrollIntoView({ behavior:'smooth', block:'center' });
}

function stopTimer(){ if(S.timer){ clearInterval(S.timer); S.timer = null; } }

function setStep(step, state, ms){
  const li = document.querySelector('#tracker li[data-step="' + step + '"]');
  if(!li) return;
  li.className = state;
  const tag = li.querySelector('.ms');
  if(ms != null) tag.textContent = ms === 'cached' ? 'cached' : (ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : ms + 'ms');
  const done = document.querySelectorAll('#tracker li.done').length;
  $('tracker').style.setProperty('--prog', Math.min(1, done / 4));
}

function renderStreamHead(){
  const head = $('streamHead');
  const n = S.regions.length;
  const note = head.dataset.note;
  if(S.phase === 'extracting'){
    const secs = ((performance.now() - S.startedAt) / 1000).toFixed(1);
    head.innerHTML = '<span><span class="live-dot"></span>' + (note ? esc(note) : 'streaming · ' + n + ' region' + (n === 1 ? '' : 's')) + '</span><span class="bar indet"><i></i></span><span>' + secs + 's</span>';
  } else if(S.phase === 'done'){
    const t = S.timings;
    head.innerHTML = '<span>✓ ' + n + ' region' + (n === 1 ? '' : 's') + '</span><span class="bar"><i style="--p:1"></i></span><span>' + (t && t.cached ? 'cached' : (t ? (t.totalMs / 1000).toFixed(1) + 's' : '')) + '</span>';
  } else if(S.phase === 'error'){
    head.innerHTML = '<span style="color:var(--flag)">✕ stopped</span><span class="bar"></span><span></span>';
  } else {
    head.innerHTML = '';
  }
}

/* ============================ regions ============================ */
function chipLabel(r){
  if(S.kind === 'spreadsheet' && r.typeLabel && r.typeLabel.includes(' · ')) return r.typeLabel.split(' · ').slice(1).join(' · ');
  if(r.shape) return r.shape.replace(/^node-/, '');
  return r.type;
}

function addRegion(ev){
  const r = ev.region;
  r.box = ev.box || S.geometry[r.id] || r.box || null;
  S.regions.push(r); S.byId.set(r.id, r);
  const row = buildRow(r, S.regions.length);
  S.rowEls.set(r.id, row);
  const feed = $('feed');
  const stick = feed.parentElement.scrollHeight - feed.parentElement.scrollTop - feed.parentElement.clientHeight < 80;
  feed.appendChild(row);
  if(stick && S.tab === 'stream') $('panel-stream').parentElement.scrollTop = $('panel-stream').parentElement.scrollHeight;
  $('streamCount').textContent = String(S.regions.length);
  if(r.box) placeBox(r, S.regions.length - 1, true);
  $('resultMeta').textContent = 'routed by content sniffing · ' + S.regions.length + ' regions so far';
}

function buildRow(r, n){
  const row = document.createElement('div');
  row.className = 'row enter' + (isLow(r) ? ' low' : '');
  row.dataset.id = r.id; row.setAttribute('role', 'button'); row.tabIndex = 0;
  const lang = r.lang || r.language;
  row.innerHTML =
    '<span class="idx">' + String(n).padStart(2, '0') + '</span>' +
    '<span class="tchip">' + esc(chipLabel(r)) + '</span>' +
    '<div class="body"><div class="txt' + (r.text ? '' : ' none') + '">' + (r.text ? esc(r.text) : 'no text · ' + esc(r.shape || r.type)) + '</div>' +
    '<div class="meta">' + esc(r.id) + (lang ? '<span class="lang-badge">' + esc(lang) + '</span>' : '') + '</div></div>' +
    '<div class="conf"><span class="cbar"><i style="--w:' + Math.round(r.confidence * 100) + '%"></i></span><span>' + Math.round(r.confidence * 100) + '%</span></div>';
  row.addEventListener('click', () => selectRegion(r.id, 'feed'));
  row.addEventListener('keydown', e => { if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); selectRegion(r.id, 'feed'); } });
  row.addEventListener('pointerenter', () => setHover(r.id));
  row.addEventListener('pointerleave', () => setHover(null));
  return row;
}

/* ============================ source pane ============================ */
function hasImages(){ return S.images.length > 0; }

function buildSourceFrame(){
  const frame = $('pageFrame');
  frame.querySelectorAll('[data-dyn]').forEach(n => n.remove());
  frame.className = 'page-frame'; frame.style.aspectRatio = '';
  S.hosts = new Map();
  const imgs = S.images;
  const mk = (tag, cls) => { const e = document.createElement(tag); if(cls) e.className = cls; e.dataset.dyn = '1'; return e; };

  if(imgs.length > 1){
    frame.classList.add('multi-page');
    imgs.forEach((img, i) => {
      const block = mk('div', 'page-block');
      block.style.aspectRatio = img.width + ' / ' + img.height;
      const im = document.createElement('img'); im.src = img.dataUrl; im.alt = img.pageLabel || ('Page ' + (i + 1)); im.draggable = false;
      block.appendChild(im);
      if(img.pageLabel){ const lb = document.createElement('div'); lb.className = 'preview-page-label'; lb.textContent = img.pageLabel; block.appendChild(lb); }
      frame.appendChild(block);
      S.hosts.set(i + 1, block);
    });
  } else if(imgs.length === 1){
    frame.classList.add('sheet');
    frame.style.aspectRatio = imgs[0].width + ' / ' + imgs[0].height;
    const im = mk('img', 'preview-image'); im.src = imgs[0].dataUrl; im.alt = 'Uploaded file preview'; im.draggable = false;
    frame.appendChild(im);
    S.hosts.set(1, frame);
  } else {
    // spreadsheets have no bitmap of themselves: a grid canvas the cell boxes land on
    frame.classList.add('grid-bg');
    const sk = mk('div', 'src-skeleton'); sk.textContent = S.kind === 'spreadsheet' ? 'reading cells…' : 'processing…';
    sk.id = 'srcSkeleton';
    frame.appendChild(sk);
    S.hosts.set(1, frame);
  }
  $('pageFrame').style.setProperty('--zoom', S.zoom);
  $('scanline').classList.remove('once'); $('scanline').classList.add('run');
  $('srcHint').textContent = hasImages() ? 'hover a region to link both panes' : 'cells appear as they are mapped';
}

function hostFor(r){
  if(S.hosts.size > 1) return S.hosts.get((r.box && r.box.page) || 1) || S.hosts.get(1);
  return S.hosts.get(1) || $('pageFrame');
}

function effectiveBox(r){
  const b = r.box;
  if(!b) return null;
  // No page images but a multi-page document (preview rendering failed): stack pages in one frame.
  if(!hasImages() && S.kind === 'document'){
    const pages = S.regions.filter(x => x.box && x.box.page).map(x => x.box.page);
    const n = pages.length ? Math.max(...pages) : 1;
    if(n > 1 && b.page){
      const band = 100 / n;
      return { ...b, top:(b.page - 1) * band + (b.top / 100) * band, height:(b.height / 100) * band };
    }
  }
  return b;
}

function addGhosts(){
  if(!hasImages()) return;
  const ids = Object.keys(S.geometry);
  ids.forEach((id, i) => {
    const b = S.geometry[id];
    const el = document.createElement('div');
    el.className = 'region ghost'; el.dataset.id = id; el.dataset.dyn = '1';
    el.style.cssText = 'top:' + b.top + '%;left:' + b.left + '%;width:' + b.width + '%;height:' + b.height + '%;--ed:' + Math.min(i * 35, 600) + 'ms';
    el.style.pointerEvents = 'none';
    (S.hosts.get(1) || $('pageFrame')).appendChild(el);
    S.boxEls.set(id, el);
  });
}

function placeBox(r, order, animate){
  const b = effectiveBox(r); if(!b) return;
  const sk = $('srcSkeleton'); if(sk) sk.remove();
  let el = S.boxEls.get(r.id);
  const existed = !!el;
  const total = Math.max(S.regions.length, 1);
  const delay = animate ? 0 : Math.min(order * Math.max(8, 900 / total), 900);
  if(!el){
    el = document.createElement('div'); el.dataset.dyn = '1';
    el.setAttribute('role', 'button'); el.tabIndex = 0;
    el.addEventListener('click', () => selectRegion(r.id, 'source'));
    el.addEventListener('keydown', e => { if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); selectRegion(r.id, 'source'); } });
    el.addEventListener('pointerenter', () => setHover(r.id));
    el.addEventListener('pointerleave', () => setHover(null));
    hostFor(r).appendChild(el);
    S.boxEls.set(r.id, el);
  }
  const wasGhost = el.classList.contains('ghost');
  const enter = !existed || wasGhost;   // already-visible boxes must not replay their entrance when the run finishes
  el.className = 'region' + (enter ? ' enter' : '') + (isLow(r) ? ' low' : '') + (r.shape ? ' shape-' + r.shape : '') +
    (r.id === S.activeId ? ' active' : '') + (r.id === S.hoverId ? ' hover' : '');
  el.style.cssText = 'top:' + b.top + '%;left:' + b.left + '%;width:' + b.width + '%;height:' + b.height + '%;--ed:' + delay + 'ms';
  el.title = r.typeLabel || r.id;
  el.setAttribute('aria-label', (r.typeLabel || r.id) + (r.text ? ': ' + r.text : ' (non-text region)'));
  el.innerHTML = '<span class="region-label">' + esc(r.typeLabel || r.id) + '</span>' +
    (S.kind === 'spreadsheet' ? '<span class="cell-text">' + esc(cellDisplay(r)) + '</span>' : '');
  if(wasGhost) el.style.setProperty('--ed', '0ms');
}

function cellDisplay(r){
  const t = r.text || '';
  if(r.type === 'formula-cell'){
    if(t.includes('unresolved')) return '#REF!';
    return t.includes(' → ') ? t.split(' → ', 2)[1] : t;
  }
  return t;
}

function syncOverlay(){
  S.regions.forEach((r, i) => { if(r.box) placeBox(r, i, S.boxEls.has(r.id) ? true : false); });
  // ghosts that never became regions (shouldn't happen) are dropped
  S.boxEls.forEach((el, id) => { if(el.classList.contains('ghost') && !S.byId.has(id)) { el.remove(); S.boxEls.delete(id); } });
}

/* ============================ link: hover / select ============================ */
function setHover(id){
  if(S.hoverId === id) return;
  const prev = S.hoverId; S.hoverId = id;
  const toggle = (rid, on) => {
    if(!rid) return;
    const b = S.boxEls.get(rid); if(b) b.classList.toggle('hover', on);
    const r = S.rowEls.get(rid); if(r) r.classList.toggle('hover', on);
    document.querySelectorAll('[data-region-id="' + CSS.escape(rid) + '"]').forEach(n => n.classList.toggle('hover', on));
  };
  toggle(prev, false); toggle(id, true);
  if(S.graphCtl) S.graphCtl.setHover(id);
}

function selectRegion(id, origin){
  S.activeId = (S.activeId === id) ? null : id;
  const apply = (rid, on) => {
    const b = S.boxEls.get(rid); if(b) b.classList.toggle('active', on);
    const r = S.rowEls.get(rid); if(r) r.classList.toggle('active', on);
  };
  S.byId.forEach((_, rid) => apply(rid, rid === S.activeId));
  if(S.activeId){
    const row = S.rowEls.get(S.activeId), box = S.boxEls.get(S.activeId);
    if(origin !== 'feed' && row && S.tab === 'stream') row.scrollIntoView({ block:'nearest', behavior:'smooth' });
    if(origin !== 'source' && box && S.mode === 'source') box.scrollIntoView({ block:'nearest', inline:'nearest', behavior:'smooth' });
  }
  if(S.mode === 'recon') renderRecon();
  if(S.graphCtl) S.graphCtl.setActive(S.activeId);
}

/* ============================ finish ============================ */
function finishRun(ev){
  stopTimer();
  S.phase = 'done'; S.doc = ev.doc; S.timings = ev.timings;
  S.regions = ev.doc.regions; S.byId = new Map(S.regions.map(r => [r.id, r]));
  const cached = !!ev.timings.cached;
  setStep('extract', 'done', cached ? 'cached' : ev.timings.extractMs);
  setStep('map', 'done', cached ? 'cached' : ev.timings.mapMs);
  setStep('validate', 'done', cached ? 'cached' : 0);
  $('tracker').style.setProperty('--prog', 1);

  // refresh row labels (typeLabel is authoritative on the finished doc) and add any missing rows
  S.regions.forEach((r, i) => {
    let row = S.rowEls.get(r.id);
    if(!row){ row = buildRow(r, i + 1); S.rowEls.set(r.id, row); $('feed').appendChild(row); }
    else row.querySelector('.tchip').textContent = chipLabel(r);
  });
  $('streamCount').textContent = String(S.regions.length);
  syncOverlay();

  $('resultTitle').textContent = S.doc.title;
  $('resultMeta').textContent = S.doc.pageLabel + ' · ' + S.doc.meta;
  $('resultAdapter').textContent = S.doc.adapter;
  const tchip = $('resultTimings');
  tchip.hidden = false;
  tchip.textContent = cached ? '⚡ from cache · instant' : '⚡ ' + (ev.timings.totalMs / 1000).toFixed(1) + 's total';
  tchip.title = cached ? 'Identical file seen before — served from the content-hash cache (same result a fresh run produces).'
    : 'sniff ' + ev.timings.sniffMs + 'ms · preview ' + ev.timings.previewMs + 'ms · extract ' + ev.timings.extractMs + 'ms · map ' + ev.timings.mapMs + 'ms';

  renderConfidenceStrip(); renderChecks();
  renderText(); renderJson(); renderTable();
  S.graphDirty = true;
  if(S.tab === 'graph') renderGraph();
  if(S.mode === 'recon') renderRecon();
  renderStreamHead();

  const sl = $('scanline'); sl.classList.remove('run'); sl.classList.remove('once'); void sl.offsetWidth; sl.classList.add('once');
  if(!S.regions.some(r => r.box) && !hasImages()) $('noPreview').hidden = false;
}

function renderConfidenceStrip(){
  const n = S.regions.length || 1;
  const hi = S.regions.filter(r => r.confidence >= 0.95).length;
  const lo = S.regions.filter(r => r.confidence < LOW).length;
  const mid = S.regions.length - hi - lo;
  $('confidenceStrip').innerHTML =
    '<div class="conf-dist"><div class="conf-bar" role="img" aria-label="' + hi + ' high, ' + mid + ' medium, ' + lo + ' low confidence regions">' +
    (hi ? '<i class="hi" style="flex:' + hi / n + '"></i>' : '') + (mid ? '<i class="mid" style="flex:' + mid / n + '"></i>' : '') + (lo ? '<i class="lo" style="flex:' + lo / n + '"></i>' : '') +
    '</div><div class="conf-key"><span><i class="k hi"></i><b>' + hi + '</b> at 95%+</span><span><i class="k mid"></i><b>' + mid + '</b> 85–95%</span>' +
    '<span class="' + (lo ? 'lo' : '') + '"><i class="k lo"></i><b>' + lo + '</b> below 85%' + (lo ? ' · needs review' : '') + '</span></div></div>';
}

function renderChecks(){
  const strip = $('checksStrip');
  const checks = S.doc.checks || [];
  if(!checks.length){ strip.innerHTML = ''; return; }
  strip.innerHTML = '<span class="checks-strip-label">Validation checks — artifact-level, independent of per-region confidence</span>' +
    checks.map((c, i) =>
      '<div class="check-badge ' + (c.passed ? 'pass' : 'fail') + '" style="animation-delay:' + i * 80 + 'ms"><span class="mark">' + (c.passed ? '✓' : '✕') + '</span>' +
      '<div><strong>' + esc(c.name) + '</strong><span>' + esc(c.detail) + '</span></div></div>').join('');
}

/* ============================ tabs ============================ */
function initTabs(){
  const buttons = document.querySelectorAll('.tab-btn');
  buttons.forEach(btn => btn.addEventListener('click', () => setTab(btn.dataset.tab)));
}
function setTab(name){
  S.tab = name;
  document.querySelectorAll('.tab-btn').forEach(b => b.setAttribute('aria-selected', b.dataset.tab === name ? 'true' : 'false'));
  ['stream', 'graph', 'xray', 'table', 'json', 'text'].forEach(n => { $('panel-' + n).hidden = (n !== name); });
  if(name === 'graph') renderGraph();
}

function renderText(){
  const lines = S.regions.map(r => r.text ? r.text : '[' + (r.typeLabel || r.type) + ' — excluded, non-text]').join('\n');
  $('panel-text').innerHTML = '<span class="muted">' + esc(S.doc.pageLabel) + ' · flattened for search indexing</span>\n\n' + esc(lines);
}
function renderJson(){
  let text = esc(JSON.stringify(S.doc.realArtifact, null, 2));
  text = text.replace(/&quot;([a-zA-Z_]+)&quot;:/g, '<span class="k">"$1"</span>:').replace(/: &quot;([^&]*)&quot;/g, ': <span class="s">"$1"</span>');
  $('panel-json').innerHTML = text;
}
function renderTable(){
  const panel = $('panel-table'), d = S.doc;
  if(!d.table || !d.table.length){
    panel.innerHTML = '<div class="empty-state">' + esc(d.tableEmptyReason || 'No tabular data in this artifact.') + '<br>Table export stays empty rather than guessing.</div>';
    return;
  }
  const cols = d.tableColumns;
  const rows = d.table.map(row => '<tr>' + cols.map(c => {
    let v = row[c];
    if(c === 'confidence') v = Math.round(v * 100) + '%';
    if(v === null || v === undefined) v = '—';
    return '<td>' + esc(String(v)) + '</td>';
  }).join('') + '</tr>').join('');
  panel.innerHTML = '<table class="extract-table"><thead><tr>' + cols.map(h => '<th>' + esc(h) + '</th>').join('') + '</tr></thead><tbody>' + rows + '</tbody></table>';
}

/* ============================ graph tab ============================ */
function renderGraph(){
  const panel = $('panel-graph');
  if(!S.doc){ panel.innerHTML = '<div class="skel"><i></i>The graph is built from the finished artifact…</div>'; return; }
  if(!S.graphDirty && S.graphCtl) return;
  S.graphModel = NX.graph.buildModel(S.regions, S.kind);
  panel.innerHTML =
    '<div class="graph-wrap"><div class="graph-bar"><span id="graphCounts"></span><span class="sp"></span>' +
    '<button type="button" class="chip-toggle" id="isoToggle" aria-pressed="' + S.showIsolated + '">Show isolated</button>' +
    '<button type="button" class="btn btn-ghost btn-sm" id="mermaidBtn">Copy Mermaid</button></div><div id="graphHost"></div></div>';
  const aspect = S.images[0] ? S.images[0].width / S.images[0].height : 1.6;
  S.graphCtl = NX.graph.render($('graphHost'), S.graphModel, {
    showIsolated:S.showIsolated, aspect:S.kind === 'diagram' ? aspect : 1.6, activeId:S.activeId,
    onSelect:id => selectRegion(id, 'graph'), onHover:id => setHover(id),
  });
  const c = S.graphCtl.counts ? S.graphCtl.counts() : { nodes:0, links:0 };
  $('graphCounts').textContent = c.nodes + ' nodes · ' + c.links + ' relationships';
  $('isoToggle').addEventListener('click', () => { S.showIsolated = !S.showIsolated; S.graphDirty = true; renderGraph(); });
  $('mermaidBtn').addEventListener('click', () => copyText(NX.graph.toMermaid(S.graphModel)).then(ok => toast(ok ? 'Mermaid flowchart copied to clipboard' : 'Copy failed — select the text from the JSON tab instead')));
  S.graphDirty = false;
}

async function copyText(text){
  try{ await navigator.clipboard.writeText(text); return true; }
  catch(e){
    try{
      const ta = document.createElement('textarea'); ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select(); const ok = document.execCommand('copy'); document.body.removeChild(ta); return ok;
    }catch(err){ return false; }
  }
}

/* ============================ x-ray tab ============================ */
function renderXray(){
  const panel = $('panel-xray');
  if(S.xrayState === 'idle'){ panel.innerHTML = ''; return; }
  if(S.xrayState === 'pending' || !S.xray){
    panel.innerHTML = S.xrayState === 'unavailable'
      ? '<div class="skel">The flattened baseline could not be computed for this file.</div>'
      : '<div class="skel"><i></i>Computing what a generic “flatten it” pipeline would have kept…</div>';
    return;
  }
  const x = S.xray;
  const stats = x.stats.map(s =>
    '<div class="xr-stat"><small>' + esc(s.label) + '</small><div class="vals"><span class="nx" data-count="' + s.nexdoc + '">0</span><span class="flat" title="Plain text/CSV has no slot for this — not representable, by construction">flat: —</span></div></div>').join('');
  const nxRows = S.regions.slice(0, 40).map(r => {
    const rels = (r.relationships || []).length;
    return '<div class="xrow" data-id="' + esc(r.id) + '"><span class="tchip">' + esc(chipLabel(r)) + '</span><span class="t">' + esc(r.text || '(' + (r.shape || r.type) + ')') +
      '</span><span class="rel">' + Math.round(r.confidence * 100) + '%' + (rels ? ' · ' + rels + '→' : '') + '</span></div>';
  }).join('') + (S.regions.length > 40 ? '<div class="xrow"><span class="t native">… ' + (S.regions.length - 40) + ' more regions</span></div>' : '');
  const lost = x.lost.map((l, i) => '<li style="animation-delay:' + (200 + i * 120) + 'ms">' + esc(l) + '</li>').join('');
  panel.innerHTML =
    '<div class="xray"><div class="xr-stats">' + stats + '</div>' +
    '<div class="xr-split">' +
      '<div class="xr-col flatcol"><h4>Flattened<small>' + esc(x.flattenedLabel) + '</small></h4><pre>' + (esc(x.flattened) || '(empty — nothing recovered)') + '</pre></div>' +
      '<div class="xr-col"><h4>nexDoc<small>typed · linked · scored</small></h4><div class="xr-nx-list">' + nxRows + '</div></div>' +
    '</div>' +
    (lost ? '<ul class="xr-lost">' + lost + '</ul>' : '') + '</div>';
  panel.querySelectorAll('.xrow[data-id]').forEach(r => r.addEventListener('click', () => selectRegion(r.dataset.id, 'xray')));
  panel.querySelectorAll('.nx[data-count]').forEach(el => countUp(el, Number(el.dataset.count)));
}

function countUp(el, target){
  if(target <= 0){ el.textContent = '0'; return; }
  const t0 = performance.now(), dur = 700;
  (function tick(now){
    const k = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - k, 3);
    el.textContent = String(Math.round(target * e));
    if(k < 1) requestAnimationFrame(tick);
  })(t0);
}

/* ============================ source / reconstruction mode, zoom, labels ============================ */
function initViewerControls(){
  $('modeSource').addEventListener('click', () => setMode('source'));
  $('modeRecon').addEventListener('click', () => setMode('recon'));
  $('zoomIn').addEventListener('click', () => setZoom(S.zoom + 0.25));
  $('zoomOut').addEventListener('click', () => setZoom(S.zoom - 0.25));
  $('zoomLabel').addEventListener('click', () => setZoom(1));
  $('labelsToggle').addEventListener('click', () => {
    S.showLabels = !S.showLabels;
    $('labelsToggle').setAttribute('aria-pressed', String(S.showLabels));
    $('pageFrame').classList.toggle('show-labels', S.showLabels);
  });
  document.addEventListener('keydown', e => { if(e.key === 'Escape' && S.activeId) selectRegion(S.activeId); });
}
function setZoom(z){
  S.zoom = Math.max(0.5, Math.min(3, Math.round(z * 4) / 4));
  $('pageFrame').style.setProperty('--zoom', S.zoom);
  $('zoomLabel').textContent = Math.round(S.zoom * 100) + '%';
}
function setMode(mode, silent){
  S.mode = mode;
  $('modeSource').setAttribute('aria-selected', String(mode === 'source'));
  $('modeRecon').setAttribute('aria-selected', String(mode === 'recon'));
  $('pageFrame').hidden = (mode !== 'source');
  $('reconView').hidden = (mode !== 'recon');
  $('zoomTools').style.visibility = mode === 'source' ? 'visible' : 'hidden';
  $('labelsToggle').style.visibility = mode === 'source' ? 'visible' : 'hidden';
  if(mode === 'recon' && !silent) renderRecon();
}

/* ============================ reconstruction (redraw of what was understood) ============================ */
function renderRecon(){
  const view = $('reconView');
  if(!S.doc){ view.innerHTML = '<div class="skel"><i></i>The reconstruction is drawn once extraction finishes…</div>'; return; }
  if(S.kind === 'spreadsheet') view.innerHTML = reconSpreadsheetHtml(S.doc.realArtifact.regions);
  else if(S.kind === 'diagram') view.innerHTML = reconDiagramHtml(S.regions);
  else if(S.kind === 'document') view.innerHTML = reconDocumentHtml(S.doc.realArtifact.regions);
  else view.innerHTML = '<div class="recon-empty">No reconstruction view for kind "' + esc(S.kind) + '".</div>';
  bindRecon();
}
function bindRecon(){
  document.querySelectorAll('.recon-sheet-tab').forEach(btn => btn.addEventListener('click', () => { S.reconSheet = btn.dataset.sheet; renderRecon(); }));
  document.querySelectorAll('#reconView [data-region-id]').forEach(el => {
    el.addEventListener('click', () => selectRegion(el.dataset.regionId, 'recon'));
    el.addEventListener('pointerenter', () => setHover(el.dataset.regionId));
    el.addEventListener('pointerleave', () => setHover(null));
    if(!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '0');
    el.addEventListener('keydown', e => { if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); selectRegion(el.dataset.regionId, 'recon'); } });
  });
}

function colLettersToIndex(letters){ let n = 0; for(const ch of letters.toUpperCase()) n = n * 26 + (ch.charCodeAt(0) - 64); return n; }
function colIndexToLetters(n){ let s = ''; while(n > 0){ const rem = (n - 1) % 26; s = String.fromCharCode(65 + rem) + s; n = Math.floor((n - 1) / 26); } return s; }

function reconSpreadsheetHtml(regions){
  const sheets = new Map();
  regions.forEach(r => {
    const bang = r.id.indexOf('!'); if(bang === -1) return;
    const sheet = r.id.slice(0, bang), m = r.id.slice(bang + 1).match(/^([A-Za-z]+)(\d+)$/); if(!m) return;
    const col = colLettersToIndex(m[1]), row = parseInt(m[2], 10);
    if(!sheets.has(sheet)) sheets.set(sheet, { cells:new Map(), maxRow:0, maxCol:0 });
    const s = sheets.get(sheet); s.cells.set(row + ':' + col, r); s.maxRow = Math.max(s.maxRow, row); s.maxCol = Math.max(s.maxCol, col);
  });
  if(!sheets.size) return '<div class="recon-empty">No cell regions with a recognizable sheet!ref id to reconstruct.</div>';
  const names = [...sheets.keys()];
  if(!names.includes(S.reconSheet)) S.reconSheet = names[0];
  const tabs = names.length > 1 ? '<div class="recon-sheet-tabs" role="tablist">' + names.map(n =>
    '<button type="button" class="recon-sheet-tab" role="tab" aria-selected="' + (n === S.reconSheet) + '" data-sheet="' + esc(n) + '">' + esc(n) + '</button>').join('') + '</div>' : '';
  const s = sheets.get(S.reconSheet), rowsHtml = [];
  for(let row = 1; row <= s.maxRow; row++){
    let titleCell = null; const cells = [];
    for(let col = 1; col <= s.maxCol; col++){ const rg = s.cells.get(row + ':' + col) || null; if(rg && rg.type === 'title' && !titleCell) titleCell = rg; cells.push(rg); }
    if(titleCell){
      rowsHtml.push('<tr><td class="row-head">' + row + '</td><td class="cell-title cell-pick' + (titleCell.id === S.activeId ? ' cell-active' : '') + '" data-region-id="' + esc(titleCell.id) + '" colspan="' + s.maxCol + '">' + esc(titleCell.text || '') + '</td></tr>');
      continue;
    }
    rowsHtml.push('<tr><td class="row-head">' + row + '</td>' + cells.map(reconCell).join('') + '</tr>');
  }
  const heads = '<th class="corner"></th>' + Array.from({ length:s.maxCol }, (_, i) => '<th class="col-head">' + colIndexToLetters(i + 1) + '</th>').join('');
  return tabs + '<div class="recon-grid-wrap"><table class="recon-grid"><thead><tr>' + heads + '</tr></thead><tbody>' + rowsHtml.join('') + '</tbody></table></div>';
}
function reconCell(region){
  if(!region) return '<td class="cell-empty"></td>';
  let cls = '', content = esc(region.text || ''), title = '';
  if(region.type === 'header') cls = 'cell-header';
  else if(region.type === 'formula-cell'){
    const text = region.text || '', bad = text.includes('unresolved');
    cls = bad ? 'cell-formula-bad' : 'cell-formula';
    content = esc(bad ? '#REF!' : (text.includes(' → ') ? text.split(' → ', 2)[1] : text));
    title = ' title="' + esc(text) + '"';
  } else if(region.type === 'value') cls = 'cell-value';
  return '<td class="cell-pick ' + cls + (isLow(region) ? ' cell-low' : '') + (region.id === S.activeId ? ' cell-active' : '') + '" data-region-id="' + esc(region.id) + '"' + title + '>' + content + '</td>';
}

function reconDiagramHtml(regions){
  const aspect = S.images[0] ? S.images[0].width / S.images[0].height : 4 / 3;
  const VW = 100 * aspect, sx = v => v * (VW / 100);
  const nodes = regions.filter(r => r.type === 'node' && r.box);
  const edges = regions.filter(r => r.type === 'edge');
  const lanes = regions.filter(r => r.type === 'swimlane' && r.box);
  if(!nodes.length) return '<div class="recon-empty">No node geometry available to reconstruct — see the Stream tab for the raw region list.</div>';
  const box = {}; nodes.forEach(n => { box[n.id] = { left:sx(n.box.left), top:n.box.top, width:sx(n.box.width), height:n.box.height }; });
  const center = b => ({ x:b.left + b.width / 2, y:b.top + b.height / 2 });
  function clip(from, to, b){
    const dx = to.x - from.x, dy = to.y - from.y, hx = b.width / 2, hy = b.height / 2;
    const t = Math.min(dx !== 0 ? hx / Math.abs(dx) : Infinity, dy !== 0 ? hy / Math.abs(dy) : Infinity, 1);
    return { x:from.x + t * dx, y:from.y + t * dy };
  }
  const short = t => { if(!t) return null; t = t.trim(); return t.length > 18 ? t.slice(0, 17) + '…' : t; };
  let lanesSvg = '';
  lanes.forEach(l => {
    const b = { left:sx(l.box.left), top:l.box.top, width:sx(l.box.width), height:l.box.height }, c = center(b);
    lanesSvg += b.height >= b.width
      ? '<line class="rc-lane" data-region-id="' + esc(l.id) + '" x1="' + c.x.toFixed(2) + '" y1="' + b.top.toFixed(2) + '" x2="' + c.x.toFixed(2) + '" y2="' + (b.top + b.height).toFixed(2) + '"/>'
      : '<line class="rc-lane" data-region-id="' + esc(l.id) + '" x1="' + b.left.toFixed(2) + '" y1="' + c.y.toFixed(2) + '" x2="' + (b.left + b.width).toFixed(2) + '" y2="' + c.y.toFixed(2) + '"/>';
  });
  let shapes = '';
  nodes.forEach(n => {
    const b = box[n.id], c = center(b), low = isLow(n) ? ' low' : '', act = n.id === S.activeId ? ' active' : '';
    const attrs = ' data-region-id="' + esc(n.id) + '"';
    const hx = b.width / 2, hy = b.height / 2;
    if(n.shape === 'node-decision'){
      const pts = [[c.x, c.y - hy], [c.x + hx, c.y], [c.x, c.y + hy], [c.x - hx, c.y]].map(p => p[0].toFixed(2) + ',' + p[1].toFixed(2)).join(' ');
      shapes += '<polygon class="rc-node' + low + act + '"' + attrs + ' points="' + pts + '"/>';
    } else if(n.shape === 'node-initial'){
      const r = Math.min(hx, hy) * 0.85;
      shapes += '<circle class="rc-node solid' + act + '"' + attrs + ' cx="' + c.x.toFixed(2) + '" cy="' + c.y.toFixed(2) + '" r="' + r.toFixed(2) + '"/>';
    } else if(n.shape === 'node-final'){
      const r = Math.min(hx, hy) * 0.85;
      shapes += '<circle class="rc-node' + low + act + '"' + attrs + ' cx="' + c.x.toFixed(2) + '" cy="' + c.y.toFixed(2) + '" r="' + r.toFixed(2) + '"/>' +
        '<circle class="rc-node solid" style="pointer-events:none" cx="' + c.x.toFixed(2) + '" cy="' + c.y.toFixed(2) + '" r="' + (r * 0.5).toFixed(2) + '"/>';
    } else if(n.shape === 'node-circle'){
      shapes += '<ellipse class="rc-node' + low + act + '"' + attrs + ' cx="' + c.x.toFixed(2) + '" cy="' + c.y.toFixed(2) + '" rx="' + hx.toFixed(2) + '" ry="' + hy.toFixed(2) + '"/>';
    } else if(n.shape === 'fork-join'){
      shapes += '<rect class="rc-node solid' + act + '"' + attrs + ' x="' + b.left.toFixed(2) + '" y="' + b.top.toFixed(2) + '" width="' + b.width.toFixed(2) + '" height="' + b.height.toFixed(2) + '" rx="0.5"/>';
    } else {
      const rx = n.shape === 'node-activity' ? Math.min(b.width, b.height) * 0.28 : 1.4;
      shapes += '<rect class="rc-node' + low + act + '"' + attrs + ' x="' + b.left.toFixed(2) + '" y="' + b.top.toFixed(2) + '" width="' + b.width.toFixed(2) + '" height="' + b.height.toFixed(2) + '" rx="' + rx.toFixed(2) + '"/>';
    }
    const label = short(n.text);
    if(label) shapes += '<text class="rc-label" x="' + c.x.toFixed(2) + '" y="' + c.y.toFixed(2) + '" font-size="3.2">' + (label !== n.text.trim() ? '<title>' + esc(n.text) + '</title>' : '') + esc(label) + '</text>';
  });
  let edgesSvg = '';
  edges.forEach(e => {
    const t = (e.relationships || []).filter(r => r.type === 'connects-to').map(r => r.target);
    if(t.length < 2 || !box[t[0]] || !box[t[1]]) return;
    const fc = center(box[t[0]]), tc = center(box[t[1]]), s = clip(fc, tc, box[t[0]]), en = clip(tc, fc, box[t[1]]);
    const low = isLow(e) ? ' low' : '', act = e.id === S.activeId ? ' active' : '';
    const marker = isLow(e) ? 'rc-arrow-low' : (e.id === S.activeId ? 'rc-arrow-active' : 'rc-arrow');
    const co = ' x1="' + s.x.toFixed(2) + '" y1="' + s.y.toFixed(2) + '" x2="' + en.x.toFixed(2) + '" y2="' + en.y.toFixed(2) + '"';
    edgesSvg += '<line class="rc-hit" data-region-id="' + esc(e.id) + '"' + co + '/><line class="rc-edge' + low + act + '" data-region-id="' + esc(e.id) + '"' + co + ' marker-end="url(#' + marker + ')"/>';
    const label = short(e.text);
    if(label){
      const mx = (s.x + en.x) / 2, my = (s.y + en.y) / 2, w = Math.max(8, label.length * 1.9);
      edgesSvg += '<rect class="rc-edge-label-bg" x="' + (mx - w / 2).toFixed(2) + '" y="' + (my - 2.6).toFixed(2) + '" width="' + w.toFixed(2) + '" height="4.4"/><text class="rc-edge-label" font-size="2.6" x="' + mx.toFixed(2) + '" y="' + (my + 1).toFixed(2) + '">' + esc(label) + '</text>';
    }
  });
  const defs = '<defs>' + [['rc-arrow', '#4b5850'], ['rc-arrow-low', '#ef6a3c'], ['rc-arrow-active', '#14b88f']].map(([id, col]) =>
    '<marker id="' + id + '" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="2" markerHeight="2" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="' + col + '"/></marker>').join('') + '</defs>';
  return '<div class="recon-diagram" style="aspect-ratio:' + VW.toFixed(2) + '/100"><svg viewBox="0 0 ' + VW.toFixed(2) + ' 100" preserveAspectRatio="xMidYMid meet">' + defs + lanesSvg + edgesSvg + shapes + '</svg></div>' +
    '<div class="recon-legend"><span><span class="swatch" style="background:#e9f6f1;border:1.5px solid #14b88f"></span>high confidence</span><span><span class="swatch" style="background:#fbe8de;border:1.5px solid #ef6a3c"></span>needs review</span><span>click a shape to link it everywhere</span></div>';
}

function reconDocumentHtml(regions){
  if(!regions.length) return '<div class="recon-empty">No regions to reconstruct.</div>';
  const pages = []; let cur = null;
  regions.forEach(r => {
    const m = r.id.match(/^page(\d+)!/), pn = m ? m[1] : null;
    if(!cur || cur.pn !== pn){ cur = { pn, regions:[] }; pages.push(cur); }
    cur.regions.push(r);
  });
  return '<div class="recon-doc">' + pages.map(p =>
    '<div class="recon-page">' + (p.pn ? '<div class="recon-page-label">Page ' + esc(p.pn) + '</div>' : '') + p.regions.map(reconBlock).join('') + '</div>').join('') + '</div>';
}
function reconBlock(r){
  const cls = 'recon-block recon-block-' + r.type + (isLow(r) ? ' low' : '') + (r.id === S.activeId ? ' active' : '');
  const attr = ' data-region-id="' + esc(r.id) + '"';
  if(r.type === 'scanned-page-unprocessed') return '<div class="' + cls + '"' + attr + '>Scanned page — OCR engine unavailable, this page could not be processed.</div>';
  return '<div class="' + cls + '"' + attr + '>' + (r.text ? esc(r.text) : '<span class="native">(non-text region)</span>') + '</div>';
}

/* ============================ downloads ============================ */
function downloadBlob(filename, content, mime){
  const url = URL.createObjectURL(new Blob([content], { type:mime }));
  const a = document.createElement('a'); a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function initDownloadMenu(){
  const toggle = $('downloadToggle'), list = $('downloadList');
  toggle.addEventListener('click', e => { e.stopPropagation(); const show = list.hidden; list.hidden = !show; toggle.setAttribute('aria-expanded', String(show)); });
  list.addEventListener('click', e => e.stopPropagation());
  document.addEventListener('click', () => { list.hidden = true; toggle.setAttribute('aria-expanded', 'false'); });
  list.querySelectorAll('button[data-format]').forEach(btn => btn.addEventListener('click', () => {
    if(!S.doc) return;
    const stem = S.doc.id || 'output', art = S.doc.realArtifact, fmt = btn.dataset.format;
    let name, content, mime = 'application/json';
    if(fmt === 'schema'){ name = stem + '.schema.json'; content = JSON.stringify(art, null, 2); }
    else if(fmt === 'text'){ name = stem + '.text.txt'; content = art.regions.map(r => r.text ? r.text : '[' + r.type + ' — excluded, non-text]').join('\n'); mime = 'text/plain'; }
    else if(fmt === 'structured'){ name = stem + '.structured.json'; content = JSON.stringify(art.regions, null, 2); }
    else if(fmt === 'table'){
      const out = { columns:S.doc.tableColumns, rows:S.doc.table }; if(S.doc.tableEmptyReason) out.empty_reason = S.doc.tableEmptyReason;
      name = stem + '.table.json'; content = JSON.stringify(out, null, 2);
    } else return;
    downloadBlob(name, content, mime);
    list.hidden = true; toggle.setAttribute('aria-expanded', 'false');
  }));
}

/* ============================ ask (real Groq-backed Q&A) ============================ */
let askAvailable = false;
async function initAsk(){
  const sub = $('askSub'), input = $('askInput'), submit = $('askSubmit');
  try{
    const body = await (await fetch('/api/ask/status')).json();
    askAvailable = !!body.available; sub.textContent = body.detail;
  }catch(err){ askAvailable = false; sub.textContent = 'Could not reach the local server to check Groq configuration: ' + err.message; }
  input.disabled = !askAvailable; submit.disabled = !askAvailable;
  if(askAvailable) sub.textContent = 'Ask a real question about the artifact above — answered by Groq from its structured representation.';
  $('askForm').addEventListener('submit', async e => {
    e.preventDefault();
    if(!askAvailable || !S.doc) return;
    const q = input.value.trim(); if(!q) return;
    const box = $('askAnswer'); box.hidden = false; box.className = 'ask-answer pending'; box.textContent = 'Asking Groq…'; submit.disabled = true;
    try{
      const resp = await fetch('/api/ask', { method:'POST', headers:{ 'Content-Type':'application/json' }, body:JSON.stringify({ question:q, artifact:S.doc.realArtifact }) });
      const body = await resp.json();
      if(!resp.ok || !body.ok){ box.className = 'ask-answer error'; box.textContent = body.error || ('Request failed with status ' + resp.status); }
      else { box.className = 'ask-answer'; box.textContent = body.answer; }
    }catch(err){ box.className = 'ask-answer error'; box.textContent = 'Could not reach the local server: ' + err.message; }
    finally{ submit.disabled = !askAvailable; }
  });
}
function resetAsk(){ const b = $('askAnswer'); b.hidden = true; b.textContent = ''; $('askInput').value = ''; }

/* ============================ boot ============================ */
initTabs(); initUpload(); initViewerControls(); initDownloadMenu(); initAsk(); initSamples(); pollEngine();
setMode('source', true);

// expose a tiny handle for debugging / automated checks
window.NXApp = { state:S, handleFile };
})();
