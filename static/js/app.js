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
