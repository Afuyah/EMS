/* Small vanilla helpers: toasts and destructive-action confirmations. */
(function () {
  "use strict";

  /** Show a transient toast in the bottom-right corner. */
  function toast(message, category) {
    var host = document.getElementById("toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "toast-host";
      host.className = "fixed bottom-4 right-4 z-50 space-y-2 no-print";
      document.body.appendChild(host);
    }

    var tone = category === "error"
      ? "bg-rose-600"
      : category === "info"
        ? "bg-slate-700"
        : "bg-emerald-600";

    var node = document.createElement("div");
    node.className =
      tone +
      " rounded-lg px-4 py-2 text-sm font-medium text-white shadow-lg transition-opacity duration-300";
    node.textContent = message;
    host.appendChild(node);

    setTimeout(function () {
      node.style.opacity = "0";
      setTimeout(function () { node.remove(); }, 300);
    }, 4000);
  }

  /** Confirm before submitting anything marked data-confirm. */
  function bindConfirmations(root) {
    (root || document).querySelectorAll("[data-confirm]").forEach(function (el) {
      if (el.dataset.confirmBound === "1") return;
      el.dataset.confirmBound = "1";
      el.addEventListener("submit", function (event) {
        if (!window.confirm(el.getAttribute("data-confirm"))) {
          event.preventDefault();
        }
      });
      el.addEventListener("click", function (event) {
        if (el.tagName === "A" && !window.confirm(el.getAttribute("data-confirm"))) {
          event.preventDefault();
        }
      });
    });
  }

  /** Copy a value to the clipboard, used for device secrets. */
  function bindCopyButtons(root) {
    (root || document).querySelectorAll("[data-copy]").forEach(function (el) {
      if (el.dataset.copyBound === "1") return;
      el.dataset.copyBound = "1";
      el.addEventListener("click", function () {
        var value = el.getAttribute("data-copy");
        if (navigator.clipboard) {
          navigator.clipboard.writeText(value).then(function () {
            toast("Copied to clipboard", "success");
          });
        }
      });
    });
  }

  /**
   * Session expiry must not litter the page with sign-in pages.
   *
   * HTMX follows redirects transparently, so an expired session used to fetch
   * the sign-in page and swap its HTML into whatever fragment was refreshing.
   * With the board, the punch stream and the notification bell all polling, that
   * stacked several sign-in pages inside the page the user was on.
   *
   * The server now answers HTMX with `HX-Redirect` (a real navigation) instead
   * of a redirect, so this is the belt to that braces: a single guard so only
   * one navigation is ever started, plus a clear message rather than a silent
   * jump.
   */
  var redirectingToSignIn = false;

  function goToSignIn(message) {
    if (redirectingToSignIn) return;
    redirectingToSignIn = true;
    // Stop every polling element so nothing else fires mid-navigation.
    if (window.htmx && typeof window.htmx.trigger === "function") {
      document.querySelectorAll("[hx-trigger]").forEach(function (el) {
        el.removeAttribute("hx-trigger");
      });
    }
    if (message) toast(message, "info");
    var target = "/app/login";
    window.setTimeout(function () {
      window.location.assign(target + "?next=" + encodeURIComponent(
        window.location.pathname + window.location.search
      ));
    }, message ? 900 : 0);
  }

  document.addEventListener("DOMContentLoaded", function () {
    bindConfirmations(document);
    bindCopyButtons(document);

    document.body.addEventListener("htmx:afterSwap", function (event) {
      bindConfirmations(event.target);
      bindCopyButtons(event.target);
    });

    document.body.addEventListener("htmx:responseError", function (event) {
      var status = event.detail && event.detail.xhr ? event.detail.xhr.status : 0;
      if (status === 401 || status === 403) {
        goToSignIn("Your session expired. Please sign in again.");
        return;
      }
      toast("The server rejected that request.", "error");
    });

    // A sign-in page arriving where a fragment was expected means the session
    // expired somewhere that did not set HX-Redirect. Catch it before the swap
    // so the page is never replaced by a stray sign-in form.
    document.body.addEventListener("htmx:beforeSwap", function (event) {
      var xhr = event.detail && event.detail.xhr;
      if (!xhr) return;
      if (xhr.getResponseHeader("HX-Redirect")) {
        // HTMX handles this itself; cancel our own swap for safety.
        event.detail.shouldSwap = false;
        goToSignIn("Your session expired. Please sign in again.");
        return;
      }
      var body = xhr.responseText || "";
      var looksLikeSignIn =
        body.indexOf('action="/app/login"') !== -1 ||
        body.indexOf("name=\"password\"") !== -1;
      var expectedFragment = event.detail.target &&
        event.detail.target.id &&
        event.detail.target.id !== "attendance-board";
      if (looksLikeSignIn && expectedFragment) {
        event.detail.shouldSwap = false;
        goToSignIn("Your session expired. Please sign in again.");
      }
    });

    document.body.addEventListener("htmx:sendError", function () {
      toast("Network error — could not reach the server.", "error");
    });
  });

  // Session keep-alive: rotate cookies quietly every 10 minutes.
  setInterval(function () {
    if (redirectingToSignIn) return;
    fetch("/app/refresh-session", { method: "POST", credentials: "same-origin" })
      .then(function (response) {
        // A 401 here means the refresh token itself is gone: the session is
        // over, so say so once instead of letting the next poll discover it.
        if (response.status === 401) {
          goToSignIn("Your session expired. Please sign in again.");
        }
      })
      .catch(function () { /* handled on next navigation */ });
  }, 10 * 60 * 1000);

  window.emsToast = toast;
})();
