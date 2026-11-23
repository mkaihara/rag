"use strict";

const $ = (id) => document.getElementById(id);
const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// ---------------------------------------------------------------------------
// Markdown + LaTeX rendering. Math is cut out before Markdown parsing (so
// underscores and asterisks inside formulas survive) and the Markdown HTML is
// sanitized. Only then are citations linked and KaTeX output put back, and
// only inside text nodes: splicing into the HTML string could land markup
// inside an attribute, after the sanitizer has already looked at it.

const MATH = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|(?<![\\$\w])\$(?!\s)((?:\\\$|[^$\n])+?)(?<!\s)\$(?![\w$])/g;
const TOKENS = /@@MATH(\d+)@@|\[(\d{1,2})\]/g;

function mathNode({ tex, display }) {
  const t = document.createElement("template");
  try {
    t.innerHTML = katex.renderToString(tex, { displayMode: display, throwOnError: false, strict: "ignore", trust: false });
  } catch {
    const code = document.createElement("code");
    code.textContent = tex;
    t.content.append(code);
  }
  return t.content;
}

function citeNode(n) {
  const a = document.createElement("a");
  a.className = "cite";
  a.href = `#src-${n}`;
  a.dataset.n = n;
  a.textContent = n;
  return a;
}

function render(text, { cite = false } = {}) {
  const math = [];
  const stripped = text.replace(MATH, (m, dd, sq, rd, inl) => {
    const display = dd !== undefined || sq !== undefined;
    math.push({ tex: dd ?? sq ?? rd ?? inl, display });
    return `@@MATH${math.length - 1}@@`;
  });
  const t = document.createElement("template");
  t.innerHTML = DOMPurify.sanitize(marked.parse(stripped, { breaks: false, gfm: true }));
  const walker = document.createTreeWalker(t.content, NodeFilter.SHOW_TEXT);
  const texts = [];
  while (walker.nextNode()) texts.push(walker.currentNode);
  for (const node of texts) {
    const s = node.data;
    const parts = [];
    let last = 0;
    for (const m of s.matchAll(TOKENS)) {
      const [whole, i, n] = m;
      const replacement = i !== undefined ? math[+i] && mathNode(math[+i]) : cite && citeNode(n);
      if (!replacement) continue; // unknown placeholder, or [n] where citations are off: keep the text
      parts.push(s.slice(last, m.index), replacement);
      last = m.index + whole.length;
    }
    if (!parts.length) continue;
    parts.push(s.slice(last));
    node.replaceWith(...parts);
  }
  return t.innerHTML;
}

// ---------------------------------------------------------------------------
// Index info

async function loadInfo() {
  try {
    const r = await fetch("/api/info");
    const info = await r.json();
    const idx = info.index;
    const row = (label, value) => `<dt>${label}</dt><dd>${value}</dd>`;
    $("meta").innerHTML = idx
      ? "<dl>" +
        row("Profile", `<code>${escapeHtml(info.profile)}</code>`) +
        row("LLM", `<code>${escapeHtml(info.llm)}</code>`) +
        row("Embeddings", `<code>${escapeHtml(info.embedding)}</code>`) +
        row("Index", `${idx.chunks} chunks from ${info.documents.length} documents`) +
        "</dl>"
      : "The index has not been built yet — run the indexer.";
    $("documents").innerHTML = info.documents
      .map((d) => `<li>${escapeHtml(d.title)} <span class="authors">— ${escapeHtml(d.authors.join(", "))} · ${d.chunks} chunks</span></li>`)
      .join("");
  } catch {
    $("meta").textContent = "Could not reach the server.";
  }
}

// ---------------------------------------------------------------------------
// Asking

function renderSources(sources) {
  $("source-count").textContent = sources.length ? `· ${sources.length}` : "";
  $("sources").innerHTML = sources
    .map((s) => {
      const pages = s.pages ? ` · p. ${s.pages[0]}${s.pages[1] !== s.pages[0] ? "–" + s.pages[1] : ""}` : "";
      const sim = s.similarity != null ? ` · similarity ${s.similarity.toFixed(2)}` : " · keyword match";
      return `<li id="src-${s.n}">
        <div class="src-head"><span class="src-n">${s.n}</span><span class="src-title">${escapeHtml(s.title)}</span></div>
        <div class="src-meta">${escapeHtml(s.authors.join(", "))}<br>${escapeHtml(s.section)}${pages}${sim}</div>
        <details><summary>Show passage</summary><div class="prose">${render(s.text)}</div></details>
      </li>`;
    })
    .join("");
}

