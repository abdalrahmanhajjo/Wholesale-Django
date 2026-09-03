/*
 * Steps for a long form.
 *
 * One form, one submit, one request. The steps only show and hide sections that
 * are all present in the page, which matters for three reasons: nothing is
 * saved until the whole document is complete, the browser back button leaves
 * the page rather than losing a step's worth of typing, and with JavaScript off
 * every section is simply visible and the form works as it always did.
 *
 * A form opts in with data-steps on the <form> and data-step="Label" on each
 * section. Everything else — the nav, the buttons, the completion marks — is
 * built here, so a form gains steps by labelling its sections.
 */
(function () {
  "use strict";

  document.querySelectorAll("form[data-steps]").forEach(setup);

  function setup(form) {
    var panels = Array.prototype.slice.call(form.querySelectorAll("[data-step]"));
    if (panels.length < 2) return;

    var actions = form.querySelector(".form-actions");
    var current = 0;

    /* ---------------------------------------------------------------- nav */
    var nav = document.createElement("nav");
    nav.className = "steps";
    nav.setAttribute("aria-label", "Form steps");
    form.insertBefore(nav, panels[0]);

    var buttons = panels.map(function (panel, index) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "step";
      button.innerHTML =
        '<span class="step-index" aria-hidden="true">' + (index + 1) + "</span>" +
        '<span><span class="step-label"></span><span class="step-note"></span></span>';
      button.querySelector(".step-label").textContent = panel.dataset.step;
      button.addEventListener("click", function () { show(index); });
      nav.appendChild(button);
      panel.classList.add("step-panel");
      // Each panel is a region named by its step, so jumping to one announces
      // where you have arrived.
      panel.setAttribute("role", "group");
      panel.setAttribute("aria-label", panel.dataset.step);
      return button;
    });

    /* ------------------------------------------------------- prev / next */
    var back = document.createElement("button");
    back.type = "button";
    back.className = "btn-ghost";
    back.textContent = "Back";
    back.addEventListener("click", function () { show(current - 1); });

    var next = document.createElement("button");
    next.type = "button";
    next.className = "btn-primary";
    next.addEventListener("click", function () { show(current + 1); });

    if (actions) {
      actions.insertBefore(back, actions.firstChild);
      actions.insertBefore(next, back.nextSibling);
    }

    /* ------------------------------------------------------------- state */
    function fieldsIn(panel) {
      return Array.prototype.filter.call(
        panel.querySelectorAll("input, select, textarea"),
        function (f) { return f.type !== "hidden" && !f.disabled && f.name; }
      );
    }

    function mark(index) {
      var panel = panels[index];
      var button = buttons[index];
      var fields = fieldsIn(panel);
      var invalid = fields.filter(function (f) { return f.getAttribute("aria-invalid") === "true"; });
      var required = fields.filter(function (f) { return f.required; });
      var missing = required.filter(function (f) { return !(f.value || "").trim(); });
      var filled = fields.filter(function (f) { return (f.value || "").trim(); });

      button.classList.toggle("is-invalid", invalid.length > 0);
      button.classList.toggle("is-complete", invalid.length === 0 && missing.length === 0 && filled.length > 0);

      var note = button.querySelector(".step-note");
      if (invalid.length) {
        note.textContent = invalid.length + (invalid.length === 1 ? " problem" : " problems");
      } else if (missing.length) {
        note.textContent = missing.length + " still needed";
      } else if (filled.length) {
        note.textContent = "Complete";
      } else {
        note.textContent = "Not started";
      }
    }

    function markAll() { panels.forEach(function (_, i) { mark(i); }); }

    function show(index) {
      if (index < 0 || index >= panels.length) return;
      current = index;
      panels.forEach(function (panel, i) { panel.hidden = i !== index; });
      buttons.forEach(function (button, i) {
        if (i === index) button.setAttribute("aria-current", "step");
        else button.removeAttribute("aria-current");
      });

      back.hidden = index === 0;
      next.hidden = index === panels.length - 1;
      next.textContent = index < panels.length - 1 ? "Next: " + panels[index + 1].dataset.step : "";
      // The submit button belongs to the last step; showing it earlier invites
      // saving a half-filled document.
      form.querySelectorAll('button[type="submit"]').forEach(function (b) {
        b.hidden = index !== panels.length - 1;
      });

      markAll();
      // Move focus into the panel rather than leaving it on the nav button, so
      // the next Tab continues into the fields.
      var first = panels[index].querySelector("input, select, textarea, button");
      if (first) first.focus({ preventScroll: true });
      panels[index].scrollIntoView({ block: "nearest" });
    }

    form.addEventListener("input", markAll);
    form.addEventListener("change", markAll);

    // A rejected submit must land on the problem, not on step one.
    var firstBad = panels.findIndex(function (panel) {
      return panel.querySelector('[aria-invalid="true"], .error-text');
    });
    show(firstBad > -1 ? firstBad : 0);

    // Submitting from any step is fine — the whole form is in the request
    // either way — but the panels are unhidden first so a native validation
    // message on a hidden field can still be shown.
    form.addEventListener("submit", function () {
      panels.forEach(function (panel) { panel.hidden = false; });
    });
  }
})();
