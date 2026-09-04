/*
 * Shared behaviour for every page.
 *
 * Two things only, both of which exist because the server-rendered pages
 * otherwise leave keyboard and screen-reader users stranded:
 *   1. moving focus to the error summary after a failed submit, and
 *   2. making the off-canvas sidebar behave like a dialog on small screens.
 */
(function () {
  "use strict";

  /* ------------------------------------------------------------- errors --*/
  // Django re-renders the page on a validation failure, so the user lands at
  // the top with no indication that anything failed. Focusing the summary makes
  // it the first thing announced and the first thing seen.
  var summary = document.getElementById("form-error-summary");
  if (summary) {
    summary.focus({ preventScroll: true });
    summary.scrollIntoView({ block: "nearest" });
  }

  // The summary links to #id_<field>; make the target land in view under the
  // sticky header rather than behind it.
  document.addEventListener("click", function (event) {
    var link = event.target.closest("#form-error-summary a[href^='#']");
    if (!link) return;
    var target = document.getElementById(decodeURIComponent(link.hash.slice(1)));
    if (!target) return;
    event.preventDefault();
    target.focus({ preventScroll: true });
    target.scrollIntoView({ block: "center" });
  });


  /* ------------------------------------------------------- messages ------*/
  // A message that cannot be dismissed sits there through the next three
  // things the user does. Errors stay until dismissed; confirmations retire
  // on their own.
  document.querySelectorAll("[data-dismissible]").forEach(function (alert) {
    var close = document.createElement("button");
    close.type = "button";
    close.className = "alert-close";
    close.setAttribute("aria-label", "Dismiss this message");
    close.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-close"></use></svg>';
    close.addEventListener("click", function () { alert.remove(); });
    alert.appendChild(close);

    var after = Number(alert.dataset.autodismiss || 0);
    if (!after) return;
    var timer = setTimeout(function () { alert.remove(); }, after);
    // Reading it, or tabbing into it, cancels the countdown.
    ["mouseenter", "focusin"].forEach(function (name) {
      alert.addEventListener(name, function () { clearTimeout(timer); });
    });
  });

  /* ----------------------------------------------------- back links ------*/
  // The link points at a real URL and works on its own. Only when the previous
  // page genuinely was that URL is it turned into a history step, so the
  // forward button keeps working and no duplicate entry is pushed.
  document.querySelectorAll("a[data-back-to]").forEach(function (link) {
    link.addEventListener("click", function (event) {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
      if (!document.referrer) return;
      try {
        var from = new URL(document.referrer);
        var to = new URL(link.href);
        if (from.origin === to.origin && from.pathname === to.pathname && history.length > 1) {
          event.preventDefault();
          history.back();
        }
      } catch (err) { /* a malformed referrer just means the plain link runs */ }
    });
  });

  /* -------------------------------------------------- nav progress -------*/
  // Server-rendered navigation gives no feedback between click and paint. The
  // bar only appears after 180ms, so quick pages never flash it.
  var progress = document.getElementById("nav-progress");
  if (progress) {
    var pending = null;
    document.addEventListener("click", function (event) {
      var link = event.target.closest("a[href]");
      if (!link || link.target || link.hasAttribute("download")) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
      if (link.getAttribute("href").charAt(0) === "#") return;
      try {
        if (new URL(link.href).origin !== window.location.origin) return;
      } catch (err) { return; }
      pending = setTimeout(function () {
        progress.hidden = false;
        progress.classList.add("is-running");
      }, 180);
    });
    window.addEventListener("pageshow", function () {
      clearTimeout(pending);
      progress.hidden = true;
      progress.classList.remove("is-running");
    });
  }

  /* ------------------------------------------------------------ sidebar --*/
  var sidebar = document.getElementById("sidebar");
  var toggle = document.getElementById("sidebar-toggle");
  if (!sidebar || !toggle) return;

  var scrim = null;
  var lastFocused = null;
  var DESKTOP = window.matchMedia("(min-width: 1024px)");

  function focusable() {
    return Array.prototype.filter.call(
      sidebar.querySelectorAll("a[href], button:not([disabled]), [tabindex]:not([tabindex='-1'])"),
      function (el) { return el.offsetParent !== null; }
    );
  }

  function isOpen() {
    return !sidebar.classList.contains("-translate-x-full");
  }

  // On desktop the sidebar is a permanent landmark, so none of the dialog
  // behaviour applies and it must never be hidden from assistive technology.
  function syncHidden() {
    if (DESKTOP.matches) {
      sidebar.removeAttribute("aria-hidden");
      sidebar.removeAttribute("inert");
      toggle.setAttribute("aria-expanded", "false");
      return;
    }
    var open = isOpen();
    toggle.setAttribute("aria-expanded", String(open));
    // Without this the off-screen nav still holds tab stops, so a keyboard user
    // tabs through a dozen invisible links before reaching the page.
    if (open) {
      sidebar.removeAttribute("inert");
      sidebar.removeAttribute("aria-hidden");
    } else {
      sidebar.setAttribute("inert", "");
      sidebar.setAttribute("aria-hidden", "true");
    }
  }

  function open() {
    lastFocused = document.activeElement;
    sidebar.classList.remove("-translate-x-full");
    scrim = document.createElement("div");
    scrim.className = "nav-scrim lg:hidden";
    scrim.addEventListener("click", close);
    document.body.appendChild(scrim);
    document.body.style.overflow = "hidden";
    syncHidden();
    var first = focusable()[0];
    if (first) first.focus();
  }

  function close() {
    sidebar.classList.add("-translate-x-full");
    if (scrim) { scrim.remove(); scrim = null; }
    document.body.style.overflow = "";
    syncHidden();
    if (lastFocused && document.contains(lastFocused)) lastFocused.focus();
  }

  toggle.addEventListener("click", function () {
    isOpen() ? close() : open();
  });

  document.addEventListener("keydown", function (event) {
    if (DESKTOP.matches || !isOpen()) return;
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (event.key !== "Tab") return;
    // Keep focus inside the drawer while it covers the page.
    var items = focusable();
    if (!items.length) return;
    var first = items[0];
    var last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  // Resizing past the breakpoint must not leave a scrim or a locked body.
  DESKTOP.addEventListener("change", function () {
    if (DESKTOP.matches && isOpen()) close();
    syncHidden();
  });

  syncHidden();
})();

/* =========================================================== sidebar nav ===
 * Three small behaviours on the navigation: filtering it, collapsing sections,
 * and collapsing the whole sidebar.
 *
 * All three degrade to nothing without JavaScript. The sections are
 * <details>, so they still open and close; the search box filters nothing and
 * the rows stay visible; the sidebar stays at full width. Nothing here is the
 * only way to reach a page.
 * ========================================================================= */
(function () {
  "use strict";

  var nav = document.querySelector("[data-nav]");
  if (!nav) return;

  var STORE_SECTIONS = "lw.nav.closed";
  var STORE_COLLAPSED = "lw.nav.collapsed";

  // Private-mode browsers throw on access rather than returning null.
  function read(key, fallback) {
    try {
      var raw = window.localStorage.getItem(key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch (e) { return fallback; }
  }
  function write(key, value) {
    try { window.localStorage.setItem(key, JSON.stringify(value)); } catch (e) {}
  }

  /* ---------------------------------------------------- section memory --- */
  var sections = Array.prototype.slice.call(nav.querySelectorAll("[data-nav-section]"));
  var closed = read(STORE_SECTIONS, []);
  if (!Array.isArray(closed)) closed = [];

  sections.forEach(function (section) {
    var key = section.getAttribute("data-nav-section");
    // A section holding the current page opens regardless of what was stored:
    // remembering a closed section is useful, hiding where you are is not.
    var hasActive = !!section.querySelector(".nav-link-active");
    if (!hasActive && closed.indexOf(key) !== -1) section.open = false;

    section.addEventListener("toggle", function () {
      var at = closed.indexOf(key);
      if (section.open && at !== -1) closed.splice(at, 1);
      else if (!section.open && at === -1) closed.push(key);
      write(STORE_SECTIONS, closed);
    });
  });

  /* ----------------------------------------------------------- filter --- */
  var search = document.getElementById("nav-search");
  var empty = nav.querySelector("[data-nav-empty]");
  var rows = Array.prototype.slice.call(nav.querySelectorAll("[data-nav-item]"));

  // Sections that are not collapsible still have a header and rows that must
  // disappear together, so the template wraps them.
  var loose = Array.prototype.slice.call(nav.querySelectorAll("[data-nav-block]"));

  function matchesIn(root) {
    return Array.prototype.slice.call(root.querySelectorAll("[data-nav-item]"))
      .some(function (row) { return !row.parentNode.hidden; });
  }

  function filter(term) {
    term = term.trim().toLowerCase();
    var any = false;

    // The <li> is hidden rather than the <a>, so the row leaves the flow
    // instead of leaving a gap behind it.
    rows.forEach(function (row) {
      var hit = !term || row.getAttribute("data-label").toLowerCase().indexOf(term) !== -1;
      row.parentNode.hidden = !hit;
      if (hit) any = true;
    });

    // A section with nothing left in it should go rather than sit there as an
    // empty header; one that still has a match has to be open to show it.
    sections.forEach(function (section) {
      var live = matchesIn(section);
      section.hidden = !!term && !live;
      if (term && live) section.open = true;
    });

    loose.forEach(function (block) {
      block.hidden = !!term && !matchesIn(block);
    });

    if (empty) empty.hidden = !term || any;
  }

  if (search) {
    search.addEventListener("input", function () { filter(search.value); });
    search.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        search.value = "";
        filter("");
        search.blur();
      }
    });

    // "/" focuses the box, the convention everywhere text is searched. Not
    // when the user is already typing somewhere.
    document.addEventListener("keydown", function (event) {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      var el = document.activeElement;
      var typing = el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
      if (typing) return;
      event.preventDefault();
      search.focus();
      search.select();
    });
  }

  /* -------------------------------------------------- collapse sidebar --- */
  var sidebar = document.getElementById("sidebar");
  var collapseBtn = document.getElementById("sidebar-collapse");

  function applyCollapsed(on) {
    if (!sidebar) return;
    sidebar.classList.toggle("sidebar-collapsed", on);
    if (collapseBtn) {
      collapseBtn.setAttribute("aria-expanded", String(!on));
      collapseBtn.setAttribute("aria-label", on ? "Expand navigation" : "Collapse navigation");
    }
    // Nothing to type into while the box is hidden.
    if (on && search) { search.value = ""; filter(""); }
  }

  if (collapseBtn && sidebar) {
    applyCollapsed(read(STORE_COLLAPSED, false) === true);
    collapseBtn.addEventListener("click", function () {
      var on = !sidebar.classList.contains("sidebar-collapsed");
      applyCollapsed(on);
      write(STORE_COLLAPSED, on);
    });
  }
})();
