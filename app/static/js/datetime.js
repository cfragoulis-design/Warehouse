(function () {
  function pad2(n) { return String(n).padStart(2, '0'); }

  function formatDateTime(now) {
    // Europe/Athens local formatting (browser locale still applies)
    // Date: DD/MM/YYYY, Time: HH:MM (24h)
    const dd = pad2(now.getDate());
    const mm = pad2(now.getMonth() + 1);
    const yyyy = now.getFullYear();
    const hh = pad2(now.getHours());
    const min = pad2(now.getMinutes());
    return `${dd}/${mm}/${yyyy} • ${hh}:${min}`;
  }

  function update() {
    const el = document.getElementById('app-datetime');
    if (!el) return;
    el.textContent = formatDateTime(new Date());
  }

  // Initial paint
  update();

  // Align updates to the next minute boundary, then every minute
  const now = new Date();
  const msToNextMinute = (60 - now.getSeconds()) * 1000 - now.getMilliseconds();
  setTimeout(function () {
    update();
    setInterval(update, 60000);
  }, Math.max(0, msToNextMinute));
})();
