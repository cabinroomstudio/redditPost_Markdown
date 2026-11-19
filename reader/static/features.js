"use strict";
// Favorites bulk actions, highlights, saved comments, stats and Surprise me.
// Loaded after app.js and uses its globals (S, $, call, esc, render*).

const BLOCK_SEL = "p, li, h1, h2, h3, h4, h5, h6, td, th, pre";
const ICON_MARK = '<svg viewBox="0 0 20 20"><path d="M6 3h8v14l-4-3-4 3z"/></svg>';

function hashText(s) { let h = 5381; for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0; return (h >>> 0).toString(36); }
const uid = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
const noteState = id => (S.byId.get(id) || {}).state || {};

function mergeStates(map) {
  for (const [id, st] of Object.entries(map)) { const n = S.byId.get(id); if (n) { n.state = st; n.updated = false; } }
  renderRail(); renderList(true); renderWelcome();
  if (S.current && map[S.current]) renderToolbar();
}

// ---------- toast with Undo (replaces the plain one in app.js)
let toastTimer;
function showToast(msg, undo) {
  const t = $("toast"), b = $("toast-undo");
  $("toast-msg").textContent = msg;
  b.hidden = !undo;
  b.onclick = undo ? () => { t.classList.remove("show"); undo(); } : null;
  t.classList.add("show");
  t.style.pointerEvents = undo ? "auto" : "none";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), undo ? 6000 : 2200);
}
toast = msg => showToast(msg);

function closePopovers() {
  let closed = false;
  if (!$("hl-pop").hidden) { $("hl-pop").hidden = true; closed = true; }
  if (!$("hl-float").hidden) { $("hl-float").hidden = true; closed = true; }
  return closed;
}

