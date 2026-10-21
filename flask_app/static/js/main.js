/* ═══════════════════════════════════════════════════════════════════
   Garden Stage Controlling — main.js
   Lightweight utilities: number formatting, flash dismissal,
   table sorting, confirm dialogs.
═══════════════════════════════════════════════════════════════════ */

"use strict";

// ── Number formatting ────────────────────────────────────────────────
/**
 * Format a number as Serbian RSD  e.g. 1.234.567,89 RSD
 */
function fmtRSD(value, decimals = 0) {
  if (value === null || value === undefined || isNaN(value)) return "—";
  return new Intl.NumberFormat("sr-RS", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(value) + " RSD";
}

/**
 * Format a number with thousand separators
 */
function fmtNum(value, decimals = 0) {
  if (value === null || value === undefined || isNaN(value)) return "—";
  return new Intl.NumberFormat("sr-RS", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(value);
}

// ── Flash message auto-dismiss ───────────────────────────────────────
(function initFlash() {
  const flashes = document.querySelectorAll(".flash");
  flashes.forEach(el => {
    setTimeout(() => {
      el.style.transition = "opacity .4s";
      el.style.opacity = "0";
      setTimeout(() => el.remove(), 400);
    }, 4000);
  });
})();

// ── Confirm dialogs on dangerous buttons ────────────────────────────
(function initConfirm() {
  document.addEventListener("click", function (e) {
    const btn = e.target.closest("[data-confirm]");
    if (!btn) return;
    const msg = btn.dataset.confirm || "Da li ste sigurni?";
    if (!confirm(msg)) e.preventDefault();
  });
})();

// ── Client-side table sort ───────────────────────────────────────────
(function initTableSort() {
  document.querySelectorAll("table.data-table").forEach(function (table) {
    const headers = table.querySelectorAll("thead th[data-sort]");
    headers.forEach(function (th) {
      th.style.cursor = "pointer";
      th.title = "Klikni za sortiranje";

      th.addEventListener("click", function () {
        const col = Array.from(th.parentElement.children).indexOf(th);
        const type = th.dataset.sort || "str";
        const tbody = table.querySelector("tbody");
        const rows = Array.from(tbody.querySelectorAll("tr"));

        const asc = th.dataset.dir !== "asc";
        th.dataset.dir = asc ? "asc" : "desc";

        // Reset other header arrows
        headers.forEach(h => { if (h !== th) delete h.dataset.dir; });

        rows.sort(function (a, b) {
          const aVal = a.cells[col]?.textContent.trim() ?? "";
          const bVal = b.cells[col]?.textContent.trim() ?? "";

          if (type === "num") {
            // Strip non-numeric except minus and dot/comma
            const parse = s => parseFloat(s.replace(/\./g, "").replace(",", ".")) || 0;
            return asc ? parse(aVal) - parse(bVal) : parse(bVal) - parse(aVal);
          }
          if (type === "date") {
            const pd = s => new Date(s.split(".").reverse().join("-") || s);
            return asc ? pd(aVal) - pd(bVal) : pd(bVal) - pd(aVal);
          }
          // default: string
          return asc
            ? aVal.localeCompare(bVal, "sr")
            : bVal.localeCompare(aVal, "sr");
        });

        rows.forEach(r => tbody.appendChild(r));
      });
    });
  });
})();

// ── DB status indicator ──────────────────────────────────────────────
(function checkDB() {
  const el = document.getElementById("db-status");
  if (!el) return;
  fetch("/api/ping")
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(data => {
      if (data.ok) {
        el.textContent = el.dataset.connected || "● DB povezana";
        el.classList.add("connected");
      }
    })
    .catch(() => {
      el.textContent = el.dataset.error || "✕ DB greška";
      el.classList.add("error");
    });
})();
