/*
 * The two pieces of behaviour the sign-in, sign-up and reset pages need.
 *
 * Both are enhancements. Without this file the forms submit exactly as they do
 * with it: the password is typed into a normal password field and the server
 * decides whether it is acceptable.
 */
(function () {
  "use strict";

  /* ===================================================== reveal =========*/
  // Typing a password you cannot see, into a field that then rejects it for a
  // reason you cannot check, is the most avoidable failure on a sign-in page.
  // The button is added here rather than in the template so a page that never
  // loads this script does not show a control that does nothing.
  document.querySelectorAll('input[type="password"]').forEach(function (field) {
    var wrap = field.closest(".auth-field");
    if (!wrap) return;

    var button = document.createElement("button");
    button.type = "button";
    button.className = "auth-reveal";
    button.setAttribute("aria-controls", field.id || "");
    button.setAttribute("aria-pressed", "false");
    button.setAttribute("aria-label", "Show password");
    button.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-eye"></use></svg>';

    button.addEventListener("click", function () {
      var shown = field.type === "text";
      field.type = shown ? "password" : "text";
      button.setAttribute("aria-pressed", String(!shown));
      button.setAttribute("aria-label", shown ? "Show password" : "Hide password");
      button.querySelector("use").setAttribute("href", shown ? "#i-eye" : "#i-eye-off");
      // Keep the caret where it was; toggling type sends it to the end.
      var at = field.value.length;
      field.focus();
      try { field.setSelectionRange(at, at); } catch (err) { /* not all types allow it */ }
    });

    wrap.appendChild(button);
  });

  /* ==================================================== strength ========*/
  // Only where a password is being chosen, never on sign-in — scoring a
  // password someone already has tells them nothing they can act on.
  //
  // This is guidance, not a gate. Django's own validators decide what is
  // acceptable on submit; the meter exists so a rejection is not the first
  // feedback anyone receives.
  var LABELS = ["", "Too easy to guess", "Weak", "Reasonable", "Strong"];

  function score(value) {
    if (!value) return 0;
    var points = 0;
    if (value.length >= 8) points++;
    if (value.length >= 12) points++;
    if (/[a-z]/.test(value) && /[A-Z]/.test(value)) points++;
    if (/\d/.test(value) && /[^\w\s]/.test(value)) points++;
    // Length carries a password further than character classes do, so a long
    // passphrase is not penalised for being all lower case.
    if (value.length >= 16) points = Math.max(points, 3);
    if (/^(.)\1+$/.test(value) || /^(012|123|abc|qwe|password)/i.test(value)) points = 1;
    return Math.min(points, 4);
  }

  document.querySelectorAll("[data-strength]").forEach(function (field) {
    var meter = document.createElement("div");
    meter.className = "auth-meter";
    meter.dataset.score = "0";
    meter.innerHTML =
      '<div class="auth-meter-track" aria-hidden="true">' +
      "<span></span><span></span><span></span><span></span></div>" +
      '<span class="auth-meter-label"></span>';

    var label = meter.querySelector(".auth-meter-label");
    // Announced politely, so a screen reader hears the verdict without the
    // running commentary of every keystroke.
    label.setAttribute("role", "status");

    var wrap = field.closest(".field-wrap") || field.parentNode;
    wrap.appendChild(meter);

    var timer = null;
    field.addEventListener("input", function () {
      var value = score(field.value);
      meter.dataset.score = String(value);
      clearTimeout(timer);
      timer = setTimeout(function () {
        label.textContent = field.value ? LABELS[value] : "";
      }, 400);
    });
  });
})();
