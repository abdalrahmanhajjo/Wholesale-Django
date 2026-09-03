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
