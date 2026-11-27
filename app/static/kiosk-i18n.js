/* Kiosk English (EL/EN). Loaded only on the kiosk take page.
   The page itself is rendered in Greek exactly as before; when English is active this
   script swaps the visible Greek text (and placeholders / aria-labels) for English and
   puts the Greek back when the kiosk returns to Greek. Product names use the optional
   English name (data-name-en on each card) and fall back to the stored Greek name.

   Language rules:
   - the EN/ΕΛ button switches by hand (for someone who has not logged in yet);
   - tapping a name previews that worker's language on the PIN pad;
   - after a PIN login the kiosk follows the worker's preferred language;
   - when the session ends, or the idle screen appears, the kiosk goes back to Greek. */
(function(){
  'use strict';
  if(window.KioskI18n) return;

  const EXACT = {
    'Λήψη αναλωσίμων':'Take consumables',
    'Εκτός σύνδεσης –':'Offline –',
    'κινήσεις σε αναμονή':'moves waiting',
    'Είσαι:':'You are:',
    'Τέλος':'Done',
    'Αναίρεση':'Undo',
    'Έγινε. Το απόθεμα ενημερώθηκε.':'Done. Stock updated.',
    'Έγινε. Το απόθεμα αυξήθηκε.':'Done. Stock increased.',
    'Δεν υπάρχει διαθέσιμο απόθεμα.':'No stock available.',
    '← Κατηγορίες':'← Categories',
    'Κατηγορίες':'Categories',
    'Άλλες κατηγορίες':'Other categories',
    'Όλα':'All',
    'ΟΛ':'ALL',
    'ΚΜ':'CNT',
    'Αναζήτηση είδους…':'Search item…',
    'Αναζήτηση είδους':'Search item',
    'Μόνο χαμηλά':'Low only',
    'Μόνο διαθέσιμα':'Available only',
    'είδη':'items',
    'χαμηλά':'low',
    'Πρωινή καταμέτρηση':'Morning count',
    'Δεν υπάρχουν ακόμα αναλώσιμα.':'No consumables yet.',
    '· ★ συχνό σου':'· ★ your usual',
    'Μείον 1':'Minus 1',
    'Συν 1':'Plus 1',
    'Συν 5':'Plus 5',
    'Πακέτο':'Pack',
    'πακέτο':'pack',
    'Ποσότητα':'Quantity',
    '− Πήρα':'− I took',
    '+ Έβαλα':'+ I put back',
    'Κανένα είδος με αυτά τα φίλτρα.':'No items match these filters.',
    'Πρόσφατες κινήσεις':'Recent moves',
    'Δεν υπάρχουν κινήσεις ακόμα.':'No moves yet.',
    'Τι λείπει':'Running low',
    'Τελειώνει σύντομα':'Running out soon',
    'Άγγιξε την οθόνη για να συνεχίσεις':'Touch the screen to continue',
    'Τίποτα χαμηλό':'Nothing low',
    'Ποιος είσαι;':'Who are you?',
    'Άκυρο':'Cancel',
    'Πίσω':'Back',
    'Εργαστήριο':'Workshop',
    'Κεντρικό':'Central shop',
    'Παρτίδα και λήξη':'Batch and expiry',
    'Παρτίδα':'Batch',
    'Λήξη':'Expiry',
    'σκανάρισε ή γράψε':'scan or type',
    'Καταχώριση':'Save',
    'Χωρίς παρτίδα':'No batch',
    'Συσκευασίες':'Packs',
    'Χύμα':'Loose',
    'Σύνολο':'Total',
    'φύρα':'waste',
    'λάθος':'mistake',
    'χωρίς καταγραφή':'not recorded',
    'Μία συσκευασία λιγότερη':'One pack less',
    'Μία συσκευασία περισσότερη':'One pack more',
    'Ένα λιγότερο':'One less',
    'Ένα περισσότερο':'One more',
    'Ναι, σωστό':'Yes, correct',
    'Μεγάλη διαφορά. Σίγουρα;':'Big difference. Are you sure?',
    'Δεν καταχωρίστηκε':'Not saved',
    'Δεν υπάρχει σύνδεση':'No connection',
    'Δεν υπάρχει διαθέσιμο απόθεμα':'No stock available',
    'Η ποσότητα πρέπει να είναι μεγαλύτερη από το μηδέν':'Quantity must be more than zero',
    'Δεν ενημερώθηκε το απόθεμα':'Stock was not updated',
    'Το απόθεμα ενημερώθηκε':'Stock updated',
    'Μη έγκυρη απάντηση':'Invalid response',
    'Καμία αλλαγή':'No change',
    'Μη έγκυρη υπογραφή εργαζομένου':'Invalid worker signature',
    'Αναιρέθηκε':'Undone',
    'Δεν έγινε η αναίρεση':'Undo failed',
    'Δεν έχεις PIN — ζήτα από τον υπεύθυνο':'You have no PIN — ask the supervisor',
    'Λάθος PIN':'Wrong PIN',
    'Πολλές λάθος προσπάθειες. Περίμενε 5 λεπτά.':'Too many wrong tries. Wait 5 minutes.',
    'Γράψε παρτίδα και λήξη ή πάτα «Χωρίς παρτίδα»':'Enter batch and expiry or press “No batch”',
    'Η λήξη έχει ήδη περάσει':'The expiry date has already passed',
    'Χρειάζεται κάρτα':'Card needed',
    'Η κάρτα δεν ανήκει σε αυτόν τον εργαζόμενο':'This card belongs to someone else',
    'Η συνεδρία έληξε':'Session ended',
    'Αποθηκεύτηκε στη συσκευή':'Saved on this device',
    'Άγνωστος κωδικός':'Unknown code',
    'Κάρτα εντάξει':'Card OK',
    'Unknown worker':'Unknown worker',
    // PIN screens (login + change PIN)
    'Βάλε το PIN σου':'Enter your PIN',
    'Αλλαγή PIN':'Change PIN',
    'Βάλε το τωρινό PIN σου':'Enter your current PIN',
    'Διάλεξε νέο PIN (4 ψηφία)':'Choose a new PIN (4 digits)',
    'Ξαναβάλε το νέο PIN':'Enter the new PIN again',
    'Διάλεξε νέο δικό σου PIN':'Choose your own new PIN',
    'Τα δύο νέα PIN δεν είναι ίδια':'The two new PINs do not match',
    'Πολύ εύκολο PIN. Διάλεξε άλλο.':'PIN too easy. Choose another one.',
    'Το νέο PIN είναι ίδιο με το παλιό':'The new PIN is the same as the old one',
    'Το PIN θέλει 4 ψηφία':'The PIN needs 4 digits',
    'Το PIN άλλαξε':'PIN changed',
    'Δεν άλλαξε το PIN':'PIN not changed',
    // next-gen panel (camera / microphone)
    '📷 Δείξε προϊόν':'📷 Show item',
    '🎤 Πες τι πήρες':'🎤 Say what you took',
    'Δεν αναγνωρίστηκε. Δοκίμασε πιο κοντά.':'Not recognised. Try closer.',
    'Δεν αναγνωρίστηκε.':'Not recognised.',
    '− Πήρα 1':'− I took 1',
    'Άλλη ποσότητα':'Other quantity',
    '📷 Μήπως είναι αυτό;':'📷 Is it this one?',
    'Πάτα το σωστό είδος. Έτσι το κιόσκι μαθαίνει.':'Tap the right item. This is how the kiosk learns.',
    'Κανένα από αυτά':'None of these',
    '🎤 Σε ακούω…':'🎤 Listening…',
    'Δεν ακούστηκε τίποτα':'Nothing was heard',
    'Δεν άνοιξε το μικρόφωνο':'The microphone did not open',
    'Δεν έγινε η αναγνώριση φωνής':'Speech recognition failed',
    'Δεν έγινε η αναγνώριση':'Recognition failed',
    '🎤 Δεν το κατάλαβα':'🎤 I did not understand',
    'Κλείσιμο':'Close',
    'Ξανά':'Again',
    'Επόμενο':'Next',
    'Παράλειψη':'Skip',
    'Σωστά; Πάτα για να καταχωρηθεί.':'Correct? Tap to record it.',
    'Ποιο από αυτά;':'Which one?',
    'Δεν υπάρχει αρκετό απόθεμα':'Not enough stock',
    // categories as they are stored today (others stay as stored)
    'Γάντια':'Gloves',
    'Δίχτυα':'Nets',
    'Λάμες':'Blades',
    'Μπαχαρικά':'Spices',
    'Ξυλάκια':'Skewers',
    'Παναρίσματα':'Breadings',
    'Σάλτσες - Μαρινάτες':'Sauces - Marinades',
    'Σακούλες':'Bags',
    'Σκαφάκια':'Trays',
    'Χαρτικά':'Paper goods',
    'Λοιπά':'Other'
  };
  const PATTERNS = [
    [/^Είσαι: (.+)$/, m => 'You are: ' + m[1]],
    [/^συσκευασία (.+)$/, m => 'pack ' + m[1]],
    [/^(.+) διαθέσιμα$/, m => m[1] + ' available'],
    [/^ΧΑΜΗΛΟ • ελάχ\. (.+)$/, m => 'LOW • min ' + m[1]],
    [/^(\d+) είδη$/, m => m[1] + ' items'],
    [/^Πήρες (.+)$/, m => 'You took ' + m[1]],
    [/^Έβαλες (.+)$/, m => 'You put back ' + m[1]],
    [/^✓ Καταχωρίστηκε: (.+)$/, m => '✓ Saved: ' + m[1]],
    [/^Απορρίφθηκε κίνηση εκτός σύνδεσης: (.+)$/, m => 'Offline move rejected: ' + tr(m[1])],
    [/^σύστημα (.+?)( · (\d+) συσκ\. \+ (.+))?$/, m => 'system ' + m[1] + (m[2] ? ' · ' + m[3] + ' packs + ' + m[4] : '')],
    [/^(.+) · ~(\d+) μέρες(.*)$/, m => productName(m[1]) + ' · ~' + m[2] + ' days' + m[3]],
    [/^Δεν βρέθηκε είδος για «(.+)»\.$/, m => 'No item found for “' + m[1] + '”.'],
    [/^Πες π\.χ\. (.+)$/, m => 'Say e.g. ' + m[1]],
    [/^π\.χ\. (.+)$/, m => 'e.g. ' + m[1]]
  ];
  // aria-labels like "Όλα, 57 είδη, 2 χαμηλά" or "Συσκευασίες <item>"
  const WORDS = [[/\bΌλα\b/g,'All'],[/ είδη/g,' items'],[/ χαμηλά/g,' low'],[/^Συσκευασίες /,'Packs '],[/^Μέτρηση /,'Count '],[/^Αιτία /,'Reason '],[/^Νέο PIN για /,'New PIN for ']];

  const names = {};      // Greek product name -> English
  const back = {};       // English product name -> Greek (idle lists built while English was on)
  function loadNames(){
    document.querySelectorAll('.item[data-name-en]').forEach(card => {
      const h = card.querySelector('h3');
      const el = h ? (h.dataset.nameEl || h.textContent.trim()) : '';
      const en = (card.dataset.nameEn || '').trim();
      if(el && en){ names[el] = en; back[en] = el; if(h) h.dataset.nameEl = el; }
    });
  }
  function productName(text){ return names[text] || EXACT[text] || text; }

  function tr(text){
    const core = text.trim();
    if(!core) return text;
    let out = null;
    if(Object.prototype.hasOwnProperty.call(names, core)) out = names[core];
    else if(Object.prototype.hasOwnProperty.call(EXACT, core)) out = EXACT[core];
    else {
      for(const [re, fn] of PATTERNS){ const m = core.match(re); if(m){ out = fn(m); break; } }
    }
    if(out == null) return text;
    const lead = text.match(/^\s*/)[0], tail = text.match(/\s*$/)[0];
    return lead + out + tail;
  }
  function trAttr(text){
    const direct = tr(text);
    if(direct !== text) return direct;
    let out = text;
    WORDS.forEach(([re, rep]) => { out = out.replace(re, rep); });
    return out;
  }

  let lang = 'el';
  let manual = 'el';
  const done = new Map();   // Text node -> {el, en}
  const attrs = new Map();  // Element -> {name: {el, en}}
  let busy = false;
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEXTAREA']);
  const ATTRS = ['placeholder','aria-label','title'];

  function skipNode(node){
    for(let p = node.parentNode; p; p = p.parentNode){
      if(p.nodeType !== 1) continue;
      if(SKIP.has(p.tagName) || p.id === 'langToggle' || p.hasAttribute('data-i18n-skip')) return true;
    }
    return false;
  }
  function doText(node){
    if(skipNode(node)) return;
    const prev = done.get(node);
    if(prev && node.data === prev.en) return;
    const original = node.data;
    const next = tr(original);
    if(next !== original){ done.set(node, {el: original, en: next}); node.data = next; }
    else done.delete(node);
  }
  function doAttrs(el){
    if(el.id === 'langToggle') return;
    ATTRS.forEach(name => {
      if(!el.hasAttribute(name)) return;
      const value = el.getAttribute(name);
      const rec = attrs.get(el) || {};
      if(rec[name] && rec[name].en === value) return;
      const next = trAttr(value);
      if(next !== value){ rec[name] = {el: value, en: next}; attrs.set(el, rec); el.setAttribute(name, next); }
    });
  }
  function walk(root){
    if(!root) return;
    if(root.nodeType === 3){ doText(root); return; }
    if(root.nodeType !== 1 || SKIP.has(root.tagName)) return;
    doAttrs(root);
    const it = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    let n;
    while((n = it.nextNode())){ if(n.nodeType === 3) doText(n); else doAttrs(n); }
  }
  function restore(){
    done.forEach((rec, node) => { if(node.data === rec.en) node.data = rec.el; });
    done.clear();
    attrs.forEach((rec, el) => { Object.keys(rec).forEach(name => { if(el.getAttribute(name) === rec[name].en) el.setAttribute(name, rec[name].el); }); });
    attrs.clear();
    // Lists filled while English was on (e.g. idle "running low") get their Greek names back.
    const it = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while((n = it.nextNode())){
      const core = n.data.trim();
      if(core && Object.prototype.hasOwnProperty.call(back, core)) n.data = n.data.replace(core, back[core]);
    }
  }
  const observer = new MutationObserver(list => {
    if(busy || lang !== 'en') return;
    busy = true;
    try {
      list.forEach(m => {
        if(m.type === 'characterData') doText(m.target);
        else if(m.type === 'attributes') doAttrs(m.target);
        else m.addedNodes.forEach(walk);
      });
    } finally { busy = false; }
  });

  const toggle = document.getElementById('langToggle');
  function paintToggle(){
    if(!toggle) return;
    toggle.textContent = lang === 'en' ? 'ΕΛ' : 'EN';
    toggle.setAttribute('aria-label', lang === 'en' ? 'Ελληνικά' : 'English');
    toggle.setAttribute('aria-pressed', lang === 'en' ? 'true' : 'false');
  }
  function apply(next){
    next = next === 'en' ? 'en' : 'el';
    if(next === lang){ paintToggle(); return; }
    busy = true;
    try {
      lang = next;
      document.documentElement.lang = lang;
      document.body.classList.toggle('lang-en', lang === 'en');
      if(lang === 'en'){ loadNames(); walk(document.body); }
      else restore();
    } finally { busy = false; }
    paintToggle();
    document.dispatchEvent(new CustomEvent('kiosk:lang', {detail: {lang: lang}}));
  }

  window.KioskI18n = {
    lang: () => lang,
    t: text => (lang === 'en' ? tr(String(text)) : String(text)),
    set: next => { manual = next === 'en' ? 'en' : 'el'; apply(manual); },
    preview: next => apply(next),          // a name was tapped: that worker's language
    cancelPreview: () => apply(manual),    // picker closed without logging in
    session: next => apply(next),          // PIN login done: the worker's language
    reset: () => { manual = 'el'; apply('el'); }
  };

  observer.observe(document.body, {subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ATTRS});
  if(toggle) toggle.addEventListener('click', () => window.KioskI18n.set(lang === 'en' ? 'el' : 'en'));
  document.addEventListener('kiosk:session-start', ev => { const l = ev.detail && ev.detail.lang; if(l) window.KioskI18n.session(l); });
  document.addEventListener('kiosk:session-end', () => window.KioskI18n.reset());
  const idle = document.getElementById('idleScreen');
  if(idle){
    new MutationObserver(() => { if(!idle.hidden) window.KioskI18n.reset(); })
      .observe(idle, {attributes: true, attributeFilter: ['hidden']});
  }
  paintToggle();
  // The session check can finish before this deferred script runs (page reload mid-session).
  if(window.kioskSessionLang) window.KioskI18n.session(window.kioskSessionLang);
})();
