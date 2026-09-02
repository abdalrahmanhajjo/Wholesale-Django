/*
 * Form behaviour: live validation, searchable fields backed by the server,
 * value prefill, money formatting, submit state, and the guards that stop
 * people losing work.
 *
 * All of it is enhancement. The server validates independently and stays the
 * authority — nothing here can let an invalid document through, and with
 * JavaScript off every form still works exactly as it did.
 *
 * Rules and bounds come from data-rule, data-min, data-max and data-decimals,
 * which apps/core/form_ui.py derives from the model, so the client rule and the
 * server rule cannot drift by one of them being edited alone. Business checks
 * (a code already taken, stock short) are asked of the server at
 * /settings/check/, because only the database knows the answer.
 */
(function () {
  "use strict";

  var VALID_AFTER = 3;      // characters before a fresh field is worth judging
  var DEBOUNCE_MS = 350;
  var SEARCH_MS = 220;
  var RECENT_MAX = 5;

  /* =====================================================================
   * Small helpers
   * ===================================================================*/

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function debounce(fn, ms) {
    var timer = null;
    return function () {
      var args = arguments, self = this;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(self, args); }, ms);
    };
  }

  function getJSON(url) {
    return fetch(url, { headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error(String(r.status));
        return r.json();
      });
  }

  // localStorage is unavailable in some privacy modes and throws on access, so
  // every use is guarded and a failure simply means no recent list.
  function readStore(key, fallback) {
    try { return JSON.parse(window.localStorage.getItem(key)) || fallback; }
    catch (err) { return fallback; }
  }
  function writeStore(key, value) {
    try { window.localStorage.setItem(key, JSON.stringify(value)); }
    catch (err) { /* quota, private mode, or blocked site data */ }
  }

  /* =====================================================================
   * Rules the browser can judge on its own
   * ===================================================================*/

  // Deliberately permissive. These run while someone is typing, so the job is
  // to catch the obvious mistake — a missing @, a letter in an amount — not to
  // be the authority on what is acceptable.
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
      test: function (v) { return /^-?[\d,]*\.?\d*$/.test(v.trim()) && /\d/.test(v); },
      message: "Enter a number. Use a full stop for the decimal point."
    },
    money: {
      test: function (v) { return /^-?[\d,]*\.?\d*$/.test(v.trim()) && /\d/.test(v); },
      message: "Enter an amount, for example 1,250.00"
    },
    text: { test: function () { return true; }, message: "" }
  };

  function bare(value) { return (value || "").replace(/,/g, "").trim(); }

  function labelOf(field) {
    var label = field.id && document.querySelector('label[for="' + CSS.escape(field.id) + '"]');
    if (!label) return "This field";
    return label.textContent.replace(/\(optional\)/i, "").trim().replace(/\s+/g, " ");
  }

  function problemWith(field) {
    var value = (field.value || "").trim();

    if (!value) {
      // Required is announced on submit, or once the field has been visited —
      // never before someone has had a chance to type anything.
      if (field.required && field.dataset.touched === "yes") {
        return labelOf(field) + " is required.";
      }
      return null;
    }

    var rule = RULES[field.dataset.rule] || RULES.text;
    if (!rule.test(value)) return rule.message;

    var numeric = parseFloat(bare(value));

    if (field.dataset.decimals) {
      var clean = bare(value);
      var dot = clean.indexOf(".");
      var places = dot === -1 ? 0 : clean.length - dot - 1;
      var allowed = Number(field.dataset.decimals);
      if (places > allowed) {
        return allowed === 0
          ? "This value cannot have decimal places."
          : "Use at most " + allowed + " decimal place" + (allowed === 1 ? "" : "s") + ".";
      }
    }

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

  /* =====================================================================
   * Saying what happened, next to the field
   * ===================================================================*/

  function liveNode(field) {
    var id = field.id + "_live";
    var node = document.getElementById(id);
    if (!node) {
      node = el("p", "field-live");
      node.id = id;
      // Not a live region: the message sits beside the field and is reached
      // through aria-describedby. Announcing every keystroke would be unusable.
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

  var ICONS = { error: "&#9888;", warning: "&#9888;", ok: "&#10003;", info: "&#8505;" };
  var PREFIX = { error: "Error: ", warning: "Warning: ", ok: "", info: "" };

  function say(field, level, text) {
    var node = liveNode(field);
    var wrap = field.closest(".field-wrap");

    if (!text) {
      node.className = "field-live";
      node.textContent = "";
      field.removeAttribute("aria-invalid");
      describe(field, node.id, false);
      if (wrap) wrap.classList.remove("is-error", "is-warning", "is-ok");
      return;
    }

    node.className = "field-live field-live-" + level;
    node.innerHTML = '<span aria-hidden="true">' + ICONS[level] + "</span><span></span>";
    var body = node.lastChild;
    if (PREFIX[level]) {
      // The status is carried by a word as well as a colour and an icon.
      body.appendChild(Object.assign(el("span", "sr-only"), { textContent: PREFIX[level] }));
    }
    body.appendChild(document.createTextNode(text));

    if (level === "error") field.setAttribute("aria-invalid", "true");
    else field.removeAttribute("aria-invalid");
    describe(field, node.id, level === "error" || level === "warning");

    if (wrap) {
      wrap.classList.toggle("is-error", level === "error");
      wrap.classList.toggle("is-warning", level === "warning");
      wrap.classList.toggle("is-ok", level === "ok");
    }
  }

  function check(field) {
    // A server-rendered error owns the field until the value changes; replacing
    // it with a client guess would contradict what the server actually said.
    if (field.dataset.serverError === "yes") return;
    var problem = problemWith(field);
    if (problem) { say(field, "error", problem); return; }
    if ((field.value || "").trim() && field.dataset.touched === "yes") {
      // A tick on a hard field — a code, a tax ID — is worth the pixel. Nothing
      // is announced; it is for the eye.
      say(field, "ok", field.dataset.okText || "Looks right");
      askServer(field);
      return;
    }
    say(field, null, "");
  }

  /* =====================================================================
   * Business rules only the database can answer
   * ===================================================================*/

  var askServer = debounce(function (field) {
    var rule = field.dataset.check;
    if (!rule || !(field.value || "").trim()) return;
    var params = new URLSearchParams({ rule: rule, value: field.value.trim() });
    if (field.dataset.checkExclude) params.set("exclude", field.dataset.checkExclude);
    // Extra parameters a rule needs, read live from sibling fields.
    (field.dataset.checkWith || "").split(",").filter(Boolean).forEach(function (pair) {
      var bits = pair.split(":");
      var source = field.closest("form").querySelector('[name$="' + bits[1] + '"]');
      if (source && source.value) params.set(bits[0], source.value);
    });

    var token = (field.dataset.checkToken = String(Date.now()));
    getJSON("/settings/check/?" + params.toString())
      .then(function (data) {
        // A slower earlier request must not overwrite a newer answer.
        if (field.dataset.checkToken !== token) return;
        if (data.level === "ok") say(field, "ok", data.text || "Looks right");
        else say(field, data.level, data.text);
      })
      .catch(function () { /* offline or refused: the server still decides on submit */ });
  }, DEBOUNCE_MS);

  /* =====================================================================
   * Wiring one field
   * ===================================================================*/

  function watch(field) {
    if (field.dataset.watched === "yes") return;
    field.dataset.watched = "yes";
    if (field.hasAttribute("aria-invalid")) field.dataset.serverError = "yes";

    var wrap = field.closest(".field-wrap");
    if (wrap && (field.value || "").trim()) wrap.classList.add("is-filled");

    var run = debounce(function () {
      // Judging a half-typed email is how live validation earns its bad name,
      // so a fresh field is left alone until it is long enough to mean
      // something. Once visited, every keystroke is fair game.
      if (field.dataset.touched === "yes" || (field.value || "").trim().length >= VALID_AFTER) {
        check(field);
      }
    }, DEBOUNCE_MS);

    field.addEventListener("input", function () {
      if (field.dataset.serverError === "yes") {
        // The server's verdict is stale the moment the value changes.
        field.dataset.serverError = "no";
        field.removeAttribute("aria-invalid");
      }
      if (wrap) wrap.classList.toggle("is-filled", !!(field.value || "").trim());
      run();
    });

    field.addEventListener("focus", function () {
      if (wrap) wrap.classList.add("is-focused");
      unformatMoney(field);
    });

    field.addEventListener("blur", function () {
      field.dataset.touched = "yes";
      if (wrap) {
        wrap.classList.remove("is-focused");
        wrap.classList.toggle("is-filled", !!(field.value || "").trim());
      }
      formatMoney(field);
      check(field);
    });
  }

  /* =====================================================================
   * Money
   * ===================================================================*/

  // Grouped on blur, plain while editing, and stripped again on submit. Live
  // grouping mid-keystroke fights the caret; grouping on blur gives the same
  // readability with none of that, and the field the server receives is always
  // a clean decimal.
  function formatMoney(field) {
    if (field.dataset.rule !== "money") return;
    var raw = bare(field.value);
    if (!raw || isNaN(Number(raw))) return;
    var places = Number(field.dataset.decimals || 2);
    field.value = Number(raw).toLocaleString("en-US", {
      minimumFractionDigits: places,
      maximumFractionDigits: places
    });
  }

  function unformatMoney(field) {
    if (field.dataset.rule !== "money") return;
    if (field.value) field.value = bare(field.value);
  }

  function stripMoney(form) {
    form.querySelectorAll('[data-rule="money"]').forEach(function (field) {
      field.value = bare(field.value);
    });
  }

  function adornMoney(field) {
    if (field.dataset.rule !== "money" || field.dataset.adorned === "yes") return;
    var symbol = field.dataset.currency || document.body.dataset.baseCurrency;
    if (!symbol) return;
    field.dataset.adorned = "yes";
    var wrap = field.closest(".field-wrap");
    if (!wrap) return;
    wrap.classList.add("has-adornment");
    var tag = el("span", "field-adornment", symbol);
    tag.setAttribute("aria-hidden", "true");   // the label already says the currency
    field.parentNode.insertBefore(tag, field);
    field.classList.add("field-with-adornment");
  }

  /* =====================================================================
   * Searchable field, backed by the server
   * ===================================================================*/

  // Enhances a native <select> in place. The select keeps the value and keeps
  // submitting; the textbox in front of it only searches. If this never runs,
  // the untouched native control is still there and still works.
  function combobox(select) {
    if (select.dataset.comboReady === "yes" || select.multiple) return;
    select.dataset.comboReady = "yes";

    var kind = select.dataset.suggest || "";
    var storeKey = "ledgerwise.recent." + (kind || select.name);
    var options = Array.prototype.slice.call(select.options);

    var wrap = el("div", "combo");
    select.parentNode.insertBefore(wrap, select);

    var input = el("input", "field combo-input");
    input.type = "text";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-autocomplete", "list");
    input.autocomplete = "off";
    input.placeholder = select.dataset.comboboxPlaceholder || "Type to search…";
    if (select.required) input.required = true;

    var list = el("ul", "combo-list");
    list.id = (select.id || "combo") + "_list";
    list.setAttribute("role", "listbox");
    list.hidden = true;
    input.setAttribute("aria-controls", list.id);

    // The <select> keeps its label; the textbox borrows the same name so a
    // screen reader announces the field, not an anonymous combobox.
    var label = select.id && document.querySelector('label[for="' + CSS.escape(select.id) + '"]');
    if (label) {
      input.setAttribute("aria-label", label.textContent.replace(/\(optional\)/i, "").trim());
    } else if (select.getAttribute("aria-label")) {
      input.setAttribute("aria-label", select.getAttribute("aria-label"));
    }

    wrap.appendChild(input);
    wrap.appendChild(list);
    wrap.appendChild(select);
    select.classList.add("combo-native");
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");

    var active = -1;
    var shown = [];

    function textOf(option) { return option.textContent.trim(); }

    function sync() {
      var chosen = select.options[select.selectedIndex];
      input.value = chosen && chosen.value ? textOf(chosen) : "";
      var w = input.closest(".field-wrap");
      if (w) w.classList.toggle("is-filled", !!input.value);
    }

    function close() {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    }

    function remember(item) {
      if (!kind) return;
      var recent = readStore(storeKey, []).filter(function (r) { return r.value !== item.value; });
      recent.unshift({ value: item.value, label: item.label, detail: item.detail || "" });
      writeStore(storeKey, recent.slice(0, RECENT_MAX));
    }

    function pick(item) {
      // A server suggestion may not be among the rendered options, so it is
      // added before being selected — the value still has to pass the field's
      // queryset on the server, which is where it actually matters.
      if (!Array.prototype.some.call(select.options, function (o) { return o.value === item.value; })) {
        select.appendChild(new Option(item.label, item.value));
      }
      select.value = item.value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
      remember(item);
      sync();
      close();
      prefillFrom(select, kind, item.value);
    }

    function draw(items, heading) {
      list.innerHTML = "";
      shown = items;
      if (heading) {
        var head = el("li", "combo-heading", heading);
        head.setAttribute("role", "presentation");
        list.appendChild(head);
      }
      if (!items.length) {
        list.appendChild(el("li", "combo-empty", "No match. Check the spelling, or search on a code."));
      } else {
        items.forEach(function (item, i) {
          var node = el("li", "combo-option");
          node.id = list.id + "_" + i;
          node.setAttribute("role", "option");
          node.setAttribute("aria-selected", String(item.value === select.value));
          node.appendChild(el("span", "combo-option-label", item.label));
          if (item.detail) node.appendChild(el("span", "combo-option-detail", item.detail));
          node.addEventListener("mousedown", function (e) { e.preventDefault(); pick(item); });
          list.appendChild(node);
        });
      }
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      active = -1;
    }

    function localMatches(term) {
      var needle = term.toLowerCase();
      return options
        .filter(function (o) { return o.value !== "" && textOf(o).toLowerCase().indexOf(needle) !== -1; })
        .slice(0, 20)
        .map(function (o) { return { value: o.value, label: textOf(o), detail: "" }; });
    }

    var searchServer = debounce(function (term) {
      var token = (input.dataset.token = String(Date.now()));
      list.classList.add("is-loading");
      getJSON("/settings/suggest/" + encodeURIComponent(kind) + "/?q=" + encodeURIComponent(term))
        .then(function (data) {
          if (input.dataset.token !== token) return;   // a newer keystroke won
          list.classList.remove("is-loading");
          if (document.activeElement === input) draw(data.results, "");
        })
        .catch(function () {
          list.classList.remove("is-loading");
          // The server is unreachable; the rendered options are still here.
          if (document.activeElement === input) draw(localMatches(term), "Offline — searching this page only");
        });
    }, SEARCH_MS);

    function open(term) {
      if (!term) {
        var recent = kind ? readStore(storeKey, []) : [];
        if (recent.length) { draw(recent, "Recently used"); return; }
      }
      if (kind) { draw(localMatches(term || ""), ""); searchServer(term || ""); }
      else { draw(localMatches(term || ""), ""); }
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
    input.addEventListener("blur", function () { setTimeout(function () { close(); sync(); }, 140); });

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

  /* =====================================================================
   * What a chosen record implies for the rest of the form
   * ===================================================================*/

  function prefillFrom(select, kind, pk) {
    if (!kind || !pk) return;
    var form = select.closest("form");
    if (!form || select.dataset.prefill === "off") return;

    getJSON("/settings/suggest/" + encodeURIComponent(kind) + "/" + encodeURIComponent(pk) + "/prefill/")
      .then(function (data) {
        var filled = [];
        Object.keys(data.values || {}).forEach(function (name) {
          var target = form.querySelector('[name="' + name + '"], [name$="-' + name + '"]');
          if (!target || target === select) return;
          // Never overwrite something the user typed. A default is a starting
          // point, not a correction.
          if ((target.value || "").trim() && target.dataset.prefilled !== "yes") return;
          target.value = data.values[name];
          target.dataset.prefilled = "yes";
          target.dispatchEvent(new Event("change", { bubbles: true }));
          var w = target.closest(".field-wrap");
          if (w) {
            w.classList.add("is-prefilled", "is-filled");
            setTimeout(function () { w.classList.remove("is-prefilled"); }, 1400);
          }
          filled.push(labelOf(target));
        });
        showNotices(form, select, data.notices || [], filled);
      })
      .catch(function () { /* nothing filled in; the user types it themselves */ });
  }

  function showNotices(form, select, notices, filled) {
    var slot = form.querySelector("[data-notice-slot]");
    if (!slot) return;
    slot.innerHTML = "";

    if (filled.length) {
      notices = notices.concat([{
        level: "info",
        text: "Filled in from this record: " + filled.join(", ") + ". Change any of it if this document differs."
      }]);
    }
    notices.forEach(function (notice) {
      var box = el("div", "alert-" + (notice.level === "warning" ? "warning" : "info"));
      // A credit hold changes what the user should do next, so it is announced.
      box.setAttribute("role", notice.level === "warning" ? "alert" : "status");
      box.innerHTML = '<svg class="icon shrink-0" aria-hidden="true"><use href="#i-' +
        (notice.level === "warning" ? "alert" : "info") + '"></use></svg>';
      box.appendChild(el("p", "alert-body", notice.text));
      slot.appendChild(box);
    });
  }

  /* =====================================================================
   * Submit state, dirty guard, confirmations, drafts
   * ===================================================================*/

  function guard(form) {
    if (form.dataset.guarded === "yes") return;
    form.dataset.guarded = "yes";

    var submitting = false;
    var dirty = false;
    var draftKey = form.dataset.draftKey
      ? "ledgerwise.draft." + form.dataset.draftKey
      : null;

    restoreDraft(form, draftKey);

    var saveDraft = debounce(function () { writeDraft(form, draftKey); }, 900);

    form.addEventListener("input", function () { dirty = true; saveDraft(); });
    form.addEventListener("change", function () { dirty = true; saveDraft(); });

    form.addEventListener("submit", function (event) {
      // Two clicks on a slow connection is how a payment gets recorded twice.
      if (submitting) { event.preventDefault(); return; }

      if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
        event.preventDefault();
        return;
      }

      stripMoney(form);

      submitting = true;
      dirty = false;
      if (draftKey) { try { window.localStorage.removeItem(draftKey); } catch (e) {} }

      var buttons = form.querySelectorAll('button[type="submit"], input[type="submit"]');
      buttons.forEach(function (button) {
        button.dataset.idleLabel = button.innerHTML;
        button.disabled = true;
        button.classList.add("is-busy");
        button.innerHTML =
          '<span class="spinner" aria-hidden="true"></span><span>' +
          (button.dataset.busyLabel || "Saving…") + "</span>";
      });

      // A page restored from the back/forward cache must not still be spinning.
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

  // Drafts are per-browser and per-form, never sent anywhere. They exist so a
  // closed tab or a dropped connection does not cost an hour of typing; the
  // record itself is only ever created by a real submit.
  function writeDraft(form, key) {
    if (!key) return;
    var values = {};
    form.querySelectorAll("input, select, textarea").forEach(function (field) {
      if (!field.name || field.type === "hidden" || field.type === "password") return;
      if (field.type === "checkbox" || field.type === "radio") {
        if (field.checked) values[field.name] = field.value;
      } else if ((field.value || "").trim()) {
        values[field.name] = field.value;
      }
    });
    if (Object.keys(values).length) writeStore(key, { at: Date.now(), values: values });
  }

  function restoreDraft(form, key) {
    if (!key) return;
    var draft = readStore(key, null);
    if (!draft || !draft.values) return;
    // A draft older than a day is more likely to confuse than to help.
    if (Date.now() - (draft.at || 0) > 86400000) {
      try { window.localStorage.removeItem(key); } catch (e) {}
      return;
    }
    var slot = form.querySelector("[data-draft-slot]") || form.querySelector("[data-notice-slot]");
    if (!slot) return;

    var box = el("div", "alert-info");
    box.setAttribute("role", "status");
    box.innerHTML = '<svg class="icon shrink-0" aria-hidden="true"><use href="#i-info"></use></svg>';
    var body = el("div", "flex-1");
    body.appendChild(el("p", "alert-body",
      "You have unsaved entries on this form from " + new Date(draft.at).toLocaleString() + "."));
    var actions = el("div", "mt-2 flex gap-2");

    var restore = el("button", "btn-ghost btn-sm", "Restore them");
    restore.type = "button";
    restore.addEventListener("click", function () {
      Object.keys(draft.values).forEach(function (name) {
        var field = form.querySelector('[name="' + CSS.escape(name) + '"]');
        if (!field) return;
        if (field.type === "checkbox" || field.type === "radio") field.checked = true;
        else field.value = draft.values[name];
        field.dispatchEvent(new Event("change", { bubbles: true }));
      });
      box.remove();
    });

    var discard = el("button", "btn-ghost btn-sm", "Discard");
    discard.type = "button";
    discard.addEventListener("click", function () {
      try { window.localStorage.removeItem(key); } catch (e) {}
      box.remove();
    });

    actions.appendChild(restore);
    actions.appendChild(discard);
    body.appendChild(actions);
    box.appendChild(body);
    slot.appendChild(box);
  }

  /* =====================================================================
   * Start
   * ===================================================================*/

  function init(root) {
    var scope = root || document;
    scope.querySelectorAll("[data-rule]").forEach(function (field) {
      watch(field);
      adornMoney(field);
      formatMoney(field);
    });
    scope.querySelectorAll("select[data-combobox]").forEach(combobox);
    scope.querySelectorAll("form[method='post']").forEach(guard);
  }

  init(document);

  // Rows added to a formset after load need the same treatment.
  document.addEventListener("ledgerwise:rows-added", function (event) {
    init(event.detail && event.detail.root ? event.detail.root : document);
  });

  // Anything with data-confirm outside a form — a link that deletes, say.
  document.addEventListener("click", function (event) {
    var node = event.target.closest("[data-confirm]:not(form)");
    if (!node || node.closest("form[data-confirm]")) return;
    if (!window.confirm(node.dataset.confirm)) event.preventDefault();
  });
})();
