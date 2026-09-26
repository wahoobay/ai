// services/dashboard/app/static/theme.js
// Colour palettes for the review and sign-in pages (theme.css): four
// palettes, each light and dark, chosen with a picker and a toggle in the
// page header and remembered in this browser. Loaded in <head>, not deferred,
// so the saved choice is on <html> before the page is first drawn. The
// template's own data-palette/data-mode attributes are the defaults, so the
// page still has a complete theme if this script never runs.
"use strict";

(() => {
  const PALETTES = [["blue", "Wahoo Blue"], ["green", "Wahoo Green"], ["reef", "Reef"],
    ["sand", "Sandbar"]];
  const KEY = { palette: "wb.palette", mode: "wb.mode" };
  const root = document.documentElement;
  // Storage can be blocked (private windows, strict settings): every access is
  // guarded, and a choice made on this page still sticks here via `memory`.
  const memory = {};
  function read(key) {
    if (key in memory) return memory[key];
    try {
      return window.localStorage.getItem(key);
    } catch (err) {
      return null;
    }
  }
  function write(key, value) {
    memory[key] = value;
    try {
      window.localStorage.setItem(key, value);
    } catch (err) {
      /* storage blocked: the choice lasts for this page only */
    }
  }
  const isPalette = (id) => PALETTES.some(([known]) => known === id);
  const isMode = (m) => m === "light" || m === "dark";

  function apply(palette, mode) {
    root.dataset.palette = palette;
    root.dataset.mode = mode;
  }

  const savedPalette = read(KEY.palette);
  const savedMode = read(KEY.mode);
  apply(isPalette(savedPalette) ? savedPalette : (root.dataset.palette || "blue"),
    isMode(savedMode) ? savedMode : (root.dataset.mode || "light"));

  function buildControls() {
    const slot = document.getElementById("theme-controls");
    if (!slot) return;
    const picker = document.createElement("select");
    picker.setAttribute("aria-label", "Colour palette");
    for (const [id, name] of PALETTES) {
      const option = document.createElement("option");
      option.value = id;
      option.textContent = name;
      picker.append(option);
    }
    picker.value = root.dataset.palette;
    picker.addEventListener("change", () => {
      write(KEY.palette, picker.value);
      apply(picker.value, root.dataset.mode);
    });

    const toggle = document.createElement("button");
    toggle.type = "button";
    const label = () => {
      const dark = root.dataset.mode === "dark";
      toggle.textContent = dark ? "☀ Light" : "☾ Dark";
      toggle.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
    };
    toggle.addEventListener("click", () => {
      const mode = root.dataset.mode === "dark" ? "light" : "dark";
      write(KEY.mode, mode);
      apply(root.dataset.palette, mode);
      label();
    });
    label();
    slot.replaceChildren(picker, toggle);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", buildControls);
  } else {
    buildControls();
  }
})();
