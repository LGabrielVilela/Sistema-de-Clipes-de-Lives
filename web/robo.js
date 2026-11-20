"use strict";
/* Abas do robô: Streamers, Agenda e Contas. Usa $, esc, fmtTime, toast e api do app.js. */

const robo = { tab: "cortes", keys: {}, editing: false };
const PLAT = { twitch: "Twitch", kick: "Kick", youtube: "YouTube", tiktok: "TikTok" };

function fmtWhen(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const day = d.toDateString() === now.toDateString() ? "Hoje"
    : d.toDateString() === new Date(now.getTime() + 864e5).toDateString() ? "Amanhã"
    : d.toLocaleDateString("pt-BR", { weekday: "long", day: "2-digit", month: "2-digit" });
  return { day, time: d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" }) };
}
const changed = (name, data) => { const k = JSON.stringify(data); if (robo.keys[name] === k) return false; robo.keys[name] = k; return true; };

/* ---------------------------------------------------------------- abas */
function showTab(tab) {
  robo.tab = tab;
  try { localStorage.setItem("tab", tab); } catch {}
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-current", b.dataset.tab === tab ? "page" : "false"));
  $("#page-cortes").hidden = tab !== "cortes";
  for (const t of ["streamers", "agenda", "contas"]) $(`#page-${t}`).hidden = tab !== t;
  robo.keys = {};
  refreshTab();
}
$(".tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) showTab(b.dataset.tab); });

async function refreshTab() {
  try {
    if (robo.tab === "streamers") await renderStreamers();
    if (robo.tab === "agenda") await renderAgenda();
    if (robo.tab === "contas") await renderContas();
  } catch (err) { console.error(err); }
}

async function refreshBadges() {
  try {
    const [posts, m] = await Promise.all([api("/api/posts"), api("/api/monitor")]);
    const pend = posts.filter((p) => p.status === "pending").length;
    const live = Object.values(m.status).filter((s) => s.job_id).length;
    $("#pendingBadge").hidden = !pend; $("#pendingBadge").textContent = pend; $("#pendingBadge").className = "tab-badge yellow";
    $("#liveBadge").hidden = !live; $("#liveBadge").textContent = live;
  } catch {}
}

/* ---------------------------------------------------------------- streamers */
async function renderStreamers() {
  const m = await api("/api/monitor");
  const el = $("#page-streamers");
  if (!el.dataset.ready) {
    el.dataset.ready = "1";
    el.innerHTML = `
      <h1>Streamers</h1>
      <p class="lede">O robô confere a lista de tempos em tempos. Quando um streamer entra ao vivo jogando o jogo das palavras-chave, ele grava a live em blocos e corta cada bloco enquanto a live continua. Cadastre só quem autoriza cortes.</p>
      <label class="switch"><input type="checkbox" id="monOn"> <span id="monLabel">Robô desligado</span></label>
      <div class="settings-grid" id="monCfg">
        <label class="field" style="grid-column:span 2"><span>Palavras-chave do jogo (separadas por vírgula)</span><input type="text" data-k="keywords"></label>
        <label class="field"><span>Conferir a cada (min)</span><input type="number" min="1" max="60" data-k="interval_min"></label>
        <label class="field"><span>Gravações ao mesmo tempo</span><input type="number" min="1" max="6" data-k="max_concurrent"></label>
        <label class="field" title="Com todas as vagas ocupadas e outros streamers ao vivo esperando, cada gravação cede a vaga depois desse tempo (o bloco em andamento vai para corte). 0 = sem rodízio."><span>Turno de gravação (min)</span><input type="number" min="0" max="480" data-k="turn_minutes"></label>
        <label class="field"><span>Cortar a cada (min)</span><input type="number" min="3" max="120" data-k="block_minutes"></label>
        <label class="field"><span>Gravar no máximo (h)</span><input type="number" min="1" max="12" data-k="max_hours"></label>
        <label class="field"><span>Cortes por bloco</span><input type="number" min="1" max="10" data-k="clips_per_block"></label>
        <label class="field"><span>Duração do corte (s)</span><input type="number" min="10" max="120" data-k="clip_seconds"></label>
        <label class="field"><span>Enquadramento</span><select data-k="layout">
          <option value="auto">Automático</option><option value="split">Webcam + jogo</option>
          <option value="crop">Seguindo o rosto</option><option value="blur">Fundo desfocado</option></select></label>
        <label class="field"><span>Nota mínima do momento</span><input type="number" min="0" max="99" data-k="min_score" title="Momentos abaixo dessa nota, ou abaixo do normal do streamer, não viram corte. Clips feitos pela plateia sempre passam."></label>
        <label class="check"><input type="checkbox" data-k="cold_open"> Gancho no início (abre com o clímax)</label>
        <label class="check"><input type="checkbox" data-k="loop_end"> Final em loop</label>
      </div>
      <p class="hint" id="apiHint"></p>
      <h2>Adicionar streamer</h2>
      <form class="row-form" id="addStreamer">
        <label class="field" style="flex:1 1 280px"><span>Link do canal ou nome</span><input type="text" name="text" required placeholder="twitch.tv/nome, kick.com/nome ou youtube.com/@nome"></label>
        <label class="field"><span>Plataforma (se for só o nome)</span><select name="platform"><option value="twitch">Twitch</option><option value="kick">Kick</option><option value="youtube">YouTube</option></select></label>
        <label class="check" style="margin-bottom:9px"><input type="checkbox" name="any_game"> Cortar qualquer jogo</label>
        <button class="btn" type="submit" style="margin-bottom:2px">Adicionar</button>
      </form>
      <h2 id="listTitle">Lista</h2>
      <ul class="streamers" id="streamerList"></ul>`;
    $("#monOn").addEventListener("change", (e) => saveMonitor({ enabled: e.target.checked }));
    $("#monCfg").addEventListener("change", (e) => {
      const k = e.target.dataset.k; if (!k) return;
      let v = e.target.value;
      if (k === "keywords") v = v.split(",").map((s) => s.trim()).filter(Boolean);
      else if (e.target.type === "number") v = +v;
      else if (e.target.type === "checkbox") v = e.target.checked;
      if (k === "clip_seconds") return saveMonitor({ min_len: Math.max(5, v - 3), max_len: v + 3 });
      saveMonitor({ [k]: v });
    });
    $("#addStreamer").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      try {
        await api("/api/streamers", { method: "POST", body: JSON.stringify({ text: f.text.value, platform: f.platform.value, any_game: f.any_game.checked }) });
        f.reset(); toast("Streamer adicionado"); robo.keys = {}; renderStreamers();
      } catch (err) { toast(err.message); }
    });
    $("#streamerList").addEventListener("change", async (e) => {
      const li = e.target.closest("[data-sid]"); if (!li) return;
      await api(`/api/streamers/${li.dataset.sid}`, { method: "PATCH", body: JSON.stringify({ [e.target.name]: e.target.checked }) });
      robo.keys = {}; renderStreamers();
    });
    $("#streamerList").addEventListener("click", async (e) => {
      const li = e.target.closest("[data-sid]");
      if (e.target.closest("[data-goto]")) { showTab("cortes"); select(e.target.closest("[data-goto]").dataset.goto); return; }
      if (li && e.target.closest("[data-del]") && confirm("Remover este streamer da lista?")) {
        await api(`/api/streamers/${li.dataset.sid}`, { method: "DELETE" }); robo.keys = {}; renderStreamers();
      }
    });
  }
  const c = m.config;
  if (document.activeElement?.closest("#monCfg") == null && changed("moncfg", c)) {
    $("#monOn").checked = c.enabled;
    document.querySelectorAll("#monCfg [data-k]").forEach((i) => {
      const v = i.dataset.k === "clip_seconds" ? Math.round((c.min_len + c.max_len) / 2) : c[i.dataset.k];
      if (i.type === "checkbox") i.checked = v !== false;
      else i.value = Array.isArray(v) ? v.join(", ") : v;
    });
  }
  $("#monLabel").textContent = c.enabled
    ? `Robô ligado — ${m.running ? "conferindo agora…" : m.last_run ? "última conferência às " + new Date(m.last_run * 1000).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" }) : "começando"}`
    : "Robô desligado";
  $("#apiHint").innerHTML = `Categoria do jogo: Twitch ${m.apis.twitch ? "pela API oficial" : "pelo título da live"}, Kick ${m.apis.kick ? "pela API oficial" : "pelo título da live"}, YouTube pelo título. ${(!m.apis.twitch || !m.apis.kick) ? "Cadastre as chaves na aba Contas para filtrar pela categoria exata." : ""}`;
  if (!changed("streamers", [m.streamers, m.status])) return;
  $("#listTitle").textContent = `Lista (${m.streamers.length})`;
  $("#streamerList").innerHTML = m.streamers.length ? m.streamers.map((s) => {
    const st = m.status[s.id];
    let state = !c.enabled ? "Robô desligado" : !st ? "Ainda não conferido" : st.error && !st.live ? esc(st.error) : "Offline";
    let on = false;
    if (st?.live) {
      on = true;
      state = `Ao vivo${st.category ? " — " + esc(st.category) : ""}${st.viewers ? ` — ${st.viewers.toLocaleString("pt-BR")} espectadores` : ""}`;
      if (st.job_id) state += ` — <a href="#" data-goto="${st.job_id}">gravando e cortando</a>`;
      else if (!st.matched) state += " — fora do jogo escolhido, não gravando";
      else if (st.note) state += " — " + esc(st.note);
    }
    return `<li data-sid="${s.id}">
      <span class="plat ${s.platform}">${PLAT[s.platform]}</span>
      <div><div class="s-name">${esc(s.name)}</div><div class="s-state ${on ? "on" : ""}">${state}</div></div>
      <div class="s-actions">
        <label><input type="checkbox" name="enabled" ${s.enabled ? "checked" : ""}> Vigiar</label>
        <label><input type="checkbox" name="any_game" ${s.any_game ? "checked" : ""}> Qualquer jogo</label>
        <button class="btn danger" type="button" data-del>Remover</button>
      </div></li>`;
  }).join("") : `<li class="empty-note">Nenhum streamer ainda. Adicione acima quem autoriza cortes.</li>`;
}
async function saveMonitor(patch) {
  try { await api("/api/config", { method: "PATCH", body: JSON.stringify({ monitor: patch }) }); robo.keys = {}; renderStreamers(); toast("Salvo"); }
  catch (err) { toast(err.message); }
}

