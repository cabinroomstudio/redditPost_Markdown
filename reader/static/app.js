"use strict";

// ---------- bridge: pywebview in the app, HTTP when served by devserver.py
function call(method, ...args) {
  if (window.pywebview && window.pywebview.api) return window.pywebview.api[method](...args);
  return fetch("/api/" + method, { method: "POST", body: JSON.stringify(args) }).then(r => r.json());
}

const $ = id => document.getElementById(id);
const S = {
  lib: null, notes: [], byId: new Map(),
  view: "all", sub: null, tag: null,
  query: "", hits: null,
  current: null, open: null, sig: "",
  settings: {},
  selected: new Set(), anchor: null,   // multi-select in the list
};
const ICON_STAR = '<svg viewBox="0 0 20 20"><path d="M10 16.5s-6.5-3.9-6.5-8.6A3.4 3.4 0 0 1 10 6a3.4 3.4 0 0 1 6.5 1.9c0 4.7-6.5 8.6-6.5 8.6z"/></svg>';

// ---------- helpers
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function toast(msg) { const t = $("toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2200); }
function parseDate(s) { const d = new Date(s); return isNaN(d) ? null : d; }
function ago(s) {
  const d = typeof s === "number" ? new Date(s * 1000) : parseDate(s);
  if (!d) return "";
  const days = Math.floor((Date.now() - d) / 864e5);
  if (days < 1) { const h = Math.floor((Date.now() - d) / 36e5); return h < 1 ? "just now" : h + "h ago"; }
  if (days < 30) return days + "d ago";
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: d.getFullYear() === new Date().getFullYear() ? undefined : "numeric" });
}
function fullDate(s) { const d = parseDate(s); return d ? d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : ""; }
const num = n => Number(n || 0).toLocaleString();
const isRead = n => !!(n.state && n.state.read_at);
const subLabel = s => s ? "r/" + esc(s) : "My own docs";

// ---------- sanitiser: allowlist, applied inside an inert <template>
const OK_TAGS = new Set("P A STRONG EM B I U CODE PRE BLOCKQUOTE UL OL LI H1 H2 H3 H4 H5 H6 HR BR TABLE THEAD TBODY TR TH TD DEL S SUP SUB IMG SPAN DIV".split(" "));
function sanitize(html) {
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  const walk = node => {
    for (const el of [...node.children]) {
      if (!OK_TAGS.has(el.tagName)) { el.replaceWith(document.createTextNode(el.textContent)); continue; }
      for (const a of [...el.attributes]) {
        const name = a.name.toLowerCase();
        const keep = (el.tagName === "A" && name === "href" && /^(https?:|#)/i.test(a.value)) ||
          (el.tagName === "IMG" && name === "src" && /^https:/i.test(a.value)) ||
          (el.tagName === "IMG" && name === "alt") ||
          ((el.tagName === "TH" || el.tagName === "TD") && name === "align");
        if (!keep) el.removeAttribute(a.name);
      }
      walk(el);
    }
  };
  walk(tpl.content);
  return tpl.content;
}

function highlight(root, query) {
  const terms = query.toLowerCase().split(/\s+/).filter(t => t.length > 1);
  if (!terms.length) return;
  const re = new RegExp("(" + terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "gi");
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) if (re.test(walker.currentNode.nodeValue)) nodes.push(walker.currentNode);
  for (const tn of nodes) {
    const frag = document.createDocumentFragment();
    tn.nodeValue.split(re).forEach((part, i) => {
      if (i % 2) { const m = document.createElement("mark"); m.textContent = part; frag.append(m); }
      else if (part) frag.append(part);
    });
    tn.replaceWith(frag);
  }
}

// ---------- settings
function applySettings() {
  const s = S.settings, root = document.documentElement;
  root.dataset.theme = s.theme || "auto";
  root.style.setProperty("--read-size", (s.size || 18) + "px");
  root.style.setProperty("--read-width", (s.width || 720) + "px");
  root.style.setProperty("--read-font", s.font === "sans" ? "var(--sans)" : "var(--serif)");
  $("size-val").textContent = (s.size || 18) + "px";
  document.querySelectorAll(".seg[data-key]").forEach(seg => {
    seg.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.v === String(s[seg.dataset.key])));
  });
  $("sort").value = s.sort || "captured";
}
async function setSetting(key, value) {
  S.settings[key] = value;
  applySettings();
  S.settings = await call("update_settings", { [key]: value });
  if (key === "collapse_replies" && S.open) setAllReplies(!value);
  if (key === "sort") renderList();
}

