"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const state = { jobs: [], selected: null, job: null, clipsKey: "", busyClips: new Set() };

const STEP_LABELS = { download: "Baixar", transcribe: "Transcrever", analyze: "Escolher momentos", render: "Renderizar" };
const STEP_ORDER = Object.keys(STEP_LABELS);
const LAYOUTS = { split: "webcam + jogo", face: "seguindo o rosto", blur: "fundo desfocado", vertical: "vertical original" };

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtTime(t) {
  t = Math.max(0, Math.round(t));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(s).padStart(2, "0")}`;
}
function toast(msg) {
  const el = document.createElement("div");
  el.className = "toast"; el.textContent = msg; el.setAttribute("role", "status");
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2600);
}
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) {
    let msg = `Erro ${res.status}`;
    try { const j = await res.json(); msg = typeof j.detail === "string" ? j.detail : (j.detail?.[0]?.msg || msg); } catch {}
    throw new Error(msg);
  }
  return res.json();
}

/* ---------- formulário ---------- */
const form = $("#newJob");
function readSettings() {
  const f = new FormData(form);
  return {
    // duração fixa: o corte fica perto do tempo escolhido (±3 s) para terminar no fim de uma fala
    num_clips: +f.get("num_clips"), min_len: Math.max(5, +f.get("clip_seconds") - 3), max_len: +f.get("clip_seconds") + 3,
    live_minutes: +f.get("live_minutes"), block_minutes: +f.get("block_minutes"),
    layout: f.get("layout"), language: f.get("language"),
    title_overlay: form.title_overlay.checked,
    cold_open: form.cold_open.checked,
    loop_end: form.loop_end.checked,
    section_start: f.get("section_start") === "" ? null : +f.get("section_start"),
    section_end: f.get("section_end") === "" ? null : +f.get("section_end"),
  };
}
form.addEventListener("change", () => {
  const live = form.mode.value === "live";
  form.classList.toggle("is-live", live);
  $("#numLabel").textContent = live ? "Cortes por bloco" : "Cortes";
});
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("button[type=submit]", form);
  $("#formError").textContent = "";
  btn.disabled = true;
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ url: form.url.value, mode: form.mode.value, settings: readSettings() }),
    });
    form.url.value = "";
    await refreshList();
    select(job.id);
    if (isNarrow()) { $("#newJobBox").open = false; $("#detail").scrollIntoView({ behavior: "smooth", block: "start" }); }
  } catch (err) {
    $("#formError").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

/* ---------- lista ---------- */
const isRecording = (j) => j.mode === "live" && j.status === "running";
function statusLabel(j) {
  if (j.mode === "live") {
    if (j.status === "running") return "Gravando";
    if (j.status === "done") return `${(j.parts || []).length} partes`;
  }
  if (j.status === "done") return `${j.clips.length} cortes`;
  if (j.status === "error") return "Falhou";
  if (j.status === "canceled") return "Cancelado";
  if (j.status === "queued") return "Na fila";
  if (j.step === "download" && j.mode === "live") return "Gravando";
  return STEP_LABELS[j.step] + "…";
}
function renderList() {
  const ul = $("#jobList");
  const visible = state.jobs.filter((j) => j.mode !== "parte");
  if (!visible.length) {
    ul.innerHTML = `<li class="empty-note">Os vídeos que você enviar aparecem aqui.</li>`;
    return;
  }
  const current = state.jobs.find((j) => j.id === state.selected);
  const activeId = current?.mode === "parte" ? current.parent : state.selected;
  ul.innerHTML = visible.map((j) => {
    const live = isRecording(j);
    return `<li><button type="button" data-id="${j.id}" aria-current="${j.id === activeId}">
      <span class="t">${esc(j.title || j.url)}</span>
      <span class="badge ${live ? "live" : ""}">${esc(statusLabel(j))}</span>
      <span class="s">${new Date(j.created_at * 1000).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" })}${j.channel ? " — " + esc(j.channel) : ""}</span>
    </button></li>`;
  }).join("");
  $("#recDot").classList.toggle("on", state.jobs.some(isRecording));
}
$("#jobList").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-id]");
  if (!b) return;
  select(b.dataset.id);
  if (isNarrow()) setTimeout(() => $("#detail").scrollIntoView({ behavior: "smooth", block: "start" }), 150);
});
$("#cleanup").addEventListener("click", async () => {
  try {
    const plan = await api("/api/jobs/cleanup?dry_run=true", { method: "POST" });
    const kept = [plan.kept_active && `${plan.kept_active} gravando/processando`, plan.kept_posts && `${plan.kept_posts} com posts na Agenda`].filter(Boolean).join(" e ");
    if (!plan.videos) { toast(kept ? `Nada para limpar (ficam ${kept})` : "Nada para limpar"); return; }
    const gb = (plan.bytes / 1024 ** 3).toFixed(plan.bytes > 1024 ** 3 ? 1 : 2);
    if (!confirm(`Apagar ${plan.videos} vídeo(s) concluído(s) e os cortes deles, liberando ${gb} GB?${kept ? `

Ficam na lista: ${kept}.` : ""}

Isso não pode ser desfeito.`)) return;
    const r = await api("/api/jobs/cleanup?dry_run=false", { method: "POST" });
    toast(`${r.videos} vídeo(s) apagado(s)`);
    if (state.selected && !(await api("/api/jobs")).some((j) => j.id === state.selected)) select(null);
    await refreshList();
  } catch (err) { toast(err.message); }
});

async function refreshList() {
  state.jobs = await api("/api/jobs");
  renderList();
}

/* ---------- detalhe ---------- */
function select(id) {
  state.selected = id; state.job = null; state.clipsKey = "";
  history.replaceState(null, "", id ? `#${id}` : "#");
  renderList();
  refreshDetail();
}
async function refreshDetail() {
  const detail = $("#detail");
  if (!state.selected) {
    detail.replaceChildren($("#emptyTpl").content.cloneNode(true));
    return;
  }
  let job;
  try { job = await api(`/api/jobs/${state.selected}`); }
  catch { state.selected = null; return refreshDetail(); }
  const first = !state.job || state.job.id !== job.id;
  state.job = job;
  if (first) detail.innerHTML = skeleton(job);
  updateHead(job);
  if (job.mode === "live") { updateLive(job); return; }
  updateProgress(job);
  updateTimeline(job);
  updateClips(job);
}

