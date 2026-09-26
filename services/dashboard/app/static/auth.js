// services/dashboard/app/static/auth.js
// Every POST under /api carries X-Requested-With: wahoobay. A cross-origin
// form cannot set it, which is what stands in for a CSRF token here.
const CSRF = { "Content-Type": "application/json", "X-Requested-With": "wahoobay" };

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: CSRF,
    credentials: "same-origin",
    body: JSON.stringify(body || {}),
  });
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty body is fine */ }
  return { ok: r.ok, status: r.status, data };
}

function safeNext(value) {
  // Post-login redirect targets must be same-origin absolute paths. The
  // server already sanitises `next` before rendering it into the page,
  // but that isn't load-bearing on its own — this field is still an
  // attacker-controlled form value from the caller's point of view, so
  // it's re-validated here before ever reaching location.href.
  //
  // Reject embedded tab/CR/LF before anything else: the WHATWG URL
  // parser strips those bytes wherever they occur in the input, not
  // only at the edges, so "/\t/evil.example" would pass every check
  // below as a plain string but parse to "//evil.example" — a
  // protocol-relative URL — the moment it reaches location.href.
  if (typeof value !== "string" || /[\t\r\n]/.test(value)) return "/";
  return (value.startsWith("/") && !value.startsWith("//") && !value.startsWith("/\\"))
    ? value : "/";
}

function wireLoginForm() {
  const form = document.getElementById("login-form");
  if (!form) return;
  const err = document.getElementById("login-error");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.hidden = true;
    const res = await postJSON("/api/auth/login", {
      username: form.username.value,
      password: form.password.value,
    });
    if (res.ok) {
      window.location.href = safeNext(form.next.value);
    } else {
      err.textContent = res.data.error || "Sign-in failed.";
      err.hidden = false;
    }
  });
}

async function renderIdentityChip() {
  const slot = document.getElementById("identity-chip");
  if (!slot) return;
  const r = await fetch("/api/auth/me", { credentials: "same-origin" });
  const me = await r.json();
  const link = (href, text, cls) => {
    const a = document.createElement("a");
    a.href = href;
    a.textContent = text;
    if (cls) a.className = cls;
    return a;
  };
  const span = (text, cls) => {
    const s = document.createElement("span");
    s.textContent = text;
    s.className = cls;
    return s;
  };
  slot.hidden = false;
  if (!me.authenticated) {
    // Signed out: the way in for reviewers (demo walk-through 2026-09-26).
    // Log in returns to the review tool.
    slot.classList.add("signed-out");
    slot.replaceChildren(span("Reviewers:", "chip-label"), link("/register", "Sign up"),
      span("·", "chip-sep"), link("/login?next=/review", "Log in"));
    return;
  }
  slot.classList.remove("signed-out");
  const parts = [];
  const canReview = me.status === "active" && (me.role === "reviewer" || me.role === "admin");
  if (canReview && !window.location.pathname.startsWith("/review")) {
    parts.push(link("/review", "Review fish →", "chip-review"));
  }
  const out = document.createElement("button");
  out.type = "button";
  out.className = "chip-signout";
  out.textContent = "Sign out";
  out.addEventListener("click", async () => {
    await postJSON("/api/auth/logout", {});
    window.location.reload();
  });
  parts.push(span(`${me.display_name} (${me.role})`, "chip-name"), out);
  slot.replaceChildren(...parts);

  if (me.status === "pending") {
    const banner = document.createElement("div");
    banner.className = "pending-banner";
    banner.textContent =
      "Your account is awaiting administrator approval. You can browse, " +
      "but not yet submit corrections.";
    document.body.prepend(banner);
  }
}

function wireRegisterForm() {
  const form = document.getElementById("register-form");
  if (!form) return;
  const err = document.getElementById("register-error");
  const done = document.getElementById("register-done");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.hidden = true;
    if (form.password.value !== form.password2.value) {
      err.textContent = "Passwords do not match."; err.hidden = false; return;
    }
    const res = await postJSON("/api/auth/register", {
      username: form.username.value,
      display_name: form.display_name.value,
      email: form.email.value,
      password: form.password.value,
    });
    if (res.ok) { form.hidden = true; done.textContent = res.data.message; done.hidden = false; }
    else { err.textContent = res.data.error || "Registration failed."; err.hidden = false; }
  });
}

async function autoVerifyEmail() {
  const el = document.getElementById("verify-token");
  if (!el || !el.dataset.token) return;
  const res = await postJSON("/api/auth/verify-email", { token: el.dataset.token });
  el.textContent = res.ok ? res.data.message : (res.data.error || "Verification failed.");
}

function wireForgotForm() {
  const form = document.getElementById("forgot-form");
  if (!form) return;
  const done = document.getElementById("forgot-done");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const res = await postJSON("/api/auth/forgot-password", { email: form.email.value });
    form.hidden = true;
    done.textContent = res.data.message || "Check your email.";
    done.hidden = false;
  });
}

function wireResetForm() {
  const form = document.getElementById("reset-form");
  if (!form) return;
  const err = document.getElementById("reset-error");
  const done = document.getElementById("reset-done");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.hidden = true;
    if (form.password.value !== form.password2.value) {
      err.textContent = "Passwords do not match."; err.hidden = false; return;
    }
    const res = await postJSON("/api/auth/reset-password", {
      token: form.token.value, password: form.password.value,
    });
    if (res.ok) { form.hidden = true; done.textContent = res.data.message; done.hidden = false; }
    else { err.textContent = res.data.error || "Reset failed."; err.hidden = false; }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  wireLoginForm();
  renderIdentityChip();
  wireRegisterForm();
  autoVerifyEmail();
  wireForgotForm();
  wireResetForm();
});