// ================= multi-select + bulk actions
S.selectMode = false;
function selectClick(id, range) {
  if (range && S.anchor) {
    const ids = (S.visible || []).map(n => n.id);
    const a = ids.indexOf(S.anchor), b = ids.indexOf(id);
    if (a >= 0 && b >= 0) ids.slice(Math.min(a, b), Math.max(a, b) + 1).forEach(x => S.selected.add(x));
  } else {
    S.selected.has(id) ? S.selected.delete(id) : S.selected.add(id);
    S.anchor = id;
  }
  renderList(true);
}
function clearSelection(quiet) {
  S.selected.clear(); S.selectMode = false;
  $("btn-select").classList.remove("on");
  if (!quiet) renderList(true);
}
function renderBulkBar() {
  const n = S.selected.size;
  $("bulkbar").hidden = !n && !S.selectMode;
  $("bulk-count").textContent = n ? `${n} selected` : "Click notes to select";
  $("bulkbar").querySelectorAll("button[data-bulk]").forEach(b => { if (!["all", "clear"].includes(b.dataset.bulk)) b.disabled = !n; });
}
$("btn-select").onclick = () => {
  S.selectMode = !S.selectMode;
  $("btn-select").classList.toggle("on", S.selectMode);
  if (!S.selectMode) S.selected.clear();
  renderList(true);
};
$("bulkbar").addEventListener("click", async e => {
  const act = e.target.closest("button[data-bulk]")?.dataset.bulk;
  if (!act) return;
  const ids = [...S.selected];
  if (act === "clear") return clearSelection();
  if (act === "all") { (S.visible || []).forEach(n => S.selected.add(n.id)); return renderList(true); }
  if (act === "tag") return askBulkTag(ids);
  if (!ids.length) return;
  let res, msg;
  if (act === "fav") {
    const allFav = ids.every(id => noteState(id).starred);
    res = await call("bulk_update", ids, { starred: allFav ? null : true });
    msg = allFav ? `Removed ${ids.length} from favorites` : `Added ${ids.length} to favorites`;
  } else if (act === "read" || act === "unread") {
    const before = Object.fromEntries(ids.map(id => [id, { read_at: noteState(id).read_at ?? null }]));
    res = await call("bulk_read", ids, act === "read");
    mergeStates(res);
    return showToast(`Marked ${ids.length} as ${act}`, async () => mergeStates(await call("bulk_set", before)));
  } else if (act === "archive") {
    res = await call("bulk_update", ids, { archived: true });
    msg = `Archived ${ids.length}`;
    S.selected.clear();
  }
  mergeStates(res);
  showToast(msg);
});
function askBulkTag(ids) {
  if (!ids.length) return;
  const bar = $("bulkbar");
  if (bar.querySelector("input")) return bar.querySelector("input").focus();
  const inp = document.createElement("input");
  inp.placeholder = "tag, then Enter";
  inp.className = "bulk-tag";
  bar.insertBefore(inp, bar.querySelector('[data-bulk="all"]'));
  inp.focus();
  inp.addEventListener("keydown", async e => {
    if (e.key === "Escape") { inp.remove(); e.stopPropagation(); return; }
    if (e.key !== "Enter") return;
    const t = inp.value.trim().toLowerCase().replace(/\s+/g, "-").replace(/^#/, "");
    inp.remove();
    if (!t) return;
    mergeStates(await call("bulk_add_tag", [...S.selected], t));
    showToast(`Tagged ${ids.length} with #${t}`);
  });
}
// plain clicks select while in select mode
$("list").addEventListener("click", e => {
  if (!S.selectMode) return;
  const li = e.target.closest(".item");
  if (!li || li.dataset.ref) return;
  e.stopImmediatePropagation();
  selectClick(li.dataset.id, e.shiftKey);
}, true);

$("btn-allread").onclick = async () => {
  const ids = (S.visible || []).filter(n => !isRead(n)).map(n => n.id);
  if (!ids.length) return showToast("Everything here is already read");
  const before = Object.fromEntries(ids.map(id => [id, { read_at: null }]));
  mergeStates(await call("bulk_read", ids, true));
  showToast(`Marked ${ids.length} as read`, async () => { mergeStates(await call("bulk_set", before)); showToast("Undone"); });
};

// ================= Surprise me
function surprise() {
  const fresh = n => !isRead(n) && !n.state.archived;
  let pool = (S.view === "highlights" || S.view === "comments" ? [] : filtered()).filter(fresh);
  if (!pool.length) pool = S.notes.filter(fresh);
  if (!pool.length) return showToast("Nothing unread. Nice work.");
  const pick = pool.filter(n => n.id !== S.current)[Math.floor(Math.random() * Math.max(1, pool.length - 1))] || pool[0];
  openNote(pick.id).then(() => showToast("Surprise! " + pick.minutes + " min read"));
}
$("btn-surprise").onclick = surprise;
$("btn-surprise2").onclick = surprise;

// ================= after a note renders: comment bookmarks + highlights
function afterArticle(res) {
  $("stats").hidden = true;
  $("rail-stats").classList.remove("on");
  closePopovers();
  decorateComments();
  applyHighlights();
}

// ----- saved comments
function commentBody(byline) {
  const parts = [];
  for (let el = byline.nextElementSibling; el && !el.classList.contains("byline") && el.tagName !== "BLOCKQUOTE"; el = el.nextElementSibling) parts.push(el.innerText);
  return parts.join("\n\n").trim();
}
function decorateComments() {
  const saved = new Set((noteState(S.current).saved_comments || []).map(c => c.key));
  const add = (host, info) => {
    info.key = info.author + ":" + hashText(info.text.slice(0, 120));
    host.dataset.ckey = info.key;
    host._info = info;
    const b = document.createElement("button");
    b.className = "save-c" + (saved.has(info.key) ? " on" : "");
    b.title = "Save this comment (b)";
    b.innerHTML = ICON_MARK;
    b.addEventListener("click", e => { e.stopPropagation(); toggleSavedComment(host); });
    (host.querySelector(".c-head") || host).append(b);
  };
  document.querySelectorAll("#comments .comment").forEach(card => {
    const who = card.querySelector(".who")?.textContent.replace(/^u\//, "") || "?";
    const body = card.querySelector(".c-body");
    add(card, { author: who, score: Number(card.dataset.score) || 0, text: (body ? body.innerText : "").trim() });
  });
  document.querySelectorAll("#comments p.byline").forEach(p => {
    const m = p.textContent.match(/^u\/(\S+)\s+·\s+(-?\d+)\s+pts/);
    add(p, { author: m ? m[1] : "?", score: m ? Number(m[2]) : 0, text: commentBody(p) });
  });
}
async function toggleSavedComment(host) {
  const info = host._info;
  if (!info || !S.current) return;
  const list = [...(noteState(S.current).saved_comments || [])];
  const i = list.findIndex(c => c.key === info.key);
  if (i >= 0) list.splice(i, 1);
  else list.push({ key: info.key, author: info.author, score: info.score, text: info.text.slice(0, 3000), saved_at: Date.now() / 1000 });
  await patch({ saved_comments: list.length ? list : null }, i >= 0 ? "Comment removed from saved" : "Comment saved");
  host.querySelector(".save-c")?.classList.toggle("on", i < 0);
}
let hovered = null;
$("comments").addEventListener("mouseover", e => {
  const bq = e.target.closest("blockquote");
  hovered = (bq && bq.querySelector(":scope > p.byline")) || e.target.closest(".comment");
});
function saveHoveredComment() { if (hovered && hovered.isConnected) toggleSavedComment(hovered); }

// ----- highlights
function textRangeIn(block, start, end) {
  const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
  const pieces = [];
  let pos = 0;
  while (walker.nextNode()) {
    const node = walker.currentNode, len = node.nodeValue.length;
    const a = Math.max(start, pos), b = Math.min(end, pos + len);
    if (a < b) pieces.push([node, a - pos, b - pos]);
    pos += len;
  }
  return pieces;
}
function wrapHighlight(block, start, end, h) {
  for (const [node, a, b] of textRangeIn(block, start, end)) {
    let target = node;
    if (a > 0) target = target.splitText(a);
    if (b - a < target.nodeValue.length) target.splitText(b - a);
    const m = document.createElement("mark");
    m.className = "hl" + (h.note ? " has-note" : "");
    m.dataset.hid = h.id;
    if (h.note) m.title = h.note;
    target.replaceWith(m);
    m.append(target);
  }
}
function applyHighlights() {
  const hls = noteState(S.current).highlights || [];
  if (!hls.length || S.open?.viewing) return;
  const blocks = [...document.querySelectorAll("#body, #comments")].flatMap(r => [...r.querySelectorAll(BLOCK_SEL)]);
  let missing = 0;
  for (const h of hls) {
    let hit = null;
    for (const bl of blocks) {
      const txt = bl.textContent, full = (h.prefix || "") + h.text + (h.suffix || "");
      const i = txt.indexOf(full);
      if (i >= 0) { hit = [bl, i + (h.prefix || "").length]; break; }
    }
    if (!hit) for (const bl of blocks) { const i = bl.textContent.indexOf(h.text); if (i >= 0) { hit = [bl, i]; break; } }
    if (hit) wrapHighlight(hit[0], hit[1], hit[1] + h.text.length, h);
    else missing++;
  }
  if (missing) showToast(`${missing} highlight${missing > 1 ? "s" : ""} no longer match this capture; still listed under Highlights`);
}

function selectionInfo() {
  const sel = getSelection();
  if (!sel.rangeCount || sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  const anc = range.commonAncestorContainer;
  const el = anc.nodeType === 3 ? anc.parentElement : anc;
  if (!el.closest("#body, #comments")) return null;
  const block = el.closest(BLOCK_SEL);
  if (!block || !block.closest("#body, #comments")) return { error: "Highlight within one paragraph at a time" };
  if (el.closest("mark.hl")) return { error: "That is already highlighted" };
  const pre = document.createRange();
  pre.setStart(block, 0);
  pre.setEnd(range.startContainer, range.startOffset);
  const raw = range.toString();
  let start = pre.toString().length + (raw.length - raw.trimStart().length);
  const text = raw.trim();
  if (text.length < 2) return null;
  const all = block.textContent;
  const card = block.closest(".comment");
  return {
    rect: range.getBoundingClientRect(), block, start,
    h: { id: uid(), text, prefix: all.slice(Math.max(0, start - 32), start), suffix: all.slice(start + text.length, start + text.length + 32),
      where: card ? (card.querySelector(".who")?.textContent.replace(/^u\//, "") || "comment") : "post", created: Date.now() / 1000 },
  };
}
function highlightSelection() {
  const info = selectionInfo();
  if (!info) return false;
  if (info.error) { showToast(info.error); return true; }
  const list = [...(noteState(S.current).highlights || []), info.h];
  wrapHighlight(info.block, info.start, info.start + info.h.text.length, info.h);
  getSelection().removeAllRanges();
  $("hl-float").hidden = true;
  patch({ highlights: list }, "Highlighted");
  return true;
}
$("article").addEventListener("mouseup", () => setTimeout(() => {
  const info = selectionInfo();
  const f = $("hl-float");
  if (!info || info.error || S.open?.viewing) { f.hidden = true; return; }
  f.hidden = false;
  f.style.left = Math.max(8, info.rect.left + info.rect.width / 2 - f.offsetWidth / 2) + "px";
  f.style.top = Math.max(8, info.rect.top - f.offsetHeight - 8) + "px";
}, 0));
$("hl-float").addEventListener("mousedown", e => e.preventDefault()); // keep the selection
$("hl-float").onclick = () => highlightSelection();
$("scroller").addEventListener("scroll", () => { $("hl-float").hidden = true; $("hl-pop").hidden = true; });

let popHid = null;
$("article").addEventListener("click", e => {
  const m = e.target.closest("mark.hl");
  if (!m || !getSelection().isCollapsed) return;
  const h = (noteState(S.current).highlights || []).find(x => x.id === m.dataset.hid);
  if (!h) return;
  popHid = h.id;
  $("hl-pop-quote").textContent = h.text;
  $("hl-pop-note").value = h.note || "";
  const pop = $("hl-pop"), r = m.getBoundingClientRect();
  pop.hidden = false;
  pop.style.left = Math.min(innerWidth - pop.offsetWidth - 12, Math.max(12, r.left)) + "px";
  pop.style.top = Math.min(innerHeight - pop.offsetHeight - 12, r.bottom + 8) + "px";
  pop.style.right = "auto";
  $("hl-pop-note").focus();
});
async function editHighlight(remove) {
  const list = [...(noteState(S.current).highlights || [])];
  const i = list.findIndex(x => x.id === popHid);
  if (i < 0) return;
  if (remove) list.splice(i, 1);
  else list[i] = { ...list[i], note: $("hl-pop-note").value.trim() || undefined };
  $("hl-pop").hidden = true;
  await patch({ highlights: list.length ? list : null }, remove ? "Highlight removed" : "Highlight note saved");
  document.querySelectorAll(`mark.hl[data-hid="${CSS.escape(popHid)}"]`).forEach(m => {
    if (remove) m.replaceWith(...m.childNodes);
    else { m.title = list[i].note || ""; m.classList.toggle("has-note", !!list[i].note); }
  });
}
$("hl-pop-save").onclick = () => editHighlight(false);
$("hl-pop-remove").onclick = () => editHighlight(true);
$("hl-pop-note").addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) editHighlight(false); });
document.addEventListener("mousedown", e => { if (!e.target.closest("#hl-pop, mark.hl")) $("hl-pop").hidden = true; });

// ================= Highlights / Saved comments collections in the list pane
function renderCollection(keepScroll) {
  $("list-tools").hidden = true;
  $("bulkbar").hidden = true;
  const isHl = S.view === "highlights";
  const q = S.query.toLowerCase();
  const rows = [];
  for (const n of S.notes) {
    if (S.sub && n.subreddit !== S.sub) continue;
    for (const x of (isHl ? n.state.highlights : n.state.saved_comments) || []) {
      const hay = (x.text + " " + (x.note || "") + " " + n.title).toLowerCase();
      if (q && !q.split(/\s+/).every(t => hay.includes(t))) continue;
      rows.push({ n, x, t: x.created || x.saved_at || 0 });
    }
  }
  rows.sort((a, b) => b.t - a.t);
  const el = $("list"), top = el.scrollTop;
  el.innerHTML = rows.map(({ n, x }) => isHl
    ? `<li class="item ref" data-id="${esc(n.id)}" data-ref="hl:${esc(x.id)}">
        <div class="quote">${esc(x.text)}</div>${x.note ? `<div class="ref-note">${esc(x.note)}</div>` : ""}
        <div class="m"><span class="sub">${subLabel(n.subreddit)}</span><span>${x.where && x.where !== "post" ? "u/" + esc(x.where) : "post"}</span><span>${ago(x.created)}</span></div>
        <div class="ref-title">${esc(n.title)}</div></li>`
    : `<li class="item ref" data-id="${esc(n.id)}" data-ref="c:${esc(x.key)}">
        <div class="m"><b>u/${esc(x.author)}</b><span>${num(x.score)} pts</span><span>saved ${ago(x.saved_at)}</span></div>
        <div class="quote plain">${esc(x.text)}</div>
        <div class="ref-title">${esc(n.title)} · ${subLabel(n.subreddit)}</div></li>`).join("");
  if (keepScroll) el.scrollTop = top;
  S.visible = [];
  $("count").textContent = `${rows.length} ${isHl ? (rows.length === 1 ? "highlight" : "highlights") : (rows.length === 1 ? "saved comment" : "saved comments")}`;
  const empty = $("list-empty");
  empty.hidden = !!rows.length;
  if (!rows.length) empty.textContent = q ? `Nothing matches "${S.query}".` : isHl
    ? "Select text while reading and press h (or the Highlight button) to keep the best lines here."
    : "Hover a comment and press b, or click its bookmark, to save it here.";
}
async function openRef(id, ref) {
  await openNote(id);
  setTimeout(() => {
    const [kind, key] = [ref.slice(0, ref.indexOf(":")), ref.slice(ref.indexOf(":") + 1)];
    const target = kind === "hl"
      ? document.querySelector(`mark.hl[data-hid="${CSS.escape(key)}"]`)
      : document.querySelector(`[data-ckey="${CSS.escape(key)}"]`);
    if (!target) return showToast(kind === "hl" ? "That highlight no longer matches this capture" : "That comment is not in the latest capture");
    const card = target.closest(".comment");
    if (card && card._replies && target.closest(".replies")) toggleReplies(card, true);
    if (card) card.hidden = false;
    const sc = $("scroller");  // scroll only the reading pane, never the page
    sc.scrollTop += target.getBoundingClientRect().top - sc.getBoundingClientRect().top - sc.clientHeight / 3;
    target.classList.add("flash");
    setTimeout(() => target.classList.remove("flash"), 1600);
  }, 80);
}

// ================= stats
function dayKey(d) { return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); }
function weekStart(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); return x; }
function computeStats() {
  const notes = S.notes;
  const read = notes.filter(n => n.state.read_at);
  const now = new Date();
  const wk = weekStart(now), mo = new Date(now.getFullYear(), now.getMonth(), 1);
  const at = n => new Date(n.state.read_at * 1000);
  const days = new Set(read.map(n => dayKey(at(n))));
  let cur = 0;
  const d = new Date(now);
  if (!days.has(dayKey(d))) d.setDate(d.getDate() - 1);
  while (days.has(dayKey(d))) { cur++; d.setDate(d.getDate() - 1); }
  let best = 0, run = 0, prev = null;
  for (const k of [...days].sort()) {
    const t = new Date(k + "T12:00:00");
    run = prev && Math.round((t - prev) / 864e5) === 1 ? run + 1 : 1;
    best = Math.max(best, run); prev = t;
  }
  const weeks = [];
  for (let i = 7; i >= 0; i--) {
    const s = weekStart(now); s.setDate(s.getDate() - i * 7);
    const e = new Date(s); e.setDate(e.getDate() + 7);
    weeks.push({ s, read: read.filter(n => at(n) >= s && at(n) < e).length,
      saved: notes.filter(n => { const c = parseDate(n.captured); return c && c >= s && c < e; }).length });
  }
  const subs = new Map();
  notes.forEach(n => { const v = subs.get(n.subreddit) || { total: 0, read: 0 }; v.total++; if (n.state.read_at) v.read++; subs.set(n.subreddit, v); });
  return {
    total: notes.length, read: read.length,
    week: read.filter(n => at(n) >= wk).length, month: read.filter(n => at(n) >= mo).length,
    words: read.reduce((a, n) => a + n.words, 0), cur, best,
    favs: notes.filter(n => n.state.starred).length,
    hls: notes.reduce((a, n) => a + (n.state.highlights || []).length, 0),
    saved: notes.reduce((a, n) => a + (n.state.saved_comments || []).length, 0),
    noted: notes.filter(n => n.state.my_note && n.state.my_note.trim()).length,
    weeks, subs: [...subs].sort((a, b) => b[1].total - a[1].total).slice(0, 10),
  };
}
function showStats() {
  saveNoteNow();
  const st = computeStats();
  const pct = st.total ? Math.round(st.read / st.total * 100) : 0;
  const maxW = Math.max(1, ...st.weeks.map(w => Math.max(w.read, w.saved)));
  const card = (v, l, sub) => `<div class="stat"><div class="v">${v}</div><div class="l">${l}</div>${sub ? `<div class="s">${sub}</div>` : ""}</div>`;
  $("stats").innerHTML = `
    <h1>Reading stats</h1>
    <p class="lede">${st.read} of ${st.total} threads read (${pct}%). ${st.total - st.read} still waiting.</p>
    <div class="stat-grid">
      ${card(st.week, "read this week")}${card(st.month, "read this month")}${card(st.read, "read in all")}
      ${card(st.cur + (st.cur === 1 ? " day" : " days"), "current streak", `best ${st.best} ${st.best === 1 ? "day" : "days"}`)}
      ${card(num(st.words), "words read", `about ${Math.round(st.words / 230 / 60 * 10) / 10} hours`)}
      ${card(st.favs, "favorites")}${card(st.hls, "highlights")}${card(st.saved, "saved comments")}${card(st.noted, "with your notes")}
    </div>
    <h2>Last 8 weeks</h2>
    <div class="legend"><span class="k read"></span>read <span class="k saved"></span>saved</div>
    <div class="weeks">${st.weeks.map(w => `<div class="wk" title="${w.read} read, ${w.saved} saved"><div class="bars">
        <i class="read" style="height:${w.read / maxW * 100}%"></i><i class="saved" style="height:${w.saved / maxW * 100}%"></i></div>
        <span>${w.s.toLocaleDateString(undefined, { day: "numeric", month: "short" })}</span></div>`).join("")}</div>
    <h2>By subreddit</h2>
    <div class="subbars">${st.subs.map(([s, v]) => `<div class="sb"><span class="nm">${subLabel(s)}</span>
        <span class="track"><i style="width:${v.read / v.total * 100}%"></i></span><span class="ct">${v.read}/${v.total}</span></div>`).join("")}</div>
    <button class="pill-btn" id="btn-surprise3">Surprise me with an unread note</button>`;
  $("btn-surprise3").onclick = surprise;
  $("welcome").hidden = true;
  $("article").hidden = true;
  $("toolbar").hidden = true;
  $("drawer").hidden = true;
  $("stats").hidden = false;
  $("scroller").scrollTop = 0;
  $("progress-bar").style.width = "0";
  $("rail-stats").classList.add("on");
  S.current = null;
  document.querySelectorAll("#list .item.on").forEach(li => li.classList.remove("on"));
}
