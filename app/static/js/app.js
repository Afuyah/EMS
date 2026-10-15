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

  document.addEventListener("DOMContentLoaded", function () {
    bindConfirmations(document);
    bindCopyButtons(document);

    document.body.addEventListener("htmx:afterSwap", function (event) {
      bindConfirmations(event.target);
      bindCopyButtons(event.target);
    });

    document.body.addEventListener("htmx:responseError", function () {
      toast("The server rejected that request.", "error");
    });

    document.body.addEventListener("htmx:sendError", function () {
      toast("Network error — could not reach the server.", "error");
    });
  });

  // Session keep-alive: rotate cookies quietly every 10 minutes.
  setInterval(function () {
    fetch("/app/refresh-session", { method: "POST", credentials: "same-origin" })
      .catch(function () { /* handled on next navigation */ });
  }, 10 * 60 * 1000);

  window.emsToast = toast;
})();
