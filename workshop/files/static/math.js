// Typeset the TeX that render.py leaves in .math elements, with the vendored KaTeX (#256).
document.querySelectorAll(".math").forEach(function (el) {
  try {
    katex.render(el.textContent, el, {
      displayMode: el.classList.contains("display"),
      throwOnError: false,
    });
  } catch (e) {
    // a malformed formula stays as its TeX source
  }
});
