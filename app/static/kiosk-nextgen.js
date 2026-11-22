/* Kiosk next-gen: product recognition (USB webcam), voice take (USB mic),
   greeting by name. Loaded only when an admin turned one of them on, and every
   camera/microphone control stays hidden until the browser really sees the
   device. All stock changes go through the page's normal take flow
   (KioskBridge.move -> «Ποιος είσαι;» / PIN -> server), never around it. */
(function(){
  'use strict';
  const cfgNode = document.getElementById('nextgenConfig');
  if(!cfgNode || !window.KioskBridge) return;
  let cfg = {};
  try { cfg = JSON.parse(cfgNode.textContent || '{}'); } catch (err) { return; }
  const bridge = window.KioskBridge;
  const AJAX = {'X-Requested-With':'XMLHttpRequest','Accept':'application/json'};
  const SPEECH_KEY = 'ng.webspeech.failed';
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition || null;
  let hw = {camera:false, mic:false};

  // ------------------------------------------------------------------ UI shell
  const dock = el('div', 'ngDock'); dock.hidden = true;
  const preview = el('video', 'ngPreview'); preview.muted = true; preview.playsInline = true; preview.hidden = true; preview.setAttribute('aria-hidden','true');
  const camBtn = el('button', 'ngBtn'); camBtn.type = 'button'; camBtn.hidden = true; camBtn.innerHTML = '<span class="ngDot"></span>📷 Δείξε προϊόν';
  const micBtn = el('button', 'ngBtn'); micBtn.type = 'button'; micBtn.hidden = true; micBtn.textContent = '🎤 Πες τι πήρες';
  dock.append(preview, camBtn, micBtn);
  const panel = el('div', 'ngPanel'); panel.hidden = true; panel.setAttribute('role','dialog'); panel.setAttribute('aria-live','polite');
  document.body.append(dock, panel);

  function el(tag, cls){ const n = document.createElement(tag); if(cls) n.className = cls; return n; }
  function esc(v){ return String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
  function refreshDock(){ dock.hidden = camBtn.hidden && micBtn.hidden && preview.hidden; }
  function closePanel(){ panel.hidden = true; panel.innerHTML = ''; }
  function say(url){ try { const a = new Audio(url); a.play().catch(()=>{}); } catch (err) {} }
  function candidateCard(c, best, actions){
    return '<div class="ngCand'+(best?' best':'')+'"><img src="/consumables/'+esc(c.id)+'/image" alt="" onerror="this.replaceWith(Object.assign(document.createElement(\'span\'),{className:\'ngFallback\',textContent:\'?\'}))">'
      + '<div><b>'+esc(c.name)+'</b><small>'+esc(c.qty_label || '')+'</small></div><div class="ngActs">'+actions+'</div></div>';
  }

  // ------------------------------------------------------------------ hardware
  function micUsable(devices){
    const mics = devices.filter(d => d.kind === 'audioinput' && d.deviceId !== 'default' && d.deviceId !== 'communications');
    // The motherboard's analog jacks are always listed even with nothing plugged in:
    // only a USB / headset microphone counts.
    return mics.some(d => d.label && !/HDA Intel|Built-?in|ALC\d|Analog|Monitor of/i.test(d.label));
  }
  async function detectHardware(){
    if(!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
    let devices = [];
    try { devices = await navigator.mediaDevices.enumerateDevices(); } catch (err) { return; }
    const next = {camera: devices.some(d => d.kind === 'videoinput'), mic: micUsable(devices)};
    const changed = next.camera !== hw.camera || next.mic !== hw.mic;
    hw = next;
    if(cfg.vision){ if(hw.camera) startCamera(); else stopCamera(); }
    micBtn.hidden = !(cfg.voice_take && hw.mic && (cfg.server_stt || (SR && !speechFailed())));
    refreshDock();
    if(changed || !detectHardware.sent){
      detectHardware.sent = true;
      fetch('/consumables/take/nextgen/hardware', {method:'POST', headers:Object.assign({'Content-Type':'application/json'}, AJAX), body: JSON.stringify({camera: hw.camera, mic: hw.mic, speech: !!(SR && !speechFailed()) || !!cfg.server_stt})}).catch(()=>{});
    }
  }
  if(navigator.mediaDevices && navigator.mediaDevices.addEventListener){
    navigator.mediaDevices.addEventListener('devicechange', () => setTimeout(detectHardware, 800));
  }

  // ------------------------------------------------------------------ greeting
  if(cfg.smart_voice){
    document.addEventListener('kiosk:session-start', ev => {
      const id = ev.detail && ev.detail.worker_id;
      if(id) say('/consumables/take/nextgen/voice/greeting?w='+encodeURIComponent(id)+'&d='+new Date().toISOString().slice(0,13));
    });
  }

  // ------------------------------------------------------------------ product recognition
  const SAMPLE_MS = 400, SW = 64, SH = 48;
  let stream = null, timer = null, bg = null, prev = null, stable = 0, cooling = 0, busy = false, lastFrame = null;
  const small = document.createElement('canvas'); small.width = SW; small.height = SH;
  const sctx = small.getContext('2d', {willReadFrequently: true});
  async function startCamera(){
    if(stream || !cfg.vision) return;
    try {
      stream = await navigator.mediaDevices.getUserMedia({audio:false, video:{width:{ideal:640}, height:{ideal:480}, frameRate:{ideal:10, max:15}}});
    } catch (err) { stream = null; camBtn.hidden = true; refreshDock(); return; }
    preview.srcObject = stream;
    try { await preview.play(); } catch (err) {}
    camBtn.hidden = false; refreshDock();
    bg = null; prev = null; stable = 0;
    timer = setInterval(sample, SAMPLE_MS);
  }
  function stopCamera(){
    if(timer) clearInterval(timer); timer = null;
    if(stream) stream.getTracks().forEach(t => t.stop()); stream = null;
    camBtn.hidden = true; preview.hidden = true; refreshDock();
  }
  function grey(){
    sctx.drawImage(preview, 0, 0, SW, SH);
    const d = sctx.getImageData(0, 0, SW, SH).data, out = new Float32Array(SW*SH);
    for(let i = 0, j = 0; i < d.length; i += 4, j++) out[j] = d[i]*0.299 + d[i+1]*0.587 + d[i+2]*0.114;
    return out;
  }
  function diff(a, b){
    let n = 0, x0 = SW, y0 = SH, x1 = -1, y1 = -1;
    for(let j = 0; j < a.length; j++){
      if(Math.abs(a[j]-b[j]) > 28){ n++; const x = j % SW, y = (j / SW) | 0; if(x<x0)x0=x; if(x>x1)x1=x; if(y<y0)y0=y; if(y>y1)y1=y; }
    }
    return {frac: n / a.length, box: x1 < 0 ? null : [x0, y0, x1, y1]};
  }
  function sample(){
    if(document.visibilityState === 'hidden' || busy || !preview.videoWidth || !panel.hidden) return;
    const cur = grey();
    if(!bg){ bg = cur; prev = cur; return; }
    const vsBg = diff(cur, bg), vsPrev = diff(cur, prev);
    prev = cur;
    if(cooling > 0){ cooling--; if(vsBg.frac < 0.04) cooling = 0; return; }
    if(vsBg.frac < 0.04){
      for(let j = 0; j < bg.length; j++) bg[j] = bg[j]*0.85 + cur[j]*0.15;   // follow slow light changes
      stable = 0; preview.hidden = true; refreshDock(); return;
    }
    if(vsBg.frac > 0.07 && vsBg.frac < 0.85 && vsPrev.frac < 0.03){
      stable++;
      preview.hidden = false; refreshDock();
      if(stable >= 2){ stable = 0; capture(vsBg.box, false); }
    } else {
      stable = 0;
    }
  }
  function capture(box, manual){
    const vw = preview.videoWidth, vh = preview.videoHeight;
    if(!vw) return;
    let x = 0, y = 0, w = vw, h = vh;
    if(box && !manual){
      const sx = vw / SW, sy = vh / SH, padX = (box[2]-box[0]+1)*sx*0.15, padY = (box[3]-box[1]+1)*sy*0.15;
      x = Math.max(0, box[0]*sx - padX); y = Math.max(0, box[1]*sy - padY);
      w = Math.min(vw - x, (box[2]-box[0]+1)*sx + 2*padX); h = Math.min(vh - y, (box[3]-box[1]+1)*sy + 2*padY);
    } else {
      x = vw*0.15; y = vh*0.1; w = vw*0.7; h = vh*0.8;
    }
    const scale = Math.min(1, 320 / Math.max(w, h));
    const c = document.createElement('canvas'); c.width = Math.round(w*scale); c.height = Math.round(h*scale);
    c.getContext('2d').drawImage(preview, x, y, w, h, 0, 0, c.width, c.height);
    busy = true;
    c.toBlob(async blob => {
      lastFrame = blob;
      try {
        const fd = new FormData(); fd.append('frame', blob, 'frame.jpg');
        const res = await fetch('/consumables/take/nextgen/vision/match', {method:'POST', body:fd, headers:AJAX});
        const data = await res.json();
        if(res.ok && data.ok && data.suggestions && data.suggestions.length){ bridge.noteActivity(); showVision(data); }
        else if(manual) bridge.toast('Δεν αναγνωρίστηκε. Δοκίμασε πιο κοντά.', true);
      } catch (err) {
        if(manual) bridge.toast('Δεν αναγνωρίστηκε.', true);
      } finally {
        busy = false; cooling = 15;
      }
    }, 'image/jpeg', 0.82);
  }
  function learn(id){
    if(!lastFrame) return;
    const fd = new FormData(); fd.append('consumable_id', String(id)); fd.append('frame', lastFrame, 'frame.jpg');
    fetch('/consumables/take/nextgen/vision/learn', {method:'POST', body:fd, headers:AJAX}).catch(()=>{});
    lastFrame = null;
  }
  function showVision(data){
    const list = data.suggestions.map((s, i) => candidateCard(s, i === 0 && data.confident,
      '<button type="button" class="ngAct go" data-take="'+esc(s.id)+'">− Πήρα 1</button><button type="button" class="ngAct" data-open="'+esc(s.id)+'">Άλλη ποσότητα</button>')).join('');
    panel.innerHTML = '<h2>📷 Μήπως είναι αυτό;</h2><p class="ngSub">Πάτα το σωστό είδος. Έτσι το κιόσκι μαθαίνει.</p><div class="ngList">'+list+'</div>'
      + '<div class="ngFoot"><button type="button" class="ngAct" data-close>Κανένα από αυτά</button></div>';
    panel.hidden = false;
  }

  // ------------------------------------------------------------------ voice take
  let recording = null, voiceLines = [], voiceMode = 'take';
  function speechFailed(){ try { return localStorage.getItem(SPEECH_KEY) === '1'; } catch (err) { return false; } }
  function markSpeechFailed(){ try { localStorage.setItem(SPEECH_KEY, '1'); } catch (err) {} }
  function listening(text){
    panel.innerHTML = '<h2>🎤 Σε ακούω…</h2><p class="ngSub">π.χ. «πήρα δύο κούτες γάντια μίντιουμ»</p><p class="ngHeard" id="ngHeard">'+esc(text||'')+'</p><div class="ngWave" id="ngWave"></div>'
      + '<div class="ngFoot"><button type="button" class="ngAct" data-close>Άκυρο</button><button type="button" class="ngAct go" data-stop>Τέλος</button></div>';
    panel.hidden = false;
  }
  function startVoice(){
    bridge.noteActivity();
    if(SR && !speechFailed()) return startWebSpeech();
    if(cfg.server_stt) return startRecorder();
    micBtn.hidden = true; refreshDock();
  }
  function startWebSpeech(){
    const rec = new SR();
    rec.lang = 'el-GR'; rec.interimResults = true; rec.maxAlternatives = 1; rec.continuous = false;
    let finalText = '', failed = false;
    listening('');
    micBtn.classList.add('live');
    rec.onresult = ev => {
      let text = '';
      for(let i = 0; i < ev.results.length; i++) text += ev.results[i][0].transcript;
      finalText = text;
      const heard = document.getElementById('ngHeard'); if(heard) heard.textContent = text;
    };
    rec.onerror = ev => {
      if(['network','not-allowed','service-not-allowed','language-not-supported'].includes(ev.error)){
        failed = true; markSpeechFailed();
      }
    };
    rec.onend = () => {
      micBtn.classList.remove('live'); recording = null;
      if(failed){ if(cfg.server_stt) startRecorder(); else { closePanel(); micBtn.hidden = true; refreshDock(); bridge.toast('Η φωνητική αναγνώριση δεν είναι διαθέσιμη', true); } return; }
      if(finalText.trim()) parseText(finalText); else { closePanel(); bridge.toast('Δεν ακούστηκε τίποτα', true); }
    };
    recording = {stop: () => rec.stop(), abort: () => { finalText = ''; rec.abort(); }};
    try { rec.start(); } catch (err) { failed = true; rec.onend(); }
  }
  async function startRecorder(){
    let media;
    try { media = await navigator.mediaDevices.getUserMedia({audio:{channelCount:1, echoCancellation:true, noiseSuppression:true}}); }
    catch (err) { bridge.toast('Δεν άνοιξε το μικρόφωνο', true); return; }
    const type = ['audio/webm;codecs=opus','audio/ogg;codecs=opus','audio/webm'].find(t => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || '';
    const rec = new MediaRecorder(media, type ? {mimeType:type} : undefined);
    const chunks = [];
    let cancelled = false, spoke = false, quietSince = 0;
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const analyser = ctx.createAnalyser(); analyser.fftSize = 512;
    ctx.createMediaStreamSource(media).connect(analyser);
    const buf = new Uint8Array(analyser.fftSize);
    const started = Date.now();
    listening('');
    micBtn.classList.add('live');
    const meter = setInterval(() => {
      analyser.getByteTimeDomainData(buf);
      let sum = 0; for(let i = 0; i < buf.length; i++){ const v = (buf[i]-128)/128; sum += v*v; }
      const rms = Math.sqrt(sum / buf.length), wave = document.getElementById('ngWave');
      if(wave) wave.style.setProperty('--lvl', Math.min(100, Math.round(rms*400))+'%');
      if(rms > 0.06){ spoke = true; quietSince = 0; } else if(spoke && !quietSince) quietSince = Date.now();
      if((spoke && quietSince && Date.now()-quietSince > 1200) || Date.now()-started > 7000) stop();
    }, 100);
    function stop(){ if(rec.state !== 'inactive') rec.stop(); }
    rec.ondataavailable = e => { if(e.data && e.data.size) chunks.push(e.data); };
    rec.onstop = async () => {
      clearInterval(meter); media.getTracks().forEach(t => t.stop()); ctx.close().catch(()=>{});
      micBtn.classList.remove('live'); recording = null;
      if(cancelled) return;
      if(!spoke){ closePanel(); bridge.toast('Δεν ακούστηκε τίποτα', true); return; }
      listening('…');
      const fd = new FormData(); fd.append('audio', new Blob(chunks, {type: rec.mimeType || 'audio/webm'}), 'take.webm');
      try {
        const res = await fetch('/consumables/take/nextgen/voice/transcribe', {method:'POST', body:fd, headers:AJAX});
        showDraft(await res.json());
      } catch (err) { closePanel(); bridge.toast('Δεν έγινε η αναγνώριση φωνής', true); }
    };
    recording = {stop: stop, abort: () => { cancelled = true; stop(); }};
    rec.start(250);
  }
  async function parseText(text){
    const fd = new FormData(); fd.append('text', text);
    try {
      const res = await fetch('/consumables/take/nextgen/voice/parse', {method:'POST', body:fd, headers:AJAX});
      showDraft(await res.json());
    } catch (err) { closePanel(); bridge.toast('Δεν έγινε η αναγνώριση', true); }
  }
  function showDraft(data){
    if(!data || !data.ok || !data.lines || !data.lines.length){
      panel.innerHTML = '<h2>🎤 Δεν το κατάλαβα</h2><p class="ngSub">'+esc((data && (data.message || data.text)) || '')+'</p><p class="ngSub">Πες π.χ. «πήρα δύο κούτες γάντια μίντιουμ».</p>'
        + '<div class="ngFoot"><button type="button" class="ngAct" data-close>Κλείσιμο</button><button type="button" class="ngAct go" data-again>Ξανά</button></div>';
      panel.hidden = false; return;
    }
    voiceMode = data.mode === 'add' ? 'add' : 'take';
    voiceLines = data.lines.slice();
    renderLine(data.text);
  }
  function renderLine(text){
    const line = voiceLines[0];
    if(!line){ closePanel(); return; }
    const verb = voiceMode === 'add' ? '+ Έβαλα' : '− Πήρα';
    if(!line.candidates.length){
      panel.innerHTML = '<h2>🎤 «'+esc(text || line.heard)+'»</h2><p class="ngSub">Δεν βρέθηκε είδος για «'+esc(line.heard)+'».</p>'
        + '<div class="ngFoot"><button type="button" class="ngAct" data-skip>Επόμενο</button><button type="button" class="ngAct go" data-again>Ξανά</button></div>';
      panel.hidden = false; return;
    }
    const cands = line.candidates.map((c, i) => candidateCard(c, i === 0 && line.confident,
      '<button type="button" class="ngAct '+(voiceMode==='add'?'add':'go')+'" data-voice="'+esc(c.id)+'" data-qty="'+esc(c.qty)+'">'+verb+' '+esc(c.qty)+'</button>')).join('');
    panel.innerHTML = '<h2>🎤 «'+esc(text || line.heard)+'»</h2><p class="ngSub">'+(line.confident ? 'Σωστά; Πάτα για να καταχωρηθεί.' : 'Ποιο από αυτά;')+'</p><div class="ngList">'+cands+'</div>'
      + '<div class="ngFoot"><button type="button" class="ngAct" data-close>Άκυρο</button>'+(voiceLines.length > 1 ? '<button type="button" class="ngAct" data-skip>Παράλειψη</button>' : '')+'<button type="button" class="ngAct" data-again>Ξανά</button></div>';
    panel.hidden = false;
    if(line.confident) say('/consumables/take/nextgen/voice/confirm?item='+encodeURIComponent(line.candidates[0].id)+'&qty='+encodeURIComponent(line.candidates[0].qty)+'&mode='+voiceMode);
  }

  // ------------------------------------------------------------------ events
  camBtn.addEventListener('click', () => { bridge.noteActivity(); capture(null, true); });
  micBtn.addEventListener('click', () => { if(recording) recording.stop(); else startVoice(); });
  panel.addEventListener('click', ev => {
    const t = ev.target.closest('button');
    if(!t) return;
    if(t.hasAttribute('data-close')){ if(recording) recording.abort(); closePanel(); return; }
    if(t.hasAttribute('data-stop')){ if(recording) recording.stop(); return; }
    if(t.hasAttribute('data-again')){ closePanel(); startVoice(); return; }
    if(t.hasAttribute('data-skip')){ voiceLines.shift(); renderLine(''); return; }
    if(t.dataset.take){ learn(t.dataset.take); closePanel(); bridge.move(t.dataset.take, 1, 'take'); return; }
    if(t.dataset.open){ learn(t.dataset.open); closePanel(); bridge.focusItem(t.dataset.open, 1); return; }
    if(t.dataset.voice){
      const id = t.dataset.voice, qty = Number(t.dataset.qty) || 1;
      voiceLines.shift();
      closePanel();
      if(!bridge.move(id, qty, voiceMode)) bridge.toast('Δεν υπάρχει αρκετό απόθεμα', true);
    }
  });
  document.addEventListener('kiosk:moved', () => { if(voiceLines.length) setTimeout(() => renderLine(''), 900); });
  if(cfg.vision || cfg.voice_take){ detectHardware(); setInterval(detectHardware, 5 * 60 * 1000); }
})();
