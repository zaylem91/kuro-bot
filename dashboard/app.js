"use strict";
/* ╔══════════════════════════════════════════════════════════════════╗
   ║  Scanlation — Dashboard App v2 (Hardened)                      ║
   ║  • Response caching & request dedup (no duplicate fetches)      ║
   ║  • AbortController + 15s timeout on every request               ║
   ║  • Event delegation (zero window.* globals, zero inline JS)     ║
   ║  • Visibility API (stop polling hidden tabs)                    ║
   ║  • Online/offline detection with auto-reconnect                 ║
   ║  • Debounced search inputs                                      ║
   ║  • 30-min idle auto-logout                                      ║
   ║  • IIFE scope (nothing leaks to window)                         ║
   ╚══════════════════════════════════════════════════════════════════╝ */
(function(){

/* ═══════════ CONFIG ═══════════ */
const TIMEOUT_MS   = 15000;   // max wait per request
const CACHE_TTL    = 25000;   // default GET cache lifetime
const IDLE_LIMIT   = 30*60*1000; // auto-logout after 30 min idle
const REFRESH_MS   = 30000;   // overview auto-refresh interval
const DEBOUNCE_MS  = 400;     // search input debounce
const TOAST_MS     = 4000;    // toast auto-dismiss

/* ═══════════ i18n ENGINE ═══════════ */
const LOCALES = window.DASH_LOCALES || {};
const DEFAULT_LANG = "en";
let LANG = localStorage.getItem("dash_lang") || DEFAULT_LANG;
if (!LOCALES[LANG]) {
  LANG = Object.keys(LOCALES)[0] || DEFAULT_LANG;
}

const getLocale = (l = LANG) => LOCALES[l] || LOCALES[DEFAULT_LANG] || LOCALES.en || LOCALES.ar || {};

/**
 * Translate a key with optional parameters (e.g. {n}, {id}, {act}).
 * Falls back to English, then Arabic, then key itself if missing.
 */
const t = (key, params) => {
  const cur = (getLocale(LANG).translations) || {};
  const en = (LOCALES.en && LOCALES.en.translations) || {};
  const ar = (LOCALES.ar && LOCALES.ar.translations) || {};
  let val = cur[key] != null ? cur[key] : (en[key] != null ? en[key] : (ar[key] != null ? ar[key] : key));
  if (params && typeof val === "string") {
    for (const [k, v] of Object.entries(params)) {
      val = val.replaceAll("{" + k + "}", v);
    }
  }
  return val;
};

const tStatus = (status) => {
  const cur = (getLocale(LANG).statuses) || {};
  const en = (LOCALES.en && LOCALES.en.statuses) || {};
  const ar = (LOCALES.ar && LOCALES.ar.statuses) || {};
  return cur[status] || en[status] || ar[status] || status;
};

const tAction = (action) => {
  const cur = (getLocale(LANG).actions) || {};
  const en = (LOCALES.en && LOCALES.en.actions) || {};
  const ar = (LOCALES.ar && LOCALES.ar.actions) || {};
  return cur[action] || en[action] || ar[action] || action;
};

const tRole = (role) => {
  const cur = (getLocale(LANG).roles) || {};
  const en = (LOCALES.en && LOCALES.en.roles) || {};
  const ar = (LOCALES.ar && LOCALES.ar.roles) || {};
  return cur[role] || en[role] || ar[role] || role;
};

// Proxy I object for seamless backwards-compat with any legacy I[LANG][key] access
const I = new Proxy({}, {
  get: () => new Proxy({}, {
    get: (_, key) => t(key)
  })
});
const statusMap = new Proxy({}, {
  get: () => new Proxy({}, {
    get: (_, key) => tStatus(key)
  })
});
const actionMap = new Proxy({}, {
  get: () => new Proxy({}, {
    get: (_, key) => tAction(key)
  })
});
const roleMap = new Proxy({}, {
  get: () => new Proxy({}, {
    get: (_, key) => tRole(key)
  })
});

const pageTitles = {overview:"pgOverview", mangas:"pgMangas", chapters:"pgCh", staff:"pgStaff", finance:"pgFin", jobs:"pgJobs", database:"pgDb"};
const SHARE_MODE = Boolean(window.DASH_SHARE_MODE);

const applyLang = () => {
  const loc = getLocale(LANG);
  document.documentElement.lang = LANG;
  document.documentElement.dir = loc.dir || (LANG === "ar" ? "rtl" : "ltr");
  
  document.querySelectorAll("[data-i]").forEach(e => {
    const k = e.getAttribute("data-i");
    const val = t(k);
    if (k === "loginHint") e.innerHTML = val;
    else e.textContent = val;
  });
  
  document.querySelectorAll("[data-ph]").forEach(e => {
    const k = e.getAttribute("data-ph");
    e.placeholder = t(k);
  });
  
  const langKeys = Object.keys(LOCALES);
  const langBtn = document.getElementById("langBtn");
  if (langBtn) {
    if (langKeys.length <= 2) {
      const other = langKeys.find(l => l !== LANG) || (LANG === "ar" ? "en" : "ar");
      langBtn.textContent = other === "ar" ? "عربي" : other.toUpperCase();
    } else {
      const flag = langFlags[LANG] ? langFlags[LANG] + " " : "";
      langBtn.textContent = flag + LANG.toUpperCase();
      langBtn.title = loc.name || LANG;
    }
  }
  
  const shareBtn = document.getElementById("shareBtn");
  if (shareBtn) {
    shareBtn.textContent = t("share");
    shareBtn.title = t("shareTitle");
  }
  
  const refBtn = document.getElementById("refreshBtn");
  if (refBtn) refBtn.title = t("refresh");
  
  const act = document.querySelector("#tabs button.active");
  if (act) {
    const tk = pageTitles[act.dataset.tab];
    if (tk) document.getElementById("pageTitle").textContent = t(tk);
  }
};

const langFlags = { ar:"🇪🇬", en:"🇬🇧", fr:"🇫🇷", es:"🇪🇸", ru:"🇷🇺" };
const langPickTitles = {
  ar: "اختر لغة لوحة التحكم",
  en: "Select Dashboard Language",
  fr: "Choisir la langue du tableau de bord",
  es: "Seleccionar idioma del panel",
  ru: "Выберите язык панели управления"
};

const changeLang = (code) => {
  if (!LOCALES[code]) return;
  LANG = code;
  localStorage.setItem("dash_lang", LANG);
  applyLang();
  closeModal();
  const act = document.querySelector("#tabs button.active");
  if (act) {
    const tab = act.dataset.tab;
    if (loaders[tab]) loaders[tab]();
  }
  if (window._currMangaCh) {
    doMangaChaptersModal(window._currMangaCh.id, window._currMangaCh.name);
  }
};

const openLangModal = () => {
  const listHtml = Object.keys(LOCALES).map(code => {
    const loc = LOCALES[code];
    const isCurrent = code === LANG;
    const flag = langFlags[code] || "🌐";
    return `
      <button class="btn-ghost" data-act="setLang" data-lang="${code}" style="width:100%; display:flex; align-items:center; justify-content:space-between; padding:12px 16px; margin-bottom:8px; border-radius:var(--radius-sm); border:1px solid ${isCurrent ? 'var(--primary)' : 'var(--border)'}; background:${isCurrent ? 'rgba(88,101,242,0.12)' : 'var(--bg)'}; font-size:15px; cursor:pointer;">
        <span style="display:flex; align-items:center; gap:12px;">
          <span style="font-size:22px">${flag}</span>
          <span style="font-weight:${isCurrent ? '700' : '500'}; color:var(--txt)">${esc(loc.name || code)}</span>
          <span class="muted" style="font-size:12px; text-transform:uppercase;">(${esc(code)})</span>
        </span>
        ${isCurrent ? '<span style="color:var(--primary); font-weight:bold; font-size:16px;">✓</span>' : ''}
      </button>`;
  }).join("");

  openModal(`
    <h3 style="margin-bottom:16px;">🌐 ${langPickTitles[LANG] || langPickTitles.en}</h3>
    <div style="display:flex; flex-direction:column; gap:4px; max-height:60vh; overflow-y:auto;">
      ${listHtml}
    </div>
    <div class="row" style="justify-content:flex-end; margin-top:16px;">
      <button class="btn-ghost" data-act="closeModal">${t("close")}</button>
    </div>
  `);
};

document.getElementById("langBtn").addEventListener("click", () => {
  const langKeys = Object.keys(LOCALES);
  if (langKeys.length <= 2) {
    changeLang(LANG === "ar" ? "en" : "ar");
  } else {
    openLangModal();
  }
});
applyLang();

/* ═══════════ UTILITIES ═══════════ */
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money=c=>{const a=Math.abs(c||0);return(c<0?"-":"")+"$"+Math.floor(a/100)+"."+String(a%100).padStart(2,"0");};
const pill=s=>{
  const txt = tStatus(s);
  return '<span class="pill p-'+esc(s)+'">'+esc(txt).toUpperCase()+"</span>";
};
const actionPill=act=>{
  const txt = tAction(act);
  return '<span class="pill p-'+esc(act)+'">'+esc(txt)+"</span>";
};
const debounce=(fn,ms)=>{let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>fn(...a),ms);};};
const fmtUp=s=>{if(s==null||s<0)return "?";if(s<60)return s+"s";
  const m=Math.floor(s/60);if(m<60)return m+"m";const h=Math.floor(m/60);
  if(h<48)return h+"h "+(m%60)+"m";return Math.floor(h/24)+"d "+(h%24)+"h";};
