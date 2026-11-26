"use strict";

function $(id) {
  return document.getElementById(id);
}

const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, function (c) {
    return HTML_ESCAPES[c];
  });
}

// ---------------------------------------------------------------------------
// Markdown + LaTeX rendering. Math is cut out before Markdown parsing (so
// underscores and asterisks inside formulas survive) and the Markdown HTML is
// sanitized. Only then are citations linked and KaTeX output put back, and
// only inside text nodes: splicing into the HTML string could land markup
// inside an attribute, after the sanitizer has already looked at it.

// The four ways to write math, one capture group each:
//   1. $$...$$      display
//   2. \[...\]      display
//   3. \(...\)      inline
//   4. $...$        inline. Not after a backslash, "$" or letter/digit; no
//                   space just inside either "$"; not followed by a
//                   letter/digit or "$" (so prices like "$5 and $6" stay text).
const MATH = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|(?<![\\$\w])\$(?!\s)((?:\\\$|[^$\n])+?)(?<!\s)\$(?![\w$])/g;

// Placeholders left in the text: @@MATH<i>@@ for formula i, and [n] citations.
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
  // 1. Replace every formula with a placeholder, remembering the formula.
  const math = [];
  function cutOutMath(whole, dollarDisplay, bracketDisplay, parenInline, dollarInline) {
    let tex;
    let display;
    if (dollarDisplay !== undefined) {
      tex = dollarDisplay;
      display = true;
    } else if (bracketDisplay !== undefined) {
      tex = bracketDisplay;
      display = true;
    } else if (parenInline !== undefined) {
      tex = parenInline;
      display = false;
    } else {
      tex = dollarInline;
      display = false;
    }
    math.push({ tex: tex, display: display });
    const index = math.length - 1;
    return `@@MATH${index}@@`;
  }
  const stripped = text.replace(MATH, cutOutMath);

  // 2. Markdown to HTML, then sanitize it.
  const html = marked.parse(stripped, { breaks: false, gfm: true });
  const t = document.createElement("template");
  t.innerHTML = DOMPurify.sanitize(html);

  // 3. In every text node, swap the placeholders for formulas and citation links.
  const walker = document.createTreeWalker(t.content, NodeFilter.SHOW_TEXT);
  const texts = [];
  while (walker.nextNode()) {
    texts.push(walker.currentNode);
  }
  for (const node of texts) {
    const s = node.data;
    const parts = [];
    let last = 0;
    for (const m of s.matchAll(TOKENS)) {
      const whole = m[0];
      const mathIndex = m[1];
      const citeNumber = m[2];

      let replacement = null;
      if (mathIndex !== undefined) {
        const formula = math[Number(mathIndex)];
        if (formula) {
          replacement = mathNode(formula);
        }
      } else if (cite) {
        replacement = citeNode(citeNumber);
      }
      if (!replacement) {
        continue; // unknown placeholder, or [n] where citations are off: keep the text
      }

      parts.push(s.slice(last, m.index));
      parts.push(replacement);
      last = m.index + whole.length;
    }
    if (parts.length === 0) {
      continue;
    }
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

    function row(label, value) {
      return `<dt>${label}</dt><dd>${value}</dd>`;
    }

    if (idx) {
      $("meta").innerHTML =
        "<dl>" +
        row("Profile", `<code>${escapeHtml(info.profile)}</code>`) +
        row("LLM", `<code>${escapeHtml(info.llm)}</code>`) +
        row("Embeddings", `<code>${escapeHtml(info.embedding)}</code>`) +
        row("Index", `${idx.chunks} chunks from ${info.documents.length} documents`) +
        "</dl>";
    } else {
      $("meta").innerHTML = "The index has not been built yet — run the indexer.";
    }

    let items = "";
    for (const d of info.documents) {
      const authors = escapeHtml(d.authors.join(", "));
      items += `<li>${escapeHtml(d.title)} <span class="authors">— ${authors} · ${d.chunks} chunks</span></li>`;
    }
    $("documents").innerHTML = items;
  } catch {
    $("meta").textContent = "Could not reach the server.";
  }
}

// ---------------------------------------------------------------------------
// Asking