function skeleton(job) {
  if (job.mode === "live") {
    return `
    <div class="job-head">
      <div><h1 id="jTitle"></h1><p class="meta" id="jMeta"></p></div>
      <div class="actions" id="jActions"></div>
    </div>
    <p class="status-msg" id="jMsg"></p>
    <ul class="parts" id="jParts"></ul>`;
  }
  return `
    ${job.mode === "parte" ? `<button class="back" type="button" data-goto="${job.parent}">Voltar para a live</button>` : ""}
    <div class="job-head">
      <div><h1 id="jTitle"></h1><p class="meta" id="jMeta"></p></div>
      <div class="actions" id="jActions"></div>
    </div>
    <ol class="steps" id="jSteps">${STEP_ORDER.map((s) => `<li data-step="${s}"><div class="bar"><i></i></div>${STEP_LABELS[s]}</li>`).join("")}</ol>
    <p class="status-msg" id="jMsg"></p>
    <div id="jNotice"></div>
    <div class="timeline" id="jTimeline" hidden></div>
    <div class="clips" id="jClips"></div>`;
}

function updateHead(job) {
  $("#jTitle").textContent = job.title || "Lendo o link…";
  $("#jMeta").innerHTML = [
    job.channel && esc(job.channel),
    job.mode === "parte" && job.range ? `Trecho da live de ${fmtTime(job.range[0])} a ${fmtTime(job.range[1])}` : "",
    job.mode === "video" && job.range ? `Trecho de ${fmtTime(job.range[0])} a ${fmtTime(job.range[1])}` : "",
    job.duration && job.mode !== "live" ? `Duração ${fmtTime(job.duration)}` : "",
    `<a href="${esc(job.url)}" target="_blank" rel="noopener">Abrir original</a>`,
  ].filter(Boolean).join(" — ");
  const running = job.status === "running" || job.status === "queued";
  const isLive = job.mode === "live";
  const hasClips = isLive ? (job.parts_info || []).some((p) => p.clips) : job.clips.length > 0;
  $("#jActions").innerHTML = `
    ${isLive && running ? `<button class="btn stop" data-act="stop">Parar gravação</button>` : ""}
    ${!isLive && running ? `<button class="btn" data-act="cancel">Cancelar</button>` : ""}
    ${!isLive && (job.status === "error" || job.status === "canceled") ? `<button class="btn" data-act="retry">Tentar de novo</button>` : ""}
    ${hasClips ? `<a class="btn" href="/api/jobs/${job.id}/zip">Baixar todos (.zip)</a>` : ""}
    ${!isLive && !running && job.duration && job.status !== "canceled" ? `<button class="btn" data-act="reanalyze" title="Usa as configurações do formulário ao lado">Gerar novos cortes</button>` : ""}
    ${!running ? `<button class="btn danger" data-act="delete">Excluir</button>` : ""}`;
}