/* ---------------------------------------------------------------- agenda */
async function renderAgenda() {
  const [posts, cfg, tr, ins, auto] = await Promise.all([api("/api/posts"), api("/api/config"), api("/api/trends"),
    api("/api/insights").catch(() => ({})), api("/api/posts/auto").catch(() => ({}))]);
  robo.auto = auto;
  const el = $("#page-agenda");
  if (!el.dataset.ready) {
    el.dataset.ready = "1";
    el.innerHTML = `
      <h1>Agenda de postagens</h1>
      <p class="lede">Cortes com nota acima do mínimo entram aqui com título, descrição e hashtags prontos. Você aprova, e o robô posta nos próximos horários livres, sem postar tudo de uma vez.</p>
      <details class="settings-box" id="pubBox">
        <summary>Configurações da agenda</summary>
        <div class="settings-grid" id="pubCfg">
          <label class="switch"><input type="checkbox" data-k="youtube.enabled"> YouTube Shorts</label>
          <label class="switch"><input type="checkbox" data-k="tiktok.enabled"> TikTok</label>
          <label class="field"><span>TikTok envia como</span><select data-k="tiktok.mode"><option value="draft">Rascunho (você publica no app)</option><option value="direct">Post direto (app auditado)</option></select></label>
          <label class="field"><span>Horários por plataforma</span><input type="text" data-k="slots" placeholder="12:00, 18:00, 21:00"></label>
          <label class="switch" title="Uma vez por dia, no máximo, troca o horário que rende bem menos que o normal do canal pelo melhor horário medido (ou testa um horário novo). Precisa de 3 vídeos medidos no horário."><input type="checkbox" data-k="auto_slots"> Ajustar horários pelas views</label>
          <label class="field"><span>Nota mínima na fila</span><input type="number" min="0" max="100" data-k="min_score"></label>
          <label class="switch"><input type="checkbox" data-k="auto_approve"> Postar sozinho os melhores</label>
          <label class="field"><span>Nota mínima para postar sozinho</span><input type="number" min="0" max="100" data-k="auto_min_score"></label>
        </div>
        <p class="hint" id="autoHint"></p>
      </details>
      <div class="agenda-grid">
        <div>
          <div class="row-form" style="justify-content:space-between;align-items:center"><h2 id="pendTitle" style="margin:24px 0 4px">Aguardando aprovação</h2><div class="row-form"><button class="btn" type="button" id="regenAll">Refazer títulos com IA</button><button class="btn" type="button" id="approveAll">Aprovar todos</button><button class="btn danger" type="button" id="discardAll">Descartar todos</button></div></div>
          <div id="pendingList"></div>
          <h2>Agendados</h2><ul class="sched" id="schedList"></ul>
          <h2>Enviados e falhas</h2><ul class="sched" id="doneList"></ul>
        </div>
        <aside>
          <h2>O que está rendendo</h2>
          <div id="learnBox"></div>
          <button class="btn" type="button" id="refreshLearn">Atualizar números agora</button>
          <h2>Hashtags em alta</h2>
          <div id="trendsBox"></div>
          <div class="stack">
            <label class="field"><span>Em alta no TikTok (copie do <a href="https://ads.tiktok.com/business/creativecenter/inspiration/popular/hashtag/pc/pt" target="_blank" rel="noopener">Creative Center</a>)</span><textarea data-k="manual_hashtags" placeholder="#gta6 #gtavi #jason…"></textarea></label>
            <label class="field"><span>Sempre usar</span><input type="text" data-k="fixed_hashtags"></label>
            <label class="check"><input type="checkbox" data-k="credit_line"> Dar crédito ao streamer na descrição</label>
            <button class="btn" type="button" id="refreshTrends">Atualizar tendências agora</button>
          </div>
        </aside>
      </div>`;
    $("#pubCfg").addEventListener("change", (e) => savePublish(e.target));
    el.querySelector("aside").addEventListener("change", (e) => { if (e.target.dataset.k) savePublish(e.target); });
    $("#approveAll").addEventListener("click", async () => { const r = await api("/api/posts/approve-all", { method: "POST" }); toast(`${r.approved} post(s) agendado(s)`); robo.keys = {}; renderAgenda(); });
    $("#discardAll").addEventListener("click", async (e) => {
      const btn = e.target;
      const pending = (await api("/api/posts")).filter((p) => p.status === "pending");
      if (!pending.length) { toast("Nada aguardando aprovação"); return; }
      if (!confirm(`Descartar os ${pending.length} posts aguardando aprovação?

Os agendados e os enviados não são afetados. Os vídeos continuam na aba Cortes, e dá para mandar de novo com "Agendar postagem".`)) return;
      btn.disabled = true;
      let done = 0, failed = 0;
      for (const p of pending) {
        try { await api(`/api/posts/${p.id}/discard`, { method: "POST" }); done++; } catch { failed++; }
        if (done % 5 === 0) btn.textContent = `Descartando… ${done}/${pending.length}`;
      }
      btn.textContent = "Descartar todos"; btn.disabled = false;
      toast(failed ? `${done} descartados, ${failed} não puderam ser descartados` : `${done} posts descartados`);
      document.activeElement?.blur(); robo.keys = {}; renderAgenda(); refreshBadges();
    });
    $("#regenAll").addEventListener("click", async (e) => {
      const btn = e.target; btn.disabled = true;
      try {
        const r = await api("/api/posts/regenerate", { method: "POST" });
        toast(`Refazendo os textos de ${r.clips} corte(s) com IA…`);
        const timer = setInterval(async () => {
          const st = await api("/api/posts/regenerate");
          btn.textContent = st.running ? `Refazendo… ${st.done}/${st.total}` : "Refazer títulos com IA";
          if (!st.running) { clearInterval(timer); btn.disabled = false; toast("Títulos refeitos"); robo.keys = {}; renderAgenda(); }
        }, 2000);
      } catch (err) { toast(err.message); btn.disabled = false; }
    });
    $("#refreshLearn").addEventListener("click", async (e) => {
      e.target.disabled = true;
      try { await api("/api/insights/refresh", { method: "POST" }); toast("Números do YouTube atualizados"); } catch (err) { toast(err.message); }
      e.target.disabled = false; robo.keys = {}; renderAgenda();
    });
    $("#refreshTrends").addEventListener("click", async (e) => {
      e.target.disabled = true;
      try { await api("/api/trends/refresh", { method: "POST" }); toast("Tendências atualizadas"); } catch (err) { toast(err.message); }
      e.target.disabled = false; robo.keys = {}; renderAgenda();
    });
    el.addEventListener("click", postClick);
    el.addEventListener("click", (e) => {  // miniatura abre a revisão em tela cheia
      const t = e.target.closest("[data-view]");
      if (t) openViewer(t.dataset.view, t);
    });
    $("#pubBox").open = !matchMedia("(max-width: 860px)").matches;  // no celular começa recolhido
  }
  const pub = cfg.publish;
  if (!el.querySelector(":focus") && changed("pubcfg", pub)) {
    el.querySelectorAll("[data-k]").forEach((i) => {
      const v = i.dataset.k.split(".").reduce((o, k) => o?.[k], pub);
      if (i.type === "checkbox") i.checked = !!v;
      else i.value = Array.isArray(v) ? v.map((x) => (i.dataset.k.includes("hashtags") ? "#" + x.replace(/^#/, "") : x)).join(i.dataset.k === "slots" ? ", " : " ") : v;
    });
  }
  $("#autoHint").textContent = pub.auto_approve
    ? `Perto de cada horário (45 min antes no YouTube), o robô escolhe o corte de MAIOR NOTA disponível (nota ${pub.auto_min_score} ou mais). A nota mede o corte: qualidade e chance de viralizar (avaliadas pela Groq com a fala e as imagens), clips e chat da plateia e o histórico do canal. Só em nota igual vale outro streamer e o corte mais novo. Cortes não escolhidos em 48 h saem da fila. Se você aprovar um corte na mão, ele reserva o próximo horário livre.${pub.tiktok.enabled && pub.tiktok.mode === "draft" ? " No TikTok em modo rascunho, ainda é preciso tocar em publicar no app; isso só some com o modo Post direto, depois da auditoria do TikTok." : ""}`
    : "";
  if (changed("trends", tr)) {
    const yt = (tr.youtube || []).slice(0, 18);
    $("#trendsBox").innerHTML = (yt.length ? `<ul class="trend-list">${yt.map((t) => `<li><b>#${esc(t.tag)}</b></li>`).join("")}</ul>
      <p class="hint">Hashtags dos Shorts mais vistos das últimas 72 h sobre o jogo${tr.updated_at ? ", atualizado " + new Date(tr.updated_at * 1000).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" }) : ""}.</p>`
      : `<p class="hint">${esc(tr.error || "Ainda sem dados. Conecte o YouTube na aba Contas e clique em atualizar.")}</p>`);
  }

  if (changed("learn", ins)) {
    const views = (n) => n >= 1000 ? (n / 1000).toLocaleString("pt-BR", { maximumFractionDigits: 1 }) + " mil" : String(n);
    const top = (ins.top || []).map((t) => `<li><b>${views(t.views)}</b>${t.retention != null ? ` · ${Math.round(t.retention)}% retido` : ""} · ${t.url ? `<a href="${esc(t.url)}" target="_blank" rel="noopener">${esc(t.title)}</a>` : esc(t.title)}</li>`).join("");
    const body = ins.ready
      ? ((ins.findings || []).length
          ? `<ul class="trend-list learn">${ins.findings.map((f) => `<li>${esc(f.text)}</li>`).join("")}</ul>
             <p class="hint">O robô já usa isso: dá mais nota aos cortes de streamer e enquadramento que rendem mais e passa a dica de título para a IA.</p>`
          : `<p class="hint">${ins.measured} vídeos medidos e nenhuma diferença forte ainda (precisa de 1,5x ou mais entre grupos com 3 vídeos ou mais).</p>`)
      : `<p class="hint">Aprendendo: ${ins.measured || 0} de ${ins.need || 8} vídeos com mais de 24 h medidos. Com ${ins.need || 8}, o robô começa a ajustar notas e títulos sozinho.</p>`;
    const hours = Object.entries(ins.groups?.horario || {}).sort((a, b) => a[0].localeCompare(b[0]))
      .map(([h, v]) => `<li><b>${esc(h)}</b> · mediana ${views(v.median)} views (${v.n} vídeo${v.n > 1 ? "s" : ""})</li>`).join("");
    const changes = (ins.slot_changes || []).slice(-3).reverse()
      .map((c) => `<li>${new Date(c.at * 1000).toLocaleDateString("pt-BR")}: ${esc(c.note)}</li>`).join("");
    $("#learnBox").innerHTML = body
      + (hours ? `<p class="hint" style="margin-top:10px">Views por horário de publicação:</p><ul class="trend-list learn">${hours}</ul>` : "")
      + (changes ? `<p class="hint" style="margin-top:10px">Ajustes de horário:</p><ul class="trend-list learn">${changes}</ul>` : "")
      + (top ? `<p class="hint" style="margin-top:10px">Mais vistos:</p><ul class="trend-list learn">${top}</ul>` : "")
      + (ins.updated_at ? `<p class="hint">Atualizado ${new Date(ins.updated_at * 1000).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" })}. Atualiza sozinho a cada 3 h.</p>` : "");
  }

  const pending = posts.filter((p) => p.status === "pending");
  $("#pendTitle").textContent = `Aguardando aprovação (${pending.length})`;
  $("#approveAll").hidden = !pending.length;
  // refazer títulos vale também para agendados que ainda não foram enviados
  $("#regenAll").hidden = !posts.some((p) => p.status === "pending" || (p.status === "scheduled" && !p.result));
  $("#discardAll").hidden = !pending.length;
  const editingNow = $("#pendingList").contains(document.activeElement);
  // quais cortes já têm post em cada plataforma (para oferecer a que falta)
  robo.has = new Set(posts.filter((p) => p.status !== "discarded").map((p) => `${p.job_id}|${p.clip_name}|${p.platform}`));
  robo.enabled = ["youtube", "tiktok"].filter((k) => pub[k].enabled);
  if (!editingNow && changed("pending", [pending.map((p) => p.id), [...robo.has].sort(), robo.enabled, auto])) {
    // no modo automático, a lista segue a ordem da disputa (quem vai subir primeiro aparece em cima)
    const rank = {};
    Object.values(auto?.platforms || {}).forEach((pl) => pl.ranking.forEach((id, i) => { rank[id] = i; }));
    const ordered = auto?.enabled ? [...pending].sort((a, b) => (rank[a.id] ?? 1e6) - (rank[b.id] ?? 1e6) || b.score - a.score) : pending;
    $("#pendingList").innerHTML = ordered.length ? ordered.map(postCard).join("")
      : `<p class="empty-note">Nada esperando. Os cortes novos aparecem aqui${pub.youtube.enabled || pub.tiktok.enabled ? "" : " assim que você ativar o YouTube ou o TikTok acima"}.</p>`;
  }
  const sched = posts.filter((p) => p.status === "scheduled" || p.status === "sending");
  if (changed("sched", sched)) {
    let last = "";
    $("#schedList").innerHTML = sched.length ? sched.map((p) => {
      const w = fmtWhen(p.scheduled_at);
      const head = w.day !== last ? `<li class="day">${w.day}</li>` : ""; last = w.day;
      return `${head}<li class="item" data-pid="${p.id}"><span class="time">${p.status === "sending" ? "agora" : w.time}</span>
        <span class="pb"><span class="pbadge ${p.platform}">${PLAT[p.platform]}</span></span>
        <span class="ttl" title="${esc(p.title)}">${p.auto ? '<span class="hint">auto · </span>' : ""}${esc(p.platform === "tiktok" ? p.caption : p.title)}</span>
        <span>${p.status === "sending" ? "Enviando…" : `<button class="btn" data-pact="now" type="button">Postar agora</button> <button class="btn" data-pact="unschedule" type="button">Tirar</button>`}</span>
        ${p.error ? `<span class="err">Tentativa ${p.attempts} falhou: ${esc(p.error)}</span>` : ""}
        ${p.wait_note && !p.error ? `<span class="err ok">${esc(p.wait_note)}</span>` : ""}</li>`;
    }).join("") : `<li class="empty-note">Nenhum post agendado.</li>`;
  }
  const done = posts.filter((p) => p.status === "done" || p.status === "failed").sort((a, b) => (b.sent_at || b.scheduled_at || 0) - (a.sent_at || a.scheduled_at || 0)).slice(0, 40);
  if (changed("done", done)) {
    $("#doneList").innerHTML = done.length ? done.map((p) => `<li class="item" data-pid="${p.id}">
      <span class="time">${p.sent_at ? fmtWhen(p.sent_at).time : "—"}</span>
      <span class="pb"><span class="pbadge ${p.platform}">${PLAT[p.platform]}</span></span>
      <span class="ttl">${esc(p.platform === "tiktok" ? p.caption : p.title)}</span>
      <span>${p.status === "failed" ? `<button class="btn" data-pact="approve" type="button">Tentar de novo</button>` : p.result?.url ? `<a class="btn" href="${esc(p.result.url)}" target="_blank" rel="noopener">Abrir</a>` : ""}</span>
      ${p.status === "failed" ? `<span class="err">${esc(p.error)}</span>` : `<span class="err ok">${p.stats?.views != null ? `<b>${p.stats.views.toLocaleString("pt-BR")} views · ${p.stats.likes} curtidas${p.stats.retention != null ? ` · retenção ${Math.round(p.stats.retention)}%` : ""}</b> · ` : ""}${esc(p.result?.note || "Enviado")}</span>`}</li>`).join("")
      : `<li class="empty-note">Nada enviado ainda.</li>`;
  }
}

function postCard(p) {
  const tiktok = p.platform === "tiktok";
  return `<article class="post-card" data-pid="${p.id}">
    <button class="thumb" type="button" data-view="${p.id}" aria-label="Assistir em tela cheia">
      <img src="/media/${p.job_id}/${p.thumb}" alt="" loading="lazy">
      <span class="play" aria-hidden="true"></span>
    </button>
    <div class="stack">
      <div class="post-head"><span class="pbadge ${p.platform}">${PLAT[p.platform]}</span> Nota ${p.score} ${p.streamer ? "— " + esc(p.streamer) : ""}</div>
      ${rubricLine(p)}
      ${autoLine(p)}
      ${tiktok ? `<textarea data-f="caption" aria-label="Legenda do TikTok">${esc(p.caption)}</textarea>`
        : `<input type="text" data-f="title" value="${esc(p.title)}" aria-label="Título do Short">
           <textarea data-f="description" aria-label="Descrição">${esc(p.description)}</textarea>
           <input type="text" data-f="tags" value="${esc((p.tags || []).join(", "))}" aria-label="Tags de busca">`}
      <div class="row-form">
        <button class="btn primary-dl" type="button" data-pact="approve">Aprovar e agendar</button>
        <button class="btn" type="button" data-pact="now">Postar agora</button>
        <button class="btn danger" type="button" data-pact="discard">Descartar</button>
        ${missingPlatforms(p).map((m) => `<button class="btn" type="button" data-also="${m}">+ ${PLAT[m]}</button>`).join("")}
      </div>
    </div></article>`;
}

function rubricLine(p) {
  // régua do gancho dada pela Groq: por que o corte tem essa nota
  const r = p.rubric;
  if (!r) return "";
  const items = [["gancho", "Gancho", 3], ["momento_forte", "Momento", 2], ["desfecho_no_corte", "Desfecho", 2],
    ["entende_sozinho", "Sozinho", 1], ["entra_direto", "Direto", 1], ["detalhe_especifico", "Detalhe", 1]];
  return `<p class="hint rubric">${items.map(([k, l, m]) => `${l} <b>${r[k] ?? 0}/${m}</b>`).join(" · ")}</p>`;
}

function autoLine(p) {
  // "Postar sozinho os melhores": mostra em que posição o corte está na disputa pelo próximo horário livre
  const a = robo.auto;
  if (!a?.enabled) return "";
  if (p.score < a.floor) return `<p class="hint">Abaixo da nota ${a.floor}: só sobe se você aprovar.</p>`;
  const plat = a.platforms?.[p.platform];
  if (!plat) return `<p class="hint">Na disputa. Sem horário livre nos próximos 3 dias.</p>`;
  const pos = plat.ranking.indexOf(p.id) + 1;
  if (!pos) {
    // nota antiga (de antes da Groq): só entra na disputa depois de reavaliada, para comparar notas iguais
    if (p.groq_score == null && !p.groq_tried)
      return `<p class="auto-line">Aguardando a reavaliação da Groq para entrar na disputa (leva alguns minutos). Esta nota é de antes da reavaliação e ainda pode mudar.</p>`;
    return "";
  }
  const t = (s) => new Date(s * 1000).toLocaleString("pt-BR", { weekday: "short", hour: "2-digit", minute: "2-digit" });
  return `<p class="auto-line ${pos === 1 ? "lead" : ""}">${pos === 1 ? "Em 1º lugar" : `${pos}º lugar`} na disputa pelo horário de <b>${t(plat.slot)}</b> — a escolha é feita às ${t(plat.pick_at)}, e um corte melhor que sair até lá passa na frente.</p>`;
}

function missingPlatforms(p) {
  return (robo.enabled || []).filter((m) => m !== p.platform && !robo.has?.has(`${p.job_id}|${p.clip_name}|${m}`));
}

async function postClick(e) {
  const also = e.target.closest("[data-also]");
  if (also) {
    const row = also.closest("[data-pid]");
    const p = (await api("/api/posts")).find((x) => x.id === row.dataset.pid);
    also.disabled = true; toast(`Preparando o post do ${PLAT[also.dataset.also]}…`);
    try {
      await api(`/api/jobs/${p.job_id}/clips/${p.clip_index}/queue`, { method: "POST", body: JSON.stringify({ platforms: [also.dataset.also] }) });
      toast(`Post do ${PLAT[also.dataset.also]} criado, aguardando aprovação`);
    } catch (err) { toast(err.message); also.disabled = false; }
    document.activeElement?.blur(); robo.keys = {}; renderAgenda(); refreshBadges();
    return;
  }
  const b = e.target.closest("[data-pact]"); if (!b) return;
  const row = b.closest("[data-pid]"); const pid = row.dataset.pid; const act = b.dataset.pact;
  const fields = {};
  row.querySelectorAll("[data-f]").forEach((i) => { fields[i.dataset.f] = i.dataset.f === "tags" ? i.value.split(",") : i.value; });
  try {
    if (Object.keys(fields).length && act !== "discard") await api(`/api/posts/${pid}`, { method: "PATCH", body: JSON.stringify(fields) });
    if (act === "now" && !confirm("Postar este corte agora, fora da agenda?")) return;
    const r = await api(`/api/posts/${pid}/${act}`, { method: "POST" });
    // o cartão sai da lista na hora (o foco no botão impedia a lista de se atualizar sozinha)
    if (row.classList.contains("post-card")) { document.activeElement?.blur(); row.remove(); }
    toast(act === "approve" ? `Agendado: ${fmtWhen(r.scheduled_at).day.toLowerCase()} às ${fmtWhen(r.scheduled_at).time}` : act === "now" ? "Enviando agora" : act === "discard" ? "Descartado" : "Tirado da agenda");
  } catch (err) { toast(err.message); }
  robo.keys = {}; renderAgenda(); refreshBadges();
}

async function savePublish(input) {
  const path = input.dataset.k.split(".");
  let v = input.type === "checkbox" ? input.checked : input.type === "number" ? +input.value : input.value;
  if (path[0] === "slots") v = v.split(/[ ,;]+/).map((s) => s.trim()).filter((s) => /^\d{1,2}:\d{2}$/.test(s));
  if (path[0].includes("hashtags")) v = [...new Set(v.split(/[\s,]+/).map((s) => s.replace(/^#/, "").trim().toLowerCase()).filter(Boolean))];
  const patch = path.length === 2 ? { [path[0]]: { [path[1]]: v } } : { [path[0]]: v };
  try { await api("/api/config", { method: "PATCH", body: JSON.stringify({ publish: patch }) }); toast("Salvo"); robo.keys.pubcfg = ""; }
  catch (err) { toast(err.message); }
}

/* ---------------------------------------------------------------- contas */
async function renderContas() {
  const a = await api("/api/accounts");
  if (!changed("contas", a)) return;
  const s = a.secrets, yt = a.youtube, tt = a.tiktok;
  const port = location.port || "80";
  const key = (name, label) => `<label class="field"><span>${label} ${s[name] ? "(salvo)" : ""}</span><input type="password" data-secret="${name}" placeholder="${s[name] ? "••••••••  (deixe em branco para manter)" : ""}" autocomplete="off"></label>`;
  $("#page-contas").innerHTML = `
    <h1>Contas</h1>
    <p class="lede">As chaves ficam só neste computador, em <code>data/secrets.json</code>. Nada é enviado para outro lugar além das próprias plataformas.</p>
    <div class="accounts">
      <section class="acct"><h3>IA <span class="st ${a.ai.active !== "none" ? "ok" : ""}">${a.ai.active === "claude" ? "Usando Claude Haiku" : a.ai.active === "local" ? "Usando IA local (grátis)" : "Sem IA"}</span></h3>
        <p class="hint"><b>IA local (grátis):</b> ${a.ai.local_ready ? `ativa, modelo <code>${esc(a.ai.local_model)}</code>` : `desligada. Abra o Ollama ou rode <code>ollama pull ${esc(a.ai.local_model)}</code>`}.<br>
        <b>Claude Haiku (opcional, melhor qualidade, centavos por live):</b> ${a.ai.claude_paused ? `em pausa: ${esc(a.ai.claude_pause_reason)}. A IA local assume enquanto isso` : a.ai.claude_configured ? "configurado e com prioridade" : "sem chave; a IA local é usada"}.</p>
        <ol><li>Opção 1: crie uma chave em <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener">console.anthropic.com</a>, cole abaixo e salve.</li>
        <li>Opção 2: num terminal, rode <code>ant auth login</code> e entre com sua conta. O sistema usa esse login sozinho.</li></ol>
        <div class="stack">${key("ANTHROPIC_API_KEY", "Chave da API")}<button class="btn" data-save type="button">Salvar</button></div></section>

      <section class="acct"><h3>Groq (títulos, grátis) <span class="st ${a.ai.groq_configured && !a.ai.groq_paused ? "ok" : ""}">${a.ai.groq_paused ? "Em pausa" : a.ai.groq_configured ? "Configurada" : "Sem chave"}</span></h3>
        <p class="hint">Escreve o título final dos cortes que vão para a Agenda, bem melhor que a IA local, sem custo. A escolha dos momentos continua na IA local (o plano grátis tem limite diário). ${a.ai.groq_paused ? `<br><b>Em pausa:</b> ${esc(a.ai.groq_pause_reason)}. Enquanto isso a IA local escreve os títulos.` : ""}${a.ai.groq_model ? `<br>Modelo em uso: <code>${esc(a.ai.groq_model)}</code>` : ""}</p>
        <ol><li>Crie uma conta grátis em <a href="https://console.groq.com/keys" target="_blank" rel="noopener">console.groq.com/keys</a> (dá para entrar com o Google).</li>
        <li>Clique em <b>Create API Key</b>, dê um nome (ex.: cortador) e copie a chave (começa com <code>gsk_</code>).</li>
        <li>Cole abaixo, salve e clique em <b>Testar</b>.</li></ol>
        <div class="stack">${key("GROQ_API_KEY", "Chave da Groq")}
          <div class="row-form"><button class="btn" data-save type="button">Salvar</button><button class="btn" id="groqTest" type="button" ${a.secrets.GROQ_API_KEY ? "" : "disabled"}>Testar</button></div>
          <p class="hint" id="groqResult"></p></div></section>

      <section class="acct"><h3>YouTube <span class="st ${yt.connected ? "ok" : ""}">${yt.connecting ? "Aguardando login…" : yt.connected ? "Conectado" + (yt.channel ? ": " + esc(yt.channel) : "") : "Desconectado"}</span></h3>
        <ol>
          <li>No <a href="https://console.cloud.google.com/" target="_blank" rel="noopener">Google Cloud Console</a>, crie um projeto e ative a <b>YouTube Data API v3</b>.</li>
          <li>Em “Tela de consentimento OAuth”, escolha <b>Externo</b> e adicione seu e-mail como usuário de teste.</li>
          <li>Em “Credenciais”, crie um <b>ID do cliente OAuth</b> do tipo <b>App para computador</b> e baixe o JSON.</li>
          <li>Envie o arquivo abaixo e clique em Conectar.</li>
        </ol>
        <p class="warn">Até o Google auditar seu app (formulário gratuito “YouTube API Services Audit”), os vídeos enviados pela API ficam privados. A cota padrão permite cerca de 6 envios por dia.</p>
        <div class="row-form">
          <label class="btn">${yt.has_client_secret ? "Trocar client_secret.json" : "Enviar client_secret.json"}<input type="file" accept=".json" id="ytFile" hidden></label>
          ${yt.has_client_secret && !yt.connected ? `<button class="btn primary-dl" id="ytConnect" type="button">Conectar</button>` : ""}
          ${yt.connected && !yt.analytics ? `<button class="btn primary-dl" id="ytReconnect" type="button" title="Pede também a permissão de leitura do YouTube Analytics, para o robô aprender com a retenção (quanto do Short as pessoas assistem)">Reconectar para medir retenção</button>` : ""}
          ${yt.connected ? `<button class="btn danger" id="ytOff" type="button">Desconectar</button>` : ""}
        </div>${yt.error ? `<p class="hint" style="color:var(--rec)">${esc(yt.error)}</p>` : ""}</section>

      <section class="acct"><h3>TikTok <span class="st ${tt.connected ? "ok" : ""}">${tt.connected ? "Conectado" : "Desconectado"}</span></h3>
        <ol>
          <li>Em <a href="https://developers.tiktok.com/apps" target="_blank" rel="noopener">developers.tiktok.com</a>, crie um app com plataforma <b>Desktop</b> e adicione os produtos <b>Login Kit</b> e <b>Content Posting API</b>.</li>
          <li>Em Login Kit, cadastre o Redirect URI <code>http://127.0.0.1:*${esc(tt.callback_path)}</code>.</li>
          <li>Peça os escopos <code>video.upload</code> e <code>video.publish</code>. No modo sandbox, adicione sua conta como usuária de teste.</li>
          <li>Cole o Client key e o Client secret, salve e clique em Conectar.</li>
        </ol>
        <p class="warn">Antes da auditoria do TikTok, use o modo <b>rascunho</b>: o vídeo chega nas notificações do app e você toca em publicar (máximo de 5 pendentes por dia). A postagem direta fica privada até a auditoria.</p>
        <div class="stack">${key("TIKTOK_CLIENT_KEY", "Client key")}${key("TIKTOK_CLIENT_SECRET", "Client secret")}
          <div class="row-form"><button class="btn" data-save type="button">Salvar</button>
          ${tt.has_keys && !tt.connected ? `<button class="btn primary-dl" id="ttConnect" type="button">Conectar</button>` : ""}
          ${tt.connected ? `<button class="btn danger" id="ttOff" type="button">Desconectar</button>` : ""}</div></div>
        ${tt.error ? `<p class="hint" style="color:var(--rec)">${esc(tt.error)}</p>` : ""}</section>

      <section class="acct"><h3>Telegram (legendas do TikTok) <span class="st ${a.telegram.has_chat ? "ok" : ""}">${a.telegram.has_chat ? "Conectado" : a.telegram.has_token ? "Falta detectar o chat" : "Desconectado"}</span></h3>
        <ol>
          <li>No Telegram, abra o <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a>, envie <code>/newbot</code>, escolha um nome e um usuário terminado em <code>bot</code>.</li>
          <li>Copie o token que ele mostrar, cole abaixo e salve.</li>
          <li>Abra o seu bot novo, envie <code>/start</code> e clique em <b>Detectar meu chat</b>.</li>
        </ol>
        <p class="hint">Quando um rascunho chegar no TikTok, o bot manda a legenda pronta. Toque no texto para copiar e cole no rascunho.</p>
        <div class="stack">${key("TELEGRAM_BOT_TOKEN", "Token do bot")}
          <div class="row-form"><button class="btn" data-save type="button">Salvar</button>
          ${a.telegram.has_token ? `<button class="btn" id="tgDetect" type="button">Detectar meu chat</button>` : ""}
          ${a.telegram.has_chat ? `<button class="btn primary-dl" id="tgTest" type="button">Enviar teste</button>` : ""}</div></div></section>

      <section class="acct"><h3>Twitch (opcional) <span class="st ${s.TWITCH_CLIENT_ID ? "ok" : ""}">${s.TWITCH_CLIENT_ID ? "Configurado" : "Usando título da live"}</span></h3>
        <ol><li>Em <a href="https://dev.twitch.tv/console/apps" target="_blank" rel="noopener">dev.twitch.tv/console</a>, registre um app (URL de redirecionamento <code>http://localhost</code>, categoria “Other”).</li><li>Copie o Client ID e gere um Client Secret.</li></ol>
        <div class="stack">${key("TWITCH_CLIENT_ID", "Client ID")}${key("TWITCH_CLIENT_SECRET", "Client Secret")}<button class="btn" data-save type="button">Salvar</button></div></section>

      <section class="acct"><h3>Kick (opcional) <span class="st ${s.KICK_CLIENT_ID ? "ok" : ""}">${s.KICK_CLIENT_ID ? "Configurado" : "Usando título da live"}</span></h3>
        <ol><li>Em <a href="https://kick.com/settings/developer" target="_blank" rel="noopener">kick.com/settings/developer</a> (exige verificação em duas etapas), crie um app.</li><li>Copie o Client ID e o Client Secret.</li></ol>
        <div class="stack">${key("KICK_CLIENT_ID", "Client ID")}${key("KICK_CLIENT_SECRET", "Client Secret")}<button class="btn" data-save type="button">Salvar</button></div></section>
    </div>`;
  const root = $("#page-contas");
  root.querySelectorAll("[data-save]").forEach((b) => b.addEventListener("click", async () => {
    const values = {};
    b.closest(".acct").querySelectorAll("[data-secret]").forEach((i) => { if (i.value.trim()) values[i.dataset.secret] = i.value; });
    if (!Object.keys(values).length) return toast("Preencha pelo menos um campo");
    await api("/api/accounts/secrets", { method: "POST", body: JSON.stringify({ values }) });
    toast("Chaves salvas"); robo.keys = {}; renderContas(); refreshHealth();
  }));
  $("#groqTest")?.addEventListener("click", async (e) => {
    e.target.disabled = true; $("#groqResult").textContent = "Testando…";
    try {
      const r = await api("/api/accounts/groq/test", { method: "POST" });
      $("#groqResult").innerHTML = `Funcionando (<code>${esc(r.model)}</code>). Título de exemplo: <b>${esc(r.title)}</b>`;
    } catch (err) { $("#groqResult").textContent = "Não funcionou: " + err.message; }
    e.target.disabled = false;
  });
  $("#ytFile")?.addEventListener("change", async (e) => {
    const f = e.target.files[0]; if (!f) return;
    try { await api("/api/accounts/youtube/client-secret", { method: "POST", body: JSON.stringify({ content: await f.text() }) }); toast("Arquivo salvo"); }
    catch (err) { toast(err.message); }
    robo.keys = {}; renderContas();
  });
  $("#ytConnect")?.addEventListener("click", async () => { await api("/api/accounts/youtube/connect", { method: "POST" }); toast("Faça o login na janela do Google que abriu"); robo.keys = {}; });
  $("#ytReconnect")?.addEventListener("click", async () => { await api("/api/accounts/youtube/connect", { method: "POST" }); toast("Na janela do Google, entre com a conta do canal e marque todas as permissões"); robo.keys = {}; });
  $("#ytOff")?.addEventListener("click", async () => { await api("/api/accounts/youtube/disconnect", { method: "POST" }); robo.keys = {}; renderContas(); });
  $("#ttConnect")?.addEventListener("click", async () => {
    try { const r = await api("/api/accounts/tiktok/connect"); window.open(r.url, "_blank", "noopener"); toast("Autorize na aba do TikTok e volte aqui"); }
    catch (err) { toast(err.message); }
  });
  $("#tgDetect")?.addEventListener("click", async () => {
    try { const r = await api("/api/accounts/telegram/detect", { method: "POST" }); toast(`Chat de ${r.name} conectado`); robo.keys = {}; renderContas(); }
    catch (err) { toast(err.message); }
  });
  $("#tgTest")?.addEventListener("click", async () => {
    try { await api("/api/accounts/telegram/test", { method: "POST" }); toast("Mensagem de teste enviada"); }
    catch (err) { toast(err.message); }
  });
  $("#ttOff")?.addEventListener("click", async () => { await api("/api/accounts/tiktok/disconnect", { method: "POST" }); robo.keys = {}; renderContas(); });
}

/* ---------------------------------------------------------------- revisão em tela cheia (Agenda) */
const viewer = { groups: [], idx: 0, opener: null, touchY: null };
const ICON = {
  prev: '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M6 15l6-6 6 6" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  next: '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  full: '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  close: '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>',
};

/* Um item por corte: o mesmo vídeo pode ter post no YouTube e no TikTok. */
function buildGroups(posts) {
  const map = new Map();
  for (const p of posts.filter((x) => x.status === "pending")) {
    const key = `${p.job_id}|${p.clip_name}`;
    if (!map.has(key)) map.set(key, { key, job_id: p.job_id, file: p.file, thumb: p.thumb, score: p.score, streamer: p.streamer, posts: [] });
    map.get(key).posts.push(p);
  }
  return [...map.values()];
}

async function openViewer(pid, opener) {
  const posts = await api("/api/posts");
  viewer.groups = buildGroups(posts);
  if (!viewer.groups.length) return;
  viewer.idx = Math.max(0, viewer.groups.findIndex((g) => g.posts.some((p) => p.id === pid)));
  viewer.opener = opener || null;
  let el = $("#viewer");
  if (!el) {
    el = document.createElement("div");
    el.id = "viewer";
    el.className = "viewer";
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-modal", "true");
    el.setAttribute("aria-label", "Revisão dos cortes");
    document.body.appendChild(el);
    el.addEventListener("click", viewerClick);
    el.addEventListener("touchstart", (e) => { viewer.touchY = e.touches[0].clientY; }, { passive: true });
    el.addEventListener("touchend", (e) => {
      if (viewer.touchY == null || e.target.closest(".v-info")) { viewer.touchY = null; return; }
      const dy = e.changedTouches[0].clientY - viewer.touchY;
      viewer.touchY = null;
      if (Math.abs(dy) > 70) moveViewer(dy < 0 ? 1 : -1);  // deslizar para cima = próximo
    });
  }
  el.hidden = false;
  document.documentElement.classList.add("viewer-open");
  renderViewer();
}

function renderViewer() {
  const el = $("#viewer");
  const g = viewer.groups[viewer.idx];
  if (!el || !g) { closeViewer(); return; }
  const n = viewer.groups.length;
  el.innerHTML = `
    <div class="v-stage">
      <video src="/media/${g.job_id}/${g.file}" poster="/media/${g.job_id}/${g.thumb}" controls autoplay playsinline loop></video>
    </div>
    <aside class="v-info">
      <div class="v-top">
        <span class="v-count">Corte ${viewer.idx + 1} de ${n}</span>
        <div class="v-nav">
          <button class="v-btn" type="button" data-v="prev" ${viewer.idx === 0 ? "disabled" : ""} aria-label="Corte anterior" title="Anterior">${ICON.prev}</button>
          <button class="v-btn" type="button" data-v="next" ${viewer.idx >= n - 1 ? "disabled" : ""} aria-label="Próximo corte" title="Próximo">${ICON.next}</button>
          <button class="v-btn" type="button" data-v="full" aria-label="Tela cheia" title="Tela cheia">${ICON.full}</button>
          <button class="v-btn" type="button" data-v="close" aria-label="Fechar" title="Fechar">${ICON.close}</button>
        </div>
      </div>
      <div class="v-meta">Nota <b>${g.score}</b>${g.streamer ? ` — ${esc(g.streamer)}` : ""}</div>
      ${g.posts.map((p) => `<div class="v-post"><span class="pbadge ${p.platform}">${PLAT[p.platform]}</span><p>${esc(p.platform === "tiktok" ? p.caption : p.title)}</p></div>`).join("")}
      <div class="v-actions">
        <button class="btn primary-dl" type="button" data-v="approve">Aprovar e agendar${g.posts.length > 1 ? ` (${g.posts.length})` : ""}</button>
        <button class="btn" type="button" data-v="discard">Descartar</button>
      </div>
      <p class="v-hint">Use as setas do teclado ou deslize para trocar de corte. Esc fecha. Para editar o texto, use o cartão na Agenda.</p>
    </aside>`;
  el.querySelector('[data-v="close"]').focus({ preventScroll: true });
}

function moveViewer(d) {
  const i = viewer.idx + d;
  if (i < 0 || i >= viewer.groups.length) return;
  viewer.idx = i;
  renderViewer();
}

function toggleFullscreen() {
  const el = $("#viewer");
  if (document.fullscreenElement) { document.exitFullscreen?.(); return; }
  const req = el.requestFullscreen || el.webkitRequestFullscreen;
  if (req) Promise.resolve(req.call(el)).catch(() => toast("Este navegador não permite tela cheia aqui"));
}

function closeViewer() {
  const el = $("#viewer");
  if (!el || el.hidden) return;
  if (document.fullscreenElement) document.exitFullscreen?.();
  el.hidden = true;
  el.innerHTML = "";  // para o vídeo
  document.documentElement.classList.remove("viewer-open");
  if (viewer.opener && document.contains(viewer.opener)) viewer.opener.focus({ preventScroll: true });
}

async function viewerClick(e) {
  const v = e.target.closest("[data-v]")?.dataset.v;
  if (!v) { if (e.target.id === "viewer" || e.target.classList.contains("v-stage")) closeViewer(); return; }
  if (v === "close") return closeViewer();
  if (v === "prev") return moveViewer(-1);
  if (v === "next") return moveViewer(1);
  if (v === "full") return toggleFullscreen();
  const g = viewer.groups[viewer.idx];
  const btns = $("#viewer").querySelectorAll(".v-actions button");
  btns.forEach((b) => { b.disabled = true; });
  try {
    let last = null;
    for (const p of g.posts) last = await api(`/api/posts/${p.id}/${v}`, { method: "POST" });
    if (v === "approve") {
      const w = last?.scheduled_at ? fmtWhen(last.scheduled_at) : null;
      toast(w ? `Agendado: ${w.day.toLowerCase()} às ${w.time}` : "Agendado");
    } else toast("Descartado");
    viewer.groups.splice(viewer.idx, 1);
    if (viewer.idx >= viewer.groups.length) viewer.idx = viewer.groups.length - 1;
    robo.keys = {};
    renderAgenda();
    refreshBadges();
    if (!viewer.groups.length) { toast("Todos os cortes revisados"); closeViewer(); } else renderViewer();
  } catch (err) {
    toast(err.message);
    btns.forEach((b) => { b.disabled = false; });
  }
}

document.addEventListener("keydown", (e) => {
  const el = $("#viewer");
  if (!el || el.hidden || e.target.closest?.("input, textarea, select")) return;
  if (e.key === "Escape") { e.preventDefault(); closeViewer(); }
  else if (e.key === "ArrowDown" || e.key === "ArrowRight") { e.preventDefault(); moveViewer(1); }
  else if (e.key === "ArrowUp" || e.key === "ArrowLeft") { e.preventDefault(); moveViewer(-1); }
});

/* ---------------------------------------------------------------- início */
(function initRobo() {
  let saved = "cortes";
  try { saved = localStorage.getItem("tab") || "cortes"; } catch {}
  if (location.hash.length > 1) saved = "cortes";
  showTab(saved);
  refreshBadges();
  setInterval(() => { if (robo.tab !== "cortes" && robo.tab !== "contas") refreshTab(); refreshBadges(); }, 4000);
  setInterval(() => { if (robo.tab === "contas") refreshTab(); }, 5000);
})();