// ---------- library
async function loadLibrary(keepScroll) {
  const lib = await call("library");
  S.lib = lib;
  S.notes = lib.notes;
  S.byId = new Map(lib.notes.map(n => [n.id, n]));
  S.settings = lib.settings;
  applySettings();
  $("folder-label").textContent = lib.folder;
  $("folder-label").title = lib.folder;
  if (lib.skipped.length) toast(lib.skipped.length + " file(s) could not be read: " + lib.skipped.slice(0, 3).join(", "));
  renderRail();
  renderList(keepScroll);
  renderWelcome();
}

function viewFilter(n) {
  const st = n.state || {};
  switch (S.view) {
    case "unread": return !st.read_at && !st.archived;
    case "starred": return !!st.starred;
    case "noted": return !!(st.my_note && st.my_note.trim());
    case "progress": return !st.read_at && st.pos > 0.03 && !st.archived;
    case "updated": return n.updated;
    case "archived": return !!st.archived;
    default: return !st.archived;
  }
}

function filtered() {
  let list = S.notes.filter(viewFilter);
  if (S.sub) list = list.filter(n => n.subreddit === S.sub);
  if (S.tag) list = list.filter(n => (n.state.tags || []).includes(S.tag));
  if (S.hits) list = list.filter(n => S.hits.has(n.id));
  const key = S.settings.sort || "captured";
  const by = {
    captured: n => parseDate(n.captured) || 0, posted: n => parseDate(n.posted) || 0,
    opened: n => n.state.opened_at || 0, score: n => n.score, comments: n => n.comments, words: n => n.words,
  }[key];
  if (key === "title") list.sort((a, b) => a.title.localeCompare(b.title));
  else list.sort((a, b) => by(b) - by(a));
  return list;
}

function renderRail() {
  const all = S.notes;
  const count = f => all.filter(f).length;
  const views = [
    ["all", "All notes", count(n => !n.state.archived)],
    ["unread", "Unread", count(n => !n.state.read_at && !n.state.archived)],
    ["progress", "In progress", count(n => !n.state.read_at && n.state.pos > 0.03 && !n.state.archived)],
    ["starred", "Favorites", count(n => n.state.starred)],
    ["highlights", "Highlights", all.reduce((a, n) => a + (n.state.highlights || []).length, 0)],
    ["comments", "Saved comments", all.reduce((a, n) => a + (n.state.saved_comments || []).length, 0)],
    ["noted", "With my notes", count(n => n.state.my_note && n.state.my_note.trim())],
    ["updated", "Updated since read", count(n => n.updated)],
    ["archived", "Archive", count(n => n.state.archived)],
  ];
  $("views").innerHTML = views.filter(v => v[2] || ["all", "unread", "starred", "highlights", "comments"].includes(v[0]))
    .map(([k, l, c]) => `<button class="rail-item ${S.view === k ? "on" : ""}" data-view="${k}"><span class="lbl">${l}</span><span class="n">${c}</span></button>`).join("");

  const subs = new Map();
  all.forEach(n => subs.set(n.subreddit, (subs.get(n.subreddit) || 0) + 1));
  $("subs").innerHTML = `<button class="rail-item ${!S.sub ? "on" : ""}" data-sub=""><span class="lbl">Every subreddit</span></button>` +
    [...subs].sort((a, b) => b[1] - a[1]).map(([s, c]) =>
      `<button class="rail-item ${S.sub === s ? "on" : ""}" data-sub="${esc(s)}"><span class="lbl">${subLabel(s)}</span><span class="n">${c}</span></button>`).join("");

  const tags = new Map();
  all.forEach(n => (n.state.tags || []).forEach(t => tags.set(t, (tags.get(t) || 0) + 1)));
  $("tags-head").hidden = !tags.size;
  $("tags").innerHTML = [...tags].sort((a, b) => a[0].localeCompare(b[0])).map(([t, c]) =>
    `<button class="rail-item ${S.tag === t ? "on" : ""}" data-tag="${esc(t)}"><span class="lbl"># ${esc(t)}</span><span class="n">${c}</span></button>`).join("");
}

