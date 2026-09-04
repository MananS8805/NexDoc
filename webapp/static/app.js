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
  frame.querySelectorAll('.region').forEach(el => el.remove());
  frame.classList.toggle('grid-bg', doc.kind === 'spreadsheet');

  const noPreview = document.getElementById('noPreview');
  const hasBoxes = doc.regions.some(r => r.box);
  noPreview.hidden = hasBoxes;

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

function selectRegion(id){
  activeRegionId = (activeRegionId === id) ? null : id;
  renderPreview(false);
  renderActiveTab();
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
