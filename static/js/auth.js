/*
 * The behaviour the sign-in, sign-up, reset and password-change pages need.
 *
 * All of it is enhancement. Without this file the forms submit exactly as they
 * do with it: the password is typed into a normal password field and the server
 * decides whether it is acceptable. Nothing here gates a submission.
 */
(function () {
  "use strict";

  /* ===================================================== reveal =========*/
  // Typing a password you cannot see, into a field that then rejects it for a
  // reason you cannot check, is the most avoidable failure on a sign-in page.
  // The button is added here rather than in the template so a page that never
  // loads this script does not show a control that does nothing.
  document.querySelectorAll('input[type="password"]').forEach(function (field) {
    // The control wraps the input alone, so the button lands on the box rather
    // than below whatever the field-wrap also contains. Falling back to
    // .auth-field keeps an older cached copy of this script from placing the
    // button somewhere absurd if it meets newer markup.
    var wrap = field.closest(".auth-control") || field.closest(".auth-field");
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

  /* ================================================== requirements ======*/
  // The checklist states the rules the server actually applies, and nothing
  // else. AUTH_PASSWORD_VALIDATORS enforces a minimum length, refuses an
  // all-numeric password, refuses common ones, and refuses anything too close
  // to the account's own details — so those are the four lines shown. Adding
  // "one uppercase, one symbol" would look reassuring and be untrue: the
  // server neither requires them nor accepts a password just because it has
  // them.
  var COMMON = [
    "password", "passw0rd", "123456", "12345678", "123456789", "qwerty",
    "abc123", "letmein", "welcome", "admin", "iloveyou", "monkey", "dragon",
    "football", "baseball", "sunshine", "princess", "trustno1", "changeme"
  ];

  function nearby(field) {
    // The other things the account knows about this person. Django compares
    // the password against exactly these, so the hint can too.
    var form = field.form;
    if (!form) return [];
    var names = ["username", "email", "full_name", "first_name", "last_name"];
    var values = [];
    names.forEach(function (name) {
      var el = form.elements[name];
      if (el && el.value && el.value.length > 2) values.push(el.value.toLowerCase());
    });
    return values;
  }

  function similar(value, others) {
    var lower = value.toLowerCase();
    return others.some(function (other) {
      if (lower.indexOf(other) !== -1 || other.indexOf(lower) !== -1) return true;
      var local = other.split("@")[0];
      return local.length > 2 && lower.indexOf(local) !== -1;
    });
  }

  var RULES = [
    { text: "At least 8 characters", test: function (v) { return v.length >= 8; } },
    { text: "Not only numbers", test: function (v) { return !/^\d+$/.test(v); } },
    {
      text: "Not a password in common use",
      test: function (v) {
        var lower = v.toLowerCase();
        return !COMMON.some(function (c) { return lower === c || lower.indexOf(c) === 0; });
      }
    },
    {
      text: "Not too like your name or email",
      test: function (v, field) { return !similar(v, nearby(field)); }
    }
  ];

  document.querySelectorAll("[data-strength]").forEach(function (field) {
    var list = document.createElement("ul");
    list.className = "auth-rules";
    // Decorative as a live region: the meter's status label already speaks the
    // verdict, and announcing four rules on every keystroke is unusable.
    list.setAttribute("aria-hidden", "true");

    var items = RULES.map(function (rule) {
      var li = document.createElement("li");
      li.innerHTML =
        '<svg class="icon" aria-hidden="true"><use href="#i-check"></use></svg>' +
        "<span>" + rule.text + "</span>";
      list.appendChild(li);
      return li;
    });

    (field.closest(".field-wrap") || field.parentNode).appendChild(list);

    field.addEventListener("input", function () {
      var value = field.value;
      RULES.forEach(function (rule, i) {
        items[i].classList.toggle("is-met", Boolean(value) && rule.test(value, field));
      });
      list.classList.toggle("is-active", Boolean(value));
    });
  });

  /* ======================================================= match ========*/
  // Two password boxes that disagree is the most common reason a request form
  // comes back rejected. Saying so while it is still on screen costs a round
  // trip nobody wanted.
  document.querySelectorAll("[data-confirms]").forEach(function (field) {
    var form = field.form;
    if (!form) return;
    var first = form.elements[field.dataset.confirms];
    if (!first) return;

    var note = document.createElement("p");
    note.className = "auth-match";
    note.setAttribute("role", "status");
    (field.closest(".field-wrap") || field.parentNode).appendChild(note);

    function check() {
      if (!field.value) {
        note.textContent = "";
        note.classList.remove("is-ok", "is-bad");
        field.removeAttribute("aria-invalid");
        return;
      }
      var same = field.value === first.value;
      note.textContent = same ? "Both entries match." : "The two entries don't match yet.";
      note.classList.toggle("is-ok", same);
      note.classList.toggle("is-bad", !same);
      // aria-invalid and the styling can never disagree, because one sets both.
      if (same) field.removeAttribute("aria-invalid");
      else field.setAttribute("aria-invalid", "true");
    }

    field.addEventListener("input", check);
    first.addEventListener("input", function () { if (field.value) check(); });
  });

  /* ======================================================== busy ========*/
  // Every auth button already carried data-busy-label, but the script that
  // reads it (forms.js) is not loaded on these pages — so the button advertised
  // a loading state that never arrived and a slow network looked like a dead
  // form. Same behaviour, without pulling in the whole form runtime.
  document.querySelectorAll(".auth-panel form").forEach(function (form) {
    form.addEventListener("submit", function () {
      if (form.dataset.submitting === "1") return;
      form.dataset.submitting = "1";

      var buttons = form.querySelectorAll('button[type="submit"]');
      buttons.forEach(function (button) {
        button.dataset.idleLabel = button.innerHTML;
        button.disabled = true;
        button.classList.add("is-busy");
        button.innerHTML =
          '<span class="spinner" aria-hidden="true"></span><span>' +
          (button.dataset.busyLabel || "Working…") + "</span>";
      });

      // Restored from the back/forward cache, the page must not still spin.
      window.addEventListener("pageshow", function restore(e) {
        if (!e.persisted) return;
        window.removeEventListener("pageshow", restore);
        form.dataset.submitting = "0";
        buttons.forEach(function (button) {
          button.disabled = false;
          button.classList.remove("is-busy");
          if (button.dataset.idleLabel) button.innerHTML = button.dataset.idleLabel;
        });
      });
    });
  });
})();