function itemHTML(n) {
  const st = n.state || {};
  const pos = !st.read_at && st.pos > 0.03 ? `<span class="bar" title="${Math.round(st.pos * 100)}% read"><i style="width:${Math.round(st.pos * 100)}%"></i></span>` : "";
  return `<li class="item ${isRead(n) ? "read" : ""} ${S.current === n.id ? "on" : ""} ${S.selected.has(n.id) ? "sel" : ""}" data-id="${esc(n.id)}">
    <span class="dot"></span>
    <div class="t">${esc(n.title)}</div>
    ${n.preview ? `<div class="p">${esc(n.preview)}</div>` : ""}
    <div class="m"><span class="sub">${subLabel(n.subreddit)}</span><span>${ago(n.captured)}</span><span>${n.minutes} min</span><span>${num(n.comments)} comments</span>
      ${st.starred ? `<span class="star">${ICON_STAR}</span>` : ""}${n.updated ? '<span class="badge">updated</span>' : ""}${n.removed ? '<span class="badge gray">removed</span>' : ""}${pos}</div>
  </li>`;
}

function renderList(keepScroll) {
  if (S.view === "highlights" || S.view === "comments") return renderCollection(keepScroll);
  $("list-tools").hidden = false;
  const list = filtered();
  S.visible = list;
  const el = $("list"), top = el.scrollTop;
  el.innerHTML = list.map(itemHTML).join("");
  if (keepScroll) el.scrollTop = top;
  $("count").textContent = `${list.length} ${list.length === 1 ? "note" : "notes"}`;
  const empty = $("list-empty");
  empty.hidden = !!list.length;
  if (!list.length) empty.textContent = !S.lib.exists ? `No notes folder at ${S.lib.folder}` : S.query ? `Nothing matches "${S.query}".` : "Nothing here yet.";
  renderBulkBar();
}

function renderWelcome() {
  const all = S.notes;
  const unread = all.filter(n => !isRead(n) && !n.state.archived).length;
  const words = all.reduce((a, n) => a + n.words, 0);
  $("welcome-stats").textContent = `${all.length} threads saved from ${new Set(all.map(n => n.subreddit)).size} subreddits, ${num(words)} words in all. ${unread} still unread.`;
  const going = all.filter(n => !isRead(n) && n.state.pos > 0.03 && !n.state.archived).sort((a, b) => (b.state.opened_at || 0) - (a.state.opened_at || 0)).slice(0, 3);
  const fresh = all.filter(n => !isRead(n) && !n.state.archived && !(n.state.pos > 0.03)).sort((a, b) => (parseDate(b.captured) || 0) - (parseDate(a.captured) || 0)).slice(0, 4);
  const card = n => `<button class="cont" data-id="${esc(n.id)}"><div class="t">${esc(n.title)}</div><div class="m"><span>${subLabel(n.subreddit)}</span><span>${n.minutes} min</span>${n.state.pos > 0.03 ? `<span class="bar"><i style="width:${Math.round(n.state.pos * 100)}%"></i></span>` : `<span>saved ${ago(n.captured)}</span>`}</div></button>`;
  $("continue").innerHTML = (going.length ? `<div class="cont-head">Continue reading</div>${going.map(card).join("")}<br>` : "") +
    (fresh.length ? `<div class="cont-head">Newest unread</div>${fresh.map(card).join("")}` : "");
}

