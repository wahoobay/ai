// Guided tours, drawn by driver.js (static/vendor): the home page's for a first-time visitor and the
// review page's for a first-time reviewer, started from the "Take the tour" button. Booth mode
// (booth.js) adds the welcome card, the reset to it after BOOTH_IDLE_MS without a touch, keeps the
// video playing, and, once a visitor has had NUDGE_DELAY_MS on the dashboard after the tour, points at
// the review page with a card that leaves the dashboard usable. The words are for visitors who are not computer scientists: no "model", no
// "confidence", no keyboard shortcuts (a booth may be a touch screen).
(function () {
  "use strict";
  const root = document.documentElement;
  const BOOTH = root.dataset.booth === "1";
  const REVIEW_PAGE = document.body.classList.contains("rv-page");
  const REPLAYING = root.dataset.replaying === "true";
  // Staging's answers stay in staging's database; only production's train the AI.
  const DEMO_COPY = root.dataset.env === "staging";
  const BOOTH_IDLE_MS = 90 * 1000;
  const WELCOMED = "wb.welcomed";   // this visitor has seen the welcome card (sessionStorage; the reset clears it)
  const NUDGE_DELAY_MS = 30 * 1000;  // time on the dashboard, after the tour or "just look around", before the nudge
  const NUDGED = "wb.nudged";        // this visitor answered the nudge (sessionStorage; the reset clears it)
  const make = window.driver && window.driver.js && window.driver.js.driver;

  let active = null;

  // The first of these that is on screen (driver.js centres a step whose element is missing).
  function first(...selectors) {
    for (const s of selectors) {
      const el = document.querySelector(s);
      if (el && el.getClientRects().length) return el;
    }
    return undefined;
  }

  function run(steps, onEnd) {
    if (!make) return;
    if (active) active.destroy();
    active = make({
      steps,
      showProgress: true,
      progressText: "{{current}} of {{total}}",
      nextBtnText: "Next",
      prevBtnText: "Back",
      doneBtnText: "Done",
      closeBtnLabel: "Close the tour",
      overlayOpacity: 0.6,
      stagePadding: 8,
      stageRadius: 12,
      smoothScroll: true,
      popoverClass: "wb-tour",
      onDestroyed: () => {
        active = null;
        if (onEnd) onEnd();
      },
    });
    active.drive();
  }

  async function me() {
    try {
      const r = await fetch("/api/auth/me", { credentials: "same-origin" });
      return r.ok ? await r.json() : {};
    } catch (err) {
      return {};
    }
  }

  async function canReview() {
    if (root.dataset.reviewTool === "false") return false;
    const m = await me();
    return m.status === "active" && (m.role === "reviewer" || m.role === "admin");
  }

  const reviewUrl = "/review?tour=review";

  // ---- the home page ---------------------------------------------------------------

  // The Reports panel open on one tab, over the last week: a replaying environment records nothing,
  // so its 24-hour windows empty a day after the recording starts.
  function reportsTab(tab, panelSelector) {
    const body = document.getElementById("reports-body");
    if (body && body.classList.contains("collapsed")) document.getElementById("reports-toggle")?.click();
    const win = document.getElementById("reports-window");
    if (win && win.value === "24") {
      win.value = "168";
      win.dispatchEvent(new Event("change"));
    }
    const button = document.querySelector(`.reports-tab[data-tab="${tab}"]`);
    if (button && !button.classList.contains("active")) button.click();
    return first(panelSelector, `#tab-${tab}`, "#reports-panel");
  }

  // Panels fill in after their data arrives; redraw the cut-out once they have.
  const settle = (el, step, opts) => { window.setTimeout(() => opts.driver.refresh(), 900); };

  function homeSteps() {
    const steps = [
      {
        element: () => first(".stream-wrap", ".live-embed"),
        popover: {
          title: "Fish, found by an AI",
          description: (REPLAYING
            ? "This is recorded footage from an underwater camera at the Pompano Beach pier in Florida. An AI is watching it right now: "
            : "This is a live underwater camera at Wahoo Bay in Pompano Beach, Florida. An AI watches it around the clock: ")
            + "every box is a fish it found, labelled with its best guess at the species. Tap <b>boxes</b> to see the water without them.",
          side: "bottom", align: "center",
        },
      },
      {
        element: () => first(".current"),
        popover: {
          title: "What it sees right now",
          description: "Each fish in the picture, with the AI's guess and how sure it is. It is good at spotting fish and still learning their names. That's where people come in, as you'll see at the end.",
          side: "top", align: "start",
        },
      },
      {
        element: () => reportsTab("fish-explorer", "#fe-top-species"),
        onHighlighted: settle,
        popover: {
          title: "Who lives here",
          description: "The fish the AI counted most this week. Counting who lives on a reef, day after day, is how changes show up early: a species arriving as the water warms, or one becoming scarce.",
          side: "left", align: "start",
        },
      },
      {
        // The cards arrive after the tab opens: wait for the first rather than outline the whole tab.
        element: () => { reportsTab("celebrities", "#celeb-grid"); return first("#celeb-grid .celeb-card"); },
        waitForElement: 5000,
        onHighlighted: settle,
        popover: {
          title: "The regulars",
          description: "Some animals turn up so often that they have names: Bobby the blue tang, Luna the nurse shark, Barnadette the green sea turtle. Volunteers tag them when they spot them, so the reef's regulars can be followed over the years.",
          side: "left", align: "start",
        },
      },
      {
        element: () => first(".water-quality"),
        popover: {
          title: "The water they live in",
          description: "A sensor in the water measures its temperature, oxygen, acidity and clarity. Set beside the fish counts, readings like these show how heat waves and storms change which fish turn up.",
          side: "left", align: "start",
        },
      },
    ];
    // The dashboard is the visitor's to explore; the way to the review page comes later (the nudge).
    steps.push({
      popover: {
        title: "Now it's yours",
        description: "Look around as long as you like: switch the Reports tabs, or turn the boxes off on the video. <b>Take the tour</b> at the top shows this again.",
      },
    });
    return steps;
  }

  function startHome() {
    run(homeSteps(), scheduleNudge);
  }

  // ---- the review page -------------------------------------------------------------

  // Step 2's Next picks a box that still needs an answer, as a tap on it would, so step 3 has a guess to show.
  function pickABox(el, step, opts) {
    const row = document.querySelector("#rv-frame-panel .rv-checklist .rv-fb-pending");
    if (row) row.click();
    opts.driver.moveNext();
  }

  function reviewSteps() {
    return [
      {
        popover: {
          title: "How experts check the AI",
          description: "This is the page our experts and volunteers use to check the AI's work. Each photo from the camera has boxes around what the AI thinks are fish; for each one they confirm it, correct it, or say they can't tell. Try it yourself: "
            + (DEMO_COPY
              ? "this is a demo copy of the site, so your answers never reach the real data."
              : "your answers are saved and help train the next version of the AI."),
        },
      },
      {
        element: () => first(".rv-frame-canvas", "#rv-single .rv-layout"),
        waitForElement: 15000,
        popover: {
          title: "Pick a box",
          description: "Each box is something the AI thinks is a fish. Tap one to answer it. Press Next and we'll pick one for you.",
          side: "right", align: "start",
          onNextClick: pickABox,
        },
      },
      {
        element: () => first("#rv-frame-panel .rv-guess", "#rv-single .rv-guess", "#rv-frame-panel", "#rv-single .rv-side"),
        popover: {
          title: "Is the AI right?",
          description: "This is the AI's guess, and how sure it was. If it's right, tap <b>✓ Looks right</b>. If it's wrong, type the fish's name in the search box under it. Common names work.",
          side: "left", align: "start",
        },
      },
      {
        element: () => first("#rv-frame-panel .rv-row", "#rv-single .rv-row"),
        popover: {
          title: "Not sure? Say so",
          description: "Tap <b>Can't identify</b> if you can't name it, or <b>No animal here</b> if the box is around a rock, a shadow or floating weed. An honest \"I don't know\" is more useful than a guess.",
          side: "left", align: "start",
        },
      },
      {
        element: () => first(".rv-next-frame"),
        popover: {
          title: "Next photo",
          description: "Answer as many boxes as you like, then tap <b>Next frame</b> for a new photo. Boxes you leave are passed on to someone else.",
          side: "bottom", align: "start",
        },
      },
      {
        element: () => document.getElementById("rv-session-count")?.closest(".rv-meta"),
        popover: {
          title: "That's it",
          description: "Your count goes up here as you answer. Thanks for helping the AI learn the reef's fish!",
          side: "bottom", align: "end",
        },
      },
    ];
  }

  function startReview() {
    run(reviewSteps());
  }

  // ---- booth mode ------------------------------------------------------------------

  function seen(key) {
    try { return window.sessionStorage.getItem(key) === "1"; } catch (err) { return false; }
  }

  function remember(key) {
    try { window.sessionStorage.setItem(key, "1"); } catch (err) { /* asked again next time */ }
  }

  // The nudge: a card under the review button, no overlay and no focus taken, so the dashboard stays the
  // visitor's. Shown once per visitor, NUDGE_DELAY_MS after the tour ends or the welcome card is closed,
  // only where the review button is (booth mode, an account that can review).
  let nudgeTimer = null;

  function scheduleNudge() {
    if (!BOOTH || REVIEW_PAGE || seen(NUDGED)) return;
    window.clearTimeout(nudgeTimer);
    nudgeTimer = window.setTimeout(showNudge, NUDGE_DELAY_MS);
  }

  function showNudge() {
    if (active) { nudgeTimer = window.setTimeout(showNudge, 5000); return; }   // a tour restarted: after it
    const link = document.getElementById("tour-review-link");
    if (!link || seen(NUDGED) || document.querySelector(".wb-nudge")) return;
    const card = document.createElement("div");
    card.className = "wb-nudge";
    card.setAttribute("role", "status");
    card.innerHTML = `
      <p>Want to see how our experts confirm that the AI is correctly labeling our fish?</p>
      <div class="wb-nudge-actions">
        <a class="wb-nudge-go" href="${reviewUrl}">Show me →</a>
        <button type="button" class="wb-nudge-close">Not now</button>
      </div>`;
    const done = () => {
      remember(NUDGED);
      card.remove();
      link.classList.remove("wb-pulse");
    };
    card.querySelector(".wb-nudge-go").addEventListener("click", done);
    card.querySelector(".wb-nudge-close").addEventListener("click", done);
    link.classList.add("wb-pulse");
    link.parentElement.append(card);
    // On a narrow screen the button sits far right of where a card this wide fits: slide the card back
    // onto the screen and keep its arrow under the button.
    const box = card.getBoundingClientRect();
    const shift = Math.min(box.right - (window.innerWidth - 12), box.left - 12);
    if (shift > 0) {
      card.style.left = `${-shift}px`;
      card.style.setProperty("--wb-arrow", `${28 + shift}px`);
    }
  }

  function welcomeCard() {
    const card = document.createElement("div");
    card.className = "wb-welcome";
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    card.setAttribute("aria-labelledby", "wb-welcome-title");
    card.innerHTML = `
      <div class="wb-welcome-card">
        <h2 id="wb-welcome-title">Watch an AI find fish</h2>
        <p>${REPLAYING
    ? "Recorded footage from an underwater camera at the Pompano Beach pier in Florida. The AI is finding and naming the fish in it right now."
    : "A live underwater camera at Wahoo Bay in Pompano Beach, Florida. The AI is finding and naming the fish as you watch."}</p>
        <button type="button" class="wb-go">Start the tour</button>
        <button type="button" class="wb-look">or just look around</button>
      </div>`;
    const close = () => {
      card.remove();
      try { window.sessionStorage.setItem(WELCOMED, "1"); } catch (err) { /* shown again next time */ }
    };
    card.querySelector(".wb-go").addEventListener("click", () => { close(); startHome(); });
    card.querySelector(".wb-look").addEventListener("click", () => { close(); scheduleNudge(); });
    card.addEventListener("click", (ev) => { if (ev.target === card) { close(); scheduleNudge(); } });
    document.body.append(card);
    card.querySelector(".wb-go").focus({ preventScroll: true });
  }

  function boothLink(href, text) {
    const a = document.createElement("a");
    a.className = "tour-link";
    a.href = href;
    a.textContent = text;
    return a;
  }

  function idleReset() {
    let last = Date.now();
    let touched = false;
    const mark = () => { last = Date.now(); touched = true; };
    for (const type of ["pointerdown", "keydown", "touchstart", "wheel"]) {
      document.addEventListener(type, mark, { passive: true, capture: true });
    }
    window.setInterval(() => {
      if (touched && Date.now() - last > BOOTH_IDLE_MS) {
        if (active) active.destroy();
        try {
          window.sessionStorage.removeItem(WELCOMED);
          window.sessionStorage.removeItem(NUDGED);
        } catch (err) { /* nothing to clear */ }
        window.location.href = "/";   // the home page, with the welcome card, for the next visitor
        return;
      }
      // A booth is never "unattended": undo app.js's four-hour stop of the video.
      const resume = document.getElementById("live-resume");
      if (resume && !resume.hidden && !document.hidden) resume.click();
    }, 5000);
  }

  async function booth() {
    idleReset();
    if (REVIEW_PAGE) {
      if (DEMO_COPY) {
        const note = document.createElement("div");
        note.className = "wb-demo-note";
        note.textContent = "Demo copy: answers given here never reach the real data.";
        document.querySelector(".rv-main")?.prepend(note);
      }
      return;
    }
    let welcomed = false;
    try { welcomed = window.sessionStorage.getItem(WELCOMED) === "1"; } catch (err) { /* show it */ }
    if (!welcomed) welcomeCard();
  }

  // The way to the review page, beside "Take the tour" on the home page, for every account that can review
  // (booth mode's nudge points at it).
  async function reviewButton() {
    const tools = document.getElementById("tour-tools");
    if (!tools || !(await canReview())) return;
    const wrap = document.createElement("span");
    wrap.className = "tour-review";
    const link = boothLink(reviewUrl, "Try reviewing →");
    link.id = "tour-review-link";
    link.classList.add("tour-review-link");
    link.addEventListener("click", () => remember(NUDGED));
    wrap.append(link);
    tools.append(wrap);
  }

  // ---- start -----------------------------------------------------------------------

  const button = document.getElementById("tour-start");
  if (make && button) {
    button.hidden = false;
    button.addEventListener("click", () => (REVIEW_PAGE ? startReview() : startHome()));
  }
  if (!REVIEW_PAGE) reviewButton();
  if (BOOTH) booth();
  if (REVIEW_PAGE && new URLSearchParams(window.location.search).get("tour") === "review") {
    window.history.replaceState(null, "", "/review");
    startReview();
  }
})();
