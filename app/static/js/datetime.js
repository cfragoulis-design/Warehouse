<script>
(function () {
  function pad2(n) { return String(n).padStart(2, '0'); }

  function formatDateTime(now) {
    // DD/MM/YYYY • HH:MM:SS
    const dd = pad2(now.getDate());
    const mm = pad2(now.getMonth() + 1);
    const yyyy = now.getFullYear();
    const hh = pad2(now.getHours());
    const min = pad2(now.getMinutes());
    const sec = pad2(now.getSeconds());
    return `${dd}/${mm}/${yyyy} • ${hh}:${min}:${sec}`;
  }

  function update() {
    const el = document.getElementById('app-datetime');
    if (!el) return;
    el.textContent = formatDateTime(new Date());
  }

  // Initial paint
  update();

  // Align to next second, then live every second
  const now = new Date();
  const msToNextSecond = 1000 - now.getMilliseconds();

  setTimeout(function () {
    update();
    setInterval(update, 1000);
  }, msToNextSecond);
})();
</script>
