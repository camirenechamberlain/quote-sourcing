(() => {
  "use strict";

  const TOKEN_KEY = "qs_token";
  const MAX_FILES = 6;
  const MAX_PDFS = 4;
  const MAX_PDF_BYTES = 10 * 1000 * 1000;
  const isPdf = (f) => f.type === "application/pdf" || /\.pdf$/i.test(f.name || "");
  const $ = (id) => document.getElementById(id);

  const els = {
    chat: $("chat"), log: $("log"), hero: $("hero"), chips: $("chips"), quick: $("quick"),
    previews: $("previews"), chatError: $("chat-error"),
    composer: $("composer"), files: $("files"), text: $("text"), sendBtn: $("send-btn"), newChat: $("new-chat"),
  };

  let token = null;
  let pending = []; // [{blob, url, name}] photos waiting to be sent
  let busy = false;
  let finished = false; // confirmed or limit reached: the composer is closed
  let needsDetails = true;

  const store = {
    get() { try { return localStorage.getItem(TOKEN_KEY); } catch (e) { return null; } },
    set(v) { try { localStorage.setItem(TOKEN_KEY, v); } catch (e) { /* private mode: chat still works until refresh */ } },
    clear() { try { localStorage.removeItem(TOKEN_KEY); } catch (e) { /* ignore */ } },
  };

  // ---- Rendering (always textContent, never innerHTML: messages can contain anything) ----
  function linkify(container, text) {
    const re = /(https?:\/\/[^\s]+)/g;
    let last = 0, m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) container.appendChild(document.createTextNode(text.slice(last, m.index)));
      const a = document.createElement("a");
      a.href = m[0]; a.textContent = m[0]; a.target = "_blank"; a.rel = "noopener noreferrer";
      container.appendChild(a);
      last = m.index + m[0].length;
    }
    if (last < text.length) container.appendChild(document.createTextNode(text.slice(last)));
  }

  // The chat follows the conversation while the reader is at (or near) the bottom ("pinned"), and stays out of the way once they
  // scroll up to re-read. Anything that changes the layout while pinned (a new bubble, the answer buttons appearing under the
  // chat and shrinking it, photos loading, the keyboard opening) keeps the latest message in view. Your own messages re-pin.
  let pinned = true;
  function nearBottom() { return els.log.scrollHeight - els.log.scrollTop - els.log.clientHeight < 140; }
  function toBottom() { els.log.scrollTop = els.log.scrollHeight; }
  function scrollDown(force) { if (force) pinned = true; if (pinned) toBottom(); }
  els.log.addEventListener("scroll", () => { pinned = nearBottom(); }, { passive: true });
  const keepPinned = () => { if (pinned) toBottom(); };
  els.log.addEventListener("load", keepPinned, true); // images finishing loading make their bubble taller
  if (window.ResizeObserver) new ResizeObserver(keepPinned).observe(els.log); // the chat area shrinks when buttons/previews appear
  if (window.MutationObserver) new MutationObserver(keepPinned).observe(els.log, { childList: true, subtree: true });

  function addBubble(role, text, attachments) {
    els.hero.hidden = true;
    els.chat.classList.remove("landing");
    const div = document.createElement("div");
    div.className = "bubble " + role;
    if (text) { role === "bot" ? linkify(div, text) : div.appendChild(document.createTextNode(text)); }
    if (attachments && attachments.length) {
      const wrap = document.createElement("div");
      wrap.className = "thumbs";
      attachments.forEach((a) => {
        if (a.isPdf) {
          const chip = document.createElement("span"); chip.className = "doc-chip"; chip.textContent = "PDF · " + a.name;
          wrap.appendChild(chip);
        } else {
          const img = document.createElement("img"); img.src = a.url; img.alt = "Attached photo"; wrap.appendChild(img);
        }
      });
      div.appendChild(wrap);
    }
    if (role === "user") pinned = true;
    els.log.appendChild(div);
    keepPinned();
    return div;
  }

  function setTyping(on) {
    let t = document.getElementById("typing");
    if (on && !t) {
      t = document.createElement("div"); t.id = "typing"; t.className = "typing"; t.textContent = "Typing...";
      els.log.appendChild(t);
      keepPinned();
    } else if (!on && t) { t.remove(); }
  }

  function setError(msg) { els.chatError.textContent = msg || ""; }

  function renderPreviews() {
    els.previews.textContent = "";
    els.previews.hidden = pending.length === 0;
    pending.forEach((p, i) => {
      const wrap = document.createElement("div"); wrap.className = p.isPdf ? "preview pdf" : "preview";
      const rm = document.createElement("button"); rm.type = "button"; rm.textContent = "×"; rm.setAttribute("aria-label", "Remove attachment");
      rm.addEventListener("click", () => { if (p.url) URL.revokeObjectURL(p.url); pending.splice(i, 1); renderPreviews(); });
      if (p.isPdf) {
        const icon = document.createElement("span"); icon.className = "pdf-icon"; icon.textContent = "PDF";
        const name = document.createElement("span"); name.className = "pdf-name"; name.textContent = p.name;
        wrap.append(icon, name, rm);
      } else {
        const img = document.createElement("img"); img.src = p.url; img.alt = "Photo to send";
        wrap.append(img, rm);
      }
      els.previews.appendChild(wrap);
    });
  }

  function button(label, onClick, cls) {
    const b = document.createElement("button"); b.type = "button"; b.textContent = label;
    if (cls) b.className = cls === true ? "main" : cls;
    b.addEventListener("click", onClick);
    return b;
  }

  // ---- The contact-details card, shown only once the request has been refined ----
  function field(labelText, id, type, attrs) {
    const wrap = document.createElement("div"); wrap.className = "field";
    const label = document.createElement("label"); label.htmlFor = id; label.textContent = labelText;
    const input = document.createElement("input"); input.id = id; input.type = type;
    Object.entries(attrs || {}).forEach(([k, v]) => input.setAttribute(k, v));
    wrap.append(label, input);
    return { wrap, input };
  }

  function removeDetailsCard() { const f = $("details-form"); if (f) f.remove(); }

  function showDetailsCard() {
    if ($("details-form")) { $("details-name").focus(); return; }
    els.quick.hidden = true;
    const form = document.createElement("form");
    form.id = "details-form"; form.className = "bubble bot details"; form.noValidate = true;

    const intro = document.createElement("p");
    intro.textContent = "Who should I sign the requests as, and where can I send your progress link?";
    const name = field("First name", "details-name", "text", { autocomplete: "given-name", placeholder: "Irene", maxlength: "40" });
    const mobile = field("Mobile number", "details-mobile", "tel", { inputmode: "tel", autocomplete: "tel", placeholder: "0412 345 678" });
    const email = field("Email", "details-email", "email", { autocomplete: "email", placeholder: "you@example.com" });

    const consentLabel = document.createElement("label"); consentLabel.className = "check"; consentLabel.htmlFor = "details-consent";
    const consent = document.createElement("input"); consent.type = "checkbox"; consent.id = "details-consent";
    const consentText = document.createElement("span");
    consentText.textContent = "I agree that Quoteschmote can contact suppliers on my behalf using my first name, job details and photos. My mobile and email are not shared with suppliers.";
    consentLabel.append(consent, consentText);

    const err = document.createElement("p"); err.className = "error"; err.setAttribute("role", "alert");
    const submit = document.createElement("button"); submit.type = "submit"; submit.className = "primary"; submit.textContent = "Request quotes";

    form.append(intro, name.wrap, mobile.wrap, email.wrap, consentLabel, err, submit);
    els.log.appendChild(form);
    scrollDown(true);
    name.input.focus();

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      err.textContent = ""; submit.disabled = true;
      try {
        const { ok, data } = await api("/api/chat/contact", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token, name: name.input.value, mobile: mobile.input.value, email: email.input.value, consent: consent.checked }),
        });
        if (ok) {
          form.remove();
          data.replies.forEach((r) => addBubble("bot", r));
          applyState(data.state, data.job, data.needs_details, data.review_pending, data.choices);
        } else if (data && data.state === "limit_reached") {
          form.remove();
          (data.replies || [data.message]).forEach((r) => addBubble("bot", r));
          applyState("limit_reached", null, true);
        } else {
          err.textContent = (data && data.message) || "Something went wrong. Please try again.";
        }
      } catch (ex) {
        err.textContent = "Can't reach the server. Check your connection and try again.";
      } finally { submit.disabled = false; }
    });
  }

  // One review question's answers as tappable buttons, so the customer rarely has to type.
  function renderChoices(choices) {
    const selected = new Set();
    let doneBtn = null;
    if (choices.kind === "attach") {
      const skip = choices.options[0] || "Continue without attaching";
      els.quick.append(
        button(choices.label === "Photos" ? "Attach photos" : "Attach files", () => els.files.click(), "main"),
        button(skip, () => send(skip)),
      );
    } else {
      choices.options.forEach((option) => {
        if (choices.multi) {
          const b = button(option, () => {
            if (selected.has(option)) selected.delete(option); else selected.add(option);
            b.setAttribute("aria-pressed", selected.has(option) ? "true" : "false");
            if (doneBtn) doneBtn.disabled = selected.size === 0;
          }, "choice");
          b.setAttribute("aria-pressed", "false");
          els.quick.append(b);
        } else {
          els.quick.append(button(option, () => send(option), "choice"));
        }
      });
      if (choices.multi) {
        doneBtn = button("Done", () => send(choices.options.filter((o) => selected.has(o)).join(", ")), "main");
        doneBtn.disabled = true;
        els.quick.append(doneBtn);
      }
      if (choices.kind !== "confirm") {
        // For a customer who doesn't know: a typical value costs nothing; a photo estimate is one small call, only on tap
        if (choices.typical) {
          els.quick.append(button("Typical: " + choices.typical, () => send("Use typical: " + choices.typical), "assist"));
        }
        if (choices.photo_estimable) {
          els.quick.append(choices.photos_attached
            ? button("Estimate from my photo", () => send("Estimate from my photo"), "assist")
            : button("Attach a photo to estimate", () => els.files.click(), "assist"));
        }
        els.quick.append(button("Not sure", () => send("Not sure"), "muted"));
      }
    }
    els.quick.append(button("Send as is", () => send("SEND AS IS"), "muted"));
    els.text.placeholder = choices.kind === "attach" ? "Or type a note..." : choices.kind === "confirm" ? "Or type the right answer..." : "Or type your own answer...";
    els.quick.hidden = false;
  }

  function renderQuick(state, job, reviewPending, choices) {
    els.quick.textContent = "";
    els.quick.hidden = true;
    if (state === "awaiting_confirm") {
      els.quick.append(
        button("Yes, request quotes", () => send("YES"), true),
        button("Change something", () => { removeDetailsCard(); els.text.placeholder = "What would you like to change?"; els.text.focus(); }),
        button("Cancel", () => send("CANCEL")),
      );
      els.quick.hidden = false;
    } else if (state === "collecting" && choices) {
      renderChoices(choices);
    } else if (state === "collecting" && reviewPending) {
      els.quick.append(button("Send as is", () => send("SEND AS IS")));
      els.quick.hidden = false;
    } else if (state === "confirmed" && job) {
      const a = document.createElement("a"); a.className = "main"; a.href = job.track_url; a.textContent = "Track progress";
      els.quick.append(a, button("Start another request", resetChat));
      els.quick.hidden = false;
    } else if (state === "limit_reached") {
      els.quick.append(button("Back to start", resetChat));
      els.quick.hidden = false;
    }
  }

  function applyState(state, job, needs, reviewPending, choices) {
    needsDetails = needs !== false;
    if (state === "need_details") { state = "awaiting_confirm"; renderQuick(state, job, false); showDetailsCard(); return; }
    finished = state === "confirmed" || state === "limit_reached";
    els.text.disabled = finished; els.sendBtn.disabled = finished; els.files.disabled = finished;
    els.composer.hidden = finished;
    renderQuick(state, job, reviewPending, choices);
  }

  // ---- Network ----
  async function api(path, options) {
    const res = await fetch(path, options);
    let data = null;
    try { data = await res.json(); } catch (e) { /* non-JSON error */ }
    return { ok: res.ok, status: res.status, data };
  }

  function resetChat() {
    store.clear(); token = null; pending = []; finished = false; needsDetails = true;
    els.log.querySelectorAll(".bubble, .typing").forEach((n) => n.remove());
    els.hero.hidden = false; els.chat.classList.add("landing");
    els.quick.hidden = true; els.previews.hidden = true; setError("");
    els.composer.hidden = false; els.text.disabled = false; els.sendBtn.disabled = false; els.files.disabled = false;
    els.text.value = ""; els.text.style.height = "auto"; els.text.placeholder = "Describe your job in a few words and add photos or drawings. We read your photos to fill in the details, so you answer fewer questions.";
    els.newChat.hidden = true;
  }

  async function ensureSession() {
    if (token) return true;
    const { ok, data } = await api("/api/chat/start", { method: "POST" });
    if (!ok) { setError((data && data.message) || "Something went wrong. Please try again."); return false; }
    token = data.token; store.set(token);
    els.newChat.hidden = false;
    return true;
  }

  async function restore() {
    const saved = store.get();
    if (!saved) return;
    const { ok, data } = await api("/api/chat/state?token=" + encodeURIComponent(saved));
    if (!ok) { store.clear(); return; }
    token = saved;
    els.newChat.hidden = false;
    data.messages.forEach((m) => addBubble(m.role, m.text || (m.photos ? "(" + m.photos + " attachment" + (m.photos > 1 ? "s" : "") + ")" : "")));
    applyState(data.state, data.job, data.needs_details, data.review_pending, data.choices);
    scrollDown(true);
  }

  // ---- Photos: shrink in the browser (smaller upload, and canvas re-encoding drops location metadata) ----
  function compress(file) {
    return new Promise((resolve) => {
      if (!file.type.startsWith("image/") || file.type === "image/gif") return resolve(file);
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        const max = 2000;
        const scale = Math.min(1, max / Math.max(img.width, img.height));
        const canvas = document.createElement("canvas");
        canvas.width = Math.round(img.width * scale); canvas.height = Math.round(img.height * scale);
        canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
        URL.revokeObjectURL(url);
        canvas.toBlob((b) => resolve(b || file), "image/jpeg", 0.85);
      };
      img.onerror = () => { URL.revokeObjectURL(url); resolve(file); };
      img.src = url;
    });
  }

  els.files.addEventListener("change", async () => {
    setError("");
    const chosen = Array.from(els.files.files);
    els.files.value = "";
    for (const f of chosen) {
      if (pending.length >= MAX_FILES) { setError("You can attach up to " + MAX_FILES + " files at a time."); break; }
      if (isPdf(f)) {
        if (f.size > MAX_PDF_BYTES) { setError(f.name + " is over 10MB. Try a smaller or compressed PDF."); continue; }
        if (pending.filter((p) => p.isPdf).length >= MAX_PDFS) { setError("You can attach up to " + MAX_PDFS + " PDFs at a time."); continue; }
        pending.push({ blob: f, url: null, name: f.name, isPdf: true }); // PDFs are sent as-is; the server cleans them
      } else if (f.type.startsWith("image/")) {
        const blob = await compress(f);
        pending.push({ blob, url: URL.createObjectURL(blob), name: f.name, isPdf: false });
      } else {
        setError(f.name + " isn't a photo or PDF.");
      }
    }
    renderPreviews();
    // Once the chat is under way, choosing photos IS the message: send them straight away. On the landing page, and when the
    // customer has typed a caption, wait for them to press Send so the description and photos go together.
    const chatStarted = !els.chat.classList.contains("landing");
    if (chatStarted && pending.length > 0 && !els.text.value.trim()) send();
  });

  // ---- Sending ----
  async function send(forcedText) {
    if (busy || finished) return;
    const text = (forcedText !== undefined ? forcedText : els.text.value).trim();
    if (!text && pending.length === 0) return;

    busy = true; els.sendBtn.disabled = true; setError("");
    if (!(await ensureSession())) { busy = false; els.sendBtn.disabled = false; return; }

    removeDetailsCard();
    addBubble("user", text, pending.map((p) => ({ url: p.url, name: p.name, isPdf: p.isPdf })));
    const form = new FormData();
    form.append("token", token); form.append("text", text);
    pending.forEach((p, i) => form.append(
      "files", p.blob, p.isPdf ? p.name : (p.name || "photo" + i).replace(/\.[^.]+$/, "") + ".jpg",
    ));
    if (forcedText === undefined) els.text.value = "";
    els.text.style.height = "auto";
    els.quick.hidden = true;
    const sentPending = pending; pending = []; els.previews.hidden = true;
    setTyping(true);

    try {
      const { ok, status, data } = await api("/api/chat/message", { method: "POST", body: form });
      setTyping(false);
      if (status === 401) { resetChat(); setError("Your session expired. Please start again."); return; }
      if (!ok) {
        setError((data && data.message) || "Something went wrong. Please try again.");
        // Put the photos back so the user doesn't have to re-add them
        pending = sentPending; renderPreviews();
        if (forcedText === undefined) els.text.value = text;
        return;
      }
      data.replies.forEach((r) => addBubble("bot", r));
      els.text.placeholder = "Type your reply...";
      applyState(data.state, data.job, data.needs_details, data.review_pending, data.choices);
    } catch (e) {
      setTyping(false);
      setError("Can't reach the server. Check your connection and try again.");
      pending = sentPending; renderPreviews();
    } finally {
      busy = false;
      if (!finished) els.sendBtn.disabled = false;
    }
  }

  els.composer.addEventListener("submit", (e) => { e.preventDefault(); send(); });
  els.text.addEventListener("input", () => {
    els.text.style.height = "auto";
    els.text.style.height = Math.min(els.text.scrollHeight, 140) + "px";
  });
  els.text.addEventListener("keydown", (e) => {
    // Enter sends on desktop; on touch devices Enter is a newline (the Send button sends)
    const fine = window.matchMedia("(pointer: fine)").matches;
    if (e.key === "Enter" && !e.shiftKey && fine) { e.preventDefault(); send(); }
  });
  els.newChat.addEventListener("click", resetChat);

  // Example chips pre-fill the box so a visitor can start with one tap
  els.chips.querySelectorAll(".chip-btn").forEach((b) => b.addEventListener("click", () => {
    els.text.value = b.dataset.text + " ";
    els.text.dispatchEvent(new Event("input"));
    els.text.focus();
  }));

  restore();
})();
