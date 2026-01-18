document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".qty-row").forEach(row => {
    const display = row.querySelector(".qty-display");
    const unit    = row.querySelector(".qty-unit");
    const minus   = row.querySelector(".minus");
    const plus    = row.querySelector(".plus");

    const pcsPerBox = Number(row.dataset.pcsPerBox || 1);

    function getValue(){
      return parseInt(display.value || "0", 10);
    }

    function setValue(v){
      display.value = Math.max(0, Math.round(v));
    }

    function step(){
      return unit.value === "BOX" ? pcsPerBox : 1;
    }

    minus.addEventListener("click", () => {
      setValue(getValue() - step());
    });

    plus.addEventListener("click", () => {
      setValue(getValue() + step());
    });
  });
});
