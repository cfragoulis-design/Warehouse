/* Admin step-up for AJAX: when a sensitive request answers 401
 * {error:"step_up_required"}, ask for the admin PIN inline, confirm it, and
 * retry the very same request. The page and whatever was typed stay put. */
(function(){
  if(window.__whStepUp) return;
  window.__whStepUp = true;
  var nativeFetch = window.fetch.bind(window);
  var pending = null;

  function el(tag, css, text){
    var node = document.createElement(tag);
    if(css) node.setAttribute('style', css);
    if(text) node.textContent = text;
    return node;
  }

  function askPin(){
    if(pending) return pending;
    pending = new Promise(function(resolve){
      var shade = el('div', 'position:fixed;inset:0;z-index:10000;background:rgba(2,6,23,.78);display:flex;align-items:center;justify-content:center;padding:16px');
      shade.id = 'stepUpPrompt';
      shade.setAttribute('role', 'dialog');
      shade.setAttribute('aria-modal', 'true');
      var box = el('form', 'width:min(360px,100%);display:grid;gap:10px;background:#0e1530;color:#e7e9ee;border:1px solid #243055;border-radius:14px;padding:18px;font-family:system-ui,Arial,sans-serif');
      var title = el('strong', 'font-size:20px', 'PIN διαχειριστή');
      var hint = el('span', 'color:#a7b0c7', 'Η αλλαγή είναι ευαίσθητη. Γράψε το PIN σου για να συνεχίσει.');
      var input = el('input', 'font:inherit;padding:12px;border-radius:10px;border:1px solid #243055;background:#0b1020;color:inherit');
      input.type = 'password';
      input.inputMode = 'numeric';
      input.autocomplete = 'off';
      input.setAttribute('aria-label', 'PIN');
      var err = el('span', 'min-height:20px;color:#fecdd3;font-weight:700', '');
      var row = el('div', 'display:flex;gap:8px;justify-content:flex-end');
      var cancel = el('button', 'font:inherit;padding:10px 14px;border-radius:10px;border:1px solid #243055;background:#162044;color:inherit', 'Άκυρο');
      cancel.type = 'button';
      var ok = el('button', 'font:inherit;padding:10px 14px;border-radius:10px;border:0;background:#2563eb;color:#fff;font-weight:800', 'Συνέχεια');
      ok.type = 'submit';
      row.appendChild(cancel); row.appendChild(ok);
      [title, hint, input, err, row].forEach(function(n){ box.appendChild(n); });
      shade.appendChild(box);
      document.body.appendChild(shade);
      input.focus();
      function done(result){ shade.remove(); resolve(result); }
      cancel.addEventListener('click', function(){ done(false); });
      box.addEventListener('submit', function(event){
        event.preventDefault();
        var fd = new FormData();
        fd.set('pin', input.value || '');
        fd.set('next_path', location.pathname);
        ok.disabled = true;
        nativeFetch('/admin/confirm-pin', {method:'POST', body:fd, credentials:'same-origin', headers:{'X-Requested-With':'XMLHttpRequest','Accept':'application/json'}})
          .then(function(res){ return res.json().catch(function(){ return {ok:false}; }).then(function(data){ return {res:res, data:data}; }); })
          .then(function(r){
            ok.disabled = false;
            if(r.res.ok && r.data && r.data.ok){ done(true); return; }
            input.value = '';
            err.textContent = (r.data && r.data.message) || 'Λάθος PIN';
            input.focus();
          })
          .catch(function(){ ok.disabled = false; err.textContent = 'Δεν υπάρχει σύνδεση'; });
      });
    }).finally(function(){ pending = null; });
    return pending;
  }

  window.fetch = function(input, init){
    var retryInput = (typeof Request !== 'undefined' && input instanceof Request) ? input.clone() : input;
    return nativeFetch(input, init).then(function(res){
      if(res.status !== 401) return res;
      return res.clone().json().catch(function(){ return null; }).then(function(data){
        if(!data || data.error !== 'step_up_required') return res;
        return askPin().then(function(confirmed){
          if(!confirmed) return res;
          return nativeFetch(retryInput, init);
        });
      });
    });
  };
})();
