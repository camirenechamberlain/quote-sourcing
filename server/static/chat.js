(() => {
  "use strict";

  const TOKEN_KEY = "qs_token";
  const MAX_FILES = 6;
  const $ = (id) => document.getElementById(id);

  const els = {
    start: $("start"), startForm: $("start-form"), startBtn: $("start-btn"), startError: $("start-error"),
    mobile: $("mobile"), email: $("email"), consent: $("consent"),
    chat: $("chat"), log: $("log"), quick: $("quick"), previews: $("previews"), chatError: $("chat-error"),
    composer: $("composer"), files: $("files"), text: $("text"), sendBtn: $("send-btn"), newChat: $("new-chat"),
  };

  let token = null;
  let pending = []; // [{blob, url}] photos waiting to be sent
  let busy = false;
  let finished = false; // confirmed or limit reached: the composer is closed

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

  function addBubble(role, text, thumbUrls) {
    const div = document.createElement("div");
    div.className = "bubble " + role;
    if (text) { role === "bot" ? linkify(div, text) : div.appendChild(document.createTextNode(text)); }
    if (thumbUrls && thumbUrls.length) {
      const wrap = document.createElement("div");
      wrap.className = "thumbs";
      thumbUrls.forEach((u) => { const img = document.createElement("img"); img.src = u; img.alt = "Attached photo"; wrap.appendChild(img); });
      div.appendChild(wrap);
    }
    els.log.appendChild(div);
    els.log.scrollTop = els.log.scrollHeight;
    return div;
  }

  function setTyping(on) {
    let t = document.getElementById("typing");
    if (on && !t) {
      t = document.createElement("div"); t.id = "typing"; t.className = "typing"; t.textContent = "Typing...";
      els.log.appendChild(t); els.log.scrollTop = els.log.scrollHeight;
    } else if (!on && t) { t.remove(); }
  }

  function setError(msg) { els.chatError.textContent = msg || ""; }

  function renderPreviews() {
    els.previews.textContent = "";
    els.previews.hidden = pending.length === 0;
    pending.forEach((p, i) => {
      const wrap = document.createElement("div"); wrap.className = "preview";
      const img = document.createElement("img"); img.src = p.url; img.alt = "Photo to send";
      const rm = document.createElement("button"); rm.type = "button"; rm.textContent = "×"; rm.setAttribute("aria-label", "Remove photo");
      rm.addEventListener("click", () => { URL.revokeObjectURL(p.url); pending.splice(i, 1); renderPreviews(); });
      wrap.append(img, rm); els.previews.appendChild(wrap);
    });
  }

  function quickButton(label, onClick, main) {
    const b = document.createElement("button"); b.type = "button"; b.textContent = label;
    if (main) b.className = "main";
    b.addEventListener("click", onClick);
    return b;
  }

  function renderQuick(state, job) {
    els.quick.textContent = "";
    els.quick.hidden = true;
    if (state === "awaiting_confirm") {
      els.quick.append(
        quickButton("Yes, request quotes", () => send("YES"), true),
        quickButton("Change something", () => { els.text.placeholder = "What would you like to change?"; els.text.focus(); }),
        quickButton("Cancel", () => send("CANCEL")),
      );
      els.quick.hidden = false;
    } else if (state === "confirmed" && job) {
      const a = document.createElement("a"); a.className = "main"; a.href = job.track_url; a.textContent = "Track progress";
      els.quick.append(a, quickButton("Start another request", resetChat));
      els.quick.hidden = false;
    } else if (state === "limit_reached") {
      els.quick.append(quickButton("Back to start", resetChat));
      els.quick.hidden = false;
    }
  }

  function applyState(state, job) {
    finished = state === "confirmed" || state === "limit_reached";
    els.text.disabled = finished; els.sendBtn.disabled = finished; els.files.disabled = finished;
    els.composer.hidden = finished;
    renderQuick(state, job);
  }

  // ---- Network ----
  async function api(path, options) {
    const res = await fetch(path, options);
    let data = null;
    try { data = await res.json(); } catch (e) { /* non-JSON error */ }
    return { ok: res.ok, status: res.status, data };
  }

  function showChat() {
    els.start.hidden = true; els.chat.hidden = false; els.newChat.hidden = false;
  }

  function resetChat() {
    store.clear(); token = null; pending = []; finished = false;
    els.log.textContent = ""; els.quick.hidden = true; els.previews.hidden = true; setError("");
    els.composer.hidden = false; els.text.disabled = false; els.sendBtn.disabled = false; els.files.disabled = false;
    els.text.value = ""; els.text.placeholder = "Describe the job...";
    els.chat.hidden = true; els.newChat.hidden = true; els.start.hidden = false;
  }

  async function restore() {
    const saved = store.get();
    if (!saved) return;
    const { ok, data } = await api("/api/chat/state?token=" + encodeURIComponent(saved));
    if (!ok) { store.clear(); return; }
    token = saved;
    showChat();
    data.messages.forEach((m) => addBubble(m.role, m.text || (m.photos ? "(" + m.photos + " photo" + (m.photos > 1 ? "s" : "") + ")" : "")));
    applyState(data.state, data.job);
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
      if (pending.length >= MAX_FILES) { setError("You can attach up to " + MAX_FILES + " photos at a time."); break; }
      const blob = await compress(f);
      pending.push({ blob, url: URL.createObjectURL(blob), name: f.name });
    }
    renderPreviews();
  });

  // ---- Sending ----
  async function send(forcedText) {
    if (busy || finished) return;
    const text = (forcedText !== undefined ? forcedText : els.text.value).trim();
    if (!text && pending.length === 0) return;

    busy = true; els.sendBtn.disabled = true; setError("");
    const thumbs = pending.map((p) => p.url);
    addBubble("user", text, thumbs);
    const form = new FormData();
    form.append("token", token); form.append("text", text);
    pending.forEach((p, i) => form.append("files", p.blob, (p.name || "photo" + i).replace(/\.[^.]+$/, "") + ".jpg"));
    if (forcedText === undefined) els.text.value = "";
    els.text.style.height = "auto";
    els.quick.hidden = true;
    const sentPending = pending; pending = []; els.previews.hidden = true;
    setTyping(true);

    try {
      const { ok, status, data } = await api("/api/chat/message", { method: "POST", body: form });
      setTyping(false);
      if (status === 401) { resetChat(); els.startError.textContent = "Your session expired. Please start again."; return; }
      if (!ok) {
        setError((data && data.message) || "Something went wrong. Please try again.");
        // Put the photos back so the user doesn't have to re-add them
        pending = sentPending; renderPreviews();
        if (forcedText === undefined) els.text.value = text;
        return;
      }
      sentPending.forEach((p) => { /* keep blob URLs alive: the sent bubble still shows them */ });
      data.replies.forEach((r) => addBubble("bot", r));
      els.text.placeholder = "Type your reply...";
      applyState(data.state, data.job);
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

  // ---- Start ----
  els.startForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    els.startError.textContent = "";
    els.startBtn.disabled = true;
    try {
      const { ok, data } = await api("/api/chat/start", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mobile: els.mobile.value, email: els.email.value, consent: els.consent.checked }),
      });
      if (!ok) { els.startError.textContent = (data && data.message) || "Something went wrong. Please try again."; return; }
      token = data.token; store.set(token);
      showChat();
      data.messages.forEach((m) => addBubble(m.role, m.text));
      applyState(data.state, data.job);
      els.text.focus();
    } catch (err) {
      els.startError.textContent = "Can't reach the server. Check your connection and try again.";
    } finally {
      els.startBtn.disabled = false;
    }
  });

  restore();
})();
