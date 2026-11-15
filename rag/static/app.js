"use strict";

const $ = (id) => document.getElementById(id);
const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// ---------------------------------------------------------------------------
// Markdown + LaTeX rendering. Math is cut out before Markdown parsing (so
// underscores and asterisks inside formulas survive), the Markdown HTML is
// sanitized, then citations are linked and KaTeX output is put back.

const MATH = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|(?<![\\$\w])\$(?!\s)((?:\\\$|[^$\n])+?)(?<!\s)\$(?![\w$])/g;

function render(text, { cite = false } = {}) {
  const math = [];
  const stripped = text.replace(MATH, (m, dd, sq, rd, inl) => {
    const display = dd !== undefined || sq !== undefined;
    math.push({ tex: dd ?? sq ?? rd ?? inl, display });
    return `@@MATH${math.length - 1}@@`;
  });
  let html = DOMPurify.sanitize(marked.parse(stripped, { breaks: false, gfm: true }));
  if (cite) {
    html = html.replace(/\[(\d{1,2})\]/g, (m, n) => `<a class="cite" href="#src-${n}" data-n="${n}">${n}</a>`);
  }
  return html.replace(/@@MATH(\d+)@@/g, (m, i) => {
    const { tex, display } = math[+i];
    try {
      return katex.renderToString(tex, { displayMode: display, throwOnError: false, strict: "ignore", trust: false });
    } catch {
      return `<code>${escapeHtml(tex)}</code>`;
    }
  });
}

// ---------------------------------------------------------------------------
// Index info

async function loadInfo() {
  try {
    const r = await fetch("/api/info");
    const info = await r.json();
    const idx = info.index;
    $("meta").innerHTML = idx
      ? `Profile <code>${escapeHtml(info.profile)}</code> · LLM <code>${escapeHtml(info.llm)}</code> · ` +
        `embeddings <code>${escapeHtml(info.embedding)}</code> · ${idx.chunks} chunks from ${info.documents.length} documents`
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
  const paint = () => {
    pending = false;
    answerEl.innerHTML = render(answer, { cite: true });
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
  } catch (e) {
    answerEl.insertAdjacentHTML("beforeend", `<p class="error">Error: ${escapeHtml(e.message)}</p>`);
  } finally {
    answerEl.classList.remove("cursor");
    $("submit").disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Wiring

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