// ---------- search
let searchTimer;
function onSearch() {
  S.query = $("q").value.trim();
  clearTimeout(searchTimer);
  if (!S.query) { S.hits = null; renderList(); return; }
  searchTimer = setTimeout(async () => {
    const q = S.query;
    const ids = await call("search", q);
    if (q !== S.query) return;
    S.hits = new Set(ids);
    renderList();
  }, 180);
}

// ---------- open a note
async function openNote(id, versionFile) {
  if (!id) return;
  saveNoteNow();
  const res = await call("open_note", id, versionFile || null);
  if (res.error) { toast(res.error); return; }
  S.current = id;
  S.open = res;
  const n = S.byId.get(id);
  if (n) n.state = res.state;
  document.querySelectorAll("#list .item").forEach(li => li.classList.toggle("on", li.dataset.id === id));
  const li = document.querySelector(`#list .item[data-id="${CSS.escape(id)}"]`);
  if (li) li.scrollIntoView({ block: "nearest" });
  renderArticle(res);
  renderToolbar();
  renderDrawer();
  const pos = res.state.pos;
  const sc = $("scroller");
  sc.scrollTop = 0;
  if (!versionFile && pos > 0.03 && pos < 0.97 && !res.state.read_at) {
    // a timer, not requestAnimationFrame: rAF never fires while the window is in the background
    setTimeout(() => { sc.scrollTop = pos * (sc.scrollHeight - sc.clientHeight); toast("Picked up where you left off"); }, 0);
  }
  updateProgress();
}

