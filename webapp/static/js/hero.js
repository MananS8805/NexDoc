/* Landing chrome: animated dithered pixel-cloud sky, condensing pill nav,
 * theme toggle, scroll reveals. Pure decoration — nothing here touches the
 * pipeline. All motion respects prefers-reduced-motion. */
(function(){
  'use strict';
  const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- nav: condense into a compact pill after scrolling ---------- */
  const navWrap = document.getElementById('navWrap');
  function onScroll(){ navWrap.classList.toggle('scrolled', window.scrollY > 70); }
  window.addEventListener('scroll', onScroll, { passive:true });
  onScroll();

  /* ---------- theme toggle (persisted) ---------- */
  document.getElementById('themeToggle').addEventListener('click', () => {
    const root = document.documentElement;
    const next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('nexdoc-theme', next); } catch (e) {}
  });

  /* ---------- scroll reveal ---------- */
  const reveals = document.querySelectorAll('.reveal');
  if('IntersectionObserver' in window){
    const io = new IntersectionObserver(entries => {
      entries.forEach(e => { if(e.isIntersecting){ e.target.classList.add('in'); io.unobserve(e.target); } });
    }, { threshold:0.12, rootMargin:'0px 0px -6% 0px' });
    reveals.forEach(el => io.observe(el));
  } else {
    reveals.forEach(el => el.classList.add('in'));
  }

  /* ---------- dropzone: cursor-following glow ---------- */
  const dz = document.getElementById('dropzone');
  if(dz){
    dz.addEventListener('pointermove', e => {
      const r = dz.getBoundingClientRect();
      dz.style.setProperty('--mx', ((e.clientX - r.left) / r.width * 100).toFixed(1) + '%');
    });
  }

  /* ---------- dithered cloud sky ----------
   * Fractal value-noise "cloud mass" thresholded through a 4x4 Bayer matrix
   * into a 5-tone palette, drawn at low resolution and scaled up with
   * image-rendering:pixelated — the ordered-dither look of the reference. */
  const canvas = document.getElementById('sky');
  if(!canvas) return;
  const ctx = canvas.getContext('2d', { alpha:false });
  const CELL = 6;
  const BAYER = [0,8,2,10, 12,4,14,6, 3,11,1,9, 15,7,13,5];
  const SKY_TOP = [138,164,161], SKY_BOT = [170,193,186];
  const SHADE = [98,122,120], TAN = [150,136,98], CREAM = [184,170,126], LIGHT = [210,198,154];
  let cols = 0, rows = 0, img = null, t = 0, last = 0, visible = true, raf = 0;

  function hash(x, y){
    let n = Math.imul(x, 374761393) + Math.imul(y, 668265263);
    n = Math.imul(n ^ (n >>> 13), 1274126177);
    n ^= n >>> 16;
    return (n >>> 0) / 4294967295;
  }
  function vnoise(x, y){
    const xi = Math.floor(x), yi = Math.floor(y);
    const xf = x - xi, yf = y - yi;
    const u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf);
    const a = hash(xi, yi), b = hash(xi + 1, yi), c = hash(xi, yi + 1), d = hash(xi + 1, yi + 1);
    return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
  }
  function fbm(x, y){
    return vnoise(x, y) * 0.5 + vnoise(x * 2.03, y * 2.03) * 0.25 + vnoise(x * 4.1, y * 4.1) * 0.125 + vnoise(x * 8.3, y * 8.3) * 0.0625;
  }
  function smooth(e0, e1, x){ const k = Math.min(1, Math.max(0, (x - e0) / (e1 - e0))); return k * k * (3 - 2 * k); }

  function resize(){
    const w = canvas.clientWidth || window.innerWidth || 1280, h = canvas.clientHeight || 700;
    cols = Math.max(1, Math.ceil(w / CELL)); rows = Math.max(1, Math.ceil(h / CELL));
    canvas.width = cols; canvas.height = rows;
    img = ctx.createImageData(cols, rows);
    draw();
  }

  function draw(){
    const data = img.data;
    let p = 0;
    for(let r = 0; r < rows; r++){
      const yy = r / rows;
      const mass = smooth(0.42, 1.0, yy);
      const skyR = SKY_TOP[0] + (SKY_BOT[0] - SKY_TOP[0]) * yy;
      const skyG = SKY_TOP[1] + (SKY_BOT[1] - SKY_TOP[1]) * yy;
      const skyB = SKY_TOP[2] + (SKY_BOT[2] - SKY_TOP[2]) * yy;
      for(let c = 0; c < cols; c++){
        const n1 = fbm(c * 0.020 + t * 0.30, r * 0.042 + 3.1);
        const n2 = fbm(c * 0.050 - t * 0.18, r * 0.085 + 11.7);
        const d = n1 * 0.95 + n2 * 0.30 + mass * 0.55 - 0.78;
        const b = BAYER[(r & 3) * 4 + (c & 3)] / 16 - 0.5;
        const v = d + b * 0.17;
        let R, G, B;
        if(v < 0){
          const j = b * 5;                       // faint dither in the open sky too
          R = skyR + j; G = skyG + j; B = skyB + j;
        } else {
          // cloud tops are lit; deeper into the mass (larger v) / lower on screen shades toward blue-grey
          const under = smooth(0.82, 1.0, yy) * 0.14;
          const lv = v - under;
          const col = lv < 0.10 ? SHADE : lv < 0.24 ? TAN : lv < 0.42 ? CREAM : LIGHT;
          R = col[0]; G = col[1]; B = col[2];
        }
        data[p++] = R; data[p++] = G; data[p++] = B; data[p++] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
  }

  function frame(now){
    raf = requestAnimationFrame(frame);
    if(!visible || document.hidden) return;
    if(now - last < 90) return;                  // ~11fps: a slow drift, cheap on CPU
    const dt = last ? (now - last) / 1000 : 0;
    last = now;
    t += dt * 0.35;
    draw();
  }

  window.addEventListener('resize', () => { clearTimeout(resize._t); resize._t = setTimeout(resize, 150); });
  resize();
  if(!reduceMotion){
    raf = requestAnimationFrame(frame);
    if('IntersectionObserver' in window){
      new IntersectionObserver(es => { visible = es[0].isIntersecting; }, { threshold:0 }).observe(document.getElementById('hero'));
    }
  }
})();
