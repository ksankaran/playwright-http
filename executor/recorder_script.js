/**
 * Recorder overlay script — injected into the page via context.add_init_script().
 *
 * Captures user interactions (clicks, typing, navigation, selects, scrolls)
 * and sends structured events to the Playwright CDP session via
 * console.debug("__RECORDER__:" + JSON.stringify(event)).
 *
 * Also provides visual feedback: a blue outline on the element under cursor.
 *
 * NOTE: This script runs BEFORE document.documentElement exists, so all DOM
 * manipulation must be deferred until the DOM is ready.
 */
(function () {
  if (window.__CHECKMATE_RECORDER__) return;
  window.__CHECKMATE_RECORDER__ = true;

  // ── Visual highlight overlay (deferred until DOM is ready) ─────────────────
  let overlay = null;
  let lastHighlighted = null;

  function initOverlay() {
    if (overlay) return;
    const root = document.documentElement || document.body;
    if (!root) return;
    overlay = document.createElement("div");
    overlay.id = "__checkmate-overlay";
    Object.assign(overlay.style, {
      position: "fixed",
      pointerEvents: "none",
      border: "2px solid #0088ff",
      borderRadius: "3px",
      background: "rgba(0, 136, 255, 0.08)",
      zIndex: "2147483647",
      display: "none",
      transition: "all 0.05s ease-out",
    });
    root.appendChild(overlay);
  }

  // Initialize overlay when DOM is ready
  if (document.documentElement) {
    initOverlay();
  } else {
    document.addEventListener("DOMContentLoaded", initOverlay, { once: true });
    // Fallback: also try on first mousemove
  }

  document.addEventListener(
    "mousemove",
    (e) => {
      if (!overlay) initOverlay();
      if (!overlay) return;
      const el = document.elementFromPoint(e.clientX, e.clientY);
      if (!el || el === overlay) {
        overlay.style.display = "none";
        lastHighlighted = null;
        return;
      }
      if (el === lastHighlighted) return;
      lastHighlighted = el;
      const rect = el.getBoundingClientRect();
      Object.assign(overlay.style, {
        display: "block",
        top: rect.top + "px",
        left: rect.left + "px",
        width: rect.width + "px",
        height: rect.height + "px",
      });
    },
    true
  );

  // ── Helpers ────────────────────────────────────────────────────────────────

  function bestSelector(el) {
    if (!el || !el.tagName) return "";
    // 1. data-testid (most stable — always prefer over dynamic IDs)
    const testId = el.getAttribute("data-testid");
    if (testId) return `[data-testid="${CSS.escape(testId)}"]`;
    // 2. id (skip framework-generated IDs: radix-*, react-*, :r)
    if (el.id && !/^(radix-|react-|:r)/.test(el.id))
      return "#" + CSS.escape(el.id);
    // 3. aria-label
    const ariaLabel = el.getAttribute("aria-label");
    if (ariaLabel)
      return `${el.tagName.toLowerCase()}[aria-label="${CSS.escape(ariaLabel)}"]`;
    // 4. name attribute (for inputs)
    const name = el.getAttribute("name");
    if (name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
    // 5. title attribute (for icon-only buttons with no text, e.g. "Move to folder")
    const titleAttr = el.getAttribute("title");
    if (titleAttr && el.tagName === "BUTTON")
      return `button[title="${CSS.escape(titleAttr)}"]`;
    // 6. unique text content for buttons/links (short text only)
    if (
      (el.tagName === "BUTTON" || el.tagName === "A") &&
      el.textContent &&
      el.textContent.trim().length < 60
    ) {
      return el.tagName.toLowerCase();
    }
    // 7. Fallback: build a minimal path
    const tag = el.tagName.toLowerCase();
    const classes = Array.from(el.classList || [])
      .slice(0, 2)
      .map((c) => "." + CSS.escape(c))
      .join("");
    return tag + classes;
  }

  function labelFor(el) {
    if (!el) return null;
    // Check explicit <label for="...">
    if (el.id) {
      const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (label) return label.textContent.trim();
    }
    // Check wrapping <label>
    const parent = el.closest && el.closest("label");
    if (parent) {
      const clone = parent.cloneNode(true);
      // Remove the input itself to get just the label text
      const inputs = clone.querySelectorAll("input, select, textarea");
      inputs.forEach((i) => i.remove());
      const text = clone.textContent.trim();
      if (text) return text;
    }
    // aria-label
    const ariaLabel = el.getAttribute && el.getAttribute("aria-label");
    if (ariaLabel) return ariaLabel;
    // placeholder
    const placeholder = el.getAttribute && el.getAttribute("placeholder");
    if (placeholder) return placeholder;
    return null;
  }

  function visibleText(el) {
    if (!el) return "";
    var _cap = function(t) { return t.length > 80 ? t.slice(0, 77) + "..." : t; };
    // For interactive elements (links, buttons) prefer the most prominent
    // text to avoid concatenating heading + subtitle + URL from card-style links
    // (e.g. "Checkmate-qa" + "No description" + "http://..." → wrong).
    if (el.tagName === "A" || el.tagName === "BUTTON") {
      // 1. First direct text node
      for (const node of el.childNodes) {
        if (node.nodeType === 3) {
          const t = node.textContent.trim();
          if (t) return _cap(t);
        }
      }
      // 2. Heading inside the link (card-style: <a><div><h3>Title</h3><p>desc</p></div></a>)
      var heading = el.querySelector("h1, h2, h3, h4, h5, h6");
      if (heading) {
        const t = heading.textContent.trim();
        if (t) return _cap(t);
      }
      // 3. First child element's OWN text (skip if it's a wrapper with many children)
      for (const child of el.children) {
        // If child has 2+ child elements, it's probably a wrapper — skip to avoid
        // concatenating all its descendants' text into one string.
        if (child.children.length >= 2) continue;
        const t = child.textContent.trim();
        if (t) return _cap(t);
      }
      // 4. Final fallback: first child's text even if it's a wrapper
      for (const child of el.children) {
        const t = child.textContent.trim();
        if (t) return _cap(t);
      }
    }
    const text = (el.textContent || "").trim();
    return text.length > 80 ? text.slice(0, 77) + "..." : text;
  }

  // ── Locator Waterfall helpers ─────────────────────────────────────────────
  // Capture multiple locator strategies at recording time so the executor can
  // try them in order during playback (UTML → CSS → ariaPath → coordinates).

  /**
   * Count how many elements match the given CSS selector on the current page.
   * A score > 1 means the selector is ambiguous (multiple matches).
   */
  function ambiguityCount(selector) {
    try {
      return selector ? document.querySelectorAll(selector).length : 0;
    } catch { return 0; }
  }

  /**
   * Build an ancestor-contextual aria path for unambiguous element identification.
   * Walks UP the DOM collecting role + accessible name of each ancestor until
   * a landmark role or 3 levels are reached.
   *
   * Example output: "listitem[name='Test Case #47'] > button[name='Draft']"
   *
   * This resolves the "20 Draft buttons" problem — the ancestor context
   * (which list item) narrows the match to exactly 1.
   */
  function ariaPath(el) {
    if (!el || !el.tagName) return "";
    const parts = [];
    let current = el;
    const landmarkRoles = new Set([
      "banner", "complementary", "contentinfo", "form", "main",
      "navigation", "region", "search", "dialog",
    ]);
    let depth = 0;

    while (current && current !== document.body && depth < 4) {
      const role = current.getAttribute("role") || _implicitRole(current);
      const name = _accessibleName(current);

      if (role || name) {
        let segment = role || current.tagName.toLowerCase();
        if (name) segment += `[name='${name.replace(/'/g, "\\'")}']`;
        parts.unshift(segment);
        // Stop at landmark roles — they provide enough context
        if (landmarkRoles.has(role)) break;
      }
      current = current.parentElement;
      depth++;
    }
    return parts.join(" > ");
  }

  /** Map common HTML tags to their implicit ARIA role. */
  function _implicitRole(el) {
    const tag = el.tagName;
    if (tag === "BUTTON") return "button";
    if (tag === "A" && el.hasAttribute("href")) return "link";
    if (tag === "INPUT") {
      const t = (el.type || "text").toLowerCase();
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (t === "submit" || t === "button") return "button";
      return "textbox";
    }
    if (tag === "SELECT") return "combobox";
    if (tag === "TEXTAREA") return "textbox";
    if (tag === "IMG") return "img";
    if (tag === "NAV") return "navigation";
    if (tag === "MAIN") return "main";
    if (tag === "HEADER") return "banner";
    if (tag === "FOOTER") return "contentinfo";
    if (tag === "UL" || tag === "OL") return "list";
    if (tag === "LI") return "listitem";
    if (tag === "TABLE") return "table";
    if (tag === "TR") return "row";
    if (tag === "TD" || tag === "TH") return "cell";
    return "";
  }

  /** Get the accessible name of an element (aria-label, text, etc.) */
  function _accessibleName(el) {
    // Explicit aria-label
    const ariaLabel = el.getAttribute("aria-label");
    if (ariaLabel) return ariaLabel.trim().slice(0, 60);
    // Explicit aria-labelledby
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const labelEl = document.getElementById(labelledBy);
      if (labelEl) return labelEl.textContent.trim().slice(0, 60);
    }
    // For interactive elements, use visible text (but keep short)
    const tag = el.tagName;
    if (tag === "BUTTON" || tag === "A" || el.getAttribute("role")) {
      const text = visibleText(el);
      if (text && text.length <= 40) return text;
    }
    return "";
  }

  function emit(event) {
    event.timestamp = Date.now();
    event.url = location.href;
    console.debug("__RECORDER__:" + JSON.stringify(event));
  }

  // ── Input / typing capture ────────────────────────────────────────────────
  // We buffer keystrokes per field and flush when:
  //   - The field loses focus (focusout)
  //   - The user clicks somewhere else (mousedown — fires before click)
  //   - A different field receives input
  //   - A form is submitted
  let activeInput = null;
  let inputBuffer = "";

  function flushInput() {
    if (activeInput && inputBuffer !== "") {
      const el = activeInput;
      emit({
        type: "type",
        selector: bestSelector(el),
        tag: el.tagName,
        text: labelFor(el) || "",
        value: inputBuffer,
        is_password: el.type === "password",
      });
    }
    activeInput = null;
    inputBuffer = "";
  }

  function isInputLike(el) {
    if (!el) return false;
    const tag = el.tagName;
    // Check by tagName string (more reliable across frames than instanceof)
    if (tag === "INPUT" || tag === "TEXTAREA") return true;
    if (el.isContentEditable) return true;
    return false;
  }

  // Capture keystrokes via 'input' event
  document.addEventListener(
    "input",
    (e) => {
      const el = e.target;
      if (!isInputLike(el)) return;
      if (el !== activeInput) {
        flushInput();
        activeInput = el;
      }
      // contentEditable uses textContent, inputs use value
      inputBuffer = el.value !== undefined ? el.value : el.textContent || "";
    },
    true
  );

  // Catch values set by autofill, date pickers, or frameworks via 'change'
  document.addEventListener(
    "change",
    (e) => {
      const el = e.target;

      // Native <select> change — only emit if it's a visible select
      // (hidden selects controlled by custom components are handled by the MutationObserver)
      if (el.tagName === "SELECT") {
        const rect = el.getBoundingClientRect();
        const isHidden = rect.width < 5 || rect.height < 5;
        if (!isHidden) {
          const selectedOption = el.options && el.options[el.selectedIndex];
          emit({
            type: "select",
            selector: bestSelector(el),
            tag: "SELECT",
            text: labelFor(el) || "",
            value: selectedOption ? selectedOption.text : el.value,
          });
        }
        return;
      }

      // For inputs/textareas: if the value differs from our buffer (e.g.
      // autofill, date picker, programmatic set), capture it immediately
      if (isInputLike(el)) {
        const val = el.value !== undefined ? el.value : el.textContent || "";
        if (val && val !== inputBuffer) {
          // Flush any prior buffer for a different field first
          if (activeInput && activeInput !== el) flushInput();
          activeInput = el;
          inputBuffer = val;
        }
      }
    },
    true
  );

  // Flush on blur
  document.addEventListener("focusout", () => flushInput(), true);

  // Flush on mousedown — this fires BEFORE the click event, so any
  // pending type event is emitted before the click that follows
  document.addEventListener(
    "mousedown",
    (e) => {
      const el = e.target;
      if (el && el !== activeInput && activeInput) {
        flushInput();
      }
    },
    true
  );

  // Flush on form submit (catches Enter-to-submit without blur)
  document.addEventListener(
    "submit",
    () => flushInput(),
    true
  );

  // ── Custom dropdown / combobox capture ───────────────────────────────────
  // Handles shadcn/ui, Radix, and other headless UI libraries that use
  // role="combobox" + role="option" instead of native <select>.
  //
  // Strategy: MutationObserver on combobox button text + hidden select value.
  // When shadcn/ui selects an option it:
  //   1. Updates the <button role="combobox"> text content
  //   2. Sets the hidden <select>.value programmatically (no 'change' event)
  // We detect both via polling/MutationObserver for reliability.

  function findLabelFor(el) {
    if (!el) return "";
    // Look for a <label> sibling in the parent container
    const parent = el.parentElement;
    if (parent) {
      const label = parent.querySelector("label");
      if (label) return label.textContent.trim();
    }
    return labelFor(el) || "";
  }

  function observeComboboxes() {
    // Watch <button role="combobox"> text changes (shadcn/ui updates the button text on selection)
    const comboboxes = document.querySelectorAll("[role='combobox']");
    comboboxes.forEach((btn) => {
      if (btn.__checkmate_observed) return;
      btn.__checkmate_observed = true;

      let lastText = btn.textContent.trim();

      const obs = new MutationObserver(() => {
        const newText = btn.textContent.trim();
        if (newText && newText !== lastText) {
          const label = findLabelFor(btn);
          emit({
            type: "select",
            selector: bestSelector(btn),
            tag: btn.tagName,
            text: label,
            value: newText,
          });
          lastText = newText;
        }
      });

      obs.observe(btn, { childList: true, subtree: true, characterData: true });
    });

    // Watch hidden <select> value changes via polling (no DOM event fires)
    const selects = document.querySelectorAll("select");
    selects.forEach((sel) => {
      if (sel.__checkmate_observed) return;

      // Only poll hidden selects — visible ones are handled by the 'change' listener
      const rect = sel.getBoundingClientRect();
      const isHidden = rect.width < 5 || rect.height < 5;
      if (!isHidden) return;

      sel.__checkmate_observed = true;
      let lastValue = sel.value;

      const interval = setInterval(() => {
        if (!document.contains(sel)) { clearInterval(interval); return; }
        if (sel.value !== lastValue) {
          // Find the associated combobox button to get the human-readable label
          const parent = sel.parentElement;
          const combobox = parent && parent.querySelector("[role='combobox']");
          const label = combobox ? findLabelFor(combobox) : findLabelFor(sel);
          const selectedOption = sel.options[sel.selectedIndex];
          const optionText = selectedOption ? selectedOption.text : sel.value;
          // Only emit if there's no combobox observer (avoid double-emit)
          if (!combobox || !combobox.__checkmate_observed) {
            emit({
              type: "select",
              selector: bestSelector(sel),
              tag: "SELECT",
              text: label,
              value: optionText,
            });
          }
          lastValue = sel.value;
        }
      }, 150);
    });
  }

  // Seed on load + re-run when new elements appear
  document.addEventListener("DOMContentLoaded", observeComboboxes, { once: true });
  if (document.readyState !== "loading") observeComboboxes();

  new MutationObserver(observeComboboxes).observe(document.documentElement || document, {
    childList: true,
    subtree: true,
  });

  // ── Hover-trigger finder (used by click-time detection) ──────────────────
  // Given a clicked element inside a nav submenu, walks up the DOM to find
  // the visible sibling trigger (e.g. "Pricing") that opened the submenu.
  //
  // IMPORTANT: Must NOT fire for always-visible nav lists (sidebars, tab bars)
  // where all links are siblings at the same level. A hover trigger only makes
  // sense when the clicked element is NESTED inside a dropdown/submenu that was
  // revealed by hovering. We detect this by checking: if the clicked element's
  // own link/button ancestor is a direct sibling of the candidate trigger,
  // they're at the same level → flat nav, no hover needed.
  function findNavSubmenuTrigger(el) {
    const inNav = el.closest &&
      el.closest("nav, header, [role='menubar'], [role='navigation']");
    if (!inNav) return null;

    // Find the clicked element's nearest interactive ancestor (link/button)
    const clickedInteractive = el.closest && el.closest("a, button, [role='menuitem']");

    let node = el.parentElement;
    while (node && node !== inNav && node !== document.body) {
      const parent = node.parentElement;
      if (!parent) break;

      // Count how many sibling links/buttons exist at this level
      let siblingLinkCount = 0;
      let candidateTrigger = null;

      for (const sib of parent.children) {
        if (sib === node) continue;
        const sibTag = sib.tagName;
        if (sibTag !== "A" && sibTag !== "BUTTON" &&
            sib.getAttribute("role") !== "menuitem") continue;
        const sibStyle = window.getComputedStyle(sib);
        const sibVisible =
          sibStyle.display !== "none" &&
          sibStyle.visibility !== "hidden" &&
          parseFloat(sibStyle.opacity) !== 0 &&
          sib.offsetWidth > 0;
        if (!sibVisible) continue;
        siblingLinkCount++;
        if (!candidateTrigger) {
          const sibText = visibleText(sib).trim();
          if (sibText && sibText !== visibleText(el).trim()) {
            candidateTrigger = { text: sibText, selector: bestSelector(sib), tag: sibTag, element: sib };
          }
        }
      }

      // If there are 2+ sibling links at the same level as the clicked element's
      // container, this is a flat nav list (sidebar, tab bar), not a dropdown.
      // In a real hover-dropdown, the trigger is the ONLY link at its level and
      // the dropdown items are nested inside a child container.
      if (siblingLinkCount >= 2) {
        node = node.parentElement;
        continue;
      }

      // Also skip if the clicked interactive element is a direct sibling of the
      // candidate (same level = flat nav, not nested submenu).
      if (candidateTrigger && clickedInteractive &&
          candidateTrigger.element.parentElement === clickedInteractive.parentElement) {
        node = node.parentElement;
        continue;
      }

      if (candidateTrigger) {
        return { text: candidateTrigger.text, selector: candidateTrigger.selector, tag: candidateTrigger.tag };
      }
      node = node.parentElement;
    }
    return null;
  }

  // Track the last hover text emitted to avoid duplicates from both paths
  let lastHoverEmitted = "";

  // Track last emitted click selector so the menuitem handler can detect
  // whether the trigger click was already captured (and skip re-emitting it).
  let _lastClickSelector = "";

  function emitHoverIfNew(trigger) {
    if (!trigger || trigger.text === lastHoverEmitted) return;
    lastHoverEmitted = trigger.text;
    emit({ type: "hover", selector: trigger.selector, tag: trigger.tag, text: trigger.text });
  }

  // ── Click capture ─────────────────────────────────────────────────────────
  document.addEventListener(
    "click",
    (e) => {
      const el = e.target;
      if (!el || el === overlay) return;
      // Skip clicks on root elements — never intentional user interactions.
      // These fire from dropdown overlay clicks, portal closings, etc.
      const tag = el.tagName;
      if (tag === "HTML" || tag === "BODY") return;
      // Skip clicks on the input field that's currently being typed into
      if (isInputLike(el)) return;

      // Suppress click events on combobox triggers and options —
      // those are captured via MutationObserver (combobox text change).
      // Only suppress role='option' and role='combobox' / aria-haspopup='listbox'
      // (Select/combobox patterns). Do NOT suppress aria-haspopup='menu' or
      // aria-haspopup='true' — those are DropdownMenus we want to record.
      const roleEl = el.closest
        ? el.closest("[role='option'],[role='combobox'],[aria-haspopup='listbox']")
        : null;
      if (roleEl) return;

      // ── Radix UI DropdownMenu menuitem click ──────────────────────────────
      // When a [role='menuitem'] is clicked (e.g. "Mark as Ready" in the
      // status DropdownMenu), we must also record the trigger click that opened
      // the menu. Radix marks the open trigger with aria-expanded="true".
      // If that trigger wasn't already emitted as the last click, emit it first
      // so the replay can open the dropdown before selecting the item.
      const menuitemEl = el.closest ? el.closest("[role='menuitem']") : null;
      if (menuitemEl) {
        const openTrigger = document.querySelector(
          '[aria-haspopup="menu"][aria-expanded="true"]'
        );
        if (openTrigger) {
          const triggerSel = bestSelector(openTrigger);
          if (triggerSel !== _lastClickSelector) {
            // Use the trigger button's own center coordinates (NOT the menuitem
            // click position) so the coordinate fallback finds the right element.
            const tRect = openTrigger.getBoundingClientRect();
            const tCx = Math.round(tRect.left + tRect.width / 2);
            const tCy = Math.round(tRect.top + tRect.height / 2);
            emit({
              type: "click",
              selector: triggerSel,
              tag: openTrigger.tagName,
              text: visibleText(openTrigger),
              ariaPath: ariaPath(openTrigger),
              ambiguityCount: ambiguityCount(triggerSel),
              coordinates: {
                x: tCx,
                y: tCy,
                pageX: Math.round(tCx + window.scrollX),
                pageY: Math.round(tCy + window.scrollY),
              },
            });
          }
        }
        // Emit the menuitem click itself
        const menuSel = bestSelector(menuitemEl);
        emit({
          type: "click",
          selector: menuSel,
          tag: menuitemEl.tagName,
          text: visibleText(menuitemEl),
          ariaPath: ariaPath(menuitemEl),
          ambiguityCount: ambiguityCount(menuSel),
          coordinates: {
            x: Math.round(e.clientX),
            y: Math.round(e.clientY),
            pageX: Math.round(e.clientX + window.scrollX),
            pageY: Math.round(e.clientY + window.scrollY),
          },
        });
        return; // handled — skip general click logic below
      }

      // ── Click-time hover detection ────────────────────────────────────────
      // If this click is inside a nav submenu, emit a hover step for the parent
      // trigger BEFORE the click. This is more reliable than mouseenter+timer
      // because at click time the submenu is guaranteed to be open.
      const navTrigger = findNavSubmenuTrigger(el);
      if (navTrigger) {
        emitHoverIfNew(navTrigger);
      }

      // Prefer the nearest interactive ancestor (button/link) so we capture
      // the full interactive element rather than an inner span or SVG icon.
      let clickTarget = (el.closest && el.closest('button, a[href], [role="button"]')) || el;
      // Don't escalate to root elements
      if (clickTarget.tagName === "HTML" || clickTarget.tagName === "BODY") return;
      const sel = bestSelector(clickTarget);
      _lastClickSelector = sel;
      emit({
        type: "click",
        selector: sel,
        tag: clickTarget.tagName,
        text: visibleText(clickTarget),
        ariaPath: ariaPath(clickTarget),
        ambiguityCount: ambiguityCount(sel),
        coordinates: {
          x: Math.round(e.clientX),
          y: Math.round(e.clientY),
          pageX: Math.round(e.clientX + window.scrollX),
          pageY: Math.round(e.clientY + window.scrollY),
        },
      });
    },
    true
  );

  // ── Hover capture: click-time only ───────────────────────────────────────
  // Hover steps are emitted exclusively from the click handler above via
  // findNavSubmenuTrigger(). Mouseenter-based hover recording is intentionally
  // disabled: it captures every nav item the cursor passes through (logo,
  // Services, Pricing…) producing noise that misleads the AI refiner into
  // keeping the wrong hover step. Click-time detection emits exactly ONE
  // correct hover per submenu click — the direct parent trigger.

  // ── Scroll capture (debounced) ────────────────────────────────────────────
  let scrollTimer = null;
  window.addEventListener(
    "scroll",
    () => {
      clearTimeout(scrollTimer);
      scrollTimer = setTimeout(() => {
        emit({
          type: "scroll",
          selector: "window",
          tag: "WINDOW",
          text: "",
          value: `${Math.round(window.scrollX)},${Math.round(window.scrollY)}`,
          coordinates: {
            x: Math.round(window.scrollX),
            y: Math.round(window.scrollY),
          },
        });
      }, 500);
    },
    true
  );

  // ── Navigation capture via History API patching ───────────────────────────
  const origPushState = history.pushState;
  const origReplaceState = history.replaceState;

  history.pushState = function () {
    origPushState.apply(this, arguments);
    emit({ type: "navigate", selector: "", tag: "", text: "", value: arguments[2] || "" });
  };
  history.replaceState = function () {
    origReplaceState.apply(this, arguments);
    emit({ type: "navigate", selector: "", tag: "", text: "", value: arguments[2] || "" });
  };
  window.addEventListener("popstate", () => {
    emit({ type: "navigate", selector: "", tag: "", text: "", value: location.href });
  });

  console.debug("[Checkmate Recorder] Overlay script loaded.");
})();
