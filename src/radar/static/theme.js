// Sparat tema (ljust eller mörkt) sätts innan sidan ritas, så att den inte blinkar till i fel färger.
(function () {
  try {
    var theme = localStorage.getItem("radar.theme");
    if (theme === "light" || theme === "dark") document.documentElement.setAttribute("data-theme", theme);
  } catch (error) { /* lagring avstängd: systemets tema gäller */ }
})();
