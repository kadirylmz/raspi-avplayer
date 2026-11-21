"use strict";
const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let base = "";            // "" = this device, "/remote/<id>" = remote device
let timer = null, page = "panel", devices = [];
const key = () => localStorage.getItem("avp_key") || "";

// ---------------------------------------------------------------- helpers
function toast(msg, type = "") {
  const t = document.createElement("div"); t.className = "toast " + type; t.textContent = msg;
  $("#toasts").appendChild(t); setTimeout(() => t.remove(), 5000);
}
async function api(path, opt = {}) {
  opt.headers = Object.assign({ "X-API-Key": key() }, opt.headers || {});
  if (opt.json !== undefined) { opt.body = JSON.stringify(opt.json); opt.headers["Content-Type"] = "application/json"; }
  const r = await fetch(base + path, opt);
  if (r.status === 401) {
    const k = prompt("Bu cihaz API anahtarı istiyor. Anahtarı girin:");
    if (k) { localStorage.setItem("avp_key", k); document.cookie = "avp_key=" + k + ";path=/;max-age=31536000"; return api(path, opt); }
  }
  let data = null; try { data = await r.json(); } catch (e) { }
  if (!r.ok) throw new Error((data && data.detail) || ("HTTP " + r.status));
  return data;
}
async function act(fn, ok) { try { const r = await fn(); if (ok) toast(ok, "ok"); return r; } catch (e) { toast(e.message, "err"); } }
const bytes = n => { if (!n) return "0 B"; const u = ["B", "KB", "MB", "GB", "TB"]; let i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), 4); return (n / 1024 ** i).toFixed(i ? 1 : 0) + " " + u[i]; };
const dur = s => { s = Math.round(s || 0); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s % 60).padStart(2, "0"); };
const uptime = s => { const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return (d ? d + "g " : "") + h + "sa " + m + "dk"; };
const mbps = b => b ? (b / 1e6).toFixed(1) + " Mbps" : "-";
const bar = (p, warn = 75, bad = 90) => `<div class="bar ${p >= bad ? "bad" : p >= warn ? "warn" : ""}"><i style="width:${Math.min(p, 100)}%"></i></div>`;
const STATES = { playing: ["OYNATIYOR", "ok"], paused: ["DURAKLATILDI", "warn"], starting: ["BAŞLATILIYOR", "acc"], idle: ["DURDURULDU", ""], ended: ["BİTTİ", ""], error: ["HATA", "bad"], retrying: ["TEKRAR DENENİYOR", "warn"], rebooting: ["CİHAZ YENİDEN BAŞLIYOR", "bad"], service_down: ["PLAYER SERVİSİ KAPALI", "bad"] };
const MODES = { player_restart: "PLAYER RESTART – sadece mpv süreci", service_restart: "PLAYER SERVICE RESTART – systemd servisi", device_reboot: "DEVICE REBOOT – Raspberry Pi gerçekten yeniden başlar" };
const vinfo = v => v ? `${v.width}x${v.height} · ${v.fps} fps${v.vfr ? " (VFR)" : " (CFR)"} · ${esc(v.codec).toUpperCase()} · ${mbps(v.bitrate)} · ${v.pix_fmt}` : "-";
const ainfo = a => a ? `${esc(a.codec)} · ${a.channels} kanal · ${a.sample_rate} Hz` : "ses yok";

function modal(html) { $("#modalbox").innerHTML = html; $("#modal").classList.remove("hidden"); }
function closeModal() { $("#modal").classList.add("hidden"); $("#modalbox").innerHTML = ""; }
$("#modal").addEventListener("click", e => { if (e.target.id === "modal") closeModal(); });

