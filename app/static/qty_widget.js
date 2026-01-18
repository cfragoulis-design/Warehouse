// Qty widget: base unit is PCS; BOX adds pcsPerBox per click.
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.qty-row').forEach((row) => {
    const display = row.querySelector('.qty-display');
    const unit = row.querySelector('.qty-unit');
    const minus = row.querySelector('.minus');
    const plus = row.querySelector('.plus');
    const pcsPerBox = Number(row.dataset.pcsPerBox || 1);

    const getValue = () => {
      const n = parseInt(display.value || '0', 10);
      return Number.isFinite(n) ? n : 0;
    };

    const setValue = (v) => {
      const n = Math.max(0, Math.round(v));
      display.value = String(n);
    };

    const step = () => (unit && unit.value === 'BOX' ? pcsPerBox : 1);

    if (minus) minus.addEventListener('click', () => setValue(getValue() - step()));
    if (plus) plus.addEventListener('click', () => setValue(getValue() + step()));
  });
});