function renderSources(sources) {
  if (sources.length) {
    $("source-count").textContent = `· ${sources.length}`;
  } else {
    $("source-count").textContent = "";
  }

  let items = "";
  for (const s of sources) {
    // " · p. 12" or " · p. 12–13"
    let pages = "";
    if (s.pages) {
      const first = s.pages[0];
      const last = s.pages[1];
      if (last !== first) {
        pages = ` · p. ${first}–${last}`;
      } else {
        pages = ` · p. ${first}`;
      }
    }

    // Sources found only by the keyword search have no similarity.
    let sim;
    if (s.similarity !== null && s.similarity !== undefined) {
      sim = ` · similarity ${s.similarity.toFixed(2)}`;
    } else {
      sim = " · keyword match";
    }

    items += `<li id="src-${s.n}">
        <div class="src-head"><span class="src-n">${s.n}</span><span class="src-title">${escapeHtml(s.title)}</span></div>
        <div class="src-meta">${escapeHtml(s.authors.join(", "))}<br>${escapeHtml(s.section)}${pages}${sim}</div>
        <details><summary>Show passage</summary><div class="prose">${render(s.text)}</div></details>
      </li>`;
  }
  $("sources").innerHTML = items;
}

// On desktop widths, shrink a display formula that is slightly too wide for
// the answer column (down to 75%) so it shows whole. Narrower screens keep
// full-size math that scrolls sideways inside its own box.
const WIDE = matchMedia("(min-width: 861px)");
function fitFormulas() {
  for (const d of $("answer").querySelectorAll(".katex-display")) {
    d.style.fontSize = "";
    if (!WIDE.matches) {
      continue;
    }
    const ratio = d.clientWidth / d.scrollWidth;
    if (ratio < 1) {
      const size = Math.max(0.75, ratio * 0.98);
      d.style.fontSize = `${size}em`;
    }
  }
}
window.addEventListener("resize", fitFormulas);
// KaTeX fonts load when first used, which changes the width of the math.
if (document.fonts) {
  document.fonts.addEventListener("loadingdone", fitFormulas);
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
  let pending = false; // a repaint is already scheduled for the next frame
  let truncated = false;
  function paint() {
    pending = false;
    answerEl.innerHTML = render(answer, { cite: true });
    fitFormulas();
  }

  try {
    const r = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    if (!r.ok) {
      let body = {};
      try {
        body = await r.json();
      } catch {
        body = {};
      }
      throw new Error(body.detail || `HTTP ${r.status}`);
    }

    // The answer arrives as newline-delimited JSON events.
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) {
        break;
      }
      buf += decoder.decode(chunk.value, { stream: true });

      // Handle every complete line; keep a partial last line in buf.
      let nl = buf.indexOf("\n");
      while (nl >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        nl = buf.indexOf("\n");
        if (!line) {
          continue;
        }
        const ev = JSON.parse(line);
        if (ev.type === "sources") {
          renderSources(ev.sources);
          answerEl.innerHTML = '<p class="thinking">Reading the sources and writing an answer…</p>';
          answerEl.classList.add("cursor");
        } else if (ev.type === "token") {
          answer += ev.text;
          // Repaint at most once per frame, however fast the tokens come.
          if (!pending) {
            pending = true;
            requestAnimationFrame(paint);
          }
        } else if (ev.type === "done") {
          truncated = Boolean(ev.truncated);
          const t = ev.timings;
          function seconds(ms) {
            return (ms / 1000).toFixed(1) + " s";
          }
          let firstToken = "—";
          if (t.first_token_ms) {
            firstToken = seconds(t.first_token_ms);
          }
          $("timings").textContent =
            `Retrieval ${seconds(t.retrieval_ms)} · first token ${firstToken} · total ${seconds(t.total_ms)}`;
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
  if (empty) {
    t.value = t.placeholder; // measure the placeholder's height
  }
  t.style.height = "auto";
  const borders = t.offsetHeight - t.clientHeight;
  t.style.height = `${t.scrollHeight + borders}px`;
  if (empty) {
    t.value = "";
  }
}
$("question").addEventListener("input", fitQuestion);
window.addEventListener("resize", fitQuestion);
if (document.fonts) {
  document.fonts.ready.then(fitQuestion);
}
fitQuestion();

$("ask").addEventListener("submit", function (e) {
  e.preventDefault();
  const q = $("question").value.trim();
  if (q) {
    ask(q);
  }
});
$("question").addEventListener("keydown", function (e) {
  // Enter asks; Shift+Enter starts a new line.
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("ask").requestSubmit();
  }
});
$("examples").addEventListener("click", function (e) {
  if (e.target.tagName !== "BUTTON") {
    return;
  }
  $("question").value = e.target.textContent;
  fitQuestion();
  $("ask").requestSubmit();
});
$("answer").addEventListener("click", function (e) {
  // A click on a [n] citation scrolls to source n and flashes it.
  const a = e.target.closest("a.cite");
  if (!a) {
    return;
  }
  const li = document.getElementById(`src-${a.dataset.n}`);
  if (!li) {
    return;
  }
  e.preventDefault();
  li.scrollIntoView({ behavior: "smooth", block: "nearest" });
  li.classList.add("flash");
  setTimeout(function () {
    li.classList.remove("flash");
  }, 1200);
});

loadInfo();