function updateProgress(job) {
  const idx = STEP_ORDER.indexOf(job.step);
  document.querySelectorAll("#jSteps li").forEach((li, i) => {
    const bar = $(".bar i", li);
    li.classList.remove("done", "active");
    if (job.status === "done" || i < idx) { li.classList.add("done"); bar.style.width = ""; }
    else if (i === idx && job.status !== "error") { li.classList.add("active"); bar.style.width = `${Math.round(job.progress * 100)}%`; }
    else bar.style.width = "0";
  });
  const msg = $("#jMsg");
  msg.textContent = job.status === "error" ? `Não deu certo: ${job.message}` : job.message;
  if (job.used_ai === false && job.ai_error) {
    $("#jNotice").innerHTML = `<p class="notice">Estes cortes foram escolhidos só pelo volume e pelo chat, porque a IA não respondeu: ${esc(job.ai_error)}. Depois de resolver, clique em “Gerar novos cortes”.</p>`;
    return;
  }
  msg.classList.toggle("error", job.status === "error");
  $("#jNotice").innerHTML = "";
}

function updateTimeline(job) {
  const box = $("#jTimeline");
  if (!job.timeline?.length || !job.duration) { box.hidden = true; return; }
  box.hidden = false;
  const pts = job.timeline, n = pts.length;
  let top = "", bottom = "";
  pts.forEach((v, i) => {
    const x = (i / (n - 1)) * 1000, h = 4 + v * 44;
    top += `${i ? "L" : "M"}${x.toFixed(1)},${(50 - h).toFixed(1)}`;
    bottom = `L${x.toFixed(1)},${(50 + h).toFixed(1)}` + bottom;
  });
  const bands = job.clips.map((c, i) => {
    const x = (c.start / job.duration) * 1000, w = Math.max(((c.end - c.start) / job.duration) * 1000, 3);
    return `<rect class="band" data-clip="${i}" x="${x}" y="0" width="${w}" height="100"><title>Corte ${i + 1}: ${fmtTime(c.start)}–${fmtTime(c.end)}</title></rect>`;
  }).join("");
  box.innerHTML = `
    <p class="timeline-cap">Empolgação ao longo do vídeo (volume e chat). As faixas amarelas são os cortes.</p>
    <svg viewBox="0 0 1000 100" preserveAspectRatio="none" role="img" aria-label="Gráfico de empolgação com ${job.clips.length} cortes marcados">
      ${bands}<path class="wave" d="${top}${bottom}Z"/>
    </svg>
    <div class="axis"><span>0:00</span><span>${fmtTime(job.duration / 2)}</span><span>${fmtTime(job.duration)}</span></div>`;
}
$("#detail").addEventListener("click", (e) => {
  const band = e.target.closest(".band");
  if (band) {
    const card = document.getElementById(`clip-${band.dataset.clip}`);
    card?.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
    card?.classList.add("flash"); setTimeout(() => card?.classList.remove("flash"), 1200);
  }
});

/* ---------- sessão de live ---------- */
function updateLive(job) {
  const msg = $("#jMsg");
  msg.textContent = job.status === "error" ? `Não deu certo: ${job.message}` : job.message;
  msg.classList.toggle("error", job.status === "error");
  const parts = job.parts_info || [];
  const key = JSON.stringify(parts);
  if (key === state.clipsKey) return;
  state.clipsKey = key;
  $("#jParts").innerHTML = parts.length ? parts.map((p, i) => {
    const st = p.status === "done" ? `<b>${p.clips}</b> ${p.clips === 1 ? "corte" : "cortes"}`
      : p.status === "error" ? "Falhou" : p.status === "canceled" ? "Cancelado"
      : p.status === "queued" ? "Na fila" : esc(STEP_LABELS[p.step] || "") + "…";
    return `<li><button type="button" data-goto="${p.id}">
      <span class="when">${p.range ? fmtTime(p.range[0]) : ""}</span>
      <span class="what">Parte ${i + 1}<small>${esc(p.status === "done" || p.status === "error" ? p.message : (p.message || ""))}</small></span>
      <span class="count">${st}</span></button></li>`;
  }).join("") : `<li class="empty-note" style="padding:12px 4px;color:var(--muted)">O primeiro bloco aparece aqui assim que a gravação completar ${job.settings.block_minutes} minutos. Os cortes de cada bloco ficam prontos enquanto a live continua.</li>`;
}

