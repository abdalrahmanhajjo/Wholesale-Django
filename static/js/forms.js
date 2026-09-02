/*
 * Form behaviour: live validation, searchable selects, submit state, and the
 * guards that stop people losing work.
 *
 * Everything here is an enhancement. The server validates independently and
 * remains the authority — nothing below can let an invalid document through,
 * and with JavaScript off every form still works exactly as it did.
 *
 * The validation rules and their bounds come from `data-rule`, `data-min`,
 * `data-max` and `data-decimals`, which apps/core/form_ui.py derives from the
 * model. Neither side can drift from the other by being edited alone.
 */
(function () {
  "use strict";

  var VALID_AFTER = 3;      // characters before a field is worth judging
  var DEBOUNCE_MS = 350;

  /* =====================================================================
   * Rules
   * ===================================================================*/

  // Deliberately permissive. This runs while someone is typing, so its job is
  // to catch the obvious mistake — a missing @, a stray letter in an amount —
  // not to be the authority on what is acceptable. The server decides that.
  var RULES = {
    email: {
      test: function (v) { return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(v); },
      message: "Enter an email address such as name@company.com."
    },
    url: {
      test: function (v) { return /^https?:\/\/[^\s.]+\.[^\s]{2,}$/.test(v); },
      message: "Enter a full web address, starting with https://"
    },
    phone: {
      test: function (v) { return /^[+()\-\s\d]{6,}$/.test(v) && (v.match(/\d/g) || []).length >= 6; },
      message: "Enter a phone number with at least six digits, for example +961 71 234 567."
    },
    date: {
      test: function (v) { return !isNaN(Date.parse(v)); },
      message: "Enter a date in the format YYYY-MM-DD."
    },
    integer: {
      test: function (v) { return /^-?\d+$/.test(v.trim()); },
      message: "Enter a whole number, with no decimal point."
    },
    decimal: {
      test: function (v) { return /^-?\d*\.?\d*$/.test(v.trim()) && /\d/.test(v); },
      message: "Enter a number. Use a full stop for the decimal point."
    },
    money: {
      test: function (v) { return /^-?\d*\.?\d*$/.test(v.trim()) && /\d/.test(v); },
      message: "Enter an amount, for example 1250.00"
    },
    text: { test: function () { return true; }, message: "" }
  };

  function problemWith(field) {
    var value = (field.value || "").trim();

    if (!value) {
      // Required is announced on submit, not while the field is still empty and
      // untouched — nagging someone before they have typed anything is noise.
      if (field.required && field.dataset.touched === "yes") {
        return labelOf(field) + " is required.";
      }
      return null;
    }

    var rule = RULES[field.dataset.rule] || RULES.text;
    if (!rule.test(value)) return rule.message;

    if (field.dataset.decimals) {
      var dot = value.indexOf(".");
      var places = dot === -1 ? 0 : value.length - dot - 1;
      var allowed = Number(field.dataset.decimals);
      if (places > allowed) {
        return allowed === 0
          ? "This value cannot have decimal places."
          : "Use at most " + allowed + " decimal place" + (allowed === 1 ? "" : "s") + ".";
      }
    }

    var numeric = parseFloat(value);
    if (!isNaN(numeric)) {
      if (field.dataset.min !== undefined && numeric < Number(field.dataset.min)) {
        return "Enter " + field.dataset.min + " or more.";
      }
      if (field.dataset.max !== undefined && numeric > Number(field.dataset.max)) {
        return "Enter " + field.dataset.max + " or less.";
      }
    }

    if (field.maxLength > 0 && value.length > field.maxLength) {
      return "Keep this to " + field.maxLength + " characters.";
    }
    return null;
  }

  function labelOf(field) {
    var label = field.id && document.querySelector('label[for="' + CSS.escape(field.id) + '"]');
    if (!label) return "This field";
    return label.textContent.replace(/\(optional\)/i, "").trim().replace(/\s+/g, " ");
  }

  /* =====================================================================
   * Showing the result
   * ===================================================================*/

  function liveNode(field) {
    var id = field.id + "_live";
    var node = document.getElementById(id);
    if (!node) {
      node = document.createElement("p");
      node.id = id;
      node.className = "field-live";
      // Not a live region: the message sits beside the field and is reached by
      // aria-describedby. Announcing every keystroke would be unusable.
      (field.closest(".field-wrap") || field.parentNode).appendChild(node);
    }
    return node;
  }

  function describe(field, id, on) {
    var parts = (field.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
    var at = parts.indexOf(id);
    if (on && at === -1) parts.push(id);
    if (!on && at !== -1) parts.splice(at, 1);
    if (parts.length) field.setAttribute("aria-describedby", parts.join(" "));
    else field.removeAttribute("aria-describedby");
  }

  function render(field, problem) {
    // A server-rendered error owns the field until the value changes; replacing
    // it with a client guess would contradict what the server actually said.
    if (field.dataset.serverError === "yes") return;

    var node = liveNode(field);
    if (problem) {
      node.className = "field-live field-live-error";
      node.innerHTML = '<span aria-hidden="true">&#9888;</span><span><span class="sr-only">Error: </span></span>';
      node.lastChild.appendChild(document.createTextNode(problem));
      field.setAttribute("aria-invalid", "true");
      describe(field, node.id, true);
    } else if ((field.value || "").trim() && field.dataset.touched === "yes") {
      // Confirming a hard field — a tax ID, an IBAN-like reference — is worth
      // the pixel. Nothing is announced; the tick is for the eye.
      node.className = "field-live field-live-ok";
      node.innerHTML = '<span aria-hidden="true">&#10003;</span><span>Looks right</span>';
      field.removeAttribute("aria-invalid");
      describe(field, node.id, false);
    } else {
      node.className = "field-live";
      node.textContent = "";
      field.removeAttribute("aria-invalid");
      describe(field, node.id, false);
    }
  }

  function check(field) { render(field, problemWith(field)); }

  /* =====================================================================
   * Wiring
   * ===================================================================*/

  function watch(field) {
    if (field.dataset.watched === "yes") return;
    field.dataset.watched = "yes";
    if (field.hasAttribute("aria-invalid")) field.dataset.serverError = "yes";

    var timer = null;

    field.addEventListener("input", function () {
      // The server's verdict is stale the moment the value changes.
      if (field.dataset.serverError === "yes") {
        field.dataset.serverError = "no";
        field.removeAttribute("aria-invalid");
      }
      clearTimeout(timer);
      timer = setTimeout(function () {
        // Judging a half-typed email is how live validation earns its bad name,
        // so a fresh field is left alone until it is long enough to mean
        // something. Once it has been visited, every keystroke is fair game.
        if (field.dataset.touched === "yes" || (field.value || "").trim().length >= VALID_AFTER) {
          check(field);
        }
      }, DEBOUNCE_MS);
    });

    field.addEventListener("blur", function () {
      field.dataset.touched = "yes";
      clearTimeout(timer);
      check(field);
    });
  }

  function watchAll(root) {
    (root || document).querySelectorAll("[data-rule]").forEach(watch);
  }

  /* =====================================================================
   * Searchable select
   * ===================================================================*/

  // Enhances a native <select> in place. The select keeps the value and keeps
  // submitting; the textbox in front of it only filters. If this script fails
  // to run, the untouched native control is still there.
  function combobox(select) {
    if (select.dataset.comboReady === "yes" || select.multiple) return;
    select.dataset.comboReady = "yes";

    var options = Array.prototype.slice.call(select.options);
    var wrap = document.createElement("div");
    wrap.className = "combo";
    select.parentNode.insertBefore(wrap, select);

    var input = document.createElement("input");
    input.type = "text";
    input.className = "field combo-input";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-autocomplete", "list");
    input.autocomplete = "off";
    input.placeholder = select.dataset.comboboxPlaceholder || "Type to search…";

    var list = document.createElement("ul");
    list.className = "combo-list";
    list.id = (select.id || "combo") + "_list";
    list.setAttribute("role", "listbox");
    list.hidden = true;
    input.setAttribute("aria-controls", list.id);

    // The <select> keeps its label; the textbox borrows the same name so a
    // screen reader announces the field rather than an anonymous combobox.
    var label = select.id && document.querySelector('label[for="' + CSS.escape(select.id) + '"]');
    if (label) input.setAttribute("aria-label", label.textContent.trim());

    wrap.appendChild(input);
    wrap.appendChild(list);
    wrap.appendChild(select);
    select.classList.add("combo-native");
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");

    var active = -1;
    var shown = [];

    function label_(option) { return option.textContent.trim(); }

    function sync() {
      var chosen = select.options[select.selectedIndex];
      input.value = chosen && chosen.value ? label_(chosen) : "";
    }

    function close() {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    }

    function pick(option) {
      select.value = option.value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
      sync();
      close();
    }

    function open(filter) {
      var needle = (filter || "").toLowerCase();
      shown = options.filter(function (o) {
        return o.value !== "" && label_(o).toLowerCase().indexOf(needle) !== -1;
      });
      list.innerHTML = "";
      if (!shown.length) {
        var empty = document.createElement("li");
        empty.className = "combo-empty";
        empty.textContent = "No match for “" + filter + "”";
        list.appendChild(empty);
      } else {
        shown.slice(0, 50).forEach(function (option, i) {
          var item = document.createElement("li");
          item.className = "combo-option";
          item.id = list.id + "_" + i;
          item.setAttribute("role", "option");
          item.setAttribute("aria-selected", String(option.value === select.value));
          item.textContent = label_(option);
          item.addEventListener("mousedown", function (e) { e.preventDefault(); pick(option); });
          list.appendChild(item);
        });
      }
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      active = -1;
    }

    function highlight(next) {
      var items = list.querySelectorAll(".combo-option");
      if (!items.length) return;
      if (active > -1) items[active].classList.remove("is-active");
      active = (next + items.length) % items.length;
      items[active].classList.add("is-active");
      input.setAttribute("aria-activedescendant", items[active].id);
      items[active].scrollIntoView({ block: "nearest" });
    }

    input.addEventListener("input", function () { open(input.value); });
    input.addEventListener("focus", function () { open(""); });
    input.addEventListener("blur", function () { setTimeout(function () { close(); sync(); }, 120); });

    input.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") { event.preventDefault(); if (list.hidden) open(input.value); highlight(active + 1); }
      else if (event.key === "ArrowUp") { event.preventDefault(); highlight(active - 1); }
      else if (event.key === "Enter") {
        if (!list.hidden && active > -1) { event.preventDefault(); pick(shown[active]); }
      } else if (event.key === "Escape") {
        if (!list.hidden) { event.stopPropagation(); close(); sync(); }
      } else if (event.key === "Tab") { close(); sync(); }
    });

    sync();
  }

  function comboAll(root) {
    (root || document).querySelectorAll("select[data-combobox]").forEach(combobox);
  }

  /* =====================================================================
   * Submit state, dirty guard, confirmations
   * ===================================================================*/

  function guard(form) {
    if (form.dataset.guarded === "yes") return;
    form.dataset.guarded = "yes";

    var submitting = false;
    var dirty = false;

    form.addEventListener("input", function () { dirty = true; });
    form.addEventListener("change", function () { dirty = true; });

    form.addEventListener("submit", function (event) {
      // Two clicks on a slow connection is how a payment gets recorded twice.
      if (submitting) { event.preventDefault(); return; }

      var buttons = form.querySelectorAll('button[type="submit"], input[type="submit"]');
      var confirmMessage = form.dataset.confirm;
      if (confirmMessage && !window.confirm(confirmMessage)) {
        event.preventDefault();
        return;
      }

      submitting = true;
      dirty = false;
      buttons.forEach(function (button) {
        button.dataset.idleLabel = button.innerHTML;
        button.disabled = true;
        button.classList.add("is-busy");
        button.innerHTML =
          '<span class="spinner" aria-hidden="true"></span><span>' +
          (button.dataset.busyLabel || "Saving…") + "</span>";
      });
      // If the browser restores this page from the back/forward cache, the
      // button must not still be spinning.
      window.addEventListener("pageshow", function restore(e) {
        if (!e.persisted) return;
        window.removeEventListener("pageshow", restore);
        submitting = false;
        buttons.forEach(function (button) {
          button.disabled = false;
          button.classList.remove("is-busy");
          if (button.dataset.idleLabel) button.innerHTML = button.dataset.idleLabel;
        });
      });
    });

    if (form.dataset.guardUnsaved !== "off") {
      window.addEventListener("beforeunload", function (event) {
        if (!dirty || submitting) return;
        event.preventDefault();
        event.returnValue = "";
      });
    }
  }

  /* =====================================================================
   * Start
   * ===================================================================*/

  function init(root) {
    watchAll(root);
    comboAll(root);
    (root || document).querySelectorAll("form[method='post']").forEach(guard);
  }

  init(document);

  // Rows added to a formset after load need the same treatment.
  document.addEventListener("ledgerwise:rows-added", function (event) {
    init(event.detail && event.detail.root ? event.detail.root : document);
  });

  // Anything with data-confirm outside a form (a link that deletes, say).
  document.addEventListener("click", function (event) {
    var el = event.target.closest("[data-confirm]:not(form)");
    if (!el || el.closest("form[data-confirm]")) return;
    if (!window.confirm(el.dataset.confirm)) event.preventDefault();
  });
})();