function renderArticle(res) {
  const s = res.summary;
  $("welcome").hidden = true;
  $("article").hidden = false;
  $("toolbar").hidden = false;

  const old = $("viewing-old");
  old.hidden = !res.viewing;
  if (res.viewing) {
    const v = res.versions.find(v => v.file === res.viewing);
    old.innerHTML = `<span>You are reading an older capture (v${v ? v.version : "?"}). The latest is v${s.version}.</span><button data-latest>Back to latest</button>`;
  }

  $("btn-open").hidden = !s.url;
  if (!s.url) {
    $("head").innerHTML = `<div class="kicker"><span class="sub">My own doc</span><span>${esc(s.file)}</span></div><h1>${esc(s.title)}</h1>
      <div class="facts"><span><b>${s.minutes}</b> min read</span><span title="${esc(fullDate(s.captured))}">edited ${ago(s.captured)}</span></div>`;
  } else $("head").innerHTML = `
    <div class="kicker"><span class="sub">r/${esc(s.subreddit)}</span><span>u/${esc(s.author)}</span><span title="${esc(fullDate(s.posted))}">posted ${ago(s.posted)}</span>${s.removed ? '<span class="badge gray">removed by mods</span>' : ""}</div>
    <h1>${esc(s.title)}</h1>
    <div class="facts"><span><b>${num(s.score)}</b> points</span><span><b>${Math.round((s.ratio || 0) * 100)}%</b> upvoted</span><span><b>${num(s.comments)}</b> of ${num(s.comments_reported)} comments kept</span><span><b>${s.minutes}</b> min read</span><span title="${esc(fullDate(s.captured))}">saved ${ago(s.captured)}</span><span>v${s.version}</span></div>`;

  const frag = sanitize(res.html);
  // drop the markdown's own title and the "r/sub · u/x · pts" line: the header shows them
  const first = frag.firstElementChild;
  if (first && first.tagName === "H1") first.remove();
  const meta = frag.firstElementChild;
  if (meta && meta.tagName === "P" && /^r\//.test(meta.textContent)) meta.remove();

  const body = document.createDocumentFragment();
  const comments = document.createDocumentFragment();
  let changelog = null, mode = "body", card = null, ccount = 0;
  for (const el of [...frag.children]) {
    if (el.tagName === "H2" && /^Comments \(/.test(el.textContent)) { mode = "comments"; $("ctitle").textContent = el.textContent; continue; }
    if (el.tagName === "H2" && /^Changelog$/.test(el.textContent.trim())) { mode = "changelog"; continue; }
    if (mode === "body") body.append(el);
    else if (mode === "changelog") { changelog = changelog || document.createElement("div"); changelog.append(el); }
    else {
      if (el.tagName === "H3") {
        card = makeCard(el.textContent); comments.append(card); ccount++;
      } else if (el.tagName === "HR") {
        card = null;
      } else if (card) {
        if (el.tagName === "BLOCKQUOTE") card._replies.append(el); else card._text.append(el);
      } else comments.append(el);
    }
  }
  $("body").replaceChildren(body);
  comments.querySelectorAll(".comment").forEach(finishCard);
  $("comments").replaceChildren(comments);
  $("cbar").hidden = !ccount;
  $("changelog").innerHTML = "";
  if (changelog) {
    const d = document.createElement("details");
    d.innerHTML = "<summary>Capture history</summary>";
    d.append(changelog);
    $("changelog").append(d);
  }
  if (S.query) highlight($("article"), S.query);
  $("f-op").checked = false;
  $("f-score").value = "-999";
  setAllReplies(!S.settings.collapse_replies);
  afterArticle(res);
}

function makeCard(headText) {
  // "u/name — 12 pts (OP)"
  const m = headText.match(/^u\/(\S+)\s+\S+\s+(-?\d+)\s+pts(\s+\(OP\))?/);
  const card = document.createElement("section");
  card.className = "comment";
  card.dataset.score = m ? m[2] : "0";
  card.dataset.op = m && m[3] ? "1" : "0";
  card.innerHTML = `<div class="c-head"><span class="who">u/${esc(m ? m[1] : headText)}</span>${m && m[3] ? '<span class="op">OP</span>' : ""}<span class="pts">${m ? num(m[2]) + " pts" : ""}</span></div>`;
  card._text = document.createElement("div");
  card._text.className = "c-body";
  card._replies = document.createElement("div");
  card._replies.className = "replies";
  return card;
}

function finishCard(card) {
  card.append(card._text);
  const bylines = [...card._replies.querySelectorAll("p")].filter(p => /^u\//.test((p.firstElementChild && p.firstElementChild.tagName === "STRONG" && p.firstElementChild.textContent) || ""));
  bylines.forEach(p => {
    p.classList.add("byline");
    p.innerHTML = p.innerHTML.replace(/ · OP\b/, ' · <span class="op">OP</span>');
    if (/OP<\/span>/.test(p.innerHTML)) card.dataset.op = "1";
  });
  if (bylines.length) {
    const btn = document.createElement("button");
    btn.className = "reply-toggle";
    btn._n = bylines.length;
    btn.addEventListener("click", () => toggleReplies(card));
    card.append(btn, card._replies);
  }
  syncToggle(card);
}

function toggleReplies(card, show) {
  if (!card._replies.parentNode) return;
  card._replies.hidden = show === undefined ? !card._replies.hidden : !show;
  syncToggle(card);
}
function syncToggle(card) {
  const btn = card.querySelector(".reply-toggle");
  if (btn) btn.textContent = card._replies.hidden ? `Show ${btn._n} ${btn._n === 1 ? "reply" : "replies"}` : `Hide ${btn._n === 1 ? "reply" : "replies"}`;
}
function setAllReplies(show) {
  document.querySelectorAll("#comments .comment").forEach(c => toggleReplies(c, show));
  $("btn-toggle-replies").textContent = show ? "Collapse all replies" : "Show all replies";
  $("btn-toggle-replies").dataset.show = show ? "1" : "0";
}

function filterComments() {
  const opOnly = $("f-op").checked, min = Number($("f-score").value);
  let shown = 0;
  document.querySelectorAll("#comments .comment").forEach(c => {
    const ok = Number(c.dataset.score) >= min && (!opOnly || c.dataset.op === "1");
    c.hidden = !ok;
    if (ok) shown++;
  });
  const total = document.querySelectorAll("#comments .comment").length;
  $("ctitle").textContent = shown === total ? $("ctitle").textContent.replace(/ · .*/, "") : `${$("ctitle").textContent.replace(/ · .*/, "")} · showing ${shown}`;
}

function renderToolbar() {
  const n = S.byId.get(S.current);
  if (!n) return;
  const st = n.state || {};
  $("btn-star").classList.toggle("on", !!st.starred);
  $("btn-star").title = st.starred ? "Unstar (s)" : "Star (s)";
  $("btn-read").classList.toggle("on", !!st.read_at);
  $("btn-read").title = st.read_at ? "Mark unread (m)" : "Mark read (m)";
  $("btn-archive").classList.toggle("on", !!st.archived);
  $("btn-archive").title = st.archived ? "Move back to library (e)" : "Archive (e)";
  $("btn-notes").classList.toggle("on", !$("drawer").hidden || !!(st.my_note && st.my_note.trim()));
  const sel = $("versions"), vs = S.open.versions;
  sel.hidden = !vs.length;
  if (vs.length) {
    sel.innerHTML = `<option value="">v${S.open.summary.version} · latest</option>` + vs.slice().reverse().map(v => `<option value="${esc(v.file)}">v${v.version}</option>`).join("");
    sel.value = S.open.viewing || "";
  }
}

// ---------- state changes
async function patch(changes, msg) {
  const id = S.current, n = S.byId.get(id);
  if (!n) return;
  n.state = await call("update_note", id, changes);
  n.updated = false;
  renderToolbar(); renderRail(); renderList(true); renderWelcome();
  if (msg) toast(msg);
}
async function setRead(read) {
  const id = S.current, n = S.byId.get(id);
  if (!n) return;
  n.state = await call("mark_read", id, read);
  n.updated = false;
  renderToolbar(); renderRail(); renderList(true); renderWelcome();
}
const toggleStar = () => { const st = S.byId.get(S.current)?.state || {}; patch({ starred: st.starred ? null : true }, st.starred ? "Removed from favorites" : "Added to favorites"); };
const toggleRead = () => { const st = S.byId.get(S.current)?.state || {}; setRead(!st.read_at).then(() => toast(st.read_at ? "Marked unread" : "Marked read")); };
const toggleArchive = () => { const st = S.byId.get(S.current)?.state || {}; patch({ archived: st.archived ? null : true }, st.archived ? "Back in the library" : "Archived"); };

// reading position + auto mark read
let posTimer;
function updateProgress() {
  const sc = $("scroller");
  const max = sc.scrollHeight - sc.clientHeight;
  const frac = max > 0 ? sc.scrollTop / max : 1;
  $("progress-bar").style.width = S.current && !$("article").hidden ? (frac * 100).toFixed(1) + "%" : "0";
  return frac;
}
$("scroller").addEventListener("scroll", () => {
  const frac = updateProgress();
  if (!S.current || S.open?.viewing) return;
  clearTimeout(posTimer);
  posTimer = setTimeout(() => {
    const n = S.byId.get(S.current);
    if (!n) return;
    if (frac > 0.92 && !n.state.read_at) setRead(true).then(() => toast("Finished. Marked as read"));
    else if (!n.state.read_at && Math.abs((n.state.pos || 0) - frac) > 0.02) {
      call("update_note", n.id, { pos: Math.round(frac * 1000) / 1000 }).then(st => { n.state = st; renderList(true); });
    }
  }, 600);
});

// ---------- drawer: my notes + tags
function renderDrawer() {
  const st = S.byId.get(S.current)?.state || {};
  $("my-note").value = st.my_note || "";
  $("tag-chips").innerHTML = (st.tags || []).map(t => `<span class="chip">${esc(t)}<button data-untag="${esc(t)}" aria-label="Remove tag ${esc(t)}">×</button></span>`).join("");
  $("saved").textContent = "";
}
let noteTimer, noteDirty = false;
function saveNoteNow() {
  if (!noteDirty || !S.current) return;
  noteDirty = false;
  clearTimeout(noteTimer);
  const id = S.current, text = $("my-note").value;
  call("update_note", id, { my_note: text.trim() ? text : null }).then(st => {
    const n = S.byId.get(id); if (n) n.state = st;
    $("saved").textContent = "Saved"; renderRail(); renderToolbar();
  });
}
$("my-note").addEventListener("input", () => { noteDirty = true; $("saved").textContent = "Saving…"; clearTimeout(noteTimer); noteTimer = setTimeout(saveNoteNow, 600); });
$("my-note").addEventListener("blur", saveNoteNow);
$("tag-input").addEventListener("keydown", e => {
  if (e.key !== "Enter") return;
  const t = e.target.value.trim().toLowerCase().replace(/\s+/g, "-").replace(/^#/, "");
  if (!t) return;
  const tags = [...new Set([...(S.byId.get(S.current)?.state.tags || []), t])];
  e.target.value = "";
  patch({ tags }).then(renderDrawer);
});
$("tag-chips").addEventListener("click", e => {
  const t = e.target.closest("[data-untag]")?.dataset.untag;
  if (!t) return;
  const tags = (S.byId.get(S.current)?.state.tags || []).filter(x => x !== t);
  patch({ tags: tags.length ? tags : null }).then(renderDrawer);
});
function toggleDrawer(force) {
  const d = $("drawer");
  d.hidden = force === undefined ? !d.hidden : !force;
  if (!d.hidden) $("my-note").focus(); else saveNoteNow();
  renderToolbar();
}

// ---------- navigation
function step(dir) {
  const list = S.visible || [];
  if (!list.length) return;
  const i = list.findIndex(n => n.id === S.current);
  const next = list[Math.min(list.length - 1, Math.max(0, i < 0 ? 0 : i + dir))];
  if (next && next.id !== S.current) openNote(next.id);
}

// ---------- events
$("list").addEventListener("click", e => {
  const li = e.target.closest(".item");
  if (!li) return;
  if (li.dataset.ref) return openRef(li.dataset.id, li.dataset.ref);   // highlight / saved comment
  if (e.ctrlKey || e.metaKey || e.shiftKey) { e.preventDefault(); return selectClick(li.dataset.id, e.shiftKey); }
  if (S.selected.size) clearSelection();
  S.anchor = li.dataset.id;
  openNote(li.dataset.id);
});
$("continue").addEventListener("click", e => { const b = e.target.closest(".cont"); if (b) openNote(b.dataset.id); });
$("rail").addEventListener("click", e => {
  const b = e.target.closest(".rail-item");
  if (!b) return;
  if (b.dataset.stats !== undefined) return showStats();
  if (b.dataset.view !== undefined) { S.view = b.dataset.view; S.tag = null; clearSelection(true); }
  if (b.dataset.sub !== undefined) S.sub = b.dataset.sub || null;
  if (b.dataset.tag !== undefined) S.tag = S.tag === b.dataset.tag ? null : b.dataset.tag;
  renderRail(); renderList();
});
$("q").addEventListener("input", onSearch);
$("sort").addEventListener("change", e => setSetting("sort", e.target.value));
$("btn-prev").onclick = () => step(-1);
$("btn-next").onclick = () => step(1);
$("btn-star").onclick = toggleStar;
$("btn-read").onclick = toggleRead;
$("btn-archive").onclick = toggleArchive;
$("btn-notes").onclick = () => toggleDrawer();
$("btn-close-drawer").onclick = () => toggleDrawer(false);
$("btn-open").onclick = () => S.open && call("open_url", S.open.summary.url);
$("btn-file").onclick = () => S.current && call("reveal", S.current);
$("btn-folder").onclick = () => call("open_folder");
$("btn-export").onclick = async () => toast(await call("export_my_notes"));
$("btn-keys").onclick = () => { $("keys").hidden = false; };
$("btn-keys-close").onclick = () => { $("keys").hidden = true; };
$("keys").addEventListener("click", e => { if (e.target.id === "keys") $("keys").hidden = true; });
$("versions").addEventListener("change", e => openNote(S.current, e.target.value || null));
$("viewing-old").addEventListener("click", e => { if (e.target.closest("[data-latest]")) openNote(S.current); });
$("btn-type").onclick = e => { e.stopPropagation(); $("typepanel").hidden = !$("typepanel").hidden; };
document.addEventListener("click", e => { if (!e.target.closest("#typepanel") && !e.target.closest("#btn-type")) $("typepanel").hidden = true; });
$("typepanel").addEventListener("click", e => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.id === "size-up") return setSetting("size", Math.min(26, (S.settings.size || 18) + 1));
  if (b.id === "size-dn") return setSetting("size", Math.max(13, (S.settings.size || 18) - 1));
  const seg = b.closest(".seg[data-key]");
  if (!seg) return;
  let v = b.dataset.v;
  if (seg.dataset.key === "width") v = Number(v);
  if (seg.dataset.key === "collapse_replies") v = v === "true";
  setSetting(seg.dataset.key, v);
});
$("btn-toggle-replies").onclick = () => setAllReplies($("btn-toggle-replies").dataset.show !== "1");
$("f-op").addEventListener("change", filterComments);
$("f-score").addEventListener("change", filterComments);
// links open in the real browser, never inside the app window
$("article").addEventListener("click", e => {
  const a = e.target.closest("a[href]");
  if (!a) return;
  e.preventDefault();
  if (/^https?:/i.test(a.getAttribute("href"))) call("open_url", a.href);
});

document.addEventListener("keydown", e => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if (e.key === "Escape") {
    if (!$("keys").hidden) { $("keys").hidden = true; return; }
    if (!$("typepanel").hidden) { $("typepanel").hidden = true; return; }
    if (!$("drawer").hidden) { toggleDrawer(false); return; }
    if (closePopovers()) return;
    if (S.selected.size) { clearSelection(); return; }
    if (S.query) { $("q").value = ""; onSearch(); }
    document.activeElement.blur();
    return;
  }
  if (typing || e.ctrlKey || e.altKey || e.metaKey) return;
  const k = e.key;
  if (k === "/") { e.preventDefault(); $("q").focus(); $("q").select(); }
  else if (k === "j" || (k === "ArrowDown" && document.activeElement.id === "list")) { e.preventDefault(); step(1); }
  else if (k === "k" || (k === "ArrowUp" && document.activeElement.id === "list")) { e.preventDefault(); step(-1); }
  else if (k === "r") surprise();
  else if (k === "h" && highlightSelection()) e.preventDefault();
  else if (!S.current) return;
  else if (k === "s" || k === "f") toggleStar();
  else if (k === "b") saveHoveredComment();
  else if (k === "m") toggleRead();
  else if (k === "e") toggleArchive();
  else if (k === "n") { e.preventDefault(); toggleDrawer(); }
  else if (k === "c") setAllReplies($("btn-toggle-replies").dataset.show !== "1");
  else if (k === "o") call("open_url", S.open.summary.url);
  else if (k === "?") $("keys").hidden = false;
});

// pick up new captures from reddit-md while the app is open
setInterval(async () => {
  if (document.hidden) return;
  const sig = await call("signature");
  if (sig !== S.sig) {
    const first = !S.sig;
    S.sig = sig;
    if (!first) { await loadLibrary(true); if (S.query) onSearch(); toast("Library updated"); }
  }
}, 5000);
window.addEventListener("beforeunload", saveNoteNow);

async function start() {
  await loadLibrary();
  S.sig = await call("signature");
}
// devserver.py opens the page with ?dev and answers on the HTTP bridge
if (new URLSearchParams(location.search).has("dev") || (window.pywebview && window.pywebview.api)) start();
else window.addEventListener("pywebviewready", start, { once: true });