// On desktop widths, shrink a display formula that is slightly too wide for
// the answer column (down to 75%) so it shows whole. Narrower screens keep
// full-size math that scrolls sideways inside its own box.
const WIDE = matchMedia("(min-width: 861px)");
function fitFormulas() {
  for (const d of $("answer").querySelectorAll(".katex-display")) {
    d.style.fontSize = "";
    if (!WIDE.matches) continue;
    const ratio = d.clientWidth / d.scrollWidth;
    if (ratio < 1) d.style.fontSize = `${Math.max(0.75, ratio * 0.98)}em`;
  }
}
window.addEventListener("resize", fitFormulas);
// KaTeX fonts load when first used, which changes the width of the math.
document.fonts?.addEventListener("loadingdone", fitFormulas);

async function ask(question) {
  const answerEl = $("answer");
  $("result").hidden = false;
  $("sources").innerHTML = "";
  $("source-count").textContent = "";
  $("timings").textContent = "";
  answerEl.innerHTML = '<p class="thinking">Searching the papers…</p>';
  answerEl.classList.remove("cursor");
  $("submit").disabled = true;

  let answer = "";
  let pending = false;
  let truncated = false;
  const paint = () => {
    pending = false;
    answerEl.innerHTML = render(answer, { cite: true });
    fitFormulas();
  };

  try {
    const r = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${r.status}`);
    }
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        const ev = JSON.parse(line);
        if (ev.type === "sources") {
          renderSources(ev.sources);
          answerEl.innerHTML = '<p class="thinking">Reading the sources and writing an answer…</p>';
          answerEl.classList.add("cursor");
        } else if (ev.type === "token") {
          answer += ev.text;
          if (!pending) { pending = true; requestAnimationFrame(paint); }
        } else if (ev.type === "done") {
          truncated = Boolean(ev.truncated);
          const t = ev.timings;
          const s = (ms) => (ms / 1000).toFixed(1) + " s";
          $("timings").textContent =
            `Retrieval ${s(t.retrieval_ms)} · first token ${t.first_token_ms ? s(t.first_token_ms) : "—"} · total ${s(t.total_ms)}`;
        } else if (ev.type === "error") {
          throw new Error(ev.message);
        }
      }
    }
    paint();
    if (truncated) {
      answerEl.insertAdjacentHTML("beforeend", '<p class="note">The answer was cut off at the length limit.</p>');
    }
  } catch (e) {
    answerEl.insertAdjacentHTML("beforeend", `<p class="error">Error: ${escapeHtml(e.message)}</p>`);
  } finally {
    answerEl.classList.remove("cursor");
    $("submit").disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Wiring

// Grow the question box to fit its text, or its placeholder when empty: at
// phone widths the placeholder wraps to three lines and two rows cut it off.
function fitQuestion() {
  const t = $("question");
  const empty = !t.value;
  if (empty) t.value = t.placeholder;
  t.style.height = "auto";
  const borders = t.offsetHeight - t.clientHeight;
  t.style.height = `${t.scrollHeight + borders}px`;
  if (empty) t.value = "";
}
$("question").addEventListener("input", fitQuestion);
window.addEventListener("resize", fitQuestion);
document.fonts?.ready.then(fitQuestion);
fitQuestion();

$("ask").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("question").value.trim();
  if (q) ask(q);
});
$("question").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("ask").requestSubmit();
  }
});
$("examples").addEventListener("click", (e) => {
  if (e.target.tagName !== "BUTTON") return;
  $("question").value = e.target.textContent;
  fitQuestion();
  $("ask").requestSubmit();
});
$("answer").addEventListener("click", (e) => {
  const a = e.target.closest("a.cite");
  if (!a) return;
  const li = document.getElementById(`src-${a.dataset.n}`);
  if (!li) return;
  e.preventDefault();
  li.scrollIntoView({ behavior: "smooth", block: "nearest" });
  li.classList.add("flash");
  setTimeout(() => li.classList.remove("flash"), 1200);
});

loadInfo();