const fmtTime=iso=>{if(!iso)return "—";const d=new Date(iso);if(isNaN(d))return esc(iso);
  const s=Math.floor((Date.now()-d.getTime())/1000);
  if(s<0)return d.toLocaleDateString();
  const timeLoc=(getLocale(LANG).time)||{};
  if(s<60)return timeLoc.now||"now";
  if(s<3600){const m=Math.floor(s/60);return (timeLoc.mAgo||"{m}m ago").replace("{m}",m);}
  if(s<86400){const h=Math.floor(s/3600);return (timeLoc.hAgo||"{h}h ago").replace("{h}",h);}
  return d.toLocaleString(LANG,{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"});};
const copyToClip=async(text,msg)=>{
  if(!text)return;
  try{
    await navigator.clipboard.writeText(text);
    toast(msg||t("copiedSuccess"),1);
  }catch(e){
    prompt(t("copyManual"),text);
  }
};

/* ═══════════ API MANAGER (cache + dedup + timeout + abort) ═══════════ */
const _cache=new Map();
const _inflight=new Map();
let _loadGen=0; // generation counter — discard stale renders

const cacheGet=key=>{const e=_cache.get(key);if(!e)return null;if(Date.now()>e.exp){_cache.delete(key);return null;}return e.d;};
const cacheSet=(key,data,ttl)=>_cache.set(key,{d:data,exp:Date.now()+ttl});
const cacheDrop=pattern=>{for(const k of _cache.keys())if(k.includes(pattern))_cache.delete(k);};

const api=async(path,opts)=>{
  opts=opts||{};const method=(opts.method||"GET").toUpperCase();
  const url=path;
  const cacheKey=method==="GET"?path:null;

  // 1. Return cached if fresh (GET only)
  if(cacheKey){const hit=cacheGet(cacheKey);if(hit)return hit;}

  // 2. Dedup: if identical GET is already in-flight, piggyback on it
  if(cacheKey && _inflight.has(cacheKey)){ return _inflight.get(cacheKey); }

  const reqProm=(async()=>{
    const ac=new AbortController();
    const timeout=opts.timeout||TIMEOUT_MS;
    const tid=setTimeout(()=>ac.abort(),timeout);
    try{
      const defaultHeaders=(opts.body instanceof FormData)?{}:{"Content-Type":"application/json"};
      const r=await fetch(url,{...opts,signal:ac.signal,
        headers:{...defaultHeaders,...(opts.headers||{})}});
      clearTimeout(tid);
      let j=null;try{j=await r.json();}catch(e){}
      if(r.status===401){logout(true);throw new Error("401");}
      if(r.status===429){const ra=r.headers.get("Retry-After");
        throw new Error("429 — rate limited"+(ra?", retry in "+ra+"s":""));}
      if(!r.ok)throw new Error((j&&(j.error||j.detail))||("HTTP "+r.status));

      // Cache GET results
      if(cacheKey)cacheSet(cacheKey,j,CACHE_TTL);

      // Invalidate related caches on mutations
      if(method!=="GET"){
        cacheDrop("/api/stats");
        if(path.includes("/mangas"))cacheDrop("/api/mangas");
        if(path.includes("/chapters"))cacheDrop("/api/chapters");
        if(path.includes("/staff")||path.includes("/pay"))cacheDrop("/api/staff");
        if(path.includes("/finance")||path.includes("/pricing")||path.includes("/pay"))cacheDrop("/api/finance");
        if(path.includes("/database"))cacheDrop("/api/database");
      }
      return j;
    }catch(e){
      clearTimeout(tid);
      if(e.name==="AbortError")throw new Error(I[LANG].timeout);
      throw e;
    }finally{if(cacheKey)_inflight.delete(cacheKey);}
  })();

  if(cacheKey)_inflight.set(cacheKey,reqProm);
  return reqProm;
};

/* ═══════════ TOAST ═══════════ */
const toast=(msg,ok)=>{const c=$("toastContainer"),t=document.createElement("div");
  t.className="toast "+(ok?"toast-ok":"toast-err");t.innerHTML=(ok?"✅ ":"❌ ")+msg;
  c.appendChild(t);setTimeout(()=>{t.classList.add("toast-out");setTimeout(()=>t.remove(),350);},TOAST_MS);};
const loginMsg=html=>$("loginMsg").innerHTML=html?'<div class="err">'+html+"</div>":"";

/* ═══════════ CONNECTION DETECTION ═══════════ */
const updateConn=online=>{const b=$("connBadge");
  if(online){b.classList.remove("offline");b.querySelector("span").textContent=I[LANG].online;}
  else{b.classList.add("offline");b.querySelector("span").textContent=I[LANG].offline;}};
window.addEventListener("online",()=>{updateConn(true);toast("🌐 "+I[LANG].online,1);
  const a=document.querySelector("#tabs button.active");if(a)loadTab(a.dataset.tab);});
window.addEventListener("offline",()=>{updateConn(false);toast("📡 "+I[LANG].offline);});

/* ═══════════ AUTH / SESSION ═══════════ */

if(location.protocol==="http:"&&!["localhost","127.0.0.1"].includes(location.hostname))
  $("httpWarn").style.display="block";

 const logout=async exp=>{
  _cache.clear();
  try { await fetch("/api/logout", {method:"POST"}); }catch(e){}
  $("appWrap").classList.add("hidden");$("loginWrap").classList.remove("hidden");
  closeSidebar();stopAutoRefresh();stopIdleTimer();
   if(exp)loginMsg("⚠️ "+(SHARE_MODE?t("shareExpired"):t("sessionExpired")));
};

 $("logoutBtn").addEventListener("click",()=>logout(false));
 $("shareBtn").addEventListener("click",()=>{
   openModal(`<div style="padding:10px 0">
     <h3 style="margin-bottom:16px">${I[LANG].shareTitle}</h3>
     <p class="muted" style="margin-bottom:16px">${I[LANG].shareReadOnly}</p>
     <div class="field"><label>${I[LANG].shareDuration}</label>
       <select id="shareMinutes" style="width:100%">
         <option value="15">${I[LANG].share15}</option>
         <option value="60" selected>${I[LANG].share60}</option>
         <option value="1440">${I[LANG].share1440}</option>
       </select>
     </div>
     <div class="row" style="justify-content:flex-end;gap:10px;margin-top:18px">
       <button class="btn-ghost" data-act="closeModal">${I[LANG].cancel}</button>
       <button class="btn-green" data-act="shareCreate">${I[LANG].shareCreate}</button>
     </div>
   </div>`);
 });

$("loginBtn").addEventListener("click",async e=>{
  const b=e.currentTarget;b.disabled=true;b.innerHTML='<span class="spin"></span>';
  const tok=$("tokInput").value.trim();
  $("tokInput").value="";
  loginMsg("");
  
  if(!tok){
    loginMsg(t("tokenRequired"));
    b.disabled=false;
    b.textContent=I[LANG].login;
    return;
  }
  
  try {
    const ac=new AbortController();setTimeout(()=>ac.abort(),TIMEOUT_MS);
    const r=await fetch("/api/login",{
      method: "POST",
      signal: ac.signal,
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({token: tok})
    });
    
    if(r.status===401)throw new Error(t("tokenInvalid"));
    if(r.status===429)throw new Error(t("rateLimited"));
    if(r.status===503)throw new Error(t("serviceDisabled"));
    if(!r.ok)throw new Error("HTTP "+r.status);
    
    // Cookie is set!
    enter();
  }catch(err){
    loginMsg("✖ "+(err.name==="AbortError"?I[LANG].timeout:esc(err.message)));
  }
  b.disabled=false;
  b.textContent=I[LANG].login;
});

 const enter=()=>{$("loginWrap").classList.add("hidden");$("appWrap").classList.remove("hidden");
  startAutoRefresh();startIdleTimer();loadOverview();};

 const READONLY_ACTIONS=new Set(["mangaSync","mangaStatus","mangaDel","chApprove","chReject","chReopen","chUnclaim","payUser","payUserFin","savePrice","bulkChApprove","bulkChReject","bulkMangaSync","bulkMangaDel","addMangaGo","rejectGo","editMangaModal","saveMangaEdit","createDiscordChannel","addStaffModal","saveStaffAdd","removeStaff","editStaffModal","saveStaffEdit","createBackup","delBackup"]);
 if(SHARE_MODE){
   document.body.classList.add("share-readonly");
   $("shareBtn").classList.add("hidden");
   $("logoutBtn").classList.add("hidden");
   $("loginWrap").classList.add("hidden");
   const dbBtn = document.querySelector('#tabs button[data-tab="database"]');
   if (dbBtn) dbBtn.classList.add("hidden");
   enter();
 }

// Toggle Token visibility
$("toggleTokBtn").addEventListener("click", () => {
  const inp = $("tokInput");
  if (inp.type === "password") {
    inp.type = "text";
    $("toggleTokBtn").textContent = "🙈";
  } else {
    inp.type = "password";
    $("toggleTokBtn").textContent = "👁️";
  }
});

/* ═══════════ IDLE TIMER (auto-logout) ═══════════ */
let _idleTimer=null;
const resetIdle=()=>{if(!_idleTimer)return;clearTimeout(_idleTimer);
  _idleTimer=setTimeout(()=>{if(!$("appWrap").classList.contains("hidden"))logout(true);},IDLE_LIMIT);};
const startIdleTimer=()=>{_idleTimer=setTimeout(()=>{if(!$("appWrap").classList.contains("hidden"))logout(true);},IDLE_LIMIT);};
const stopIdleTimer=()=>{clearTimeout(_idleTimer);_idleTimer=null;};
["mousemove","keydown","click","scroll","touchstart"].forEach(ev=>
  document.addEventListener(ev,resetIdle,{passive:true}));

/* ═══════════ SIDEBAR ═══════════ */
const sidebar=$("sidebar"),sOverlay=$("sidebarOverlay");
const openSidebar=()=>{sidebar.classList.add("open");sOverlay.classList.add("open");};
const closeSidebar=()=>{sidebar.classList.remove("open");sOverlay.classList.remove("open");};
$("menuBtn").addEventListener("click",()=>sidebar.classList.contains("open")?closeSidebar():openSidebar());
$("sidebarToggle").addEventListener("click",closeSidebar);
sOverlay.addEventListener("click",closeSidebar);

/* ═══════════ TABS ═══════════ */
const TABS=["overview","mangas","chapters","staff","finance","jobs","database"];
const loaders={overview:loadOverview,mangas:loadMangas,chapters:loadChapters,
  staff:loadStaff,finance:loadFinance,jobs:loadJobs,database:loadDatabase};

const loadTab=name=>{if(loaders[name])loaders[name]();};

$("tabs").addEventListener("click",e=>{const b=e.target.closest("button");if(!b)return;
  _loadGen++; // invalidate any in-flight renders from previous tab
  document.querySelectorAll("#tabs button").forEach(x=>x.classList.remove("active"));b.classList.add("active");
  TABS.forEach(t=>{const sec=$("tab-"+t);
    if(!sec)return;
    if(t===b.dataset.tab){sec.classList.remove("hidden");}
    else{sec.classList.add("hidden");}});
  const tk=pageTitles[b.dataset.tab];
  if(tk&&I[LANG][tk])$("pageTitle").textContent=I[LANG][tk];
  closeSidebar();loadTab(b.dataset.tab);});

$("refreshBtn").addEventListener("click",()=>{_cache.clear();
  const a=document.querySelector("#tabs button.active");if(a)loadTab(a.dataset.tab);});

/* ═══════════ AUTO-REFRESH (Visibility API) ═══════════ */
let _refreshTimer=null;
const startAutoRefresh=()=>{stopAutoRefresh();
  _refreshTimer=setInterval(()=>{
    if(document.hidden)return; // don't fetch when tab is not visible
    if($("appWrap").classList.contains("hidden"))return;
    const a=document.querySelector("#tabs button.active");
    if(a&&a.dataset.tab==="overview")loadOverview();
  },REFRESH_MS);};
const stopAutoRefresh=()=>{clearInterval(_refreshTimer);_refreshTimer=null;};
document.addEventListener("visibilitychange",()=>{
  if(!document.hidden&&!$("appWrap").classList.contains("hidden")){
    const a=document.querySelector("#tabs button.active");if(a)loadTab(a.dataset.tab);}});


/* ═══════════ TABLE STATE (Sort & Pagination) ═══════════ */
const tableState = {
  mangas: { sort: "name", order: "asc", offset: 0, limit: 50 },
  chapters: { sort: "manga_name", order: "asc", offset: 0, limit: 50 },
  staff: { sort: "load", order: "desc", offset: 0, limit: 100 }
};

const renderPagination = (tab, total) => {
  const st = tableState[tab];
  const container = $(tab === 'mangas' ? 'pageMangas' : 'pageChapters');
  const maxPage = Math.max(1, Math.ceil(total / st.limit));
  const curPage = Math.floor(st.offset / st.limit) + 1;
  
  let html = `<button class="btn-small btn-ghost" data-act="pagePrev" data-tab="${tab}" ${curPage <= 1 ? 'disabled' : ''}>«</button>`;
  html += `<span class="page-info">${curPage} / ${maxPage}</span>`;
  html += `<button class="btn-small btn-ghost" data-act="pageNext" data-tab="${tab}" ${curPage >= maxPage ? 'disabled' : ''}>»</button>`;
  
  container.innerHTML = html;
};

/* ═══════════ BULK ACTIONS ═══════════ */
let _selected = new Set();
let _bulkType = null; // 'mangas' or 'chapters'

const updateBulkUI = () => {
  const tb = $("bulkToolbar");
  if (_selected.size === 0) {
    tb.classList.remove("show");
    tb.classList.add("hidden");
    return;
  }
  tb.classList.remove("hidden");
  // force reflow
  void tb.offsetWidth;
  tb.classList.add("show");
  
  $("bulkCount").textContent = I[LANG].bulkSelected.replace("{n}", _selected.size);
  
  let actions = "";
  if (_bulkType === "chapters") {
    actions += `<button class="btn-small btn-green" data-act="bulkChApprove">✅ ${I[LANG].bulkApproveAll}</button>`;
    actions += `<button class="btn-small btn-danger" data-act="bulkChReject">✖ ${I[LANG].bulkRejectAll}</button>`;
  } else if (_bulkType === "mangas") {
    actions += `<button class="btn-small btn-ghost" data-act="bulkMangaSync">🔄 ${I[LANG].bulkSyncAll}</button>`;
    actions += `<button class="btn-small btn-danger" data-act="bulkMangaDel">🗑 ${I[LANG].bulkDeleteAll}</button>`;
  }
  const actContainer = $("bulkActions");
  if (actContainer) actContainer.innerHTML = actions;
};

const clearBulk = () => {
  _selected.clear();
  document.querySelectorAll(".chk-row, [data-act='chkAll']").forEach(cb => cb.checked = false);
  updateBulkUI();
};

/* ═══════════ SKELETON ═══════════ */
const showSkeleton=(el,count,type)=>{let h="";
  if(type==="stats"){for(let i=0;i<count;i++)h+='<div class="skeleton skeleton-stat"></div>';}
  else{for(let i=0;i<count;i++)h+='<div class="skeleton skeleton-text" style="width:'+((60+Math.random()*30)|0)+'%"></div>';}
  el.innerHTML=h;};
const skelRow=cols=>'<tr><td colspan="'+cols+'"><div class="skeleton skeleton-text"></div><div class="skeleton skeleton-text" style="width:60%"></div></td></tr>';


function doSort(th) {
  const tab = th.closest('section').id.replace('tab-', '');
  const key = th.dataset.sort;
  if (!key || !tableState[tab]) return;
  
  const st = tableState[tab];
  if (st.sort === key) {
    st.order = st.order === 'asc' ? 'desc' : 'asc';
  } else {
    st.sort = key;
    st.order = 'asc'; // default
  }
  st.offset = 0; // reset page on sort
  
  // Update UI arrows
  th.closest('tr').querySelectorAll('th').forEach(h => {
    h.classList.remove('active', 'asc', 'desc');
  });
  th.classList.add('active', st.order);
  
  loadTab(tab);
}

function doPage(tab, dir) {
  const st = tableState[tab];
  st.offset += dir * st.limit;
  if (st.offset < 0) st.offset = 0;
  loadTab(tab);
}

function doCheckAll(cb) {
  const tbody = $(cb.dataset.target);
  const type = cb.dataset.target === "mangaRows" ? "mangas" : "chapters";
  _bulkType = type;
  
  const isChecked = cb.checked;
  tbody.querySelectorAll('.chk-row').forEach(rowCb => {
    rowCb.checked = isChecked;
    if (isChecked) _selected.add(rowCb.dataset.id);
    else _selected.delete(rowCb.dataset.id);
  });
  updateBulkUI();
}

function doCheckRow(cb) {
  const type = cb.closest('tbody').id === "mangaRows" ? "mangas" : "chapters";
  _bulkType = type;
  
  if (cb.checked) _selected.add(cb.dataset.id);
  else _selected.delete(cb.dataset.id);
  
  // Update "Check All" state visually (optional, simple logic here)
  updateBulkUI();
}

async function doBulkCh(action) {
  const actName = tAction(action);
  const msg = t("bulkChConfirm", {act: actName, count: _selected.size});
  if (!(await asyncConfirm(msg, "btn-green"))) return;
  const ids = Array.from(_selected);
  clearBulk();
  
  let success = 0;
  await Promise.all(ids.map(async id => {
    try {
      await api("/api/chapters/" + id + "/" + action, {method:"POST", body:"{}"});
      success++;
    } catch(e) {}
  }));
  toast(t("bulkCompleted", {act: actName, success, total: ids.length}), 1);
  if (window._currMangaCh) doMangaChaptersModal(window._currMangaCh.id, window._currMangaCh.name);
  else loadChapters();
}

async function doBulkManga(action) {
  const actName = tAction(action);
  const msg = t("bulkMangaConfirm", {act: actName, count: _selected.size});
  if (!(await asyncConfirm(msg, action==="delete"?"btn-danger":"btn-green"))) return;
  const ids = Array.from(_selected);
  clearBulk();
  
  let success = 0;
  await Promise.all(ids.map(async id => {
    try {
      if (action === "sync") await api("/api/mangas/" + id + "/sync", {method:"POST"});
      if (action === "delete") await api("/api/mangas/" + id, {method:"DELETE"});
      success++;
    } catch(e) {}
  }));
  toast(t("bulkCompleted", {act: actName, success, total: ids.length}), 1);
  loadMangas();
}

/* ═══════════ EVENT DELEGATION (replaces all window.* / inline onclick) ═══════════ */
const actions={
  shareCreate: ()=>createShareLink(),
  shareCopy: async ()=>{
    const input=$("shareUrlOutput");
    if(!input)return;
    try{
      await navigator.clipboard.writeText(input.value);
      toast(I[LANG].shareCopy,1);
    }catch(e){
      input.select();
      toast(t("copyManual"));
    }
  },

  // Sorting
  sortCol:      el => doSort(el.closest('th')),
  // Pagination
  pagePrev:     el => doPage(el.dataset.tab, -1),
  pageNext:     el => doPage(el.dataset.tab, 1),
  // Checkboxes
  chkAll:       el => doCheckAll(el),
  chkRow:       el => doCheckRow(el),
  bulkClose:    () => clearBulk(),
  bulkChApprove:() => doBulkCh('approve'),
  bulkChReject: () => doBulkCh('reject'),
  bulkMangaSync:() => doBulkManga('sync'),
  bulkMangaDel: () => doBulkManga('delete'),
  // Manga actions
  mangaDetail:  el=>doMangaDetail(+el.dataset.id),
  mangaChaptersModal: el=>doMangaChaptersModal(+el.dataset.id, el.dataset.name),
  mangaSync:    el=>doMangaSync(+el.dataset.id),
  mangaStatus:  el=>doMangaStatus(+el.dataset.id,el.dataset.status),
  mangaDel:     el=>doMangaDel(+el.dataset.id,el.dataset.name),
  editMangaModal: el=>doEditMangaModal(+el.dataset.id),
  saveMangaEdit: el=>doSaveMangaEdit(+el.dataset.id),
  createDiscordChannel: el=>doCreateDiscordChannel(+el.dataset.id),
  addStaffModal: el=>doAddStaffModal(+el.dataset.id),
  saveStaffAdd: el=>doSaveStaffAdd(+el.dataset.id),
  removeStaff:  el=>doRemoveStaff(+el.dataset.mid, el.dataset.uid, el.dataset.role),
  // Chapter actions
  chApprove:    el=>doChAct(+el.dataset.id,"approve"),
  chReject:     el=>doChReject(+el.dataset.id),
  chReopen:     el=>doChAct(+el.dataset.id,"reopen"),
  chUnclaim:    el=>doChAct(+el.dataset.id,"unclaim"),
  // Staff/Finance
  staffDetail:  el=>doStaffDetail(el.dataset.uid),
  staffModalTab:el=>renderStaffModal(el.dataset.tab),
  editStaffModal: el=>doEditStaffModal(el.dataset.uid),
  saveStaffEdit: el=>doSaveStaffEdit(el.dataset.uid),
  copyText:     el=>copyToClip(el.dataset.copy, el.dataset.msg),
  payUser:      el=>doPayUser(el.dataset.uid,"staff"),
  payUserFin:   el=>doPayUser(el.dataset.uid,"finance"),
  savePrice:    el=>doSavePrice(el.dataset.key),
  // Modal
  closeModal:   ()=>closeModal(),
  setLang:      el=>changeLang(el.dataset.lang),
  // Modal form submits (handler set dynamically)
  addMangaGo:   ()=>doAddManga(),
  rejectGo:     el=>doRejectSubmit(+el.dataset.id),
  delBackup:    el=>doDeleteBackup(el.dataset.file),
  inspectBackup:el=>doInspectBackup(el.dataset.file),
  inspectTab:   el=>renderInspectModal(window._currInspectData, el.dataset.table, window._currInspectCat),
  inspectCat:   el=>renderInspectModal(window._currInspectData, null, el.dataset.cat),
  inspectSelectChange: el=>renderInspectModal(window._currInspectData, el.value, window._currInspectCat),
};
document.addEventListener("click",e=>{
  const btn=e.target.closest("[data-act]");if(!btn)return;
  if(SHARE_MODE && READONLY_ACTIONS.has(btn.dataset.act))return;
  const handler=actions[btn.dataset.act];if(handler)handler(btn);
  const th=e.target.closest("th.sortable");if(th)actions.sortCol(th);
});
document.addEventListener("change",e=>{
  const el=e.target.closest("[data-act]");if(!el)return;
  if(SHARE_MODE && READONLY_ACTIONS.has(el.dataset.act))return;
  const handler=actions[el.dataset.act];if(handler)handler(el);
});

async function createShareLink(){
  const minutes=Number($("shareMinutes")?.value||60);
  try{
    const result=await api("/api/share-links",{method:"POST",body:JSON.stringify({minutes})});
    const safeUrl=esc(result.url||"");
    openModal(`<div style="padding:10px 0">
      <h3 style="margin-bottom:12px">${I[LANG].shareCreated}</h3>
      <p class="muted" style="margin-bottom:14px">${I[LANG].shareReadOnly}</p>
      <input id="shareUrlOutput" dir="ltr" readonly value="${safeUrl}" style="width:100%;font-size:12px">
      <div class="row" style="justify-content:flex-end;gap:10px;margin-top:18px">
        <button class="btn-green" data-act="shareCopy">${I[LANG].shareCopy}</button>
        <button class="btn-ghost" data-act="closeModal">${I[LANG].close}</button>
      </div>
    </div>`);
    $("shareUrlOutput").select();
  }catch(e){toast(esc(e.message));}
}

/* ═══════════ OVERVIEW ═══════════ */
async function loadOverview(){const gen=++_loadGen;
  const g=$("statGrid"),act=$("recentAct");
  if(g && !g.children.length) showSkeleton(g,6,"stats");
  if(act && !act.children.length) showSkeleton(act,4,"text");
  try{const s=await api("/api/stats");if(gen!==_loadGen)return;
    g.innerHTML=
      statCard("👥",s.staff_registered,I[LANG].tStaff,"purple")+
      statCard("⏱️",fmtUp(s.uptime_seconds),I[LANG].tUptime,"blue")+
      statCard("💰",money((s.unpaid||{}).cents),I[LANG].lblUnpaid+" ("+((s.unpaid||{}).count||0)+")","orange")+
      statDictCard("📚",s.mangas_by_status,I[LANG].tMangas,"purple")+
      statDictCard("📄",s.chapters_by_status,I[LANG].tCh,"blue")+
      statDictCard("⚙️",s.jobs_by_status,I[LANG].tJobs,"green");
    act.innerHTML=(s.recent_activity||[]).map(a=>{
      const mName = esc(a.manga||"");
      const shortName = mName.length > 40 ? mName.substring(0,40)+"..." : mName;
      const chLabel = t("chLabelPrefix", {n: esc(a.chapter)});
      return `<div class="activity-item" style="display:flex; justify-content:space-between; align-items:center;">
        <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
          <b style="color:var(--txt)" title="${mName}">${shortName}</b>
          <span style="font-size:12px; font-weight:700; color:var(--acc); background:var(--glow); padding:2px 8px; border-radius:var(--radius-xs); border:1px solid rgba(139,92,246,0.2);">
            ${chLabel}
          </span>
          ${actionPill(a.action)}
        </div>
        <span class="activity-time" title="${esc(a.at||"")}">${fmtTime(a.at)}</span>
      </div>`;
    }).join("")||`<div class="muted" style="padding:12px 0">${t("noRecentActivity")}</div>`;
  }catch(e){if(gen===_loadGen){toast(esc(e.message));g.innerHTML="";act.innerHTML="";}}}

const statCard=(icon,value,label,color)=>
  `<div class="stat" data-color="${color}" style="display:flex;flex-direction:column;align-items:center;justify-content:center;padding:20px 14px;text-align:center;">
     <div style="font-size:32px;margin-bottom:10px;filter:drop-shadow(0 4px 10px rgba(0,0,0,0.5))">${icon}</div>
     <div style="font-size:28px;font-weight:800;color:var(--txt);line-height:1">${esc(value)}</div>
     <div style="font-size:13px;color:var(--muted);margin-top:8px;font-weight:600;">${esc(label)}</div>
   </div>`;

const statDictCard=(icon,obj,label,color)=>{
  const e=Object.entries(obj||{});
  if(!e.length)return statCard(icon,"-",label,color);
  const total = e.reduce((acc, [, v]) => acc + (parseInt(v, 10) || 0), 0);
  return `<div class="stat" data-color="${color}" style="display:flex;flex-direction:column;padding:16px;text-align:start;">
     <div class="row" style="justify-content:space-between;align-items:center;margin-bottom:12px;border-bottom:1px solid var(--border);padding-bottom:8px;flex-wrap:nowrap;">
       <div style="display:flex;align-items:center;gap:8px;">
         <span style="font-size:22px;filter:drop-shadow(0 2px 5px rgba(0,0,0,0.5))">${icon}</span>
         <span style="font-size:15px;font-weight:700;color:var(--txt)">${esc(label)}</span>
       </div>
       <span class="badge" style="font-size:12px;padding:2px 8px;font-weight:700;" title="${t('totalLabel')}">${total}</span>
     </div>
     <div style="display:flex;flex-direction:column;gap:8px;">
       ${e.map(([k,v])=>`<div style="display:flex;justify-content:space-between;align-items:center;width:100%;">${pill(k)} <b style="font-size:14px;color:var(--txt);font-family:monospace;">${esc(v)}</b></div>`).join("")}
     </div>
  </div>`;
};

/* ═══════════ MANGAS ═══════════ */
const debouncedMangaSearch=debounce(()=>loadMangas(),DEBOUNCE_MS);
$("mangaSearch").addEventListener("input",debouncedMangaSearch);
$("mangaSearch").addEventListener("keydown",e=>{if(e.key==="Enter")loadMangas();});
$("mangaSearchBtn").addEventListener("click",loadMangas);
$("mangaStatusF").addEventListener("change",loadMangas);

async function loadMangas(){const gen=++_loadGen;
  const rows=$("mangaRows");
  if(rows && !rows.children.length) rows.innerHTML=skelRow(7);
  const q=$("mangaSearch").value,st=$("mangaStatusF").value;
  try{const d=await api("/api/mangas?limit="+tableState.mangas.limit+"&offset="+tableState.mangas.offset+"&sort="+tableState.mangas.sort+"&order="+tableState.mangas.order+"&search="+encodeURIComponent(q)+"&status="+encodeURIComponent(st));
    if(gen!==_loadGen)return;
    clearBulk(); renderPagination("mangas", d.total || 0);
    rows.innerHTML=(d.items||[]).map(m=>{
      const cover = m.cover_url ? `<img src="${esc(m.cover_url)}" class="manga-card-img" loading="lazy">` : `<div class="manga-card-empty">📚</div>`;
      let statusColor = "blue";
      if (m.status === "ongoing") statusColor = "green";
      if (m.status === "dropped") statusColor = "red";
      if (m.status === "paused") statusColor = "orange";
      
      const stCur = tStatus(m.status);
      
      return `
      <div class="manga-card" data-act="mangaDetail" data-id="${m.id}">
        ${cover}
        <div class="manga-card-overlay">
           <div class="manga-card-title" dir="auto">${esc(m.name)}</div>
           <div class="manga-card-stats">
              <span class="manga-card-badge ${statusColor}">${stCur}</span>
              <span class="manga-card-badge purple">📄 ${m.chapters}</span>
              ${m.in_review > 0 ? `<span class="manga-card-badge red">✅ ${m.in_review}</span>` : ''}
              <span class="manga-card-badge blue">👥 ${m.staff}</span>
           </div>
        </div>
      </div>`;
    }).join("")||`<div class="muted" style="grid-column: 1 / -1; text-align: center; padding: 40px;">${I[LANG].noData}</div>`;
  }catch(e){if(gen===_loadGen){toast(esc(e.message));rows.innerHTML="";}}}

async function doMangaSync(id){
  const msg = t("mangaSyncConfirm", {id});
  if(!(await asyncConfirm(msg, "btn-green")))return;
  try{await api("/api/mangas/"+id+"/sync",{method:"POST"});toast(t("mangaSyncQueued", {id}),1);loadMangas();}
  catch(e){toast(esc(e.message));}}
async function doMangaStatus(id,cur){const order={ongoing:"paused",paused:"dropped",dropped:"ongoing"};
  const nx=order[cur]||"ongoing";
  const stCur = tStatus(cur);
  const stNx = tStatus(nx);
  const msg = t("mangaStatusConfirm", {id, cur: stCur, nx: stNx});
  if(!(await asyncConfirm(msg, "btn-green")))return;
  try{const r=await api("/api/mangas/"+id+"/status",{method:"PUT",body:JSON.stringify({status:nx})});
    toast(t("mangaStatusChanged", {cur: esc(cur), status: esc(r.status), removed: r.removed_staff}),1);loadMangas();}
  catch(e){toast(esc(e.message));}}
async function doMangaDel(id,name){
  const msg1 = t("mangaDelConfirm", {name: esc(name)});
  if(!(await asyncConfirm(msg1, "btn-danger")))return;
  try{await api("/api/mangas/"+id,{method:"DELETE"});toast(t("mangaDeleted", {id}),1);loadMangas();}
  catch(e){
    if(String(e.message).includes("unpaid")) {
       const msg2 = t("mangaDelForceConfirm");
       if(await asyncConfirm(msg2, "btn-danger")) {
          try{await api("/api/mangas/"+id+"?force=1",{method:"DELETE"});toast(t("mangaForceDeleted"),1);loadMangas();return;}
          catch(e2){toast(esc(e2.message));return;}
       }
    } else {
       toast(esc(e.message));
    }
  }
}
async function doMangaDetail(id){try{const m=await api("/api/mangas/"+id);
  const coverHtml = m.cover_url ? `<img src="${esc(m.cover_url)}" class="manga-cover" style="width:80px;height:115px;border-radius:var(--radius-sm)">` : `<div class="manga-cover empty" style="width:80px;height:115px;border-radius:var(--radius-sm)"></div>`;
  const mdUrlHtml = m.mangadex_url ? `<a href="${esc(m.mangadex_url)}" target="_blank" class="btn-small btn-ghost" style="padding:4px 8px;text-decoration:none">🌐 MangaDex</a>` : '';
  const chnlHtml = m.discord_channel_id ? `<span class="btn-small btn-ghost" style="padding:4px 8px;" dir="ltr">#️⃣ ${m.discord_channel_id}</span>` : `<button class="btn-small btn-green" data-act="createDiscordChannel" data-id="${m.id}" style="padding:4px 8px;">➕ ${t("btnCreateChannel")}</button>`;
  const driveHtml = m.drive_folder_id ? `<a href="https://drive.google.com/drive/folders/${esc(m.drive_folder_id)}" target="_blank" class="btn-small btn-ghost" style="padding:4px 8px;text-decoration:none">📁 Drive</a>` : '';
  const editMangaBtn = `<button class="btn-small btn-ghost" data-act="editMangaModal" data-id="${m.id}" style="padding:4px 8px;">✏️ ${t("btnEdit")}</button>`;
  
  const staffHtml = (m.staff||[]).map(s => {
    let av = s.avatar_url ? `<img src="${esc(s.avatar_url)}" class="avatar-sm" style="width:30px;height:30px">` : `<div class="avatar-sm empty" style="width:30px;height:30px;font-size:14px"></div>`;
    return `<div class="row" style="justify-content:flex-start; gap:12px; padding:8px; background:var(--bg); border-radius:var(--radius-sm)">
      ${av} <div style="flex:1; font-size:13px"><b>${esc(s.username||s.user_id)}</b></div> ${pill(s.role)}
      <button class="btn-ghost" data-act="removeStaff" data-mid="${m.id}" data-uid="${esc(s.user_id)}" data-role="${esc(s.role)}" style="color:var(--err);padding:2px 8px;font-size:12px;cursor:pointer" title="${t('btnRemove')}">✕</button>
    </div>`;
  }).join("") || `<div class="muted">${I[LANG].noActiveStaff}</div>`;

  const chHtml = (m.chapters||[]).slice(0,100).map(c => {
    let av = c.assignee_avatar ? `<img src="${esc(c.assignee_avatar)}" class="avatar-sm" style="width:24px;height:24px;margin-inline-start:auto" title="${esc(c.assignee_name)}">` : '';
    let txt = c.assignee_name && !c.assignee_avatar ? `<span style="font-size:11px; color:var(--muted); margin-inline-start:auto" title="${esc(c.assignee_name)}">${esc(c.assignee_name)}</span>` : '';
    return `<div class="row" style="background:var(--bg); padding:6px 12px; border-radius:var(--radius-xs); border:1px solid var(--border)">
      <span style="font-size:13px;font-weight:bold;min-width:45px">${t("chLabelPrefix", {n: c.chapter_number})}</span>
      ${pill(c.status)}
      ${av || txt}
    </div>`;
  }).join("") || `<div class="muted">${I[LANG].noChapters}</div>`;

  let html = `<div style="display:flex; flex-direction:column; gap:20px;">
    <!-- Header -->
    <div class="row" style="align-items:flex-start; gap:20px; padding-bottom:16px; border-bottom:1px solid var(--border)">
      ${coverHtml}
      <div style="flex:1">
        <h2 style="margin:0 0 10px; font-size:22px; color:var(--txt); line-height:1.2">${esc(m.name)}</h2>
        <div class="row" style="gap:12px; justify-content:flex-start">
          ${pill(m.status)}
          <span class="muted" style="font-size:13px" dir="ltr">ID: ${m.id}</span>
        </div>
        <div class="row" style="margin-top:16px; gap:8px; justify-content:flex-start; flex-wrap:wrap">
           ${driveHtml} ${mdUrlHtml} ${chnlHtml} ${editMangaBtn}
        </div>
      </div>
    </div>
    
    <!-- Body -->
    <div style="display:grid; grid-template-columns: 1fr 1fr; gap:24px">
       <div style="display:flex; flex-direction:column; gap:12px">
         <div class="row" style="justify-content:space-between; align-items:center;">
           <h4 style="margin:0; color:var(--txt)">👥 ${I[LANG].cStaff} (${(m.staff||[]).length})</h4>
           <button class="btn-small btn-ghost" data-act="addStaffModal" data-id="${m.id}" style="padding:2px 8px; font-size:12px;">+ ${t("btnAdd")}</button>
         </div>
         <div style="display:flex; flex-direction:column; gap:6px">
           ${staffHtml}
         </div>
       </div>
       <div style="display:flex; flex-direction:column; gap:12px">
         <h4 style="margin:0; color:var(--txt)">📄 ${I[LANG].tCh} (${(m.chapters||[]).length})</h4>
         <div style="max-height:220px; overflow-y:auto; display:flex; flex-direction:column; gap:6px; padding-inline-end:6px">
           ${chHtml}
         </div>
       </div>
    </div>
    
    <!-- Footer -->
    <div class="row" style="margin-top:8px; justify-content:flex-end">
      <button data-act="closeModal" class="btn-ghost">${I[LANG].close}</button>
    </div>
  </div>`;
  openModal(html);
}catch(e){toast(esc(e.message));}}

$("addMangaBtn").addEventListener("click",()=>{
  openModal(`<h3>${t("addManga")}</h3>`+
  `<div class="field"><label>${t("cName")}</label><input id="nm_name"></div>`+
  `<div class="field"><label>${t("driveLinkOrId")}</label><input id="nm_drive" dir="ltr"></div>`+
  `<div class="field"><label>${t("startChapter")}</label><input id="nm_start" dir="ltr" value="0"></div>`+
  `<div class="row"><button data-act="addMangaGo">${t("send")}</button><button class="btn-ghost" data-act="closeModal">${t("cancel")}</button></div>`+
  `<p class="muted" style="margin-top:10px">${t("addMangaHint")}</p>`);});
async function doAddManga(){
  try{await api("/api/mangas",{method:"POST",body:JSON.stringify({name:$("nm_name").value,
    drive:$("nm_drive").value,start_chapter:$("nm_start").value})});
    closeModal();toast(t("mangaAddedSyncQueued"),1);loadMangas();}
  catch(e){toast(esc(e.message));}}

async function doEditMangaModal(id){
  try{
    const m=await api("/api/mangas/"+id);
    openModal(`<h3>${t("editMangaTitle")}</h3>`+
      `<div class="field"><label>${t("mangaName")}</label><input id="em_name" value="${esc(m.name||'')}"></div>`+
      `<div class="field"><label>${t("driveFolderId")}</label><input id="em_drive" dir="ltr" value="${esc(m.drive_folder_id||'')}"></div>`+
      `<div class="field"><label>${t("mangadexId")}</label><input id="em_mangadex" dir="ltr" value="${esc(m.mangadex_id||'')}"></div>`+
      `<div class="field"><label>${t("startChapter")}</label><input id="em_start" dir="ltr" type="number" step="any" value="${m.start_chapter??0}"></div>`+
      `<div class="row" style="justify-content:flex-end;gap:8px;margin-top:16px;">`+
        `<button class="btn-green" data-act="saveMangaEdit" data-id="${id}">${t("save")}</button>`+
        `<button class="btn-ghost" data-act="closeModal">${t("cancel")}</button>`+
      `</div>`);
  }catch(e){toast(esc(e.message));}
}

async function doSaveMangaEdit(id){
  const name=$("em_name").value.trim();
  const drive_folder_id=$("em_drive").value.trim();
  const mangadex_id=$("em_mangadex").value.trim();
  const start_chapter=parseFloat($("em_start").value);

  const payload={};
  if(name) payload.name=name;
  payload.drive_folder_id=drive_folder_id;
  payload.mangadex_id=mangadex_id;
  if(!isNaN(start_chapter)) payload.start_chapter=start_chapter;

  try{
    await api("/api/mangas/"+id,{method:"PUT",body:JSON.stringify(payload)});
    toast(t("mangaUpdated"),1);
    closeModal();
    loadMangas();
    doMangaDetail(id);
  }catch(e){toast(esc(e.message));}
}

async function doCreateDiscordChannel(id){
  const msg=t("createChannelConfirm");
  if(!(await asyncConfirm(msg,"btn-green")))return;
  try{
    const res=await api("/api/mangas/"+id+"/create-channel",{method:"POST"});
    toast(t("channelCreated", {channel_id: res.channel_id}),1);
    loadMangas();
    doMangaDetail(id);
  }catch(e){toast(esc(e.message));}
}

async function doAddStaffModal(id){
  openModal(`<h3>${t("addStaffTitle")}</h3>`+
    `<div style="padding:30px;text-align:center;"><div class="spinner"></div><p class="muted" style="margin-top:12px">${t("loadingStaff")}</p></div>`);

  try {
    const d = await api("/api/staff");
    const staffList = d.items || [];

    let staffCards = "";
    let defaultUid = "";
    if (staffList.length > 0) {
      defaultUid = staffList[0].user_id;
      staffCards = staffList.map((s, idx) => {
        const isSel = idx === 0 ? " selected" : "";
        const av = s.avatar_url 
          ? `<img src="${esc(s.avatar_url)}" class="avatar-sm" style="width:34px;height:34px;border-radius:50%;object-fit:cover;" loading="lazy">` 
          : `<div class="avatar-sm empty" style="width:34px;height:34px;font-size:16px;"></div>`;
        const name = s.username || s.user_id;
        const roleBadges = (s.roles && s.roles.length) 
          ? s.roles.map(r => `<span class="pill" style="font-size:10px;padding:1px 6px;">${esc(r)}</span>`).join(" ") 
          : "";
        return `
          <div class="staff-picker-item${isSel}" data-uid="${esc(s.user_id)}" data-search="${esc((name + ' ' + s.user_id).toLowerCase())}">
            ${av}
            <div style="flex:1; min-width:0; text-align:start;">
              <div style="font-weight:600; font-size:13px; color:var(--txt); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">
                ${esc(name)}
              </div>
              <div class="muted" style="font-size:11px; font-family:monospace;" dir="ltr">${esc(s.user_id)}</div>
            </div>
            <div style="display:flex; align-items:center; gap:6px;">
              ${roleBadges}
              <span class="staff-picker-check">✓</span>
            </div>
          </div>`;
      }).join("");
    }

    const selectHtml = staffList.length > 0 ? `
      <div class="field" id="as_select_group">
        <label>${t("selectStaffMember")}</label>
        <input id="as_search" type="text" placeholder="${t("searchStaffPlaceholder")}" style="margin-bottom:8px; width:100%;">
        <input type="hidden" id="as_selected_uid" value="${esc(defaultUid)}">
        <div class="staff-picker-list" id="as_picker_list">
          ${staffCards}
          <div id="as_no_match" class="muted hidden" style="text-align:center; padding:16px; font-size:12px;">${t("noStaffMatch")}</div>
        </div>
        <div style="margin-top:8px; text-align:start;">
          <a href="#" id="as_toggle_manual" style="font-size:12px; color:var(--acc); text-decoration:none;">${t("manualUidToggle")}</a>
        </div>
      </div>
      <div class="field hidden" id="as_manual_group">
        <label>${t("manualUidLabel")}</label>
        <input id="as_uid_manual" dir="ltr" placeholder="123456789012345678" style="width:100%;">
        <div style="margin-top:8px; text-align:start;">
          <a href="#" id="as_toggle_select" style="font-size:12px; color:var(--acc); text-decoration:none;">${t("backToStaffList")}</a>
        </div>
      </div>
    ` : `
      <div class="field">
        <label>${t("manualUidLabel")}</label>
        <input id="as_uid_manual" dir="ltr" placeholder="123456789012345678" style="width:100%;">
        <p class="muted" style="font-size:12px; margin-top:4px;">${t("noRegisteredStaff")}</p>
      </div>
    `;

    const modalHtml = `<h3>${t("addStaffTitle")}</h3>
      ${selectHtml}
      <div class="field" style="margin-top:12px;"><label>${t("roleInManga")}</label>
        <select id="as_role" style="width:100%; font-size:14px; padding:8px; background:var(--bg); color:var(--txt); border:1px solid var(--border); border-radius:var(--radius-xs);">
          <option value="TL">${tRole("TL")}</option>
          <option value="CO-TL">${tRole("CO-TL")}</option>
          <option value="PR">${tRole("PR")}</option>
          <option value="CO-PR">${tRole("CO-PR")}</option>
          <option value="ED">${tRole("ED")}</option>
          <option value="CO-ED">${tRole("CO-ED")}</option>
        </select>
      </div>
      <div class="row" style="justify-content:flex-end;gap:8px;margin-top:16px;">
        <button class="btn-green" data-act="saveStaffAdd" data-id="${id}">${t("btnAdd")}</button>
        <button class="btn-ghost" data-act="closeModal">${t("cancel")}</button>
      </div>`;

    openModal(modalHtml);

    // Wire up clicking on member item
    const pickerList = $("as_picker_list");
    const hiddenUid = $("as_selected_uid");
    if (pickerList && hiddenUid) {
      pickerList.addEventListener("click", (e) => {
        const item = e.target.closest(".staff-picker-item");
        if (!item) return;
        pickerList.querySelectorAll(".staff-picker-item").forEach(el => el.classList.remove("selected"));
        item.classList.add("selected");
        hiddenUid.value = item.dataset.uid;
      });
    }

    // Wire up search filter
    const searchInp = $("as_search");
    if (searchInp && pickerList) {
      searchInp.addEventListener("input", () => {
        const q = searchInp.value.trim().toLowerCase();
        const items = pickerList.querySelectorAll(".staff-picker-item");
        let visibleCount = 0;
        let firstVisible = null;
        items.forEach(item => {
          const match = !q || (item.dataset.search || "").includes(q);
          if (match) {
            item.classList.remove("hidden");
            visibleCount++;
            if (!firstVisible) firstVisible = item;
          } else {
            item.classList.add("hidden");
          }
        });
        const noMatchEl = $("as_no_match");
        if (noMatchEl) {
          if (visibleCount === 0) noMatchEl.classList.remove("hidden");
          else noMatchEl.classList.add("hidden");
        }
        // If current selection is hidden, select first visible
        const currentSel = pickerList.querySelector(".staff-picker-item.selected");
        if (currentSel && currentSel.classList.contains("hidden") && firstVisible) {
          currentSel.classList.remove("selected");
          firstVisible.classList.add("selected");
          hiddenUid.value = firstVisible.dataset.uid;
        }
      });
    }

    // Wire up toggle between select and manual input
    const toggleManual = $("as_toggle_manual");
    const toggleSelect = $("as_toggle_select");
    const selGroup = $("as_select_group");
    const manGroup = $("as_manual_group");

    if (toggleManual && toggleSelect) {
      toggleManual.addEventListener("click", (e) => {
        e.preventDefault();
        selGroup.classList.add("hidden");
        manGroup.classList.remove("hidden");
        $("as_uid_manual")?.focus();
      });
      toggleSelect.addEventListener("click", (e) => {
        e.preventDefault();
        manGroup.classList.add("hidden");
        selGroup.classList.remove("hidden");
      });
    }
  } catch(e) {
    toast(esc(e.message));
    closeModal();
  }
}

async function doSaveStaffAdd(id){
  let user_id = "";
  const manGroup = $("as_manual_group");

  if (manGroup && !manGroup.classList.contains("hidden")) {
    user_id = $("as_uid_manual")?.value.trim() || "";
  } else if ($("as_selected_uid")) {
    user_id = $("as_selected_uid").value.trim();
  } else {
    user_id = $("as_uid_manual")?.value.trim() || "";
  }

  const role = $("as_role") ? $("as_role").value.trim() : "TL";
  if (!user_id) return toast(t("selectOrEnterStaff"));

  try{
    await api("/api/mangas/"+id+"/staff",{method:"POST",body:JSON.stringify({user_id,role})});
    toast(t("staffAdded"),1);
    closeModal();
    loadMangas();
    doMangaDetail(id);
  }catch(e){toast(esc(e.message));}
}

async function doRemoveStaff(mid,uid,role){
  const msg=t("removeStaffConfirm", {role});
  if(!(await asyncConfirm(msg,"btn-danger")))return;
  try{
    await api("/api/mangas/"+mid+"/staff/"+encodeURIComponent(uid)+"/"+encodeURIComponent(role),{method:"DELETE"});
    toast(t("staffRemoved"),1);
    loadMangas();
    doMangaDetail(mid);
  }catch(e){toast(esc(e.message));}
}

/* ═══════════ CHAPTERS ═══════════ */
$("chSearchBtn").addEventListener("click",loadChapters);

$("chSearch").addEventListener("keydown",e=>{if(e.key==="Enter")loadChapters();});

async function loadChapters() {
  const gen = ++_loadGen;
  const rows = $("chRows"); 
  if (rows && !rows.children.length) {
    rows.innerHTML = '<div style="grid-column: 1 / -1; display: flex; justify-content: center; padding: 40px;"><div class="spinner"></div></div>';
  }
  
  const sq = $("chSearch").value.trim();
  try {
    const d = await api("/api/chapters/overview?search=" + encodeURIComponent(sq));
    if (gen !== _loadGen) return;
    clearBulk(); 
    
    // Disable pagination for overview since we return all matching mangas
    const pageChapters = $("pageChapters");
    if (pageChapters) pageChapters.innerHTML = ""; 

    const l = I[LANG];
    rows.innerHTML = (d.items || []).map(m => {
      const cover = m.cover_url ? `<img src="${esc(m.cover_url)}" class="manga-card-img" loading="lazy">` : `<div class="manga-card-empty">📚</div>`;
      
      return `
      <div class="manga-card" data-act="mangaChaptersModal" data-id="${m.id}" data-name="${esc(m.manga_name)}">
        ${cover}
        <div class="manga-card-overlay">
           <div class="manga-card-title" dir="auto">${esc(m.manga_name)}</div>
           <div class="manga-card-stats" style="margin-top:4px;">
              ${m.tl_avail > 0 ? `<span class="manga-card-badge blue">${l.badgeTlAvail}: ${m.tl_avail}</span>` : ''}
              ${m.pr_avail > 0 ? `<span class="manga-card-badge orange">${l.badgePrAvail}: ${m.pr_avail}</span>` : ''}
              ${m.ed_avail > 0 ? `<span class="manga-card-badge purple">${l.badgeEdAvail}: ${m.ed_avail}</span>` : ''}
              ${m.claimed > 0 ? `<span class="manga-card-badge">${l.badgeClaimed}: ${m.claimed}</span>` : ''}
              ${m.in_review > 0 ? `<span class="manga-card-badge red">${l.badgeInReview}: ${m.in_review}</span>` : ''}
              ${(m.tl_avail+m.pr_avail+m.ed_avail+m.claimed+m.in_review) === 0 ? `<span class="manga-card-badge green">${l.badgeCompleted} ${m.published}</span>` : ''}
           </div>
        </div>
      </div>`;
    }).join("") || `<div class="muted" style="grid-column: 1 / -1; text-align: center; padding: 40px;">${l.noData}</div>`;
  } catch(e) {
    if (gen === _loadGen) { toast(esc(e.message)); rows.innerHTML = ""; }
  }
}

async function doChAct(id, kind) {
  const actName = tAction(kind);
  const msg = t("chActConfirm", {act: actName, id});
  if(!(await asyncConfirm(msg, "btn-green")))return;
  try {
     await api("/api/chapters/"+id+"/"+kind,{method:"POST",body:"{}"});
     toast(t("chActDone", {act: actName, id}), 1);
     if (window._currMangaCh) doMangaChaptersModal(window._currMangaCh.id, window._currMangaCh.name);
     else if (window._currStaffData) doStaffDetail(window._currStaffData.user_id);
     else loadChapters();
  } catch(e) { toast(esc(e.message)); }
}

let _pendingRejectId = 0;
function doChReject(id) {
  _pendingRejectId = id;
  openModal(`<div style="padding:10px 0">
    <h3 style="margin-bottom:8px;font-size:18px;color:var(--txt)">✖ ${t("rejectTitle")} ${id}</h3>
    <p class="muted" style="margin-bottom:16px;font-size:13px">${t("rejectSub")}</p>
    <div class="field"><label>${t("rejectReason")}</label><textarea id="rj_r" rows="4" style="width:100%;background:rgba(0,0,0,0.2)"></textarea></div>
    <div class="row" style="justify-content:flex-end;gap:12px;margin-top:20px">
      <button class="btn-ghost" data-act="closeModal">${t("cancel")}</button>
      <button class="btn-danger" data-act="rejectGo" data-id="${id}">${t("rejectConfirmBtn")}</button>
    </div></div>`);
  setTimeout(()=>$("rj_r").focus(), 50);
}
async function doRejectSubmit(id) {
  const r=$("rj_r").value.trim();if(!r)return toast(t("reasonRequired"));
  closeModal();
  try {
     await api("/api/chapters/"+id+"/reject",{method:"POST",body:JSON.stringify({reason:r})});
     toast(t("rejectedCh", {id}),1);
     if (window._currMangaCh) doMangaChaptersModal(window._currMangaCh.id, window._currMangaCh.name);
     else loadChapters();
  } catch(err){toast(esc(err.message));}
}

async function doMangaChaptersModal(id, name) {
  const l = I[LANG];
  openModal(`
    <h2 dir="auto" style="margin-bottom:8px;">${l.modalChaptersTitle} <span style="color:var(--acc)">${esc(name)}</span></h2>
    <div class="tabs-header" style="margin-bottom:16px; display:flex; gap:8px;">
       <button class="tab-btn active" id="mcTab_avail" onclick="switchChapTab('avail')">${l.tabAvailable}</button>
       <button class="tab-btn" id="mcTab_prog" onclick="switchChapTab('prog')">${l.tabInProgress}</button>
       <button class="tab-btn" id="mcTab_rev" onclick="switchChapTab('rev')">${l.tabReviewDone}</button>
    </div>
    <div class="card table-wrap" style="max-height: 60vh; overflow-y: auto;">
      <table>
        <thead>
          <tr>
            <th width="40"><input type="checkbox" data-act="chkAll" data-target="mcRows"></th>
            <th>${l.cChapter}</th>
            <th>${l.cStatus}</th>
            <th>${l.cAssign}</th>
            <th>${l.cAct}</th>
          </tr>
        </thead>
        <tbody id="mcRows"><tr><td colspan="5" class="muted" style="text-align:center; padding:30px;"><div class="spinner"></div></td></tr></tbody>
      </table>
    </div>
  `);

  try {
    const d = await api("/api/chapters?limit=200&manga_id=" + id + "&sort=chapter_number&order=desc");
    const items = d.items || [];
    const prevTab = (window._currMangaCh && window._currMangaCh.id === id) ? window._currMangaCh.activeTab || "avail" : "avail";
    window._currMangaCh = {
       id, name,
       activeTab: prevTab,
       avail: items.filter(c => !c.assigned_to && ["pending_tl","pending_pr","pending_ed"].includes(c.status)),
       prog: items.filter(c => c.assigned_to && c.status !== "published"),
       rev: items.filter(c => ["pending_review","published","rejected"].includes(c.status))
    };
    switchChapTab(prevTab);
  } catch (e) {
    $("mcRows").innerHTML = `<tr><td colspan="5" class="muted warnbox">${esc(e.message)}</td></tr>`;
  }
}

window.switchChapTab = function(tabId) {
   if (!$("mcRows") || !window._currMangaCh) return; // Modal closed
   window._currMangaCh.activeTab = tabId;
   clearBulk(); // clear selection when switching tabs
   ["avail","prog","rev"].forEach(t => $("mcTab_"+t).classList.toggle("active", t === tabId));
   
   const items = window._currMangaCh[tabId] || [];
   let html = "";
   if (items.length === 0) {
      html = `<tr><td colspan="5" class="muted" style="text-align:center; padding:30px;">${I[LANG].noChaptersInList}</td></tr>`;
   } else {
      for (const c of items) {
         html += "<tr><td><input type=\"checkbox\" class=\"chk-row\" data-act=\"chkRow\" data-id=\""+c.id+"\"></td>"+
         "<td><b style='font-size:15px'>"+esc(c.chapter_number)+"</b><br><span class='muted' style='font-size:11px' dir='ltr'>id: "+c.id+"</span></td>"+
         "<td>"+pill(c.status)+"</td><td class='muted' dir='ltr'>"+esc(c.assigned_to_name||"—")+"</td>"+
         '<td><div class="row" style="justify-content:flex-start; gap:6px; flex-wrap:nowrap">'+
         (c.status==="pending_review"?'<button class="btn-small btn-green" data-act="chApprove" data-id="'+c.id+'" title="'+I[LANG].btnApprove+'">✅ '+I[LANG].btnApprove+'</button>':"")+
         (["pending_review","pending_ed","pending_pr","pending_tl"].includes(c.status)?'<button class="btn-small btn-danger" data-act="chReject" data-id="'+c.id+'" title="'+I[LANG].btnReject+'">✖ '+I[LANG].btnReject+'</button>':"")+
         (["published","rejected"].includes(c.status)?'<button class="btn-small btn-ghost" data-act="chReopen" data-id="'+c.id+'" title="'+I[LANG].btnReopen+'">🔄 '+I[LANG].btnReopen+'</button>':"")+
         (c.assigned_to?'<button class="btn-small btn-ghost" data-act="chUnclaim" data-id="'+c.id+'" title="'+I[LANG].btnUnclaim+'">🔓 '+I[LANG].btnUnclaim+'</button>':"")+
         "</div></td></tr>";
      }
   }
   $("mcRows").innerHTML = html;
};

/* ═══════════ STAFF ═══════════ */
const debouncedStaffSearch=debounce(()=>loadStaff(),DEBOUNCE_MS);
$("staffSearch")?.addEventListener("input",debouncedStaffSearch);
$("staffSearch")?.addEventListener("keydown",e=>{if(e.key==="Enter")loadStaff();});
$("staffSearchBtn")?.addEventListener("click",loadStaff);
$("staffRoleF")?.addEventListener("change",loadStaff);
$("staffWorkloadF")?.addEventListener("change",loadStaff);
$("staffPayF")?.addEventListener("change",loadStaff);

async function loadStaff(){
  const gen=++_loadGen;
  const rows=$("staffRows");
  const sg=$("staffGrid");
  if(rows && !rows.children.length) rows.innerHTML=skelRow(6);
  if(sg && !sg.children.length) showSkeleton(sg,4,"stats");

  const q=$("staffSearch")?$("staffSearch").value.trim():"";
  const role=$("staffRoleF")?$("staffRoleF").value:"";
  const wl=$("staffWorkloadF")?$("staffWorkloadF").value:"";
  const pay=$("staffPayF")?$("staffPayF").value:"";

  const params=new URLSearchParams();
  if(q) params.set("search",q);
  if(role) params.set("role",role);
  if(wl) params.set("workload",wl);
  if(pay) params.set("payment",pay);

  try{
    const d=await api("/api/staff"+(params.toString()?"?"+params.toString():""));
    if(gen!==_loadGen)return;

    // Render Stats Grid
    if(sg && d.stats){
      sg.innerHTML=
        statCard("👥",d.stats.total_staff,I[LANG].statTotalStaff,"purple")+
        statCard("🟢",d.stats.active_staff,I[LANG].statActiveStaff,"green")+
        statCard("⚪",d.stats.idle_staff,I[LANG].statIdleStaff,"blue")+
        statCard("💰",money(d.stats.total_unpaid_cents),I[LANG].lblUnpaid+" ("+d.stats.total_unpaid_count+")","orange");
    }

    let items=d.items||[];

    // Sort items if tableState.staff is set
    const st=tableState.staff;
    if(st && st.sort){
      items.sort((a,b)=>{
        let valA,valB;
        if(st.sort==="username"){
          valA=(a.username||a.user_id||"").toLowerCase();
          valB=(b.username||b.user_id||"").toLowerCase();
          return st.order==="desc"?valB.localeCompare(valA):valA.localeCompare(valB);
        }else if(st.sort==="active_mangas"){
          valA=a.active_mangas||0;
          valB=b.active_mangas||0;
        }else if(st.sort==="claimed_chapters"){
          valA=a.claimed_chapters||0;
          valB=b.claimed_chapters||0;
        }else if(st.sort==="unpaid_cents"){
          valA=a.unpaid_cents||0;
          valB=b.unpaid_cents||0;
        }else{
          valA=(a.active_mangas||0)+(a.claimed_chapters||0);
          valB=(b.active_mangas||0)+(b.claimed_chapters||0);
        }
        return st.order==="desc"?valB-valA:valA-valB;
      });
    }

    if(!items.length){
      rows.innerHTML='<tr><td colspan="6" class="muted" style="text-align:center; padding:30px;">'+I[LANG].noData+'</td></tr>';
      return;
    }

    rows.innerHTML=items.map(u=>{
      const load=(u.active_mangas||0)+(u.claimed_chapters||0);
      const dot=load>=5?"🔴":load>=3?"🟡":load>=1?"🟢":"⚪";
      const av=u.avatar_url?'<img src="'+esc(u.avatar_url)+'" class="avatar-sm" loading="lazy">':'<div class="avatar-sm empty"></div>';
      const nameHtml='<div class="row staff-user-cell" data-act="staffDetail" data-uid="'+esc(u.user_id)+'" style="flex-wrap:nowrap; gap:12px; cursor:pointer;" title="'+esc(I[LANG].staffProfileTitle)+'">'+
        av+'<div><b style="color:var(--acc);text-decoration:underline;text-underline-offset:3px">'+esc(u.username||u.user_id)+'</b><br><span class="muted" dir="ltr" style="font-size:11px">'+esc(u.user_id)+'</span></div></div>';
      return '<tr>'+
        '<td>'+nameHtml+'</td>'+
        '<td dir="ltr">'+(u.roles||[]).map(r=>pill(r)).join(" ")+'</td>'+
        '<td>'+dot+' '+(u.active_mangas||0)+'</td>'+
        '<td>'+(u.claimed_chapters||0)+'</td>'+
        '<td><b>'+money(u.unpaid_cents)+'</b></td>'+
        '<td><div class="row" style="justify-content:flex-start; gap:6px; flex-wrap:nowrap">'+
          '<button class="btn-small btn-ghost" data-act="staffDetail" data-uid="'+esc(u.user_id)+'" title="'+esc(I[LANG].btnView)+'">👁️ '+I[LANG].btnView+'</button>'+
          '<button class="btn-small btn-green" '+(u.unpaid_cents>0?'':'disabled')+' data-act="payUser" data-uid="'+esc(u.user_id)+'" title="'+esc(I[LANG].btnPay)+'">'+I[LANG].btnPay+'</button>'+
        '</div></td>'+
      '</tr>';
    }).join("");
  }catch(e){
    if(gen===_loadGen){
      toast(esc(e.message));
      rows.innerHTML='<tr><td colspan="6" class="muted warnbox">'+esc(e.message)+'</td></tr>';
    }
  }
}

window._currStaffData=null;
async function doStaffDetail(uid){
  openModal(
    '<div style="padding:20px; text-align:center;">'+
      '<div class="skeleton skeleton-stat" style="height:60px; margin-bottom:16px;"></div>'+
      '<div class="skeleton skeleton-text" style="width:70%; margin:0 auto 12px;"></div>'+
      '<div class="skeleton skeleton-text" style="width:50%; margin:0 auto;"></div>'+
    '</div>'
  );
  try{
    const d=await api("/api/staff/"+uid);
    window._currStaffData=d;
    renderStaffModal("claims");
  }catch(e){
    toast(esc(e.message));
    closeModal();
  }
}

function renderStaffModal(activeTab){
  const d=window._currStaffData;
  if(!d)return;

  const av=d.avatar_url
    ?'<img src="'+esc(d.avatar_url)+'" class="avatar-lg" loading="lazy">'
    :'<div class="avatar-lg empty"></div>';

  const rolesHtml=(d.roles||[]).map(r=>pill(r)).join(" ")||'<span class="muted">—</span>';
  const canPayBadge=d.can_manage_payments?'<span class="pill" style="background:rgba(59,130,246,0.15);color:#60a5fa;border:1px solid rgba(59,130,246,0.3)">💳 Admin/Finance</span>':'';

  let walletHtml="";
  if(d.payment_info){
    walletHtml=
      '<div class="row" style="justify-content:space-between; align-items:center; background:rgba(0,0,0,0.15); padding:8px 12px; border-radius:var(--radius-sm); border:1px solid var(--border);">'+
        '<div style="font-size:12px; overflow:hidden; text-overflow:ellipsis;">'+
          '<span class="muted">'+I[LANG].walletInfo+(d.payment_method?' ('+esc(d.payment_method)+')':'')+': </span>'+
          '<b dir="ltr" style="color:var(--primary); word-break:break-all;">'+esc(d.payment_info)+'</b>'+
        '</div>'+
        '<button type="button" class="btn-small btn-ghost" data-act="copyText" data-copy="'+esc(d.payment_info)+'" data-msg="'+esc(I[LANG].walletCopied)+'">📋 '+I[LANG].copyWallet+'</button>'+
      '</div>';
  }else{
    walletHtml='<div class="muted" style="font-size:12px;">⚠️ '+I[LANG].noPaymentMethod+'</div>';
  }

  const driveHtml=d.drive_email
    ?'<div style="font-size:12px;"><span class="muted">'+I[LANG].driveEmailLbl+': </span><b dir="ltr">'+esc(d.drive_email)+'</b></div>'
    :'<div class="muted" style="font-size:12px;">⚠️ '+I[LANG].noDriveEmail+'</div>';

  const statsHtml=
    '<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(110px, 1fr)); gap:10px; margin:14px 0;">'+
      '<div class="card" style="padding:10px; text-align:center; background:rgba(255,255,255,0.02);">'+
        '<div class="muted" style="font-size:11px;">'+I[LANG].cClaimed+'</div>'+
        '<b style="font-size:18px;">'+(d.stats.claimed_chapters||0)+'</b>'+
      '</div>'+
      '<div class="card" style="padding:10px; text-align:center; background:rgba(255,255,255,0.02);">'+
        '<div class="muted" style="font-size:11px;">'+I[LANG].cMangas+'</div>'+
        '<b style="font-size:18px;">'+(d.stats.active_mangas||0)+'</b>'+
      '</div>'+
      '<div class="card" style="padding:10px; text-align:center; background:rgba(255,255,255,0.02);">'+
        '<div class="muted" style="font-size:11px;">'+I[LANG].completedChapters+'</div>'+
        '<b style="font-size:18px;">'+(d.stats.completed_chapters||0)+'</b>'+
      '</div>'+
      '<div class="card" style="padding:10px; text-align:center; background:rgba(255,255,255,0.02);">'+
        '<div class="muted" style="font-size:11px;">'+I[LANG].lblUnpaid+'</div>'+
        '<b style="font-size:18px; color:var(--orange);">'+money(d.stats.unpaid_cents)+'</b>'+
      '</div>'+
      '<div class="card" style="padding:10px; text-align:center; background:rgba(255,255,255,0.02);">'+
        '<div class="muted" style="font-size:11px;">'+I[LANG].totalEarned+'</div>'+
        '<b style="font-size:18px; color:var(--green);">'+money(d.stats.paid_all_time_cents)+'</b>'+
      '</div>'+
    '</div>';

  let tabContentHtml="";
  if(activeTab==="claims"){
    const claims=d.claimed_chapters||[];
    if(!claims.length){
      tabContentHtml='<div class="muted" style="text-align:center; padding:24px;">'+I[LANG].noActiveClaims+'</div>';
    }else{
      tabContentHtml=
        '<div class="table-wrap" style="max-height:240px; overflow-y:auto;">'+
          '<table>'+
            '<thead><tr><th>'+I[LANG].cChapter+'</th><th>'+I[LANG].cStatus+'</th><th>'+I[LANG].cAct+'</th></tr></thead>'+
            '<tbody>'+
              claims.map(c=>
                '<tr>'+
                  '<td><b>'+esc(c.manga_name)+'</b> - Ch '+esc(c.chapter_number)+'</td>'+
                  '<td>'+pill(c.status)+'</td>'+
                  '<td><button class="btn-small btn-ghost" data-act="chUnclaim" data-id="'+c.id+'" title="'+I[LANG].btnUnclaim+'">🔓 '+I[LANG].btnUnclaim+'</button></td>'+
                '</tr>'
              ).join("")+
            '</tbody>'+
          '</table>'+
        '</div>';
    }
  }else if(activeTab==="mangas"){
    const mangas=d.assigned_mangas||[];
    if(!mangas.length){
      tabContentHtml='<div class="muted" style="text-align:center; padding:24px;">'+I[LANG].noAssignedMangas+'</div>';
    }else{
      tabContentHtml=
        '<div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(200px, 1fr)); gap:10px; max-height:240px; overflow-y:auto; padding:4px;">'+
          mangas.map(m=>
            '<div class="card" style="padding:10px; display:flex; align-items:center; gap:10px; background:rgba(255,255,255,0.02);">'+
              (m.cover_url?'<img src="'+esc(m.cover_url)+'" style="width:36px;height:48px;border-radius:4px;object-fit:cover;">':'<div style="width:36px;height:48px;border-radius:4px;background:var(--glow);display:flex;align-items:center;justify-content:center;">📚</div>')+
              '<div style="overflow:hidden;">'+
                '<b style="font-size:13px; display:block; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">'+esc(m.name)+'</b>'+
                '<div style="margin-top:4px;">'+pill(m.role)+'</div>'+
              '</div>'+
            '</div>'
          ).join("")+
        '</div>';
    }
  }else if(activeTab==="unpaid"){
    const unpaids=d.unpaid_transactions||[];
    if(!unpaids.length){
      tabContentHtml='<div class="muted" style="text-align:center; padding:24px;">'+I[LANG].noUnpaidTransactions+'</div>';
    }else{
      tabContentHtml=
        '<div class="table-wrap" style="max-height:240px; overflow-y:auto;">'+
          '<table>'+
            '<thead><tr><th>'+I[LANG].cChapter+'</th><th>'+I[LANG].cRoles+'</th><th>'+I[LANG].cAmount+'</th></tr></thead>'+
            '<tbody>'+
              unpaids.map(t=>
                '<tr>'+
                  '<td><b>'+esc(t.manga_name)+'</b> - Ch '+esc(t.chapter_number)+'</td>'+
                  '<td>'+pill(t.role)+'</td>'+
                  '<td><b style="color:var(--orange);">'+money(t.amount_cents)+'</b></td>'+
                '</tr>'
              ).join("")+
            '</tbody>'+
          '</table>'+
        '</div>';
    }
  }

  const html=
    '<div style="padding:6px 0;">'+
      '<div class="row" style="justify-content:space-between; align-items:flex-start; margin-bottom:12px; gap:12px;">'+
        '<div class="row" style="gap:14px; align-items:center;">'+
          av+
          '<div>'+
            '<div class="row" style="gap:8px; align-items:center;">'+
              '<h2 style="margin:0; font-size:18px;">'+esc(d.username||d.user_id)+'</h2>'+
              canPayBadge+
            '</div>'+
            '<div class="row" style="gap:8px; align-items:center; margin-top:4px;">'+
              '<span class="muted" dir="ltr" style="font-size:12px;">'+esc(d.user_id)+'</span>'+
              '<button type="button" class="btn-small btn-ghost" data-act="copyText" data-copy="'+esc(d.user_id)+'" data-msg="'+esc(I[LANG].idCopied)+'" style="padding:2px 6px; font-size:11px;">'+I[LANG].copyId+'</button>'+
            '</div>'+
            '<div style="margin-top:6px;">'+rolesHtml+'</div>'+
          '</div>'+
        '</div>'+
        '<button type="button" class="btn-small btn-ghost" data-act="editStaffModal" data-uid="'+esc(d.user_id)+'">✏️ '+t("editProfile")+'</button>'+
      '</div>'+

      '<div style="display:flex; flex-direction:column; gap:6px; margin-bottom:10px;">'+
        walletHtml+
        driveHtml+
      '</div>'+

      statsHtml+

      '<div class="tabs-header" style="display:flex; gap:8px; margin-bottom:12px;">'+
        '<button type="button" class="tab-btn '+(activeTab==="claims"?"active":"")+'" data-act="staffModalTab" data-tab="claims">'+
          I[LANG].tabClaims+' ('+(d.claimed_chapters||[]).length+')'+
        '</button>'+
        '<button type="button" class="tab-btn '+(activeTab==="mangas"?"active":"")+'" data-act="staffModalTab" data-tab="mangas">'+
          I[LANG].tabMangas+' ('+(d.assigned_mangas||[]).length+')'+
        '</button>'+
        '<button type="button" class="tab-btn '+(activeTab==="unpaid"?"active":"")+'" data-act="staffModalTab" data-tab="unpaid">'+
          I[LANG].tabUnpaidLedger+' ('+(d.unpaid_transactions||[]).length+')'+
        '</button>'+
      '</div>'+

      '<div class="card" style="padding:10px; margin-bottom:16px;">'+
        tabContentHtml+
      '</div>'+

      '<div class="row" style="justify-content:flex-end; gap:10px;">'+
        (d.stats.unpaid_cents>0?'<button type="button" class="btn-green" data-act="payUser" data-uid="'+esc(d.user_id)+'">💸 '+I[LANG].btnPay+' ('+money(d.stats.unpaid_cents)+')</button>':'')+
        '<button type="button" class="btn-ghost" data-act="closeModal">'+I[LANG].close+'</button>'+
      '</div>'+
    '</div>';

  openModal(html);
}

async function doEditStaffModal(uid){
  let d = window._currStaffData;
  if (!d || d.user_id !== uid) {
    try {
      d = await api("/api/staff/" + uid);
      window._currStaffData = d;
    } catch(e) {
      return toast(esc(e.message));
    }
  }

  const currentRoles = new Set(d.roles || []);
  const allRoles = [
    {id: "raw", label: tRole("raw")},
    {id: "translation", label: tRole("translation")},
    {id: "proofreading", label: tRole("proofreading")},
    {id: "editing", label: tRole("editing")}
  ];

  const roleChecksHtml = allRoles.map(r => `
    <label style="display:flex; align-items:center; gap:6px; cursor:pointer; font-size:13px;">
      <input type="checkbox" class="es-role-cb" value="${r.id}" ${currentRoles.has(r.id) ? 'checked' : ''}>
      ${r.label}
    </label>
  `).join("");

  openModal(`<h3>${t("editStaffTitle")} (${esc(d.username || uid)})</h3>`+
    `<div class="field"><label>${t("payMethod")}</label><input id="es_paymethod" value="${esc(d.payment_method||'')}"></div>`+
    `<div class="field"><label>${t("payInfo")}</label><input id="es_payinfo" dir="ltr" value="${esc(d.payment_info||'')}"></div>`+
    `<div class="field"><label>${t("driveEmail")}</label><input id="es_driveemail" dir="ltr" type="email" value="${esc(d.drive_email||'')}"></div>`+
    `<div class="field"><label>${t("assignedRoles")}</label>`+
      `<div style="display:flex; flex-direction:column; gap:6px; margin-top:4px;">`+
        `${roleChecksHtml}`+
      `</div>`+
    `</div>`+
    `<div class="row" style="justify-content:flex-end; gap:8px; margin-top:16px;">`+
      `<button class="btn-green" data-act="saveStaffEdit" data-uid="${esc(uid)}">${t("save")}</button>`+
      `<button class="btn-ghost" data-act="closeModal">${t("cancel")}</button>`+
    `</div>`);
}

async function doSaveStaffEdit(uid){
  const payment_method = $("es_paymethod").value.trim();
  const payment_info = $("es_payinfo").value.trim();
  const drive_email = $("es_driveemail").value.trim();
  const roles = [];
  document.querySelectorAll(".es-role-cb:checked").forEach(cb => roles.push(cb.value));

  try {
    await api("/api/staff/" + uid, {
      method: "PUT",
      body: JSON.stringify({payment_method, payment_info, drive_email, roles})
    });
    toast(t("staffProfileUpdated"), 1);
    closeModal();
    loadStaff();
    doStaffDetail(uid);
  } catch(e) {
    toast(esc(e.message));
  }
}

async function doPayUser(uid,src){
  let payInfo="";
  let payMethod="";
  let name=uid;
  let unpaidCents=0;
  if(window._currStaffData && window._currStaffData.user_id===uid){
    payInfo=window._currStaffData.payment_info||"";
    payMethod=window._currStaffData.payment_method||"";
    name=window._currStaffData.username||uid;
    unpaidCents=window._currStaffData.stats?.unpaid_cents||0;
  }else{
    try{
      const d=await api("/api/staff/"+uid);
      payInfo=d.payment_info||"";
      payMethod=d.payment_method||"";
      name=d.username||uid;
      unpaidCents=d.stats?.unpaid_cents||0;
    }catch(e){}
  }

  let walletSnippet="";
  if(payInfo){
    walletSnippet=
      '<div style="background:var(--card-bg); border:1px solid var(--border); border-radius:var(--radius-sm); padding:12px; margin:12px 0; text-align:start;">'+
        '<div style="font-size:12px; color:var(--muted); margin-bottom:4px;">'+I[LANG].walletInfo+(payMethod?' ('+esc(payMethod)+')':'')+':</div>'+
        '<div class="row" style="justify-content:space-between; align-items:center; gap:8px;">'+
          '<code style="font-size:13px; font-weight:600; color:var(--primary); word-break:break-all;" dir="ltr">'+esc(payInfo)+'</code>'+
          '<button type="button" class="btn-small btn-ghost" data-act="copyText" data-copy="'+esc(payInfo)+'" data-msg="'+esc(I[LANG].walletCopied)+'">📋 '+I[LANG].copyWallet+'</button>'+
        '</div>'+
      '</div>';
  }else{
    walletSnippet='<div class="muted" style="margin:8px 0; font-size:12px;">⚠️ '+I[LANG].noPaymentMethod+'</div>';
  }

  const promptMsg = t("payConfirmPrompt", {
    prompt: t("payConfirmWalletPrompt"),
    name: esc(name),
    unpaid: unpaidCents > 0 ? ' (' + money(unpaidCents) + ')' : '',
    wallet: walletSnippet
  });

  if(!(await asyncConfirm(promptMsg,"btn-green")))return;
  try{
    const r=await api("/api/staff/"+uid+"/pay",{method:"POST"});
    toast(I[LANG].lblPaid+" <b>"+money(r.paid_cents)+"</b>",1);
    closeModal();
    if(src==="finance")loadFinance();else loadStaff();
  }catch(e){
    toast(esc(e.message));
  }
}

/* ═══════════ FINANCE ═══════════ */
async function loadFinance(){const gen=++_loadGen;
  const fg=$("finGrid");
  if(fg && !fg.children.length) showSkeleton(fg,2,"stats");
  try{const f=await api("/api/finance/overview");if(gen!==_loadGen)return;
    fg.innerHTML=statCard("✅",money(f.paid_all_time.cents),I[LANG].lblPaid+" ("+f.paid_all_time.count+")","green")+
      statCard("💰",money(f.unpaid.cents),I[LANG].lblUnpaid+" ("+f.unpaid.count+")","orange");
    $("topBalRows").innerHTML=(f.top_balances||[]).map(t=>{
      const av = t.avatar_url ? '<img src="'+esc(t.avatar_url)+'" class="avatar-sm" loading="lazy">' : '<div class="avatar-sm empty"></div>';
      const nameHtml = '<div class="row" style="flex-wrap:nowrap; gap:12px;">'+av+'<div><b>'+esc(t.username||t.user_id)+'</b><br><span class="muted" dir="ltr" style="font-size:11px">'+esc(t.user_id)+'</span></div></div>';
      return "<tr><td>"+nameHtml+"</td><td>"+t.count+"</td><td><b style='color:var(--orange)'>"+money(t.cents)+"</b></td>"+
      '<td><button class="btn-small btn-green" data-act="payUserFin" data-uid="'+esc(t.user_id)+'">'+I[LANG].btnPay+'</button></td></tr>';
    }).join("")||'<tr><td colspan="4" class="muted">—</td></tr>';
    $("recentPay").innerHTML=(f.recent_payments||[]).map(p=>{
      const av = p.avatar_url ? '<img src="'+esc(p.avatar_url)+'" class="avatar-sm" loading="lazy" style="width:24px;height:24px;margin-inline-end:8px">' : '';
      return '<div class="activity-item"><div class="row" style="justify-content:space-between;width:100%">'+
      '<div class="row" style="gap:8px"><b style="color:var(--green)">'+money(p.cents)+'</b>'+av+'<span>'+esc(p.username||p.user_id)+'</span></div>'+
      '<span class="activity-time">'+esc(p.at||"")+'</span></div></div>';
    }).join("")||'<div class="muted">—</div>';
    const pr=await api("/api/pricing");if(gen!==_loadGen)return;
    // Map pricing to inputs with translated labels
    $("priceRow").innerHTML=Object.entries(pr).map(([k,v])=>{
       const lbl = I[LANG]["pr_"+k] || k;
       return '<div class="field" style="margin:0"><label>'+esc(lbl)+'</label><div class="row" style="gap:4px;"><input dir="ltr" id="pr_'+esc(k)+'" value="'+esc(v)+'" type="number" min="0" max="10000" style="flex:1"><span class="muted" style="font-size:12px; margin-inline-start:8px">'+I[LANG].unitCents+'</span></div></div>';
    }).join("");
  }catch(e){if(gen===_loadGen){toast(esc(e.message));fg.innerHTML="";}}}

$("btnSavePricing").addEventListener("click", async () => {
  const keys = ["tl_cents", "pr_cents", "ed_under_30", "ed_30_44", "ed_45_plus"];
  const btn = $("btnSavePricing");
  btn.disabled = true;
  btn.innerHTML = I[LANG].saving;
  let success = 0;
  try {
    for(const k of keys) {
       const el = $("pr_"+k);
       if(!el) continue;
       const v = parseInt(el.value, 10);
       if(isNaN(v)) continue;
       await api("/api/pricing",{method:"PUT",body:JSON.stringify({key:k,value:v})});
       success++;
    }
    toast(I[LANG].pricesSaved.replace("{n}", success), 1);
  } catch(e) {
    toast(esc(e.message));
  }
  btn.disabled = false;
  btn.innerHTML = I[LANG].savePricing;
});

$("btnExportLedger")?.addEventListener("click", () => {
  window.open("/api/finance/export", "_blank");
});

/* ═══════════ JOBS ═══════════ */
$("jobSearchBtn").addEventListener("click",loadJobs);
$("jobStatusF").addEventListener("change",loadJobs);

async function loadJobs(){const gen=++_loadGen;
  const rows=$("jobRows");
  if(rows && !rows.children.length) rows.innerHTML=skelRow(6);
  const st=$("jobStatusF").value;
  try{const j=await api("/api/jobs?limit=50&status="+encodeURIComponent(st));if(gen!==_loadGen)return;
    $("jobCounts").innerHTML=Object.entries(j.counts||{}).map(([k,v])=>`<div class="row" style="gap:6px"><b>${esc(v)}</b> ${pill(k)}</div>`).join("")||"-";
    rows.innerHTML=(j.items||[]).map(x=>{
      let plText = "";
      if(x.payload) {
         if(x.payload.manga_id) plText += `Manga: <b>${esc(x.payload.manga_id)}</b> | `;
         if(x.payload.chapter_id) plText += `Chapter: <b>${esc(x.payload.chapter_id)}</b> | `;
         if(x.payload.drive_folder_id) plText += `Drive: ${esc(x.payload.drive_folder_id.substring(0,8))}...`;
         if(x.payload.channel_id) plText += `Chnl: ${esc(x.payload.channel_id)}`;
      }
      if(!plText) plText = esc(JSON.stringify(x.payload||{}));
      
      return "<tr><td>"+x.id+"</td><td><b>"+esc(x.job_type)+"</b></td><td class='muted' dir='ltr' style='font-size:12px'>"+plText+"</td><td>"+pill(x.status)+"</td><td>"+x.attempts+"</td><td class='muted' dir='ltr' style='font-size:12px'><span title='"+esc(x.created_at)+"'>"+fmtTime(x.created_at)+"</span></td></tr>";
    }).join("")||'<tr><td colspan="6" class="muted">—</td></tr>';
  }catch(e){if(gen===_loadGen){toast(esc(e.message));rows.innerHTML="";}}}

/* ═══════════ DATABASE TAB ═══════════ */
$("btnRefreshDb").addEventListener("click", () => {
  cacheDrop("/api/database");
  loadDatabase();
});

$("btnCreateBackup").addEventListener("click", async () => {
  if (SHARE_MODE) return toast(t("guestDbRestricted"));
  const btn = $("btnCreateBackup");
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> ' + t("creatingBackup");
  try {
    const res = await api("/api/database/backups", { method: "POST" });
    toast(t("backupCreated", { name: res.backup ? res.backup.filename : "OK" }), 1);
    cacheDrop("/api/database");
    loadDatabase();
  } catch (err) {
    toast(t("backupFailed", { err: err.message }));
  } finally {
    btn.disabled = false;
    btn.textContent = t("btnCreateBackup");
  }
});

const btnUpload = $("btnUploadBackup");
const inputUpload = $("inputBackupUpload");
if (btnUpload && inputUpload) {
  btnUpload.addEventListener("click", () => {
    if (SHARE_MODE) return toast(t("guestDbRestricted"));
    inputUpload.value = "";
    inputUpload.click();
  });

  inputUpload.addEventListener("change", async (e) => {
    if (SHARE_MODE) return toast(t("guestDbRestricted"));
    const file = e.target.files && e.target.files[0];
    if (!file) return;

    if (!file.name.toLowerCase().endsWith(".sql")) {
      toast(t("invalidSqlFile"));
      inputUpload.value = "";
      return;
    }

    const MAX_SIZE = 100 * 1024 * 1024; // 100 MB
    if (file.size > MAX_SIZE) {
      toast(t("fileTooLarge"));
      inputUpload.value = "";
      return;
    }

    const origHtml = btnUpload.innerHTML;
    btnUpload.disabled = true;
    btnUpload.innerHTML = '<span class="spin"></span> ' + t("uploadingBackup");

    try {
      const formData = new FormData();
      formData.append("file", file);

      const res = await api("/api/database/backups/upload", {
        method: "POST",
        body: formData,
        timeout: 120000
      });

      toast(t("backupUploaded", { name: res.backup ? res.backup.filename : file.name }), 1);
      cacheDrop("/api/database");
      loadDatabase();
    } catch (err) {
      toast(t("uploadFailed", { err: err.message }));
    } finally {
      btnUpload.disabled = false;
      btnUpload.innerHTML = origHtml;
      inputUpload.value = "";
    }
  });
}

async function doDeleteBackup(filename) {
  if (SHARE_MODE) return toast(t("guestDbRestricted"));
  if (!filename) return;
  const ok = await asyncConfirm(
    t("deleteBackupConfirm", { name: esc(filename) }),
    "btn-danger",
    t("deleteBackup")
  );
  if (!ok) return;
  try {
    await api("/api/database/backups/" + encodeURIComponent(filename), { method: "DELETE" });
    toast(t("backupDeleted", { name: filename }), 1);
    cacheDrop("/api/database");
    loadDatabase();
  } catch (err) {
    toast(esc(err.message));
  }
}

async function loadDatabase() {
  const gen = ++_loadGen;
  if (SHARE_MODE) {
    $("dbGrid").innerHTML = `<div class="card" style="grid-column:1/-1;text-align:center;padding:24px"><p class="muted">${t("guestDbRestricted")}</p></div>`;
    $("dbBackupRows").innerHTML = `<tr><td colspan="4" class="muted" style="text-align:center">${t("guestDbRestricted")}</td></tr>`;
    return;
  }

  const rows = $("dbBackupRows");
  if(rows && !rows.children.length) rows.innerHTML = skelRow(4);

  try {
    // 1. Load Status
    const status = await api("/api/database/status");
    if (gen !== _loadGen) return;

    const g = $("dbGrid");
    const poolText = status.pool ? `${status.pool.free} / ${status.pool.size} (max ${status.pool.max})` : "-";
    const totalRecords = status.total_records != null ? status.total_records : (
      (Number(status.tables?.mangas) || 0) +
      (Number(status.tables?.chapters) || 0) +
      (Number(status.tables?.staff) || 0) +
      (Number(status.tables?.transactions) || 0)
    );

    g.innerHTML = `
      <div class="stat" data-color="purple">
        <div class="stat-icon">🛡️</div>
        <div class="stat-value" dir="ltr">v${esc(status.schema_version)}</div>
        <div class="stat-label">${t("dbSchemaVer")}</div>
        <div class="muted" style="font-size:11px;margin-top:4px">PostgreSQL Migration</div>
      </div>
      <div class="stat" data-color="blue">
        <div class="stat-icon">⚡</div>
        <div class="stat-value" style="font-size:20px" dir="ltr">${esc(poolText)}</div>
        <div class="stat-label">${t("dbPoolConns")}</div>
        <div class="muted" style="font-size:11px;margin-top:4px">Free / Current (Max Pool)</div>
      </div>
      <div class="stat" data-color="green">
        <div class="stat-icon">📊</div>
        <div class="stat-value">${esc(totalRecords)}</div>
        <div class="stat-label">${t("dbTotalRecords")}</div>
        <div class="muted" style="font-size:11px;margin-top:4px" dir="ltr">${esc(status.tables?.mangas||0)} mangas, ${esc(status.tables?.chapters||0)} chs</div>
      </div>
      <div class="stat" data-color="yellow">
        <div class="stat-icon">💾</div>
        <div class="stat-value">${esc(status.backups_count)}</div>
        <div class="stat-label">${t("dbTotalBackups")}</div>
        <div class="muted" style="font-size:11px;margin-top:4px" dir="ltr">${esc(status.backups_total_size_human)} on disk</div>
      </div>
    `;

    // 2. Load Backups List
    const data = await api("/api/database/backups");
    if (gen !== _loadGen) return;

    const backups = data.backups || [];
    if (backups.length === 0) {
      rows.innerHTML = `<tr><td colspan="4" class="muted" style="text-align:center;padding:24px">${t("noBackupsFound")}</td></tr>`;
      return;
    }

    rows.innerHTML = backups.map(b => {
      return `<tr>
        <td>
          <div style="display:flex;align-items:center;gap:8px">
            <span style="font-size:18px">💾</span>
            <a href="javascript:void(0)" data-act="inspectBackup" data-file="${esc(b.filename)}" style="font-weight:700;color:var(--txt);text-decoration:none;cursor:pointer" title="${t("inspectBackup")}">${esc(b.filename)}</a>
          </div>
        </td>
        <td class="muted" dir="ltr" style="font-size:12px"><span title="${esc(b.created_at)}">${fmtTime(b.created_at)}</span></td>
        <td><span class="pill">${esc(b.size_human)}</span></td>
        <td style="text-align:end">
          <div class="row" style="justify-content:flex-end;gap:8px">
            <button class="btn-small btn-ghost" data-act="inspectBackup" data-file="${esc(b.filename)}" style="padding:4px 10px;font-size:12px;display:inline-flex;align-items:center;gap:4px">
              ${t("inspectBackup")}
            </button>
            <a href="/api/database/backups/${encodeURIComponent(b.filename)}/download" class="btn-small btn-ghost" style="text-decoration:none;padding:4px 10px;font-size:12px;display:inline-flex;align-items:center;gap:4px">${t("downloadBackup")}</a>
            <button class="btn-small btn-danger" data-act="delBackup" data-file="${esc(b.filename)}" style="padding:4px 10px;font-size:12px">${t("deleteBackup")}</button>
          </div>
        </td>
      </tr>`;
    }).join("");

  } catch (err) {
    if (gen === _loadGen) {
      toast(esc(err.message));
      rows.innerHTML = `<tr><td colspan="4" class="muted" style="text-align:center">${esc(err.message)}</td></tr>`;
    }
  }
}

window._currInspectData = null;
window._currInspectCat = 'all';

async function doInspectBackup(filename) {
  window._currInspectCat = 'all';
  openModal(
    `<div style="padding:36px 20px;text-align:center;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px">
      <div class="loader-orbit">
        <div class="loader-icon">🗄️</div>
      </div>
      <div>
        <h3 style="margin:0 0 8px 0;font-size:17px;font-weight:700;color:var(--txt)">${t("inspectingBackup")}</h3>
        <p class="muted" style="margin:0;font-size:12px;font-family:monospace;background:rgba(255,255,255,0.05);padding:3px 12px;border-radius:6px;display:inline-block;" dir="ltr">${esc(filename)}</p>
      </div>
      <div class="loader-bar"></div>
    </div>`,
    480
  );

  try {
    const data = await api("/api/database/backups/" + encodeURIComponent(filename) + "/inspect");
    window._currInspectData = data;
    const defaultTable = data.tables && data.tables.length > 0 ? data.tables[0].name : null;
    renderInspectModal(data, defaultTable, 'all');
  } catch (err) {
    toast(esc(err.message));
    closeModal();
  }
}

function formatInspectCol(c) {
  if (!c) return "";
  const s = String(c);
  if (s.toLowerCase() === "id") return "ID";
  return s.replace(/_/g, " ").replace(/\b\w/g, l => l.toUpperCase());
}

function formatInspectCell(col, val) {
  if (val === null || val === undefined || val === "") {
    return `<span class="muted" style="opacity:0.35">—</span>`;
  }
  const colLower = String(col).toLowerCase();
  const strVal = String(val);

  // 1. Status or Stage styling with colored badges
  if (colLower === "status" || colLower === "stage") {
    return pill(strVal);
  }

  // 2. Money / Cents formatting
  if (colLower.includes("cents") || colLower === "amount") {
    const num = parseInt(strVal, 10);
    if (!isNaN(num)) {
      return `<b style="color:var(--green);font-family:monospace;" dir="ltr">${money(num)}</b> <span class="muted" style="font-size:10px">(${num}¢)</span>`;
    }
  }

  // 3. Timestamps formatting
  if (colLower.endsWith("_at") || colLower === "timestamp" || colLower === "sent_at") {
    return `<span class="muted" style="font-size:11px" dir="ltr" title="${esc(strVal)}">${fmtTime(strVal)}</span>`;
  }

  // 4. JSON Payloads (e.g. background_jobs payload, audit details)
  if ((strVal.startsWith("{") && strVal.endsWith("}")) || (strVal.startsWith("[") && strVal.endsWith("]"))) {
    try {
      const obj = JSON.parse(strVal);
      if (typeof obj === "object" && obj !== null) {
        const entries = Object.entries(obj);
        if (entries.length === 0) return `<span class="muted">{}</span>`;
        return `<div style="display:flex;flex-wrap:wrap;gap:4px">` +
          entries.map(([k, v]) => {
            let cleanK = k.replace(/_/g, " ");
            let cleanV = String(v);
            if (cleanV.length > 25) cleanV = cleanV.substring(0, 25) + "…";
            return `<span class="pill" style="font-size:10px;padding:2px 6px;background:rgba(139,92,246,0.12);border:1px solid rgba(139,92,246,0.25)" title="${esc(k)}: ${esc(String(v))}"><span class="muted">${esc(cleanK)}:</span> <b>${esc(cleanV)}</b></span>`;
          }).join("") +
        `</div>`;
      }
    } catch (_) {}
  }

  // 5. Discord IDs / User IDs
  if (colLower.includes("discord_id") || colLower === "user_id" || colLower === "paid_by" || colLower === "changed_by" || colLower === "assigned_to") {
    return `<span style="display:inline-flex;align-items:center;gap:4px;font-family:monospace;font-size:11px" dir="ltr"><span style="opacity:0.6">👤</span><code>${esc(strVal)}</code></span>`;
  }

  // 6. Boolean values
  if (strVal === "true" || strVal === "t") {
    return `<span class="pill" style="background:rgba(34,197,94,0.15);color:var(--green);font-size:11px;padding:2px 8px">✔ Yes</span>`;
  }
  if (strVal === "false" || strVal === "f") {
    return `<span class="pill" style="background:rgba(239,68,68,0.15);color:var(--red);font-size:11px;padding:2px 8px">✖ No</span>`;
  }

  // 7. URLs
  if (strVal.startsWith("http://") || strVal.startsWith("https://")) {
    return `<a href="${esc(strVal)}" target="_blank" rel="noopener" style="color:var(--blue);text-decoration:underline;font-size:11px" dir="ltr">🔗 Link</a>`;
  }

  // Long text truncation
  if (strVal.length > 40) {
    return `<span title="${esc(strVal)}" dir="auto">${esc(strVal.substring(0, 40))}…</span>`;
  }
  return `<span dir="auto">${esc(strVal)}</span>`;
}

function renderInspectModal(data, activeTableName, activeCat) {
  if (!data) return;
  const tables = data.tables || [];

  if (activeCat) {
    window._currInspectCat = activeCat;
  }
  const curCat = window._currInspectCat || 'all';

  // Filter visible tables by category
  let visibleTables = tables;
  if (curCat !== 'all') {
    visibleTables = tables.filter(t => t.category === curCat);
    if (visibleTables.length === 0) {
      visibleTables = tables;
      window._currInspectCat = 'all';
    }
  }

  // Resolve current active table
  let currentTable = null;
  if (activeTableName) {
    currentTable = tables.find(t => t.name === activeTableName);
    if (currentTable && currentTable.category && curCat !== 'all' && currentTable.category !== curCat) {
      window._currInspectCat = currentTable.category;
      visibleTables = tables.filter(t => t.category === currentTable.category);
    }
  }
  if (!currentTable) {
    currentTable = visibleTables[0] || tables[0] || null;
  }

  // Category counts
  const catCounts = {
    all: tables.length,
    core: tables.filter(t => t.category === 'core').length,
    finance: tables.filter(t => t.category === 'finance').length,
    audit: tables.filter(t => t.category === 'audit').length,
    system: tables.filter(t => t.category === 'system').length,
  };

  // Render Category Filter Buttons
  const activeCatStyle = "background:var(--acc);color:#fff;border-color:var(--acc);font-weight:700;";
  const inactiveCatStyle = "background:rgba(255,255,255,0.04);color:var(--muted);";

  const categoriesHtml = `
    <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
      <span class="muted" style="font-size:11px;font-weight:700">${t("inspectFilterCat")}:</span>
      <button type="button" class="btn-small btn-ghost" data-act="inspectCat" data-cat="all" style="padding:4px 10px;font-size:11px;border-radius:12px;${curCat === 'all' ? activeCatStyle : inactiveCatStyle}">
        🌟 ${t("catAll")} (${catCounts.all})
      </button>
      <button type="button" class="btn-small btn-ghost" data-act="inspectCat" data-cat="core" style="padding:4px 10px;font-size:11px;border-radius:12px;${curCat === 'core' ? activeCatStyle : inactiveCatStyle}">
        📚 ${t("catCore")} (${catCounts.core})
      </button>
      <button type="button" class="btn-small btn-ghost" data-act="inspectCat" data-cat="finance" style="padding:4px 10px;font-size:11px;border-radius:12px;${curCat === 'finance' ? activeCatStyle : inactiveCatStyle}">
        💳 ${t("catFinance")} (${catCounts.finance})
      </button>
      <button type="button" class="btn-small btn-ghost" data-act="inspectCat" data-cat="audit" style="padding:4px 10px;font-size:11px;border-radius:12px;${curCat === 'audit' ? activeCatStyle : inactiveCatStyle}">
        📜 ${t("catAudit")} (${catCounts.audit})
      </button>
      <button type="button" class="btn-small btn-ghost" data-act="inspectCat" data-cat="system" style="padding:4px 10px;font-size:11px;border-radius:12px;${curCat === 'system' ? activeCatStyle : inactiveCatStyle}">
        ⚙️ ${t("catSystem")} (${catCounts.system})
      </button>
    </div>
  `;

  // Render Table Buttons with wrap so buttons NEVER get squished
  const tablesHtml = visibleTables.map(tbl => {
    const isActive = currentTable && currentTable.name === tbl.name;
    const title = t(tbl.title_key) || tbl.name;
    const btnActiveStyle = "background:var(--acc);color:#fff;border-color:var(--acc);font-weight:700;box-shadow:0 0 12px rgba(139,92,246,0.35);";
    const btnInactiveStyle = "background:rgba(255,255,255,0.04);color:var(--txt);border:1px solid var(--border);";
    return `<button type="button" class="btn-small btn-ghost" data-act="inspectTab" data-table="${esc(tbl.name)}" style="display:inline-flex;align-items:center;gap:6px;padding:6px 12px;font-size:12px;border-radius:var(--radius-sm);flex-shrink:0;${isActive ? btnActiveStyle : btnInactiveStyle}">
      <span style="font-size:14px">${tbl.icon || '📄'}</span>
      <span>${esc(title)}</span>
      <span class="badge" style="font-size:10px;padding:1px 6px;opacity:0.85">${tbl.count}</span>
    </button>`;
  }).join("");

  // Dropdown options grouped by category
  const dropdownCats = [
    { key: "core", name: t("catCore") },
    { key: "finance", name: t("catFinance") },
    { key: "audit", name: t("catAudit") },
    { key: "system", name: t("catSystem") }
  ];
  const dropdownHtml = `
    <select class="input" data-act="inspectSelectChange" style="width:auto;min-width:210px;font-size:12px;padding:4px 10px;border-radius:var(--radius-sm);background:var(--bg2);color:var(--txt);border:1px solid var(--border);">
      ${dropdownCats.map(cat => {
        const catTbls = tables.filter(t => t.category === cat.key);
        if (!catTbls.length) return "";
        return `<optgroup label="${esc(cat.name)}">` +
          catTbls.map(tInfo => {
            const isSel = currentTable && currentTable.name === tInfo.name ? "selected" : "";
            return `<option value="${esc(tInfo.name)}" ${isSel}>${tInfo.icon || ""} ${esc(t(tInfo.title_key) || tInfo.name)} (${tInfo.count})</option>`;
          }).join("") +
        `</optgroup>`;
      }).join("")}
    </select>
  `;

  // Render Table Records Preview
  let tableBodyHtml = "";
  if (!currentTable || currentTable.count === 0 || !currentTable.sample || currentTable.sample.length === 0) {
    tableBodyHtml = `<div class="muted" style="text-align:center;padding:36px 16px">${t("inspectEmptyTable")}</div>`;
  } else {
    const cols = currentTable.columns || (currentTable.sample[0] ? Object.keys(currentTable.sample[0]) : []);
    const headerHtml = cols.map(c => `<th style="padding:8px 12px;font-size:12px;white-space:nowrap;background:rgba(255,255,255,0.03);color:var(--txt);font-weight:700;">${esc(formatInspectCol(c))}</th>`).join("");

    const rowsHtml = currentTable.sample.map(row => {
      const cells = cols.map(c => {
        const val = row[c];
        const formatted = formatInspectCell(c, val);
        return `<td style="padding:8px 12px;font-size:12px;white-space:nowrap;max-width:280px;overflow:hidden;text-overflow:ellipsis;">${formatted}</td>`;
      }).join("");
      return `<tr>${cells}</tr>`;
    }).join("");

    tableBodyHtml = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span class="muted" style="font-size:11px">ℹ️ ${t("inspectSampleLimit", { n: currentTable.sample.length })}</span>
        <span class="badge" style="font-size:11px">${currentTable.count} ${t("records")}</span>
      </div>
      <div class="table-wrap" style="max-height:280px;overflow:auto;border-radius:var(--radius-sm);border:1px solid var(--border);">
        <table style="width:100%;border-collapse:collapse;margin:0">
          <thead><tr>${headerHtml}</tr></thead>
          <tbody>${rowsHtml}</tbody>
        </table>
      </div>
    `;
  }

  const curDesc = currentTable ? (t(currentTable.desc_key) || "") : "";
  const curRow = currentTable ? (t(currentTable.row_key) || "") : "";
  const curTitle = currentTable ? (t(currentTable.title_key) || currentTable.name) : "";
  const curIcon = currentTable ? (currentTable.icon || "📊") : "📊";

  const modalHtml = `
    <div style="display:flex;flex-direction:column;gap:14px">
      <!-- Header -->
      <div style="display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid var(--border);padding-bottom:12px">
        <div>
          <h3 style="margin:0 0 4px 0;display:flex;align-items:center;gap:8px;font-size:18px">
            <span>🔍</span>
            <span>${t("inspectTitle")}</span>
          </h3>
          <p class="muted" style="margin:0;font-size:12px" dir="ltr">
            <b>${esc(data.filename)}</b> &bull; <span class="pill" style="font-size:11px;padding:2px 8px">${esc(data.size_human)}</span>
          </p>
        </div>
        <button type="button" class="btn-small btn-ghost" data-act="closeModal" style="font-size:16px;padding:4px 8px">&times;</button>
      </div>

      <!-- Educational / Friendly Explanation Banner -->
      <div class="card" style="padding:12px 14px;background:rgba(139,92,246,0.06);border:1px solid rgba(139,92,246,0.2);display:flex;gap:12px;align-items:flex-start">
        <span style="font-size:24px;line-height:1">💡</span>
        <div style="font-size:12px;line-height:1.6">
          <b style="color:var(--txt);display:block;margin-bottom:2px">${t("inspectSummaryTitle")}</b>
          <span class="muted">${t("inspectSummaryNote")}</span>
        </div>
      </div>

      <!-- Quick Metrics Strip -->
      <div style="display:grid;grid-template-columns:repeat(3, 1fr);gap:10px">
        <div class="stat" data-color="green" style="padding:12px;text-align:center">
          <div class="stat-icon" style="font-size:22px;margin-bottom:4px">📊</div>
          <div class="stat-value" style="font-size:20px">${esc(data.total_records)}</div>
          <div class="stat-label" style="font-size:11px">${t("inspectTotalRecords")}</div>
        </div>
        <div class="stat" data-color="purple" style="padding:12px;text-align:center">
          <div class="stat-icon" style="font-size:22px;margin-bottom:4px">🗂️</div>
          <div class="stat-value" style="font-size:20px">${esc(data.tables_count)}</div>
          <div class="stat-label" style="font-size:11px">${t("inspectTablesCount")}</div>
        </div>
        <div class="stat" data-color="blue" style="padding:12px;text-align:center">
          <div class="stat-icon" style="font-size:22px;margin-bottom:4px">💾</div>
          <div class="stat-value" style="font-size:20px" dir="ltr">${esc(data.size_human)}</div>
          <div class="stat-label" style="font-size:11px">${t("cSize")}</div>
        </div>
      </div>

      <!-- Table Selection & Category Filter -->
      <div style="display:flex;flex-direction:column;gap:8px;background:rgba(0,0,0,0.15);padding:12px;border-radius:var(--radius-sm);border:1px solid var(--border)">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
          ${categoriesHtml}
          ${dropdownHtml}
        </div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;max-height:130px;overflow-y:auto;padding-top:4px;border-top:1px solid rgba(255,255,255,0.06)">
          ${tablesHtml}
        </div>
      </div>

      <!-- Selected Table Purpose & Preview -->
      <div style="background:rgba(0,0,0,0.2);padding:14px;border-radius:var(--radius-sm);border:1px solid var(--border)">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
          <span style="font-size:22px">${curIcon}</span>
          <b style="font-size:15px;color:var(--txt)">${esc(curTitle)}</b>
          <code style="font-size:11px;color:var(--acc);background:rgba(139,92,246,0.15);padding:2px 8px;border-radius:4px" dir="ltr">${esc(currentTable ? currentTable.name : "")}</code>
          <span class="badge" style="font-size:11px">${currentTable ? currentTable.count : 0} ${t("records")}</span>
        </div>
        ${curDesc ? `<div style="font-size:12px;line-height:1.6;color:var(--txt2);margin-bottom:6px"><b>💡 ${t("tablePurpose")}:</b> ${esc(curDesc)}</div>` : ''}
        ${curRow ? `<div style="font-size:11px;line-height:1.5;color:var(--muted);margin-bottom:12px"><b>📌 ${t("rowMeaning")}:</b> ${esc(curRow)}</div>` : ''}
        ${tableBodyHtml}
      </div>

      <!-- Modal Footer -->
      <div class="row" style="justify-content:space-between;align-items:center;margin-top:6px;padding-top:10px;border-top:1px solid var(--border)">
        <a href="/api/database/backups/${encodeURIComponent(data.filename)}/download" class="btn-small btn-ghost" style="text-decoration:none;padding:6px 14px;font-size:13px;display:inline-flex;align-items:center;gap:6px">
          ${t("inspectDownloadFull")}
        </a>
        <button type="button" class="btn-ghost" data-act="closeModal" style="padding:6px 18px">${t("close")}</button>
      </div>
    </div>
  `;

  openModal(modalHtml, 960);
}

/* ═══════════ MODAL ═══════════ */
const openModal=(html, maxW)=>{
  $("modalBox").style.maxWidth = maxW ? (typeof maxW === "number" ? maxW + "px" : maxW) : "";
  if (maxW) $("modalBox").style.width = "95vw";
  $("modalBox").innerHTML=html;
  $("modalWrap").classList.remove("hidden");
};
const closeModal=()=>{
  window._currMangaCh=null;
  window._currStaffData=null;
  window._currInspectData=null;
  $("modalBox").style.maxWidth = "";
  $("modalBox").style.width = "";
  $("modalBox").innerHTML="";
  $("modalWrap").classList.add("hidden");
};
document.addEventListener("keydown",e=>{if(e.key==="Escape"&&!$("modalWrap").classList.contains("hidden"))closeModal();});
$("modalWrap").addEventListener("click", e => { if (e.target.classList.contains("modal-bg")) closeModal(); });
const asyncConfirm=(htmlMsg,okClass,okText,cancelText)=>new Promise(res=>{
  openModal(`<div style="padding:10px 0;text-align:center"><h3 style="margin-bottom:24px;line-height:1.6;font-size:17px;color:var(--txt)">${htmlMsg}</h3>
    <div class="row" style="justify-content:center;gap:12px">
      <button class="btn-ghost" id="cCancelBtn">${esc(cancelText||I[LANG].cancel)}</button>
      <button class="${esc(okClass||'btn-green')}" id="cOkBtn">${esc(okText||I[LANG].confirm)}</button>
    </div></div>`);
  $("cCancelBtn").onclick=()=>{closeModal();res(false);};
  $("cOkBtn").onclick=()=>{closeModal();res(true);};
});

/* ═══════════ INIT ═══════════ */
// Check if already logged in via cookie
fetch("/api/stats").then(r => {
  if (r.ok) enter();
}).catch(()=>{});

})();
