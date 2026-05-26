/**
 * NRGX Chat — Senior Quant Trader advisor drawer.
 * Self-initializing: injects CSS, creates FAB + drawer, streams from /api/chat.
 *
 * Note: the JS namespace stays `window.RavenChat` (not renamed to NRGXChat)
 * because every engine page calls `window.RavenChat.setEngineContext(...)`
 * to push its current state into the chat. Renaming would silently break
 * those integrations across 16+ pages. Internal class names stay `rc*` for
 * the same reason — only user-visible strings and brand visuals updated.
 */
(function () {
  "use strict";

  // ── State ──────────────────────────────────────────────────────────────
  var messages = [];
  var engineId = null;
  var engineData = null;
  var isOpen = false;
  var isStreaming = false;
  var abortCtrl = null;

  // ── Inject CSS ─────────────────────────────────────────────────────────
  var style = document.createElement("style");
  style.textContent = [
    /* FAB — sits above the calculator toggle (bottom:20px) */
    ".rcFab{position:fixed;bottom:86px;right:24px;z-index:9990;width:54px;height:54px;border-radius:50%;" +
      "background:linear-gradient(135deg,rgba(0,122,255,0.92),rgba(88,86,214,0.92));" +
      "border:1px solid rgba(255,255,255,0.25);box-shadow:0 4px 20px rgba(0,0,0,0.15);" +
      "cursor:pointer;display:flex;align-items:center;justify-content:center;transition:transform .2s,box-shadow .2s}",
    ".rcFab:hover{transform:scale(1.08);box-shadow:0 6px 28px rgba(0,0,0,0.22)}",
    ".rcFab svg{width:26px;height:26px;overflow:visible}",
    ".rcFab.rcHidden{display:none}",

    /* Backdrop — must cover the nav hamburger (z-index:20001) */
    ".rcBackdrop{position:fixed;inset:0;z-index:20010;background:rgba(0,0,0,0.18);opacity:0;" +
      "pointer-events:none;transition:opacity .25s}",
    ".rcBackdrop.rcShow{opacity:1;pointer-events:auto}",

    /* Drawer — above backdrop and nav */
    ".rcDrawer{position:fixed;top:0;right:0;bottom:0;z-index:20011;width:420px;max-width:92vw;" +
      "background:rgba(255,255,255,0.88);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);" +
      "border-left:1px solid var(--border,rgba(15,23,42,0.10));" +
      "box-shadow:-8px 0 40px rgba(0,0,0,0.08);display:flex;flex-direction:column;" +
      "transform:translateX(100%);transition:transform .3s cubic-bezier(.4,0,.2,1)}",
    ".rcDrawer.rcShow{transform:translateX(0)}",

    /* Header */
    ".rcHeader{display:flex;align-items:center;gap:10px;padding:16px 20px;border-bottom:1px solid var(--border,rgba(15,23,42,0.10));" +
      "flex-shrink:0}",
    /* NRGX X-mark next to the title — small, restrained, brand-aligned */
    ".rcBrandIcon{width:26px;height:26px;object-fit:contain;flex-shrink:0;display:block}",
    ".rcTitle{font-size:15px;font-weight:700;color:var(--text,#0b0b0f);flex:1;letter-spacing:-0.1px}",
    ".rcHeaderBtn{background:none;border:1px solid var(--border,rgba(15,23,42,0.10));border-radius:8px;padding:5px 10px;" +
      "font-size:11px;font-weight:600;color:var(--muted,rgba(11,11,15,0.62));cursor:pointer;transition:background .15s}",
    ".rcHeaderBtn:hover{background:rgba(0,0,0,0.04)}",
    ".rcCloseBtn{background:none;border:none;cursor:pointer;padding:4px;color:var(--muted,rgba(11,11,15,0.62));" +
      "font-size:20px;line-height:1}",

    /* Context badge */
    ".rcCtxBadge{padding:6px 16px;font-size:11px;font-weight:600;color:var(--blue,rgba(0,122,255,0.95));" +
      "background:rgba(0,122,255,0.06);border-bottom:1px solid var(--border,rgba(15,23,42,0.10));" +
      "flex-shrink:0;display:none}",
    ".rcCtxBadge.rcShow{display:block}",

    /* Messages area */
    ".rcMessages{flex:1;overflow-y:auto;padding:16px 16px 8px;display:flex;flex-direction:column;gap:12px}",

    /* Message bubbles */
    ".rcMsg{max-width:88%;padding:10px 14px;border-radius:16px;font-size:13px;line-height:1.55;" +
      "word-wrap:break-word;white-space:pre-wrap}",
    ".rcMsg p{margin:0 0 8px}",
    ".rcMsg p:last-child{margin-bottom:0}",
    ".rcMsg strong{font-weight:700}",
    ".rcMsg code{font-family:'SF Mono',Menlo,monospace;font-size:12px;background:rgba(0,0,0,0.05);" +
      "padding:1px 5px;border-radius:4px}",
    ".rcMsg ol,.rcMsg ul{margin:4px 0 8px 18px;padding:0}",
    ".rcMsg li{margin-bottom:3px}",
    ".rcMsgUser{align-self:flex-end;background:linear-gradient(135deg,rgba(0,122,255,0.90),rgba(88,86,214,0.88));" +
      "color:#fff;border-bottom-right-radius:4px}",
    ".rcMsgBot{align-self:flex-start;background:var(--surfaceSolid,#ffffff);" +
      "border:1px solid var(--border,rgba(15,23,42,0.10));color:var(--text,#0b0b0f);" +
      "border-bottom-left-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,0.04)}",
    ".rcMsgBot.rcError{border-color:var(--red,rgba(255,59,48,0.95));color:var(--red)}",

    /* Streaming cursor */
    ".rcCursor{display:inline-block;width:2px;height:14px;background:var(--blue,rgba(0,122,255,0.95));" +
      "margin-left:2px;vertical-align:text-bottom;animation:rcBlink .6s infinite}",
    "@keyframes rcBlink{0%,100%{opacity:1}50%{opacity:0}}",

    /* Welcome */
    ".rcWelcome{text-align:center;padding:40px 24px;color:var(--muted,rgba(11,11,15,0.62))}",
    ".rcWelcome h3{font-size:16px;font-weight:700;color:var(--text,#0b0b0f);margin:0 0 8px}",
    ".rcWelcome p{font-size:13px;line-height:1.5;margin:0}",

    /* Composer */
    ".rcComposer{display:flex;align-items:flex-end;gap:8px;padding:12px 16px;border-top:1px solid var(--border,rgba(15,23,42,0.10));" +
      "flex-shrink:0;background:rgba(255,255,255,0.6)}",
    ".rcInput{flex:1;resize:none;border:1px solid var(--border,rgba(15,23,42,0.10));border-radius:14px;" +
      "padding:10px 14px;font-size:13px;line-height:1.45;font-family:inherit;color:var(--text,#0b0b0f);" +
      "background:var(--surfaceSolid,#ffffff);outline:none;max-height:120px;min-height:40px;" +
      "transition:border-color .15s}",
    ".rcInput:focus{border-color:var(--blue,rgba(0,122,255,0.95))}",
    ".rcInput::placeholder{color:var(--muted2,rgba(11,11,15,0.48))}",
    ".rcSendBtn{width:38px;height:38px;border-radius:50%;border:none;cursor:pointer;" +
      "background:linear-gradient(135deg,rgba(0,122,255,0.92),rgba(88,86,214,0.92));" +
      "display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:opacity .15s}",
    ".rcSendBtn:disabled{opacity:0.4;cursor:not-allowed}",
    ".rcSendBtn svg{width:16px;height:16px;fill:#fff}",
  ].join("\n");
  document.head.appendChild(style);

  // ── DOM helpers ────────────────────────────────────────────────────────
  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html) e.innerHTML = html;
    return e;
  }

  // ── Build DOM ──────────────────────────────────────────────────────────

  // FAB — stylized X mark from the NRGX wordmark. Gold blade extends out
  // past the upper-right corner (the signature shape from the logo); a
  // white cross-stroke keeps the X readable on the gradient background.
  var fab = el("button", "rcFab",
    '<svg viewBox="0 0 32 32" fill="none" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M7 25 L25 7 L31 1" stroke="#d4b27a" stroke-width="3"/>' +
      '<path d="M7 7 L25 25" stroke="#ffffff" stroke-width="3"/>' +
    '</svg>');
  fab.title = "NRGX Chat";
  fab.setAttribute("aria-label", "Open NRGX Chat");
  fab.addEventListener("click", toggleDrawer);

  // Backdrop
  var backdrop = el("div", "rcBackdrop");
  backdrop.addEventListener("click", closeDrawer);

  // Drawer
  var drawer = el("div", "rcDrawer");

  // Header — NRGX X-mark + title + actions
  var header = el("div", "rcHeader");
  var brandIcon = document.createElement("img");
  brandIcon.className = "rcBrandIcon";
  brandIcon.src = "/static/NRGX-Mark.png";
  brandIcon.alt = "";
  brandIcon.setAttribute("aria-hidden", "true");
  var titleEl = el("div", "rcTitle", "NRGX Chat");
  var newBtn = el("button", "rcHeaderBtn", "New Chat");
  newBtn.addEventListener("click", newConversation);
  var closeBtn = el("button", "rcCloseBtn", "&times;");
  closeBtn.addEventListener("click", closeDrawer);
  header.appendChild(brandIcon);
  header.appendChild(titleEl);
  header.appendChild(newBtn);
  header.appendChild(closeBtn);

  // Context badge
  var ctxBadge = el("div", "rcCtxBadge");

  // Messages area
  var msgArea = el("div", "rcMessages");

  // Composer
  var composer = el("div", "rcComposer");
  var input = document.createElement("textarea");
  input.className = "rcInput";
  input.placeholder = "Ask the Senior Quant...";
  input.rows = 1;
  input.addEventListener("input", autoGrow);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  var sendBtn = el("button", "rcSendBtn",
    '<svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>');
  sendBtn.addEventListener("click", sendMessage);
  composer.appendChild(input);
  composer.appendChild(sendBtn);

  // Assemble drawer
  drawer.appendChild(header);
  drawer.appendChild(ctxBadge);
  drawer.appendChild(msgArea);
  drawer.appendChild(composer);

  // Insert into page
  document.body.appendChild(fab);
  document.body.appendChild(backdrop);
  document.body.appendChild(drawer);

  // Show welcome
  renderWelcome();

  // ── Drawer toggle ──────────────────────────────────────────────────────
  function toggleDrawer() {
    isOpen ? closeDrawer() : openDrawer();
  }

  function openDrawer() {
    isOpen = true;
    drawer.classList.add("rcShow");
    backdrop.classList.add("rcShow");
    fab.classList.add("rcHidden");
    setTimeout(function () { input.focus(); }, 300);
  }

  function closeDrawer() {
    isOpen = false;
    drawer.classList.remove("rcShow");
    backdrop.classList.remove("rcShow");
    fab.classList.remove("rcHidden");
  }

  // ── Auto-grow textarea ─────────────────────────────────────────────────
  function autoGrow() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  }

  // ── Welcome screen ─────────────────────────────────────────────────────
  function renderWelcome() {
    msgArea.innerHTML = "";
    var w = el("div", "rcWelcome",
      "<h3>Senior Quant Trader</h3>" +
      "<p>Ask me anything about the current market, your positions, " +
      "or the data on screen. I have access to all of NRGX Labs' engines " +
      "and the live market snapshot.</p>");
    msgArea.appendChild(w);
  }

  // ── New conversation ───────────────────────────────────────────────────
  function newConversation() {
    if (isStreaming && abortCtrl) abortCtrl.abort();
    isStreaming = false;
    messages = [];
    renderWelcome();
    updateSendState();
  }

  // ── Context badge update ───────────────────────────────────────────────
  function updateCtxBadge() {
    if (engineId) {
      var labels = {
        engine1: "Engine 1 — Breach",
        engine2: "Engine 2 — SPX IC",
        engine3: "Engine 3 — Lead-Lag",
        engine4: "Engine 4 — Red Dog",
        engine5: "Engine 5 — Ichimoku",
        engine6: "Engine 6 — Pairs",
        engine7: "Engine 7 — Post-Event",
        engine8: "Engine 8 — Credit Stress",
        engine9: "Engine 9 — Calendar",
        engine10: "Engine 10 — Compare",
        engine11: "Engine 11 — News Risk",
        engine12: "Engine 12 — VIX Fade",
        engine13: "Engine 13 — Gap Regime",
        "market-intelligence": "Market Intelligence",
      };
      var label = labels[engineId] || engineId;
      ctxBadge.textContent = engineData ? label + " context loaded" : label + " (no scan data)";
      ctxBadge.classList.add("rcShow");
    } else {
      ctxBadge.classList.remove("rcShow");
    }
  }

  // ── Send button state ──────────────────────────────────────────────────
  function updateSendState() {
    sendBtn.disabled = isStreaming || !input.value.trim();
  }
  input.addEventListener("input", updateSendState);

  // ── Markdown-lite renderer ─────────────────────────────────────────────
  function renderMarkdown(text) {
    var escaped = text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");

    escaped = escaped
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");

    var lines = escaped.split("\n");
    var html = "";
    var inList = false;
    var listType = "";

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      var olMatch = line.match(/^(\d+)\.\s+(.*)/);
      var ulMatch = line.match(/^[-*]\s+(.*)/);

      if (olMatch) {
        if (!inList || listType !== "ol") {
          if (inList) html += "</" + listType + ">";
          html += "<ol>";
          inList = true;
          listType = "ol";
        }
        html += "<li>" + olMatch[2] + "</li>";
      } else if (ulMatch) {
        if (!inList || listType !== "ul") {
          if (inList) html += "</" + listType + ">";
          html += "<ul>";
          inList = true;
          listType = "ul";
        }
        html += "<li>" + ulMatch[1] + "</li>";
      } else {
        if (inList) {
          html += "</" + listType + ">";
          inList = false;
        }
        if (line.trim() === "") {
          html += "</p><p>";
        } else {
          html += line + "\n";
        }
      }
    }
    if (inList) html += "</" + listType + ">";

    html = "<p>" + html + "</p>";
    html = html.replace(/<p>\s*<\/p>/g, "");
    return html;
  }

  /** Only auto-scroll during streaming if the user is already near the bottom (read older bubbles freely). */
  var RC_SCROLL_PIN_PX = 120;
  function msgAreaNearBottom() {
    var el = msgArea;
    return el.scrollHeight - el.scrollTop - el.clientHeight <= RC_SCROLL_PIN_PX;
  }
  function scrollMsgAreaToBottom() {
    msgArea.scrollTop = msgArea.scrollHeight;
  }

  // ── Append message bubble ──────────────────────────────────────────────
  function appendMessage(role, content) {
    var welcomeEl = msgArea.querySelector(".rcWelcome");
    if (welcomeEl) welcomeEl.remove();

    var bubble = el("div", "rcMsg " + (role === "user" ? "rcMsgUser" : "rcMsgBot"));
    if (role === "user") {
      bubble.textContent = content;
    } else {
      bubble.innerHTML = renderMarkdown(content);
    }
    msgArea.appendChild(bubble);
    scrollMsgAreaToBottom();
    return bubble;
  }

  // ── Trim engine data to avoid Nginx 413 / payload bloat ─────────────
  var MAX_ENGINE_CHARS = 120000;
  function trimEngineData(data) {
    if (!data) return null;
    var s = JSON.stringify(data);
    if (s.length <= MAX_ENGINE_CHARS) return data;
    var slim = {};
    for (var k in data) {
      if (!data.hasOwnProperty(k)) continue;
      var v = data[k];
      if (Array.isArray(v) && v.length > 10) {
        slim[k] = v.slice(0, 10);
      } else if (typeof v === "object" && v !== null && JSON.stringify(v).length > 8000) {
        var inner = {};
        for (var ik in v) {
          if (!v.hasOwnProperty(ik)) continue;
          var iv = v[ik];
          if (Array.isArray(iv) && iv.length > 5) inner[ik] = iv.slice(0, 5);
          else inner[ik] = iv;
        }
        slim[k] = inner;
      } else {
        slim[k] = v;
      }
    }
    s = JSON.stringify(slim);
    while (s.length > MAX_ENGINE_CHARS && slim && typeof slim === "object") {
      var sk = Object.keys(slim);
      if (!sk.length) break;
      var worst = sk[0];
      var wl = 0;
      for (var i = 0; i < sk.length; i++) {
        var L = JSON.stringify(slim[sk[i]]).length;
        if (L > wl) {
          wl = L;
          worst = sk[i];
        }
      }
      delete slim[worst];
      s = JSON.stringify(slim);
    }
    if (s.length > MAX_ENGINE_CHARS) {
      return { _truncated: true, preview: s.substring(0, MAX_ENGINE_CHARS - 80) };
    }
    return slim;
  }

  // ── Send message + stream response ─────────────────────────────────────
  function sendMessage() {
    var text = input.value.trim();
    if (!text || isStreaming) return;

    messages.push({ role: "user", content: text });
    appendMessage("user", text);
    input.value = "";
    input.style.height = "auto";
    updateSendState();

    isStreaming = true;
    abortCtrl = new AbortController();

    var botBubble = appendMessage("assistant", "");
    var cursor = el("span", "rcCursor");
    botBubble.appendChild(cursor);

    var fullText = "";

    var body = { messages: messages };
    if (engineId) body.engineId = engineId;
    if (engineData) body.engineData = trimEngineData(engineData);

    fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: abortCtrl.signal,
    })
      .then(function (resp) {
        if (!resp.ok) {
          throw new Error("Chat request failed (" + resp.status + ")");
        }
        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";

        function pump() {
          return reader.read().then(function (result) {
            if (result.done) {
              finishStream();
              return;
            }
            buffer += decoder.decode(result.value, { stream: true });

            var lines = buffer.split("\n");
            buffer = lines.pop() || "";

            for (var j = 0; j < lines.length; j++) {
              var line = lines[j].trim();
              if (!line.startsWith("data: ")) continue;
              var payload;
              try { payload = JSON.parse(line.slice(6)); } catch (e) { continue; }

              if (payload.done) {
                finishStream();
                return;
              }
              if (payload.error) {
                botBubble.classList.add("rcError");
                fullText = payload.error;
                botBubble.innerHTML = renderMarkdown(fullText);
                finishStream();
                return;
              }
              if (payload.chunk) {
                fullText += payload.chunk;
                botBubble.innerHTML = renderMarkdown(fullText);
                botBubble.appendChild(cursor);
                if (msgAreaNearBottom()) scrollMsgAreaToBottom();
              }
            }
            return pump();
          });
        }

        return pump();
      })
      .catch(function (err) {
        if (err.name === "AbortError") return;
        botBubble.classList.add("rcError");
        fullText = err.message || "Connection failed";
        botBubble.innerHTML = renderMarkdown(fullText);
        finishStream();
      });

    function finishStream() {
      if (cursor.parentNode) cursor.remove();
      isStreaming = false;
      abortCtrl = null;
      if (fullText) {
        messages.push({ role: "assistant", content: fullText });
      }
      updateSendState();
    }
  }

  // ── Public API ─────────────────────────────────────────────────────────
  window.RavenChat = {
    setEngineContext: function (id, data) {
      engineId = id || null;
      engineData = data || null;
      updateCtxBadge();
    },
    open: openDrawer,
    close: closeDrawer,
    toggle: toggleDrawer,
  };
})();