// ---------------------------------------------------------------- router
async function loadDevices() {
  try { devices = await (await fetch("/api/devices", { headers: { "X-API-Key": key() } })).json(); } catch (e) { devices = []; }
  const cur = $("#devsel").value || "local";
  $("#devsel").innerHTML = `<option value="local">Bu cihaz</option>` + devices.map(d => `<option value="${d.id}">${esc(d.name)}</option>`).join("");
  $("#devsel").value = [...$("#devsel").options].some(o => o.value === cur) ? cur : "local";
}
$("#devsel").onchange = () => { base = $("#devsel").value === "local" ? "" : "/remote/" + $("#devsel").value; route(); };
window.addEventListener("hashchange", route);
function route() {
  clearInterval(timer);
  page = (location.hash.match(/#\/(\w+)/) || [, "panel"])[1];
  document.querySelectorAll("nav a").forEach(a => a.classList.toggle("on", a.dataset.p === page));
  const pages = { panel, videos, devices: devicesPage, settings, logs };
  (pages[page] || panel)();
}
async function banner(st) {
  const b = [];
  if (st.system.disk.warn) b.push(`<div class="b warn">⚠ Disk neredeyse dolu (%${st.system.disk.percent}, boş ${bytes(st.system.disk.free)})</div>`);
  const t = st.system.throttled;
  if (t && (t.under_voltage || t.throttled)) b.push(`<div class="b bad">⚠ Düşük gerilim / CPU kısıtlaması algılandı – güç kaynağını kontrol edin</div>`);
  $("#banner").innerHTML = b.join("");
}
function poll(fn, ms = 2000) { fn(); timer = setInterval(fn, ms); }

// ---------------------------------------------------------------- dashboard
function panel() {
  $("#view").innerHTML = `<div id="dash"><p class="mut">Yükleniyor…</p></div>`;
  poll(async () => {
    let st;
    try { st = await api("/api/status"); $("#hdot").className = "dot ok"; } catch (e) { $("#hdot").className = "dot bad"; $("#dash").innerHTML = `<div class="card">Cihaza ulaşılamıyor: ${esc(e.message)}</div>`; return; }
    banner(st);
    if (document.activeElement && ["INPUT", "SELECT"].includes(document.activeElement.tagName) && $("#dash").contains(document.activeElement)) return;
    const p = st.player, S = p.stats || {}, sys = st.system, s = st.settings, v = st.video;
    const [lab, cls] = STATES[p.state] || [p.state, ""];
    const rs = p.reset || {};
    $("#dash").innerHTML = `
    <div class="grid">
      <div class="card" style="grid-column:1/-1">
        <div class="row sp"><div><h3>${esc(st.name)} · ${esc(sys.model)}</h3>
          <div class="big">${esc(v ? v.name : "Video seçilmedi")}</div>
          <div class="mut">${v ? vinfo(v.conv_probe ? v.conv_probe.video : v.probe && v.probe.video) : ""}</div></div>
          <div><span class="badge ${cls}" style="font-size:15px;padding:6px 14px">${lab}</span>
          ${p.message ? `<div class="sm mut" style="margin-top:6px;max-width:340px">${esc(p.message)}</div>` : ""}</div></div>
        ${S.duration ? `<div style="margin:12px 0 4px">${bar(S.time_pos / S.duration * 100, 101, 101)}<div class="sm mut row sp"><span>${dur(S.time_pos)}</span><span>${dur(S.duration)}</span></div></div>` : ""}
        <div class="row" style="margin-top:10px">
          <button class="pri" onclick="pl('play')">▶ Oynat</button>
          <button onclick="pl('stop')">■ Durdur</button>
          <button onclick="pl('restart')" title="Sadece mpv süreci yeniden başlar">↻ Oynatıcıyı Yeniden Başlat</button>
          <span class="badge">${s.loop ? "🔁 Döngü açık" : "Döngü kapalı"}</span>
          ${rs.enabled ? `<span class="badge ${rs.mode === "device_reboot" ? "bad" : "warn"}">Zamanlı reset: ${esc(rs.mode)} ${rs.remaining != null ? "· " + rs.remaining + " sn" : ""}</span>` : ""}
        </div></div>
      <div class="card"><h3>Ses</h3>
        <div class="row sp"><b>${s.mute ? "🔇 Sessiz" : "🔊 " + s.volume + "%"}</b>
        <button onclick="mute(${!s.mute})">${s.mute ? "Sesi aç" : "Sustur"}</button></div>
        <input type="range" min="0" max="130" value="${s.volume}" onchange="vol(this.value)">
        <div style="margin-top:8px"><select onchange="aout(this.value)" style="width:100%">
          ${st.audio.outputs.length ? st.audio.outputs.map(o => `<option value="${esc(o.id)}" ${o.id === st.audio.selected ? "selected" : ""}>${esc(o.label)}</option>`).join("") : "<option>Ses çıkışı bulunamadı</option>"}
        </select></div></div>
      <div class="card"><h3>Cihaz</h3><div class="kv">
        <span>IP</span><span>${sys.ips.join(", ") || "yok"}</span>
        <span>Sıcaklık</span><span>${sys.temp ?? "-"} °C</span>
        <span>Çalışma süresi</span><span>${uptime(sys.uptime)}</span>
        <span>İşletim sistemi</span><span>${esc(sys.os)}</span></div></div>
      <div class="card"><h3>CPU</h3><div class="big">${sys.cpu}%</div>${bar(sys.cpu)}</div>
      <div class="card"><h3>RAM</h3><div class="big">${sys.ram.percent}%</div>${bar(sys.ram.percent)}<div class="sm mut">${bytes(sys.ram.used)} / ${bytes(sys.ram.total)}</div></div>
      <div class="card"><h3>Disk</h3><div class="big">${sys.disk.percent}%</div>${bar(sys.disk.percent)}<div class="sm mut">Boş ${bytes(sys.disk.free)} / ${bytes(sys.disk.total)}</div></div>
      <div class="card"><h3>Oynatma istatistikleri</h3><div class="kv">
        <span>Decoder</span><span>${esc(S.hwdec && S.hwdec !== "no" ? "Donanım: " + S.hwdec : S.hwdec === "no" ? "Yazılım" : "-")}</span>
        <span>Atlanan kare (VO)</span><span>${S.drops_vo ?? "-"}</span>
        <span>Atlanan kare (decoder)</span><span>${S.drops_decoder ?? "-"}</span>
        <span>Decoder hataları</span><span>${p.decoder_errors ?? 0}</span>
        <span>Buffer</span><span>${S.cache_s != null ? S.cache_s.toFixed(1) + " sn" : "-"}</span>
        <span>A/V sync</span><span>${S.avsync != null ? (S.avsync * 1000).toFixed(0) + " ms" : "-"}</span>
        <span>Gerçek FPS</span><span>${S.fps_est ? S.fps_est.toFixed(1) : "-"} / ${S.container_fps ?? "-"}</span>
        <span>mpv bellek</span><span>${S.rss_mb ? S.rss_mb + " MB" : "-"}</span>
        <span>Oynatıcı başlatma / hata</span><span>${p.restarts ?? 0} / ${p.fails ?? 0}</span></div>
        ${p.hwdec_forced_off ? `<div class="sm" style="color:var(--warn);margin-top:6px">Donanım decode hataları nedeniyle yazılımsal decode'a geçildi</div>` : ""}</div>
    </div>`;
  });
}
const pl = a => act(() => api("/api/player/" + a, { method: "POST", json: {} }), a === "play" ? "Oynatma başlatıldı" : a === "stop" ? "Durduruldu" : "Oynatıcı yeniden başlatılıyor");
const vol = v => act(() => api("/api/player/volume", { method: "POST", json: { volume: +v } }));
const mute = m => act(() => api("/api/player/volume", { method: "POST", json: { mute: m } }));
const aout = id => act(() => api("/api/settings", { method: "PUT", json: { values: { audio_output: id } } }), "Ses çıkışı değiştirildi");

// ---------------------------------------------------------------- videos
let vtargets = new Set();
function videos() {
  $("#view").innerHTML = `<h2>Videolar</h2>
  <div class="card" style="margin-bottom:14px"><div class="drop" id="drop">
    Videoları buraya sürükleyin veya <button onclick="$('#file').click()">dosya seçin</button>
    <input type="file" id="file" multiple accept="video/*,.mkv,.ts,.mxf" hidden>
    <div id="targets" class="sm" style="margin-top:10px"></div></div>
    <div id="uploads"></div></div>
  <div id="vlist"></div><div id="jobs"></div>`;
  const dz = $("#drop");
  dz.ondragover = e => { e.preventDefault(); dz.classList.add("over"); };
  dz.ondragleave = () => dz.classList.remove("over");
  dz.ondrop = e => { e.preventDefault(); dz.classList.remove("over"); upload([...e.dataTransfer.files]); };
  $("#file").onchange = e => { upload([...e.target.files]); e.target.value = ""; };
  $("#targets").innerHTML = devices.length ? `Hedef cihazlar: <label><input type="checkbox" checked disabled> ${base ? "Seçili cihaz" : "Bu cihaz"}</label> ` +
    devices.filter(d => base !== "/remote/" + d.id).map(d => `<label style="margin-left:10px"><input type="checkbox" onchange="tgt(${d.id},this.checked)" ${vtargets.has(d.id) ? "checked" : ""}> ${esc(d.name)}</label>`).join("") : "";
  poll(async () => {
    try {
      const [vs, js, st] = await Promise.all([api("/api/videos"), api("/api/jobs"), api("/api/status")]);
      banner(st); $("#hdot").className = "dot ok";
      renderVideos(vs); renderJobs(js);
    } catch (e) { $("#hdot").className = "dot bad"; }
  });
}
const tgt = (id, on) => on ? vtargets.add(id) : vtargets.delete(id);
function renderVideos(vs) {
  $("#vlist").innerHTML = vs.length ? `<div class="grid">${vs.map(v => {
    const p = v.probe, c = v.conv_probe, st = { ready: ["HAZIR", "ok"], needs_conversion: ["DÖNÜŞTÜRME GEREKLİ", "warn"], converting: ["DÖNÜŞTÜRÜLÜYOR", "acc"], corrupt: ["BOZUK", "bad"] }[v.status] || [v.status, ""];
    return `<div class="card"><div class="row sp"><b style="word-break:break-all">${esc(v.name)}</b>${v.active ? `<span class="badge ok">AKTİF</span>` : ""}</div>
    <div style="margin:6px 0"><span class="badge ${st[1]}">${st[0]}</span> ${v.converted ? `<span class="badge acc">Optimize (${esc(v.conv_profile)})</span>` : ""}</div>
    ${v.error ? `<div class="sm" style="color:var(--err)">${esc(v.error)}</div>` : ""}
    <div class="kv sm"><span>Kaynak</span><span>${p ? vinfo(p.video) : "-"}</span>
      <span>Ses</span><span>${p ? ainfo(p.audio) : "-"}</span>
      <span>Süre / Boyut</span><span>${p ? dur(p.duration) : "-"} · ${bytes(v.size)}</span>
      ${c ? `<span>Optimize</span><span>${vinfo(c.video)}<br>${ainfo(c.audio)} · ${bytes(c.size)}</span>` : ""}</div>
    ${v.job ? `<div style="margin:8px 0">${bar(v.job.progress, 101, 101)}<div class="sm mut">${v.job.state === "queued" ? "Sırada" : "%" + v.job.progress}</div></div>` : ""}
    <div class="row" style="margin-top:10px">
      <button class="pri" onclick="activate(${v.id})" ${v.status === "corrupt" || v.status === "converting" ? "disabled" : ""}>Aktif yap</button>
      <button onclick="preview(${v.id})">Önizle</button>
      <button onclick="convDlg(${v.id})" ${v.status === "converting" ? "disabled" : ""}>Dönüştür</button>
      <button onclick="replaceVideo(${v.id})">Değiştir</button>
      <button class="dng" onclick="delVideo(${v.id},'${esc(v.name).replace(/'/g, "")}')">Sil</button></div></div>`;
  }).join("")}</div>` : `<div class="card mut">Henüz video yok.</div>`;
}
function renderJobs(js) {
  $("#jobs").innerHTML = js.length ? `<div class="row sp" style="margin-top:20px"><h3 style="margin:0">Dönüştürme geçmişi</h3><button class="dng" onclick="clearJobs()">Geçmişi Temizle</button></div><div class="card" style="margin-top:8px"><table><tr><th>#</th><th>Video</th><th>Profil</th><th>Durum</th><th>Mesaj</th></tr>
  ${js.slice(0, 12).map(j => `<tr><td>${j.id}</td><td>${esc(j.name || "-")}</td><td>${esc(j.profile)}</td><td>${j.state}${j.state === "running" ? " %" + j.progress : ""}${["queued", "running"].includes(j.state) ? ` <button onclick="cancelJob(${j.id})">İptal</button>` : ""}</td><td class="sm">${esc(j.message || "")}</td></tr>`).join("")}</table></div>` : "";
}
const clearJobs = () => confirm("Tamamlanmış/iptal edilmiş tüm dönüştürme geçmişi silinsin mi?") && act(() => api("/api/jobs/clear", { method: "POST" }), "Dönüştürme geçmişi temizlendi");
const activate = (id, force) => act(async () => {
  try { await api(`/api/videos/${id}/activate`, { method: "POST", json: { force: !!force } }); toast("Aktif video ayarlandı", "ok"); }
  catch (e) { if (/dönüştürül/.test(e.message) && confirm(e.message + "\n\nYine de dönüştürmeden oynatılsın mı? (akıcılık garanti değil)")) return activate(id, true); throw e; }
});
const delVideo = (id, n) => confirm(`"${n}" silinsin mi? Orijinal ve dönüştürülmüş dosya kaldırılır.`) && act(() => api("/api/videos/" + id, { method: "DELETE" }), "Silindi");
const cancelJob = id => act(() => api(`/api/jobs/${id}/cancel`, { method: "POST" }), "İptal istendi");
function preview(id) {
  modal(`<div class="row sp"><h2>Önizleme</h2><button onclick="closeModal()">Kapat</button></div>
  <video controls autoplay muted src="${base}/media/${id}"></video>
  <p class="sm mut">Tarayıcı HEVC oynatamayabilir; bu durumda cihazda oynatma yine çalışır.</p>`);
}
function replaceVideo(id) {
  const f = document.createElement("input"); f.type = "file"; f.accept = "video/*,.mkv,.ts";
  f.onchange = () => upload([f.files[0]], id); f.click();
}

function sendFile(b, file, replaceId, onp) {
  return new Promise((res, rej) => {
    const x = new XMLHttpRequest();
    x.open("PUT", `${b}/api/videos/upload?filename=${encodeURIComponent(file.name)}${replaceId ? "&replace_id=" + replaceId : ""}`);
    x.setRequestHeader("X-API-Key", key());
    x.upload.onprogress = e => e.lengthComputable && onp(e.loaded / e.total * 100);
    x.onload = () => { let d = {}; try { d = JSON.parse(x.responseText); } catch (e) { } x.status < 300 ? res(d) : rej(new Error(d.detail || "HTTP " + x.status)); };
    x.onerror = () => rej(new Error("Ağ hatası")); x.send(file);
  });
}
async function upload(files, replaceId) {
  const dests = [base, ...[...vtargets].map(id => "/remote/" + id)].filter((v, i, a) => a.indexOf(v) === i);
  const box = $("#uploads") || document.body;
  for (const file of files) {
    const row = document.createElement("div"); row.className = "sm"; row.style.margin = "8px 0";
    row.innerHTML = `<div>${esc(file.name)} (${bytes(file.size)})</div>`; box.appendChild(row);
    const ps = dests.map(async (b, i) => {
      const line = document.createElement("div"); row.appendChild(line);
      const name = b === "" ? "Bu cihaz" : (devices.find(d => "/remote/" + d.id === b) || {}).name || "Cihaz";
      try {
        const d = await sendFile(b, file, b === base ? replaceId : null, p => line.innerHTML = `${esc(name)}: ${bar(p, 101, 101)}<span class="mut">%${p.toFixed(0)}</span>`);
        line.innerHTML = `${esc(name)}: ✔ yüklendi (${d.status === "ready" ? "uyumlu, hazır" : "dönüştürme önerilir"})`;
        if (b === base && d.status !== "ready" && files.length === 1) convDlg(d.id);
      } catch (e) { line.innerHTML = `${esc(name)}: <span style="color:var(--err)">✘ ${esc(e.message)}</span>`; }
    });
    await Promise.all(ps);
  }
}

// conversion dialog
let convState = { id: 0, profile: "auto" };
const PROFS = [["auto", "1. Otomatik optimizasyon", "Pi modeline, ekrana ve kaynağa göre en iyi format (1920x1200 ve 4K için HEVC donanım decode)"],
["keep", "2. Orijinal kaliteyi koru", "Mümkünse kopyala/remux; çözünürlüğü korur (1080p üstünü Pi 4/5 HEVC donanım çözücüsüne hazırlar)"],
["1080p", "3. 1080p (1920x1080) optimize et", "16:9 ekranlar için 1920x1080 H.264 (Pi 4 & Pi 5 tam uyumlu)"],
["1200p", "4. WUXGA (1920x1200) optimize et", "16:10 projeksiyon/monitörler için 1920x1200 (Pi 4'te HEVC donanım decode, Pi 5'te H.264/HEVC)"],
["4k", "5. 4K (3840x2160) optimize et", "3840x2160 HEVC (H.265) — Pi 4 ve Pi 5 4K donanım decoder çipiyle %10-15 CPU ile oynatır"],
["custom", "6. Özel profil", "Kodek, maksimum çözünürlük/FPS, CRF/bitrate kendiniz seçin"]];
function convDlg(id) { convState = { id, profile: "auto" }; drawConv(); }
async function drawConv() {
  const { id, profile } = convState;
  const custom = profile === "custom" ? getCustom() : null;
  let d, err = "";
  try { d = await api(`/api/videos/${id}/plan?profile=${profile}` + (custom ? "&custom=" + encodeURIComponent(JSON.stringify(custom)) : "")); } catch (e) { err = e.message; }
  const keepc = custom || { codec: "h264", max_w: "", max_h: "", max_fps: "", crf: "", bitrate_k: "" };
  modal(`<div class="row sp"><h2>Dönüştürme</h2><button onclick="closeModal()">Kapat</button></div>
  ${PROFS.map(p => `<label class="opt ${p[0] === profile ? "on" : ""}"><input type="radio" name="pf" ${p[0] === profile ? "checked" : ""} onchange="convState.profile='${p[0]}';drawConv()"><span><b>${p[1]}</b><br><span class="sm mut">${p[2]}</span></span></label>`).join("")}
  ${profile === "custom" ? `<div class="row" id="cust"><select id="c_codec" onchange="drawConv()"><option value="h264" ${keepc.codec === "h264" ? "selected" : ""}>H.264</option><option value="hevc" ${keepc.codec === "hevc" ? "selected" : ""}>HEVC</option></select>
    <input id="c_w" placeholder="Max genişlik" size="10" value="${keepc.max_w || ""}"><input id="c_h" placeholder="Max yükseklik" size="10" value="${keepc.max_h || ""}">
    <input id="c_fps" placeholder="Max FPS" size="7" value="${keepc.max_fps || ""}"><input id="c_crf" placeholder="CRF" size="5" value="${keepc.crf || ""}"><input id="c_br" placeholder="Bitrate kbps" size="9" value="${keepc.bitrate_k || ""}">
    <button onclick="drawConv()">Planı güncelle</button></div>` : ""}
  ${err ? `<p style="color:var(--err)">${esc(err)}</p>` : d ? `
  <h3 style="margin-top:14px">Önce → Sonra (${esc(d.model.toUpperCase())})</h3>
  <table><tr><th></th><th>Kaynak</th><th>Hedef</th></tr>
  <tr><td>Video</td><td>${vinfo(d.before.video)}</td><td>${d.plan.action === "copy" ? "Aynı (dönüştürme yok)" : d.plan.action === "remux" ? "Aynı video (remux)" : `${d.plan.vcodec.toUpperCase()} ${d.plan.width}x${d.plan.height} · ${d.plan.fps} fps CFR · yuv420p`}</td></tr>
  <tr><td>Ses</td><td>${ainfo(d.before.audio)}</td><td>${{ none: "-", copy: "Aynı", aac: "AAC " }[d.plan.audio.action]}${d.plan.audio.action === "aac" ? d.plan.audio.bitrate_k + "k " + (d.plan.audio.channels === 1 ? "mono" : "stereo") : ""}</td></tr>
  <tr><td>Container</td><td>${esc(d.before.container.split(",")[0])}</td><td>MP4 (faststart)</td></tr></table>
  <p><b>${esc(d.plan.summary)}</b></p>
  ${d.plan.reasons.map(r => `<div class="sm">• ${esc(r)}</div>`).join("")}
  ${d.plan.warnings.map(r => `<div class="sm" style="color:var(--warn)">⚠ ${esc(r)}</div>`).join("")}
  ${d.after ? `<p class="sm mut">Mevcut optimize dosya: ${vinfo(d.after.video)}</p>` : ""}
  <div class="row" style="margin-top:14px"><button class="pri" onclick="startConv()">${d.plan.action === "copy" ? "Orijinali kullan" : "Dönüştürmeyi başlat"}</button></div>` : ""}`);
}
function getCustom() {
  const n = id => { const v = $(id) && $(id).value; return v ? +v : undefined; };
  return { codec: $("#c_codec") ? $("#c_codec").value : "h264", max_w: n("#c_w"), max_h: n("#c_h"), max_fps: n("#c_fps"), crf: n("#c_crf"), bitrate_k: n("#c_br") };
}
async function startConv() {
  const r = await act(() => api(`/api/videos/${convState.id}/convert`, { method: "POST", json: { profile: convState.profile, custom: convState.profile === "custom" ? getCustom() : null } }));
  if (r) { toast(r.job ? "Dönüştürme kuyruğa alındı" : "Orijinal dosya kullanılacak", "ok"); closeModal(); }
}

// ---------------------------------------------------------------- devices
const hub = async (p, o) => { const b = base; base = ""; try { return await api(p, o); } finally { base = b; } };
function devicesPage() {
  $("#view").innerHTML = `<h2>Cihazlar</h2>
  <div class="card" style="margin-bottom:14px"><h3>Yeni Raspberry Pi ekle</h3><div class="row">
    <input id="dn" placeholder="Ad (ör. Salon-1)"><input id="du" placeholder="IP veya adres (ör. 192.168.1.211)"><input id="dk" placeholder="API anahtarı (Ayarlar sayfasında)">
    <button class="pri" onclick="addDev()">Ekle</button></div></div>
  <div id="ovl" class="grid"></div>`;
  poll(async () => {
    let ov = []; try { ov = await hub("/api/devices/overview"); } catch (e) { }
    const local = await hub("/api/status");
    const cards = [{ id: 0, name: local.name + " (bu cihaz)", online: true, status: local }, ...ov];
    $("#ovl").innerHTML = cards.map(c => {
      if (!c.online) return `<div class="card"><b>${esc(c.name)}</b> <span class="badge bad">ÇEVRİMDIŞI</span><div class="sm mut">${esc(c.url || "")} · ${esc(c.error || "")}</div><button class="dng" onclick="delDev(${c.id})" style="margin-top:8px">Kaldır</button></div>`;
      const s = c.status, sys = s.system, p = s.player, [lab, cls] = STATES[p.state] || [p.state, ""];
      return `<div class="card"><div class="row sp"><b>${esc(c.name)}</b><span class="badge ${cls}">${lab}</span></div>
      <div class="kv sm" style="margin-top:8px"><span>IP</span><span>${sys.ips.join(", ")}</span><span>Model</span><span>${esc(sys.model)}</span><span>OS</span><span>${esc(sys.os)}</span>
      <span>Sıcaklık</span><span>${sys.temp ?? "-"} °C</span><span>RAM</span><span>%${sys.ram.percent}</span><span>Disk</span><span>%${sys.disk.percent} (boş ${bytes(sys.disk.free)})</span>
      <span>Aktif video</span><span>${esc(s.video ? s.video.name : "-")}</span>
      <span>Ses çıkışı</span><span>${esc((s.audio.outputs.find(o => o.id === s.audio.selected) || {}).label || "-")}</span>
      <span>Çalışma</span><span>${uptime(sys.uptime)}</span></div>
      <div class="row" style="margin-top:10px"><button onclick="manage(${c.id})">Yönet</button>${c.id ? `<button class="dng" onclick="delDev(${c.id})">Kaldır</button>` : ""}</div></div>`;
    }).join("");
  }, 3000);
}
const manage = async id => { await loadDevices(); $("#devsel").value = id ? String(id) : "local"; base = id ? "/remote/" + id : ""; location.hash = "#/panel"; };
const addDev = () => act(async () => {
  await hub("/api/devices", { method: "POST", json: { name: $("#dn").value, url: $("#du").value, api_key: $("#dk").value } });
  await loadDevices(); devicesPage();
}, "Cihaz eklendi");
const delDev = id => confirm("Cihaz panelden kaldırılsın mı? (cihazın kendisi etkilenmez)") && act(async () => { await hub("/api/devices/" + id, { method: "DELETE" }); await loadDevices(); devicesPage(); });

// ---------------------------------------------------------------- settings
async function settings() {
  clearInterval(timer);
  const [s, a, hw, k] = await Promise.all([api("/api/settings"), api("/api/audio/outputs"), api("/api/hardware").catch(() => null), api("/api/settings/api-key").catch(() => ({}))]);
  const delays = [30, 60, 90, 120];
  const isCustom = !delays.includes(s.reset_delay);
  const chk = (id, v) => `<input type="checkbox" id="${id}" ${v ? "checked" : ""}>`;
  $("#view").innerHTML = `<h2>Ayarlar</h2><div class="grid">
  <div class="card"><h3>Genel</h3>
    <p>Cihaz adı<br><input id="s_name" value="${esc(s.device_name)}"></p>
    <p><label>${chk("s_boot", s.boot_autoplay)} Açılışta otomatik oynat (elektrik gelince)</label></p>
    <p><label>${chk("s_auto", s.autoplay)} Video seçilince otomatik oynat</label></p>
    <p><label>${chk("s_loop", s.loop)} Döngü (video bitince tekrar)</label></p>
    <p>Video senkron modu<br><select id="s_sync"><option value="audio" ${s.video_sync === "audio" ? "selected" : ""}>audio (önerilen, en kararlı)</option><option value="display-resample" ${s.video_sync === "display-resample" ? "selected" : ""}>display-resample (24/25 fps için düzgün pacing, daha yüksek CPU)</option></select></p>
    <p>Decoder<br><select id="s_hwdec"><option value="auto" ${s.hwdec === "auto" ? "selected" : ""}>Otomatik (donanım, hata olursa yazılım)</option><option value="no" ${s.hwdec === "no" ? "selected" : ""}>Sadece yazılım</option></select></p>
    <p><label>${chk("s_hwenc", s.prefer_hw_encode)} Donanım encoder'ı (test geçerse) kullan</label></p>
    <button class="pri" onclick="saveGeneral()">Kaydet</button></div>

  <div class="card"><h3>Ses çıkışı</h3>
    <label class="opt ${s.audio_output === "auto" ? "on" : ""}"><input type="radio" name="ao" value="auto" ${s.audio_output === "auto" ? "checked" : ""}><span>Otomatik (USB › HDMI › Analog)</span></label>
    ${a.outputs.map(o => `<label class="opt ${s.audio_output === o.id ? "on" : ""}"><input type="radio" name="ao" value="${esc(o.id)}" ${s.audio_output === o.id ? "checked" : ""}><span>${esc(o.label)}</span></label>`).join("")}
    ${a.outputs.length ? "" : `<p class="mut">Ses cihazı bulunamadı.</p>`}
    <p class="sm mut">Sadece bu cihazda gerçekten bulunan çıkışlar listelenir. Pi 5'te 3.5 mm jak yoktur.</p>
    <button class="pri" onclick="saveAudio()">Uygula</button></div>

  <div class="card" style="grid-column:1/-1"><h3>Zamanlı Reset</h3>
    <p><label>${chk("r_en", s.reset_enabled)} Video başladıktan sonra otomatik reset etkin</label></p>
    <p><label>${chk("r_osd", s.reset_osd !== false)} HDMI ekranda yeniden başlatma geri sayım uyarısını göster</label></p>
    <p>Ekranda görünecek uyarı metni:<br>
      <input id="r_txt" style="width:100%;max-width:560px" value="${esc(s.reset_osd_text || "{saniye} saniye sonra sistem yeniden başlatılacaktır")}">
      <br><span class="sm mut">İpucu: Metin içinde <code>{saniye}</code> (kalan sn) veya <code>{sure}</code> (ör. "1 dakika" / "45 saniye") kullanabilirsiniz. Hiçbirini yazmazsanız sonuna otomatik <code>(60 sn)</code> eklenir.</span></p>
    ${[["player_restart", "A) PLAYER RESTART", "Sadece mpv oynatma süreci yeniden başlar. Cihaz ve servis çalışmaya devam eder."], ["service_restart", "B) PLAYER SERVICE RESTART", "avplayer-player systemd servisi yeniden başlar ve video tekrar başlatılır."], ["device_reboot", "C) DEVICE REBOOT", "Raspberry Pi işletim sistemi GERÇEKTEN yeniden başlar. Video kesilir, açılışta otomatik başlar."]].map(m => `
      <label class="opt reset-mode ${m[0] === "device_reboot" ? "reboot" : ""} ${s.reset_mode === m[0] ? "on" : ""}"><input type="radio" name="rm" value="${m[0]}" ${s.reset_mode === m[0] ? "checked" : ""}><span><b>${m[1]}</b><br><span class="sm mut">${m[2]}</span></span></label>`).join("")}
    <div class="row"><span>Süre:</span><select id="r_delay" onchange="$('#r_cust').style.display=this.value==='c'?'':'none'">
      ${delays.map(d => `<option value="${d}" ${s.reset_delay === d ? "selected" : ""}>${d} saniye</option>`).join("")}<option value="c" ${isCustom ? "selected" : ""}>Özel süre</option></select>
      <input id="r_cust" type="number" min="5" value="${s.reset_delay}" style="width:100px;${isCustom ? "" : "display:none"}"> <span class="mut sm">sn</span>
      <span style="margin-left:14px">Tekrar:</span><select id="r_rep"><option value="once" ${s.reset_repeat === "once" ? "selected" : ""}>Her açılışta 1 kez (fiş takıldığında / elektrik gelince 1 kez çalışır)</option><option value="periodic" ${s.reset_repeat === "periodic" ? "selected" : ""}>Periyodik (sürekli her X saniyede bir tekrarlar)</option></select></div>
    <p class="sm mut">Her açılışta 1 kez: Ayar kalıcı olarak açık kalır; elektrik her kesilip geldiğinde (veya cihaz her açıldığında) video başladıktan 60 sn sonra 1 kez yeniden başlatır ve bir sonraki elektrik kesintisine kadar kesintisiz oynatır.</p>
    <div class="row"><button class="pri" onclick="saveReset()">Kaydet</button>
    <button onclick="pl('restart')">Şimdi: Player Restart</button><button onclick="pl('restart_service')">Şimdi: Servis Restart</button>
    <button class="dng" onclick="rebootNow()">Şimdi: Cihazı Reboot Et</button></div></div>

  <div class="card"><h3>Güvenlik / Ağ</h3>
    <p><label>${chk("a_en", s.auth_enabled)} Panel erişimi için API anahtarı iste</label></p>
    <p>Bu cihazın API anahtarı (başka panelden eklemek için):<br><code style="user-select:all">${esc(k.api_key || "")}</code></p>
    <div class="row"><button class="pri" onclick="saveAuth()">Kaydet</button><button onclick="regenKey()">Anahtarı yenile</button></div></div>

  <div class="card"><h3>Donanım testi</h3>${hw ? hwHtml(hw) : "<p class='mut'>Alınamadı</p>"}
    <button onclick="retest(this)">Donanımı yeniden test et</button></div></div>`;
  document.querySelectorAll("label.opt input[type=radio]").forEach(r => r.onchange = () => { document.querySelectorAll(`input[name=${r.name}]`).forEach(x => x.closest("label").classList.toggle("on", x.checked)); });
}
function hwHtml(hw) {
  const c = hw.caps, e = c.encoders || {}, d = c.decode || {}, disp = hw.display;
  const yn = (ok, note) => `<span class="badge ${ok ? "ok" : "bad"}">${ok ? "ÇALIŞIYOR" : "YOK/ÇALIŞMIYOR"}</span> <span class="sm mut">${esc(note || "")}</span>`;
  return `<div class="kv sm"><span>Model</span><span>${esc(c.model_name)} (${hw.model})</span>
  <span>FFmpeg</span><span>${esc(c.ffmpeg)}</span><span>mpv</span><span>${esc(c.mpv)}</span>
  <span>libx264</span><span>${yn(e.libx264 && e.libx264.works, e.libx264 && e.libx264.note)}</span>
  <span>libx265</span><span>${yn(e.libx265 && e.libx265.works, e.libx265 && e.libx265.note)}</span>
  <span>H.264 HW encode</span><span>${yn(e.h264_v4l2m2m && e.h264_v4l2m2m.works, e.h264_v4l2m2m && e.h264_v4l2m2m.note)}</span>
  <span>H.264 HW decode</span><span>${yn(d.h264_hw && d.h264_hw.works, d.h264_hw && d.h264_hw.note)}</span>
  <span>HEVC HW decode</span><span>${yn(d.hevc_hw && d.hevc_hw.works, d.hevc_hw && d.hevc_hw.note)}</span>
  <span>HDMI ekran</span><span>${disp.connected ? `${disp.max_w}x${disp.max_h} (maks. mod)` : "bağlı ekran algılanmadı"}</span></div>`;
}
const retest = b => { b.disabled = true; b.textContent = "Test ediliyor…"; act(() => api("/api/hardware/retest", { method: "POST" }), "Test tamamlandı").finally(settings); };
async function put(values, msg, extra) { return act(() => api("/api/settings", { method: "PUT", json: Object.assign({ values }, extra || {}) }), msg || "Kaydedildi"); }
const saveGeneral = () => put({ device_name: $("#s_name").value, boot_autoplay: $("#s_boot").checked, autoplay: $("#s_auto").checked, loop: $("#s_loop").checked, video_sync: $("#s_sync").value, hwdec: $("#s_hwdec").value, prefer_hw_encode: $("#s_hwenc").checked });
const saveAudio = () => put({ audio_output: document.querySelector("input[name=ao]:checked").value }, "Ses çıkışı uygulandı");
async function saveReset() {
  const sel = $("#r_delay").value, mode = document.querySelector("input[name=rm]:checked").value, rep = $("#r_rep").value, en = $("#r_en").checked, osd = $("#r_osd").checked, txt = $("#r_txt").value;
  const values = { reset_enabled: en, reset_osd: osd, reset_osd_text: txt, reset_mode: mode, reset_delay: sel === "c" ? +$("#r_cust").value : +sel, reset_repeat: rep };
  let confirmReboot = false;
  if (en && mode === "device_reboot") {
    if (!confirm("DİKKAT: Seçili mod CİHAZ REBOOT. Raspberry Pi gerçekten yeniden başlayacak ve video kesilecek." + (rep === "periodic" ? "\n\nPeriyodik modda cihaz her açılışta tekrar reboot eder (sürekli döngü)!" : ""))) return;
    confirmReboot = true;
  }
  put(values, "Reset ayarı kaydedildi: " + MODES[mode], { confirm_reboot: confirmReboot });
}
const saveAuth = () => put({ auth_enabled: $("#a_en").checked }, "Kaydedildi");
const regenKey = () => confirm("Anahtar yenilensin mi? Bu cihazı ekleyen paneller yeni anahtar girmeli.") && act(async () => { await api("/api/settings/api-key/regenerate", { method: "POST" }); settings(); });
const rebootNow = () => confirm("Raspberry Pi şimdi yeniden başlatılsın mı?") && act(() => api("/api/player/reboot", { method: "POST", json: { confirm: true } }), "Cihaz yeniden başlatılıyor");

// ---------------------------------------------------------------- logs
function logs() {
  $("#view").innerHTML = `<h2>Log ve Hata Kayıtları</h2><div class="card"><div class="row" style="margin-bottom:10px">
    <select id="lv"><option value="">Tüm seviyeler</option><option>WARNING</option><option>ERROR</option></select>
    <select id="ls"><option value="">Tüm kaynaklar</option><option>player</option><option>video</option><option>convert</option><option>web</option><option>api</option><option>devices</option><option>hardware</option></select>
    <a class="btn" href="${base}/api/logs/download" target="_blank">İndir</a>
    <button class="dng" id="clrlogs">Tüm Logları Temizle</button></div><div id="ll"></div></div>`;
  const load = async () => {
    try {
      const r = await api(`/api/logs?level=${$("#lv").value}&source=${$("#ls").value}&limit=400`);
      $("#ll").innerHTML = r.map(l => `<div class="logline lv-${l.level}">${new Date(l.ts * 1000).toLocaleString("tr-TR")} <b>${l.level}</b> [${esc(l.source)}] ${esc(l.msg)}</div>`).join("") || "<span class='mut'>Kayıt yok</span>";
    } catch (e) { $("#ll").textContent = e.message; }
  };
  $("#clrlogs").onclick = () => confirm("Tüm sistem logları ve hata kayıtları silinsin mi?") && act(() => api("/api/logs/clear", { method: "POST" }), "Tüm loglar temizlendi").then(load);
  $("#lv").onchange = $("#ls").onchange = load; poll(load, 4000);
}

loadDevices().then(route);