/* ---------- cortes ---------- */
function updateClips(job) {
  const key = job.clips.map((c) => `${c.name}:${c.version}:${c.start}:${c.end}`).join("|") + [...state.busyClips].join(",");
  if (key === state.clipsKey) return;
  state.clipsKey = key;
  $("#jClips").innerHTML = job.clips.map((c, i) => clipCard(job, c, i)).join("");
}
function clipCard(job, c, i) {
  const v = `?v=${c.version || 0}`;
  const tags = (c.hashtags || []).map((t) => "#" + t.replace(/^#/, "")).join(" ");
  const busy = state.busyClips.has(i);
  return `<article class="clip" id="clip-${i}" data-i="${i}">
    <div class="frame">
      <video src="/media/${job.id}/${c.file}${v}" poster="/media/${job.id}/${c.thumb}${v}" controls preload="none" playsinline></video>
      <span class="num">${i + 1}</span>
      ${busy ? `<div class="rendering">Renderizando…</div>` : ""}
    </div>
    <h3>${esc(c.title)}</h3>
    <div class="score">Potencial <b>${c.score}</b>/100 — ${fmtTime(c.start)} a ${fmtTime(c.end)} (${Math.round(c.end - c.start)}s)</div>
    ${c.layout_used ? `<div class="layout-tag">Enquadramento: ${LAYOUTS[c.layout_used] || c.layout_used}</div>` : ""}
    <p class="reason">${esc(c.reason)}</p>
    ${tags ? `<div class="tags">${esc(tags)}</div>` : ""}
    <div class="row">
      <a class="btn primary-dl" href="/media/${job.id}/${c.file}?download=1">Baixar</a>
      <button class="btn" type="button" data-act="copy">Copiar legenda do post</button>
      <button class="btn" type="button" data-act="queue">Agendar postagem</button>
    </div>
    <details class="adjust">
      <summary>Ajustar corte</summary>
      <div class="adjust-grid" data-start="${c.start}" data-end="${c.end}">
        <span>Início</span>
        <div class="nudge"><button class="btn" type="button" data-n="start:-2">−2s</button><output data-o="start">${fmtTime(c.start)}</output><button class="btn" type="button" data-n="start:2">+2s</button></div>
        <span>Fim</span>
        <div class="nudge"><button class="btn" type="button" data-n="end:-2">−2s</button><output data-o="end">${fmtTime(c.end)}</output><button class="btn" type="button" data-n="end:2">+2s</button></div>
        <input type="text" value="${esc(c.title)}" aria-label="Título do corte" data-title>
        <button class="btn" type="button" data-act="rerender" ${busy ? "disabled" : ""}>Renderizar de novo</button>
      </div>
    </details>
  </article>`;
}

$("#detail").addEventListener("click", async (e) => {
  const job = state.job;
  if (!job) return;
  const goto = e.target.closest("[data-goto]")?.dataset.goto;
  if (goto) { select(goto); return; }
  const act = e.target.closest("[data-act]")?.dataset.act;
  const nudge = e.target.closest("[data-n]")?.dataset.n;
  const card = e.target.closest(".clip");
  try {
    if (nudge) {
      const grid = e.target.closest(".adjust-grid");
      const [field, delta] = nudge.split(":");
      const val = Math.max(0, +grid.dataset[field] + +delta);
      grid.dataset[field] = val;
      $(`[data-o=${field}]`, grid).textContent = fmtTime(val);
    } else if (act === "stop") {
      await api(`/api/jobs/${job.id}/stop`, { method: "POST" });
      toast("Gravação parada. Processando os cortes.");
    } else if (act === "cancel") {
      await api(`/api/jobs/${job.id}/cancel`, { method: "POST" });
      toast("Cancelando…");
    } else if (act === "retry") {
      await api(`/api/jobs/${job.id}/retry`, { method: "POST" });
      toast("Processando de novo");
    } else if (act === "reanalyze") {
      await api(`/api/jobs/${job.id}/reanalyze`, { method: "POST", body: JSON.stringify(readSettings()) });
      toast("Gerando novos cortes");
    } else if (act === "delete") {
      const what = job.mode === "live" ? "esta live, todas as partes e todos os cortes" : "este vídeo e todos os cortes dele";
      if (!confirm(`Excluir ${what}?`)) return;
      await api(`/api/jobs/${job.id}`, { method: "DELETE" });
      select(job.mode === "parte" ? job.parent : null);
    } else if (act === "copy" && card) {
      const c = job.clips[+card.dataset.i];
      const text = `${c.title}\n\n${(c.hashtags || []).map((t) => "#" + t.replace(/^#/, "").replace(/\s+/g, "")).join(" ")}`;
      await navigator.clipboard.writeText(text);
      toast("Legenda copiada");
    } else if (act === "queue" && card) {
      const cfg = await api("/api/config");
      const platforms = ["youtube", "tiktok"].filter((p) => cfg.publish[p].enabled);
      if (!platforms.length) { toast("Ative o YouTube ou o TikTok na aba Agenda primeiro"); return; }
      toast("Preparando título e hashtags…");
      await api(`/api/jobs/${job.id}/clips/${+card.dataset.i}/queue`, { method: "POST", body: JSON.stringify({ platforms }) });
      toast("Enviado para a Agenda, aguardando sua aprovação");
      return;
    } else if (act === "rerender" && card) {
      const i = +card.dataset.i, grid = $(".adjust-grid", card);
      state.busyClips.add(i); state.clipsKey = ""; updateClips(job);
      try {
        await api(`/api/jobs/${job.id}/clips/${i}`, {
          method: "POST",
          body: JSON.stringify({ start: +grid.dataset.start, end: +grid.dataset.end, title: $("[data-title]", grid).value }),
        });
        toast(`Corte ${i + 1} renderizado`);
      } finally {
        state.busyClips.delete(i); state.clipsKey = "";
      }
    } else return;
    await refreshList(); await refreshDetail();
  } catch (err) {
    toast(err.message);
  }
});

/* ---------- status do sistema ---------- */
async function refreshHealth() {
  try {
    const h = await api("/api/health");
    $("#health").innerHTML = `
      <span class="${h.ffmpeg ? "" : "bad"}">${h.ffmpeg ? "ffmpeg ok" : "ffmpeg ausente"}</span>
      <span class="${h.ai_key ? "" : "bad"}" title="${h.ai?.claude_pause_reason ? "Claude em pausa: " + esc(h.ai.claude_pause_reason) : ""}">${
        h.ai?.active === "claude" ? "IA: Claude Haiku" : h.ai?.active === "local" ? "IA: local (grátis)" : "Sem IA"}</span>
      <span>Transcrição: ${h.whisper === "cuda" ? "GPU" : h.whisper === "cpu" ? "CPU" : "pronta"}</span>
      <span class="${h.face ? "" : "bad"}">${h.face ? "Detecção de rosto ok" : "Sem detecção de rosto"}</span>`;
  } catch {
    $("#health").innerHTML = `<span class="bad">Servidor offline</span>`;
  }
}

/* ---------- início ---------- */
const isNarrow = () => matchMedia("(max-width: 860px)").matches;
function syncFormBox() {
  // no computador o formulário fica sempre aberto; no celular vira um botão recolhível
  if (!isNarrow()) $("#newJobBox").open = true;
}
matchMedia("(max-width: 860px)").addEventListener("change", syncFormBox);

(async function init() {
  await Promise.all([refreshList(), refreshHealth()]);
  if (isNarrow()) $("#newJobBox").open = !state.jobs.length;
  const fromHash = location.hash.slice(1);
  select(state.jobs.some((j) => j.id === fromHash) ? fromHash : null);
  setInterval(async () => {
    try {
      await refreshList();
      const j = state.jobs.find((x) => x.id === state.selected);
      const cur = state.job;
      const watchingParts = cur?.mode === "live" && (cur.parts || []).some((pid) => {
        const p = state.jobs.find((x) => x.id === pid);
        return p && (p.status === "running" || p.status === "queued" || p.updated_at > cur.updated_at);
      });
      if (j && (j.status === "running" || j.status === "queued" || watchingParts || j.updated_at !== cur?.updated_at)) await refreshDetail();
    } catch {}
  }, 1500);
  setInterval(refreshHealth, 20000);
})();
