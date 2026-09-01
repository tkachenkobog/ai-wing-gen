'use strict';

// ==================================================================
// state
// ==================================================================
let SCHEMA = null;
let VALUES = {};        // constants  name -> value
let BOUNDS = {};        // bounds     name -> [lo, hi]
let RUNTIME = {};       // runtime    name -> value
let DEFAULTS = { constants: {}, bounds: {}, runtime: {} };
let dirty = false;
let running = false;
let runTimer = null;
let runStartedAt = 0;
let evalCount = 0;
let bestObj = null;
let bestLD = null;
let evalCap = 0;
let ROWS = [];
let activeDir = null;
let resPollTimer = null;
let CPU_COUNT = 0;      // logical cores, from main via paths:info

const $ = (id) => document.getElementById(id);
const G = 9.80665;

// ==================================================================
// utils
// ==================================================================
function fmt(v, digits) {
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(v);
  if (!isFinite(n)) return String(v);
  const a = Math.abs(n);
  if (n === 0) return '0';
  if (a >= 1e5 || a < 1e-4) return n.toExponential(digits === undefined ? 3 : digits);
  return String(parseFloat(n.toFixed(digits === undefined ? 5 : digits)));
}

function toast(msg, kind) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast show' + (kind ? ' ' + kind : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.className = 'toast'; }, 3800);
}

/**
 * Text input dialog. Electron does not implement window.prompt() — it just logs
 * "prompt() is and will not be supported", so we need our own.
 */
function askText(title, def = '') {
  return new Promise((resolve) => {
    const back = $('askBack'), input = $('askInput');
    $('askTitle').textContent = title;
    input.value = def;
    back.classList.add('show');
    input.focus();
    input.select();

    const done = (val) => {
      back.classList.remove('show');
      $('askOk').removeEventListener('click', onOk);
      $('askCancel').removeEventListener('click', onCancel);
      input.removeEventListener('keydown', onKey);
      back.removeEventListener('mousedown', onBack);
      resolve(val);
    };
    const onOk = () => done(input.value.trim() || null);
    const onCancel = () => done(null);
    const onKey = (e) => {
      if (e.key === 'Enter') { e.preventDefault(); onOk(); }
      if (e.key === 'Escape') { e.preventDefault(); onCancel(); }
    };
    const onBack = (e) => { if (e.target === back) onCancel(); };

    $('askOk').addEventListener('click', onOk);
    $('askCancel').addEventListener('click', onCancel);
    input.addEventListener('keydown', onKey);
    back.addEventListener('mousedown', onBack);
  });
}

/** Ukrainian needs three plural forms: 1 оцінка, 2 оцінки, 5 оцінок. */
function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b === 1) return one;
  if (b > 1 && b < 5) return few;
  return many;
}
const nStudies = (n) => `${n} ${plural(n, 'дослідження', 'дослідження', 'досліджень')}`;
const nEvals = (n) => `${n} ${plural(n, 'оцінка', 'оцінки', 'оцінок')}`;

/** Split log text into lines, tolerating CRLF. */
function splitLines(text) {
  return String(text).split(new RegExp('\\r?\\n'));
}

function isaDensity(hMeters) {
  const h = Math.max(0, Number(hMeters) || 0);
  return 1.225 * Math.pow(1 - 2.25577e-5 * h, 4.25588);
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

// ==================================================================
// studies (project -> study). Everything else operates on the active one.
// ==================================================================
let PROJECTS = [];
let ACTIVE = null;      // { project, study, dir, resultsDir, meta, frozen }

// What is highlighted in the tree. A project can be selected on its own — that
// is the whole point: a freshly created project has no studies to click, and
// The "+" button on a project row has to know which project it adds to.
let sel = { project: null, study: null };

const STATUS_LABEL = {
  new: 'нова', sized: 'розмір підібрано', running: 'рахується',
  done: 'завершено', stopped: 'перервано', failed: 'збій',
};

function renderActiveBadge() {
  const box = $('activeStudy');
  if (!ACTIVE) {
    box.innerHTML = '<span class="as-none">дослідження не вибрано</span>';
    box.title = 'Створіть дослідження на вкладці «Дослідження»';
    return;
  }
  const m = ACTIVE.meta || {};
  const proj = PROJECTS.find((p) => p.slug === ACTIVE.project);
  const st = m.status || 'new';
  box.innerHTML =
    `<span class="as-proj">${esc(proj ? proj.name : ACTIVE.project)} ▸</span>` +
    `<span class="as-name">${esc(m.name || ACTIVE.study)}</span>` +
    `<span class="as-badge st-${esc(st)}">${esc(STATUS_LABEL[st] || st)}</span>` +
    (ACTIVE.frozen ? '<span class="as-badge frozen">заморожена</span>' : '');
  box.title = ACTIVE.dir;
}

/** Lock the requirements/params forms when the study already has results. */
function applyFrozen() {
  const frozen = !!(ACTIVE && ACTIVE.frozen);

  $('page-req').classList.toggle('ro', frozen);
  $('page-params').classList.toggle('ro', frozen);
  $('reqFrozenBar').classList.toggle('show', frozen);
  $('paramsFrozenBar').classList.toggle('show', frozen);

  for (const id of ['btnSize', 'btnReqSave', 'btnSave', 'btnReset', 'btnOpen', 'btnRescale']) {
    const el = $(id);
    if (el) el.disabled = frozen;
  }
  const run = $('btnRun');
  if (run) run.disabled = frozen || running;
}

async function loadStudies() {
  const r = await window.api.listStudies();
  if (!r.ok) { toast('Не вдалося прочитати дослідження', 'bad'); return; }
  PROJECTS = r.projects || [];
  ACTIVE = r.active || null;

  // default selection: the active study; keep an existing selection if it still exists
  if (!sel.project && ACTIVE) sel = { project: ACTIVE.project, study: ACTIVE.study };
  const proj = PROJECTS.find((p) => p.slug === sel.project);
  if (!proj) sel = { project: PROJECTS.length ? PROJECTS[0].slug : null, study: null };
  else if (sel.study && !proj.studies.some((x) => x.slug === sel.study)) sel.study = null;
  renderActiveBadge();
  renderStudyTree();
  renderStudyDetail();
}

// ---------- options popup ----------
/**
 * items: [{ label, danger?, onClick } | { sep: true }]
 * Anchored to the button that opened it, so it is always obvious what object
 * the actions apply to.
 */
function showMenu(anchor, title, items) {
  const m = $('ctxMenu');
  m.innerHTML = '';
  if (title) {
    const h = document.createElement('div');
    h.className = 'menu-head';
    h.textContent = title;
    m.appendChild(h);
  }
  for (const it of items) {
    if (it.sep) {
      const s = document.createElement('div');
      s.className = 'menu-sep';
      m.appendChild(s);
      continue;
    }
    const b = document.createElement('button');
    b.textContent = it.label;
    if (it.danger) b.className = 'danger';
    b.addEventListener('click', () => { closeMenu(); it.onClick(); });
    m.appendChild(b);
  }

  m.classList.add('show');
  const r = anchor.getBoundingClientRect();
  const mw = m.offsetWidth, mh = m.offsetHeight;
  let left = r.right - mw;
  let top = r.bottom + 4;
  if (left < 6) left = 6;
  if (top + mh > window.innerHeight - 6) top = Math.max(6, r.top - mh - 4);
  m.style.left = `${left}px`;
  m.style.top = `${top}px`;

  setTimeout(() => {
    document.addEventListener('mousedown', onOutside, { once: true });
    document.addEventListener('keydown', onEscMenu);
  }, 0);
}

function closeMenu() {
  $('ctxMenu').classList.remove('show');
  document.removeEventListener('keydown', onEscMenu);
}
function onOutside(e) {
  if (!e.target.closest('#ctxMenu')) closeMenu();
  else document.addEventListener('mousedown', onOutside, { once: true });
}
function onEscMenu(e) { if (e.key === 'Escape') closeMenu(); }
window.addEventListener('resize', closeMenu);

// ---------- tree ----------
function iconBtn(cls, glyph, title) {
  const b = document.createElement('button');
  b.className = `row-btn ${cls}`;
  b.textContent = glyph;
  b.title = title;
  return b;
}

function renderStudyTree() {
  const box = $('studyTree');
  box.innerHTML = '';
  if (!PROJECTS.length) {
    box.innerHTML = '<div class="st-empty">Проєктів ще немає. ' +
      'Натисніть «+ Створити проєкт».</div>';
    return;
  }

  for (const p of PROJECTS) {
    // ---- project row ----
    const h = document.createElement('div');
    h.className = 'st-project' + (sel.project === p.slug && !sel.study ? ' active' : '');
    h.innerHTML = `<span class="st-pname">${esc(p.name)}</span>` +
      `<span class="st-count">${p.studies.length}</span>`;
    h.addEventListener('click', () => {
      sel = { project: p.slug, study: null };
      renderStudyTree(); renderStudyDetail();
    });

    // "+" adds a study to THIS project - no hidden selection involved
    const add = iconBtn('plus', '+', `Нове дослідження в «${p.name}»`);
    add.addEventListener('click', (e) => { e.stopPropagation(); newStudyIn(p); });
    h.appendChild(add);

    const pOpts = iconBtn('', '⋮', 'Опції проєкту');
    pOpts.addEventListener('click', (e) => {
      e.stopPropagation();
      showMenu(pOpts, p.name, [
        { label: 'Нове дослідження', onClick: () => newStudyIn(p) },
        { label: 'Перейменувати проєкт', onClick: () => renameProject(p) },
        { label: 'Відкрити теку', onClick: () => window.api.openStudyFolder({ project: p.slug }) },
        { sep: true },
        { label: 'Видалити проєкт', danger: true, onClick: () => deleteProject(p) },
      ]);
    });
    h.appendChild(pOpts);
    box.appendChild(h);

    if (!p.studies.length) {
      const e = document.createElement('div');
      e.className = 'st-empty';
      e.textContent = 'немає досліджень — натисніть + вище';
      box.appendChild(e);
      continue;
    }

    // ---- study rows ----
    for (const s of p.studies) {
      const isSel = sel.project === p.slug && sel.study === s.slug;
      const isActive = ACTIVE && ACTIVE.project === p.slug && ACTIVE.study === s.slug;
      const sum = s.summary || {};
      const ld = sum.best && sum.best.LD != null ? ` L/D ${Number(sum.best.LD).toFixed(1)}` : '';

      const el = document.createElement('div');
      el.className = 'st-item' + (isSel ? ' active' : '');
      el.innerHTML =
        `<span class="st-body"><span class="st-name">${esc(s.name)}</span>` +
        `<span class="st-sub">${nEvals(sum.evals || 0)}${esc(ld)}</span></span>` +
        `<span class="as-badge st-${esc(s.status)}">${esc(STATUS_LABEL[s.status] || s.status)}</span>`;
      // Click OPENS the study. Selecting without opening was the wrong model:
      // you clicked a study, saw its card, and every other tab still showed a
      // different one.
      el.addEventListener('click', () => {
        if (isActive) {
          sel = { project: p.slug, study: s.slug };
          renderStudyTree(); renderStudyDetail();
          return;
        }
        if (running) {
          toast('Спершу зупиніть розрахунок, щоб перейти до іншого дослідження', 'bad');
          return;
        }
        selectStudy(p.slug, s.slug);
      });

      const sOpts = iconBtn('', '⋮', 'Опції дослідження');
      sOpts.addEventListener('click', (e) => {
        e.stopPropagation();
        const items = [];
        items.push({ label: 'Клонувати', onClick: () => doClone({ project: p.slug, study: s.slug }) });
        items.push({ label: 'Перейменувати', onClick: () => renameStudy(p, s) });
        items.push({ label: 'Відкрити теку', onClick: () => window.api.openStudyFolder({ project: p.slug, study: s.slug }) });
        items.push({ sep: true });
        items.push({ label: 'Видалити дослідження', danger: true, onClick: () => deleteStudy(p, s) });
        showMenu(sOpts, s.name, items);
      });
      el.appendChild(sOpts);
      box.appendChild(el);
    }
  }
}

function findStudy(ref) {
  if (!ref || !ref.project || !ref.study) return null;
  const p = PROJECTS.find((x) => x.slug === ref.project);
  if (!p) return null;
  const s = p.studies.find((x) => x.slug === ref.study);
  return s ? { project: p, study: s } : null;
}

const CMP_ROWS = [
  ['Статус', (s) => STATUS_LABEL[s.status] || s.status],
  ['Оцінок', (s) => s.summary.evals ?? '—'],
  ['Впало', (s) => s.summary.failed ?? '—'],
  ['Найкраща цільова', (s) => s.summary.best ? fmt(s.summary.best.objective, 3) : '—'],
  ['Найкраще L/D', (s) => s.summary.best && s.summary.best.LD != null ? Number(s.summary.best.LD).toFixed(2) : '—'],
  ['Злітна маса, кг', (s) => s.summary.mtow_kg != null ? Number(s.summary.mtow_kg).toFixed(2) : '—'],
  ['Площа, м²', (s) => s.summary.S_m2 != null ? Number(s.summary.S_m2).toFixed(4) : '—'],
  ['Розмах, м', (s) => s.summary.b_m != null ? Number(s.summary.b_m).toFixed(3) : '—'],
  ['Подовження AR', (s) => s.summary.AR != null ? Number(s.summary.AR).toFixed(2) : '—'],
  ['Швидкість, м/с', (s) => s.summary.V_mps != null ? Number(s.summary.V_mps).toFixed(1) : '—'],
  ['Крейсерський CL', (s) => s.summary.CL ?? '—'],
  ['Профіль', (s) => s.summary.airfoil ?? '—'],
  ['Режим геометрії', (s) => s.summary.reference_mode ?? '—'],
  ['Планка стійкості', (s) => s.summary.stability_mode ?? '—'],
];

function renderStudyDetail() {
  const box = $('studyDetail');
  const hit = findStudy(sel);

  // A project can be selected without a study — that is the normal state right
  // after creating one. Show the project, not a dead end.
  if (!hit) {
    const proj = PROJECTS.find((p) => p.slug === sel.project);
    if (!proj) {
      box.innerHTML = '<div class="empty">Виберіть проєкт або дослідження ліворуч.</div>';
      return;
    }
    box.innerHTML =
      `<div class="card"><div class="card-head">${esc(proj.name)}` +
      `<span class="as-badge st-new">проєкт</span>` +
      `<div class="spacer"></div>` +
      `<button class="btn primary" data-act="add">+ Дослідження в цей проєкт</button>` +
      `</div><div class="card-body"><div class="kv">` +
      [
        ['Досліджень', proj.studies.length],
        ['Створений', proj.created_at || '—'],
        ['Тека', proj.slug],
      ].map(([k, v]) => `<div class="kv-item"><span>${esc(k)}</span><b>${esc(String(v))}</b></div>`).join('') +
      `</div>` +
      (proj.note ? `<div class="plot-hint">${esc(proj.note)}</div>` : '') +
      (proj.studies.length
        ? `<div class="plot-hint">Виберіть дослідження ліворуч, щоб побачити його параметри й результати.</div>`
        : `<div class="plot-hint">У проєкті ще немає досліджень. Нове дослідження візьме вхідні дані з шаблонів (04_SIZING/requirements.json і 01_PYTHON/wing_config.json).</div>`) +
      `</div></div>`;
    box.querySelector('[data-act="add"]').addEventListener('click', () => newStudyIn(proj));
    return;
  }

  const { project, study } = hit;
  const isActive = ACTIVE && ACTIVE.project === project.slug && ACTIVE.study === study.slug;
  const siblings = project.studies;

  box.innerHTML = '';

  // --- header card ---
  const head = document.createElement('div');
  head.className = 'card';
  head.innerHTML =
    `<div class="card-head">${esc(study.name)}` +
    `<span class="as-badge st-${esc(study.status)}">${esc(STATUS_LABEL[study.status] || study.status)}</span>` +
    (study.frozen ? '<span class="as-badge frozen">заморожена</span>' : '') +
    `<div class="spacer"></div>` +
    '<span class="st-sub">відкрите</span>' +
    `</div><div class="card-body"><div class="kv">` +
    [
      ['Проєкт', project.name],
      ['Створена', study.created_at || '—'],
      ['Розмір підібрано', study.sized_at || '—'],
      ['Прогін почато', study.run_started_at || '—'],
      ['Прогін завершено', study.run_finished_at || '—'],
      ['Клон від', study.parent || '—'],
    ].map(([k, v]) => `<div class="kv-item"><span>${esc(k)}</span><b>${esc(String(v))}</b></div>`).join('') +
    `</div>` + (study.note ? `<div class="plot-hint">${esc(study.note)}</div>` : '') + `</div>`;
  box.appendChild(head);

  // --- comparison against the other studies of this project ---
  const cmp = document.createElement('div');
  cmp.className = 'card';
  const cols = siblings;
  cmp.innerHTML =
    `<div class="card-head">Порівняння досліджень проєкту «${esc(project.name)}»</div>` +
    `<div class="card-body"><div class="table-scroll"><table class="cmp-table">` +
    `<thead><tr><th>Параметр</th>` +
    cols.map((s) => `<th>${esc(s.name)}</th>`).join('') +
    `</tr></thead><tbody>` +
    CMP_ROWS.map(([label, get]) =>
      `<tr class="${cols.some((s) => s.slug === study.slug) ? '' : ''}"><td>${esc(label)}</td>` +
      cols.map((s) => `<td>${esc(String(get(s)))}</td>`).join('') + `</tr>`).join('') +
    `</tbody></table></div></div>`;
  box.appendChild(cmp);
}

async function selectStudy(project, study) {
  const r = await window.api.selectStudy({ project, study });
  if (!r.ok) { toast(r.error, 'bad'); return; }
  ACTIVE = r.active;
  sel = { project, study };
  await reloadForActiveStudy();
  toast('Дослідження відкрито', 'ok');
}

/** Reload every tab from the newly active study. */
async function reloadForActiveStudy() {
  const c = await window.api.loadConfig();
  if (c.ok) applyConfig(c.config);
  else if (!c.noStudy) toast(c.error, 'bad');

  const rq = await window.api.loadRequirements();
  if (rq.ok) { REQ = rq.requirements; buildReqForm(); setReqDirty(false); }

  if (rq.ok && rq.report) renderSizingResult(rq.report);
  else renderSizingResult(null);

  buildForm(); applyFilter(); recompute(); setDirty(false);
  renderActiveBadge(); renderStudyTree(); renderStudyDetail();
  applyFrozen();
  ROWS = []; activeDir = null;
  $('detail').innerHTML = '<div class="empty">Виберіть оцінку зі списку ліворуч.</div>';
  await loadStudyLog();
  refreshResults(true);
}

/**
 * Show the study's saved run.log on the Розрахунок tab. Without this the tab is
 * blank for anything that ran before the app was last opened, because the live
 * log only exists in memory.
 */
async function loadStudyLog() {
  const log = $('log');
  log.textContent = '';
  if (!ACTIVE) {
    appendLog('[ui] дослідження не вибрано', 'ui');
    return;
  }
  const m = ACTIVE.meta || {};
  const sum = m.summary || {};
  appendLog(`[ui] дослідження: ${m.name || ACTIVE.study}  (${STATUS_LABEL[m.status] || m.status})`, 'ui');
  appendLog(`[ui] оцінок у теці: ${sum.evals || 0}` +
            (sum.best && sum.best.LD != null ? `, найкраще L/D ${Number(sum.best.LD).toFixed(2)}` : ''), 'ui');

  const r = await window.api.readStudyLog({ project: ACTIVE.project, study: ACTIVE.study });
  if (r.ok && r.log) {
    appendLog(`[ui] ── збережений лог (${Math.round(r.bytes / 1024)} КБ) ──`, 'ui');
    for (const line of splitLines(r.log)) appendLog(line);
  } else {
    appendLog(ACTIVE.frozen
      ? '[ui] збереженого логу немає — результати перенесені або прогін ішов до появи логів'
      : '[ui] прогону ще не було. Натисніть «Запустити оптимізацію».', 'ui');
  }
}

// --- study actions ---
$('btnNewProject').addEventListener('click', async () => {
  const name = await askText('Назва проєкту (напр. «Носитель 3 кг»)', '');
  if (!name) return;
  const r = await window.api.createProject({ name });
  if (!r.ok) { toast(r.error, 'bad'); return; }
  sel = { project: r.project.slug, study: null };   // select it right away
  await loadStudies();
  toast(`Проєкт «${r.project.name}» створено — тепер додайте в нього дослідження`, 'ok');
});

/**
 * Every action below takes the object explicitly — the project or study whose
 * row was clicked. Nothing depends on an invisible "current selection", which is
 * exactly what made the "new study" button land in the wrong project before.
 */
async function newStudyIn(proj) {
  const name = await askText(`Назва дослідження у проєкті «${proj.name}»`, '');
  if (!name) return;
  const r = await window.api.createStudy({ project: proj.slug, name });
  if (!r.ok) { toast(r.error, 'bad'); return; }
  ACTIVE = r.active;
  sel = { project: proj.slug, study: r.study.slug };
  await loadStudies();
  await reloadForActiveStudy();
  toast(`Дослідження створено в «${proj.name}»`, 'ok');
}

async function renameProject(proj) {
  const name = await askText('Нова назва проєкту', proj.name);
  if (!name) return;
  const r = await window.api.renameProject({ project: proj.slug, name });
  if (!r.ok) { toast(r.error, 'bad'); return; }
  await loadStudies();
  toast('Проєкт перейменовано', 'ok');
}

async function deleteProject(proj) {
  const r = await window.api.deleteProject({ project: proj.slug });
  if (r.canceled) return;
  if (!r.ok) { toast(r.error, 'bad'); return; }
  ACTIVE = r.active;
  sel = ACTIVE ? { project: ACTIVE.project, study: ACTIVE.study } : { project: null, study: null };
  await loadStudies();
  if (ACTIVE) await reloadForActiveStudy();
  toast(`Проєкт «${proj.name}» видалено`, 'ok');
}

async function doClone(ref) {
  const r0 = ref || (ACTIVE && { project: ACTIVE.project, study: ACTIVE.study });
  if (!r0) { toast('Немає дослідження для клонування', 'bad'); return; }
  const hit = findStudy(r0);
  const name = await askText('Назва клону', hit ? `${hit.study.name} (клон)` : 'клон');
  if (!name) return;
  const r = await window.api.cloneStudy({ ...r0, name });
  if (!r.ok) { toast(r.error, 'bad'); return; }
  ACTIVE = r.active;
  sel = { project: r0.project, study: r.study.slug };
  await loadStudies();
  await reloadForActiveStudy();
  toast('Клон створено — вхідні дані успадковані, результати чисті', 'ok');
}

// the "clone" buttons inside the frozen bars act on the active study
document.querySelectorAll('[data-clone]').forEach((b) => b.addEventListener('click', () => doClone()));

async function renameStudy(proj, study) {
  const name = await askText('Нова назва дослідження', study.name);
  if (!name) return;
  const r = await window.api.renameStudy({ project: proj.slug, study: study.slug, name });
  if (!r.ok) { toast(r.error, 'bad'); return; }
  await loadStudies();
  renderActiveBadge();
  toast('Перейменовано', 'ok');
}

async function deleteStudy(proj, study) {
  const r = await window.api.deleteStudy({ project: proj.slug, study: study.slug });
  if (r.canceled) return;
  if (!r.ok) { toast(r.error, 'bad'); return; }
  ACTIVE = r.active;
  sel = ACTIVE ? { project: ACTIVE.project, study: ACTIVE.study } : { project: proj.slug, study: null };
  await loadStudies();
  if (ACTIVE) await reloadForActiveStudy();
  toast(`Дослідження «${study.name}» видалено`, 'ok');
}

// ==================================================================
// requirements tab (per-study requirements.json)
// ==================================================================
let REQ = null;
let reqDirty = false;

const REQ_SECTIONS = {
  payload:              'Корисне навантаження',
  mission:              'Місія',
  launch:               'Старт і посадка',
  geometry_limits:      'Геометрія',
  structure:            'Конструкція',
  propulsion:           'Силова установка',
  fixed_masses:         'Фіксовані маси',
  aero_estimate:        'Аеродинаміка (оцінка)',
  stability_requirements: 'Вимоги стійкості',
};

const REQ_LABELS = {
  mass_kg: 'Маса корисного навантаження, кг',
  bay_width_m: 'Ширина відсіку, м',
  endurance_min: 'Тривалість польоту, хв',
  cruise_CL: 'Крейсерський CL',
  altitude_m: 'Висота, м',
  method: 'Спосіб старту',
  CL_max: 'Максимальний CL крила',
  span_max_m: 'Максимальний розмах, м',
  aspect_ratio_target: 'Бажане подовження AR',
  taper_ratio: 'Звуження (законцівка/корінь)',
  thickness_ratio: 'Відносна товщина t/c',
  material: 'Матеріал лонжерона',
  load_factor_limit: 'Експлуатаційне перевантаження',
  safety_factor: 'Коефіцієнт запасу',
  skin_mass_per_area_kgm2: 'Маса обшивки, кг/м²',
  spar_cap_min_area_mm2: 'Мін. площа полиці, мм²',
  tip_deflection_frac: 'Допустимий прогин, частка',
  battery_wh_per_kg: 'Питома енергія батареї, Вт·год/кг',
  battery_usable_fraction: 'Використовна частка ємності',
  total_efficiency: 'Загальний ККД приводу',
  mass_per_watt_kg: 'Маса силової групи, кг/Вт',
  fuselage_kg: 'Корпус / гондола, кг',
  avionics_kg: 'Авіоніка й серво, кг',
  recovery_kg: 'Парашут / шасі, кг',
  misc_kg: 'Інше, кг',
  CD0: 'CD0 (опір при CL=0)',
  oswald_e: 'Коефіцієнт Освальда e',
  Cn_beta_min_per_rad: 'Мін. Cn_beta, 1/рад',
  Cl_beta_max_per_rad: 'Макс. Cl_beta, 1/рад',
  static_margin: 'Запас стійкості, частка САХ',
};

const REQ_ENUMS = {
  method: ['hand', 'catapult', 'runway'],
  material: ['carbon_ud', 'glass'],
};

function setReqDirty(v) {
  reqDirty = v;
  $('reqDirtyBar').classList.toggle('show', v);
}

function buildReqForm() {
  const form = $('reqForm');
  form.innerHTML = '';
  if (!REQ) return;

  for (const [section, values] of Object.entries(REQ)) {
    if (section.startsWith('_') || typeof values !== 'object' || values === null) continue;

    const card = document.createElement('div');
    card.className = 'group';
    const head = document.createElement('div');
    head.className = 'group-head';
    head.innerHTML = `<div class="group-title">${esc(REQ_SECTIONS[section] || section)}</div>`;
    card.appendChild(head);

    const fields = document.createElement('div');
    fields.className = 'fields';

    for (const key of Object.keys(values)) {
      if (key.startsWith('_')) continue;
      const hint = values['_' + key] || '';
      const cur = values[key];

      const wrap = document.createElement('div');
      wrap.className = 'field';
      wrap.dataset.search = (key + ' ' + (REQ_LABELS[key] || '') + ' ' + hint).toLowerCase();
      wrap.innerHTML =
        `<div class="field-label"><span class="lbl">${esc(REQ_LABELS[key] || key)}</span>` +
        `<span class="code" title="${esc(section)}.${esc(key)}">${esc(key)}</span></div>`;

      const row = document.createElement('div');
      row.className = 'field-input';

      let input;
      if (REQ_ENUMS[key]) {
        input = document.createElement('select');
        input.className = 'field-sel';
        for (const opt of REQ_ENUMS[key]) {
          const o = document.createElement('option');
          o.value = opt; o.textContent = opt;
          if (opt === cur) o.selected = true;
          input.appendChild(o);
        }
        input.addEventListener('change', () => {
          values[key] = input.value; setReqDirty(true);
        });
      } else {
        input = document.createElement('input');
        input.type = 'text';
        input.spellcheck = false;
        input.value = typeof cur === 'number' ? fmt(cur, 8) : String(cur);
        input.addEventListener('input', () => {
          if (typeof cur === 'number') {
            const n = Number(input.value.trim().replace(',', '.'));
            if (input.value.trim() === '' || !isFinite(n)) { input.style.borderColor = '#e2564d'; return; }
            input.style.borderColor = '';
            values[key] = n;
          } else {
            values[key] = input.value;
          }
          setReqDirty(true);
        });
      }
      row.appendChild(input);
      wrap.appendChild(row);

      if (hint) {
        const h = document.createElement('div');
        h.className = 'field-hint';
        h.textContent = hint;
        wrap.appendChild(h);
      }
      fields.appendChild(wrap);
    }
    card.appendChild(fields);
    form.appendChild(card);
  }
}

function renderSizingResult(rep) {
  const box = $('szResult');
  if (!rep || !rep.result) {
    box.innerHTML = '<div class="empty-small">Натисніть «Підібрати розмір»</div>';
    return;
  }
  const r = rep.result;
  const m = r.masses || {};
  const row = (label, val, cls) =>
    `<div class="sizing-row${cls ? ' ' + cls : ''}"><span>${esc(label)}</span><b>${esc(val)}</b></div>`;

  const payFrac = 100 * r.payload_fraction;
  box.innerHTML =
    row('Злітна маса', r.mtow_kg.toFixed(2) + ' кг', 'hi') +
    row('частка корисного', payFrac.toFixed(0) + '%', payFrac > 55 ? 'bad' : '') +
    '<div class="sizing-head">Крило</div>' +
    row('Площа', r.S_m2.toFixed(4) + ' м²') +
    row('Розмах', r.b_m.toFixed(3) + ' м' + (r.span_clipped ? ' ⚠' : '')) +
    row('Подовження AR', r.AR.toFixed(2)) +
    row('Навантаження', r.wing_loading_kgm2.toFixed(1) + ' кг/м²') +
    row('Коренева хорда', r.c_root_m.toFixed(4) + ' м') +
    '<div class="sizing-head">Режим</div>' +
    row('Крейсер', r.v_cruise_mps.toFixed(1) + ' м/с') +
    row('Звалювання', r.v_stall_mps.toFixed(1) + ' м/с') +
    row('L/D (оцінка)', r.LD_est.toFixed(1)) +
    row('Потужність', r.power_elec_W.toFixed(0) + ' Вт') +
    '<div class="sizing-head">Лонжерон</div>' +
    row('Визначає', r.spar.driver) +
    row('Площа полиці', r.spar.cap_area_mm2.toFixed(1) + ' мм²') +
    row('Прогин', (100 * r.spar.tip_deflection_frac).toFixed(1) + '% напіврозм.') +
    '<div class="sizing-head">Маси, кг</div>' +
    Object.entries(m).filter(([, v]) => v > 0)
      .map(([k, v]) => row(k, v.toFixed(3))).join('');
}

async function doSizing(dryRun) {
  if (reqDirty) {
    const s = await window.api.saveRequirements(REQ);
    if (!s.ok) { toast('Не збереглися вимоги: ' + s.error, 'bad'); return; }
    setReqDirty(false);
  }
  $('btnSize').disabled = true;
  $('btnSizeDry').disabled = true;
  const r = await window.api.runSizing({ dryRun });
  $('btnSize').disabled = false;
  $('btnSizeDry').disabled = false;

  $('sizeLogCard').hidden = false;
  $('sizeLog').textContent = r.log || r.error || '';

  if (!r.ok) { toast('Підбір не вдався: ' + r.error, 'bad'); return; }
  if (r.report) renderSizingResult(r.report);

  if (dryRun) {
    toast('Порахувано, конфіг не змінено', 'ok');
    return;
  }

  // The design point changed on disk - pull it into the Параметри tab.
  const c = await window.api.loadConfig();
  if (c.ok) {
    applyConfig(c.config);
    buildForm(); applyFilter(); recompute(); setDirty(false);
  }
  await loadStudies();
  applyFrozen();
  toast('Розмір підібрано, параметри дослідження оновлено', 'ok');
}

$('btnSize').addEventListener('click', () => doSizing(false));
$('btnSizeDry').addEventListener('click', () => doSizing(true));
$('btnReqSave').addEventListener('click', async () => {
  const s = await window.api.saveRequirements(REQ);
  if (s.ok) { setReqDirty(false); toast('Вимоги збережено', 'ok'); }
  else toast('Помилка: ' + s.error, 'bad');
});

// ==================================================================
// tabs
// ==================================================================
$('tabs').addEventListener('click', (e) => {
  const btn = e.target.closest('.tab');
  if (!btn) return;
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t === btn));
  const id = btn.dataset.tab;
  document.querySelectorAll('.page').forEach((p) => p.classList.toggle('active', p.id === 'page-' + id));
  if (id === 'results') refreshResults();
});

// ==================================================================
// boot
// ==================================================================
async function boot() {
  const env = await window.api.detectPython();
  if (env.ok) {
    $('envDot').className = 'dot ok';
    $('envText').textContent =
      `${env.label} · Python ${env.python} · AeroSandbox ${env.aerosandbox} · SciPy ${env.scipy}`;
  } else {
    $('envDot').className = 'dot bad';
    $('envText').textContent = 'Python не готовий';
    toast(env.error, 'bad');
  }

  const s = await window.api.loadSchema();
  if (!s.ok) { toast(s.error, 'bad'); return; }
  SCHEMA = s.schema;

  for (const f of SCHEMA.constants) DEFAULTS.constants[f.name] = f.default;
  for (const b of SCHEMA.bounds) DEFAULTS.bounds[b.name] = b.default.slice();
  for (const r of SCHEMA.runtime) DEFAULTS.runtime[r.name] = r.default;

  // studies first - every other tab reads from the active one
  await loadStudies();
  if (!ACTIVE) {
    toast('Створіть або відкрийте дослідження на вкладці «Дослідження»', 'bad');
  }

  const c = await window.api.loadConfig();
  if (c.ok) applyConfig(c.config);
  else if (!c.noStudy) toast(c.error, 'bad');

  // requirements tab
  const rq = await window.api.loadRequirements();
  if (rq.ok) {
    REQ = rq.requirements;
    buildReqForm();
    setReqDirty(false);
    if (rq.report) renderSizingResult(rq.report);
  } else if (!rq.noStudy) {
    toast(rq.error, 'bad');
  }

  buildNav();
  buildForm();
  applyFilter();
  recompute();
  setDirty(false);
  applyFrozen();

  const st = await window.api.runStatus();
  setRunning(st.running, st.pid);

  console.info(
    `[boot] схема: ${SCHEMA.constants.length} констант, ${SCHEMA.runtime.length} runtime, ` +
    `${SCHEMA.bounds.length} меж | полів у формі: ${$('form').querySelectorAll('.field').length} | ` +
    `проєктів: ${PROJECTS.length}, досліджень: ${PROJECTS.reduce((n, p) => n + p.studies.length, 0)} | ` +
    `активне: ${ACTIVE ? ACTIVE.project + '/' + ACTIVE.study + (ACTIVE.frozen ? ' [заморожене]' : '') : 'немає'} | ` +
    `конфіг: ${c.config ? 'завантажено' : 'відсутній'}`
  );
}

function applyConfig(cfg) {
  VALUES = { ...DEFAULTS.constants };
  BOUNDS = {};
  for (const k in DEFAULTS.bounds) BOUNDS[k] = DEFAULTS.bounds[k].slice();
  RUNTIME = { ...DEFAULTS.runtime };

  if (!cfg) return;
  for (const k in (cfg.constants || {})) if (k in VALUES) VALUES[k] = cfg.constants[k];
  for (const k in (cfg.bounds || {})) {
    const p = cfg.bounds[k];
    if (k in BOUNDS && Array.isArray(p) && p.length === 2) BOUNDS[k] = [Number(p[0]), Number(p[1])];
  }
  for (const k in (cfg.runtime || {})) if (k in RUNTIME) RUNTIME[k] = cfg.runtime[k];
}

function collectConfig() {
  return { constants: { ...VALUES }, bounds: { ...BOUNDS }, runtime: { ...RUNTIME } };
}

// ==================================================================
// form
// ==================================================================

// The only parameters that actually change the aircraft or the run.
// Everything else is an internal coefficient - already filled in from the
// script's own values, hidden behind the "решта параметрів" toggle.
const PRIMARY = new Set([
  // mode - the single most consequential choice
  'REFERENCE_MODE', 'FIX_ROOT_CHORD', 'STABILITY_MODE',
  // design point
  'S_FIXED', 'B_FULL', 'V_DESIGN', 'CL_TARGET', 'AIRFOIL_NAME',
  // run control
  'popsize', 'maxiter', 'workers', 'time_budget_hours',
  // output
  'RESULTS_ROOT', 'EXPORT_STL',
]);

const ALL_GROUPS = () => [
  ...SCHEMA.groups,
  {
    id: 'bounds',
    title: 'Межі змінних проєктування',
    hint: '16 змінних, якими керує Differential Evolution. Зсуви передньої кромки задані В МЕТРАХ — при зміні розміру крила їх обов’язково масштабувати.',
  },
];

function buildNav() {
  const nav = $('groupNav');
  nav.innerHTML = '';
  for (const g of ALL_GROUPS()) {
    const b = document.createElement('button');
    b.className = 'gnav';
    b.textContent = g.title;
    b.dataset.group = g.id;
    b.addEventListener('click', () => {
      const el = document.querySelector(`[data-group-card="${g.id}"]`);
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      document.querySelectorAll('.gnav').forEach((x) => x.classList.toggle('active', x === b));
    });
    nav.appendChild(b);
  }
}

/**
 * Parameters whose value is a choice, not a number. Typing `absolute` or
 * `requirements` into a text box tells the user nothing about what those mean,
 * so each choice gets a human label and its own explanation.
 *
 * Labels come from the schema; only the input widget is replaced here.
 */
const CHOICE_FIELDS = {
  REFERENCE_MODE: [
    { value: 'requirements', label: 'Проєктувати з вимог',
      hint: 'Стартова форма — трапеція, побудована з площі, розмаху й звуження. ' +
            'Від крила NX-2 не успадковується нічого.' },
    { value: 'nx2', label: 'Переробляти крило NX-2',
      hint: 'Старт від обміру крила NX-2 — дев\'яти станцій, вшитих у скрипт. ' +
            'Так працював оригінальний скрипт.' },
  ],
  FIX_ROOT_CHORD: [
    { value: false, label: 'Знаходить оптимізатор',
      hint: 'Коренева хорда — результат: виходить із форми крила під задану площу. ' +
            'Обмежена стелею, щоб не роздувалась (див. «Макс. коренева хорда»).' },
    { value: true, label: 'Як в еталона',
      hint: 'Коренева хорда прибивається до значення еталонного крила й не змінюється. ' +
            'Потрібно, якщо крило мусить стикуватися з наявним фюзеляжем.' },
  ],
  STABILITY_MODE: [
    { value: 'absolute', label: 'Свої числа',
      hint: 'Береться Cn_beta та Cl_beta з полів нижче. Так задають реальну вимогу.' },
    { value: 'reference', label: 'Не гірше за NX-2',
      hint: 'Планка міряється на еталонному крилі. Обережно: NX-2 шляхово нестійкий, ' +
            'тож така вимога фактично нічого не вимагає.' },
  ],
};

/** Segmented control for a parameter with a fixed set of values. */
function choiceField(f, choices) {
  const wrap = document.createElement('div');
  wrap.className = 'field' + (PRIMARY.has(f.name) ? '' : ' adv');
  wrap.dataset.search =
    (f.label + ' ' + f.name + ' ' + choices.map((c) => c.label).join(' ')).toLowerCase();

  const def = DEFAULTS.constants[f.name];
  wrap.innerHTML =
    `<div class="field-label"><span class="lbl">${esc(f.label)}</span>` +
    `<span class="code" title="${esc(f.name)}">${esc(f.name)}</span></div>`;

  const row = document.createElement('div');
  row.className = 'field-input';
  const seg = document.createElement('div');
  seg.className = 'seg';
  const hint = document.createElement('div');
  hint.className = 'field-hint';

  const paint = () => {
    const cur = String(VALUES[f.name]);
    seg.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.v === cur));
    const c = choices.find((x) => String(x.value) === cur);
    hint.textContent = c ? c.hint : `Значення «${VALUES[f.name]}» не з переліку.`;
    seg.classList.toggle('changed', String(def) !== cur);
  };

  for (const c of choices) {
    const b = document.createElement('button');
    b.type = 'button';
    b.dataset.v = String(c.value);
    b.textContent = c.label;
    b.addEventListener('click', () => {
      // keep the JS type: writing the string "false" would become Python True
      VALUES[f.name] = c.value;
      paint(); setDirty(true); recompute();
    });
    seg.appendChild(b);
  }

  row.appendChild(seg);

  const rst = document.createElement('button');
  rst.className = 'reset-one';
  rst.textContent = '↺';
  rst.title = `Дефолт: ${def}`;
  rst.addEventListener('click', () => {
    VALUES[f.name] = def;
    paint(); setDirty(true); recompute();
  });
  row.appendChild(rst);

  wrap.appendChild(row);
  wrap.appendChild(hint);
  paint();
  return wrap;
}

/**
 * `workers` deserves better than a box you type -1 into.
 *
 * scipy maps workers = -1 onto multiprocessing.Pool() with os.cpu_count(), so
 * "-1" literally means "every logical core". One process is only useful for
 * debugging, because a crash in a worker used to take the whole pool down.
 */
function workersField(f) {
  const wrap = document.createElement('div');
  wrap.className = 'field';
  wrap.dataset.search = ('workers процеси ядра потоки паралельно ' + f.label).toLowerCase();

  const cores = CPU_COUNT || 0;
  wrap.innerHTML =
    `<div class="field-label"><span class="lbl">Скільки ядер задіяти</span>` +
    `<span class="code" title="workers">workers</span></div>`;

  const row = document.createElement('div');
  row.className = 'field-input';
  const seg = document.createElement('div');
  seg.className = 'seg';

  const manual = document.createElement('input');
  manual.type = 'text';
  manual.className = 'mini';
  manual.title = 'кількість процесів';

  const extra = document.createElement('div');
  extra.className = 'seg-extra';
  extra.appendChild(Object.assign(document.createElement('span'), { textContent: 'процесів:' }));
  extra.appendChild(manual);

  const modeOf = (v) => (v === -1 ? 'all' : (v === 1 ? 'one' : 'manual'));

  const OPTS = [
    { key: 'one', label: '1 ядро', value: 1,
      hint: 'Один процес. Повільно, зате видно кожну помилку — для відладки.' },
    { key: 'all', label: cores ? `Усі ${cores} ядер` : 'Усі ядра', value: -1,
      hint: cores ? `scipy візьме всі ${cores} логічних ядер (workers = -1).`
                  : 'scipy візьме всі логічні ядра (workers = -1).' },
    { key: 'manual', label: 'Вручну', value: null,
      hint: 'Задати кількість процесів самому — коли треба лишити ядра на інше.' },
  ];

  const hint = document.createElement('div');
  hint.className = 'field-hint';

  function paint() {
    const mode = modeOf(Number(RUNTIME.workers));
    seg.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.key === mode));
    extra.hidden = mode !== 'manual';
    if (mode === 'manual') manual.value = String(RUNTIME.workers);
    const o = OPTS.find((x) => x.key === mode);
    hint.textContent = o ? o.hint : '';
    recompute();
  }

  for (const o of OPTS) {
    const b = document.createElement('button');
    b.type = 'button';
    b.dataset.key = o.key;
    b.textContent = o.label;
    b.addEventListener('click', () => {
      if (o.key === 'manual') {
        const cur = Number(RUNTIME.workers);
        RUNTIME.workers = (cur === 1 || cur === -1)
          ? Math.max(2, Math.floor((cores || 4) / 2)) : cur;
      } else {
        RUNTIME.workers = o.value;
      }
      paint(); setDirty(true);
    });
    seg.appendChild(b);
  }

  manual.addEventListener('input', () => {
    const n = parseInt(manual.value, 10);
    if (!Number.isFinite(n) || n < 1) { manual.style.borderColor = '#e2564d'; return; }
    manual.style.borderColor = '';
    RUNTIME.workers = n;
    setDirty(true); recompute();
  });

  row.appendChild(seg);
  wrap.appendChild(row);
  wrap.appendChild(extra);
  wrap.appendChild(hint);
  paint();
  return wrap;
}

function fieldNode(f) {
  if (f.name === 'workers') return workersField(f);
  if (CHOICE_FIELDS[f.name]) return choiceField(f, CHOICE_FIELDS[f.name]);

  const wrap = document.createElement('div');
  wrap.className = 'field' + (PRIMARY.has(f.name) ? '' : ' adv');
  wrap.dataset.search = (f.label + ' ' + f.name + ' ' + (f.hint || '')).toLowerCase();

  const head = document.createElement('div');
  head.className = 'field-label';
  head.innerHTML =
    `<span class="lbl">${esc(f.label)}</span>` +
    (f.unit ? `<span class="unit">${esc(f.unit)}</span>` : '') +
    `<span class="code" title="${esc(f.name)}">${esc(f.name)}</span>`;
  wrap.appendChild(head);

  const row = document.createElement('div');
  row.className = 'field-input';

  const store = f.kind === 'runtime' ? RUNTIME : VALUES;
  const def = f.kind === 'runtime' ? DEFAULTS.runtime[f.name] : DEFAULTS.constants[f.name];

  let input;
  if (f.type === 'bool') {
    input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = !!store[f.name];
    input.addEventListener('change', () => {
      store[f.name] = input.checked;
      markChanged(input, input.checked, def);
      setDirty(true); recompute();
    });
  } else {
    input = document.createElement('input');
    input.type = 'text';
    input.spellcheck = false;
    input.value = f.type === 'str' ? String(store[f.name]) : fmt(store[f.name], 8);
    input.addEventListener('input', () => {
      if (f.type === 'str') {
        store[f.name] = input.value;
        input.classList.remove('bad');
      } else {
        const n = Number(input.value.trim().replace(',', '.'));
        if (input.value.trim() === '' || !isFinite(n)) {
          input.style.borderColor = '#e2564d';
          return;
        }
        input.style.borderColor = '';
        store[f.name] = f.type === 'int' ? Math.round(n) : n;
      }
      markChanged(input, store[f.name], def);
      setDirty(true); recompute();
    });
  }
  input.dataset.param = f.name;
  row.appendChild(input);

  const rst = document.createElement('button');
  rst.className = 'reset-one';
  rst.textContent = '↺';
  rst.title = `Дефолт: ${def}`;
  rst.addEventListener('click', () => {
    store[f.name] = def;
    if (f.type === 'bool') input.checked = !!def;
    else input.value = f.type === 'str' ? String(def) : fmt(def, 8);
    input.style.borderColor = '';
    markChanged(input, def, def);
    setDirty(true); recompute();
  });
  row.appendChild(rst);
  wrap.appendChild(row);

  if (f.hint) {
    const h = document.createElement('div');
    h.className = 'field-hint' + (/УВАГА|ПОСТУЛЮЄТЬСЯ|Захардкоджено/.test(f.hint) ? ' alert' : '');
    h.textContent = f.hint;
    wrap.appendChild(h);
  }

  markChanged(input, store[f.name], def);
  return wrap;
}

function boundNode(b) {
  const wrap = document.createElement('div');
  wrap.className = 'field adv';   // bounds are always advanced
  wrap.dataset.search = (b.label + ' ' + b.name).toLowerCase();

  const head = document.createElement('div');
  head.className = 'field-label';
  head.innerHTML =
    `<span class="lbl">${esc(b.label)}</span>` +
    (b.unit ? `<span class="unit">${esc(b.unit)}</span>` : '') +
    `<span class="code" title="${esc(b.name)}">${esc(b.name)}</span>`;
  wrap.appendChild(head);

  const row = document.createElement('div');
  row.className = 'field-input';
  const pair = document.createElement('div');
  pair.className = 'pair';

  const mk = (idx) => {
    const i = document.createElement('input');
    i.type = 'text';
    i.spellcheck = false;
    i.value = fmt(BOUNDS[b.name][idx], 6);
    i.title = idx === 0 ? 'мінімум' : 'максимум';
    i.addEventListener('input', () => {
      const n = Number(i.value.trim().replace(',', '.'));
      if (i.value.trim() === '' || !isFinite(n)) { i.style.borderColor = '#e2564d'; return; }
      i.style.borderColor = '';
      BOUNDS[b.name][idx] = n;
      const d = DEFAULTS.bounds[b.name];
      const same = BOUNDS[b.name][0] === d[0] && BOUNDS[b.name][1] === d[1];
      pair.querySelectorAll('input').forEach((x) => x.classList.toggle('changed', !same));
      setDirty(true);
    });
    return i;
  };
  const lo = mk(0), hi = mk(1);
  pair.appendChild(lo); pair.appendChild(hi);
  row.appendChild(pair);

  const rst = document.createElement('button');
  rst.className = 'reset-one';
  rst.textContent = '↺';
  rst.title = `Дефолт: ${DEFAULTS.bounds[b.name].join(' … ')}`;
  rst.addEventListener('click', () => {
    BOUNDS[b.name] = DEFAULTS.bounds[b.name].slice();
    lo.value = fmt(BOUNDS[b.name][0], 6);
    hi.value = fmt(BOUNDS[b.name][1], 6);
    lo.style.borderColor = ''; hi.style.borderColor = '';
    lo.classList.remove('changed'); hi.classList.remove('changed');
    setDirty(true);
  });
  row.appendChild(rst);
  wrap.appendChild(row);

  const d = DEFAULTS.bounds[b.name];
  const same = BOUNDS[b.name][0] === d[0] && BOUNDS[b.name][1] === d[1];
  if (!same) { lo.classList.add('changed'); hi.classList.add('changed'); }
  return wrap;
}

function markChanged(input, val, def) {
  const changed = String(val) !== String(def);
  input.classList.toggle('changed', changed);
}

function buildForm() {
  const form = $('form');
  form.innerHTML = '';

  const byGroup = {};
  for (const f of SCHEMA.constants) (byGroup[f.group] ||= []).push({ ...f, kind: 'const' });
  for (const r of SCHEMA.runtime) (byGroup[r.group] ||= []).push({ ...r, kind: 'runtime' });

  for (const g of ALL_GROUPS()) {
    const card = document.createElement('div');
    card.className = 'group' + (g.id === 'structure' ? ' warn' : '');
    card.dataset.groupCard = g.id;

    const head = document.createElement('div');
    head.className = 'group-head';
    head.innerHTML =
      `<div class="group-title">${esc(g.title)}</div>` +
      (g.hint ? `<div class="group-hint">${esc(g.hint)}</div>` : '');
    card.appendChild(head);

    const fields = document.createElement('div');
    fields.className = 'fields';
    if (g.id === 'bounds') {
      for (const b of SCHEMA.bounds) fields.appendChild(boundNode(b));
    } else {
      for (const f of (byGroup[g.id] || [])) fields.appendChild(fieldNode(f));
    }
    card.appendChild(fields);
    form.appendChild(card);
  }
}

// ==================================================================
// sizing panel
// ==================================================================
function recompute() {
  const S = Number(VALUES.S_FIXED);
  const b = Number(VALUES.B_FULL);
  const V = Number(VALUES.V_DESIGN);
  const CL = Number(VALUES.CL_TARGET);
  const alt = Number(VALUES.ALTITUDE_M);

  const rho = isaDensity(alt);
  const AR = S > 0 ? (b * b) / S : NaN;
  const cbar = b > 0 ? S / b : NaN;
  const lift = 0.5 * rho * V * V * S * CL;
  const mass = lift / G;

  $('szAR').textContent = isFinite(AR) ? AR.toFixed(2) : '—';
  $('szMAC').textContent = isFinite(cbar) ? cbar.toFixed(3) + ' м' : '—';
  $('szRho').textContent = rho.toFixed(4) + ' кг/м³';
  $('szWL').textContent = isFinite(mass / S) ? (mass / S).toFixed(2) + ' кг/м²' : '—';
  $('szLift').textContent = isFinite(lift) ? lift.toFixed(2) + ' Н' : '—';
  $('szMass').textContent = isFinite(mass) ? mass.toFixed(2) + ' кг' : '—';

  const target = Number(($('szPayTarget').value || '').replace(',', '.'));
  const note = $('szNote');
  if (isFinite(target) && target > 0 && isFinite(mass) && mass > 0) {
    const frac = target / mass;
    $('szFrac').textContent = (frac * 100).toFixed(0) + '%';
    if (frac > 1) {
      note.textContent = `Недосяжно: крило в цій точці проєктування несе ${mass.toFixed(2)} кг ` +
        `ПОВНОЇ маси, а вам потрібно ${target} кг лише корисного навантаження. ` +
        `Треба збільшувати площу, швидкість або CL.`;
    } else if (frac > 0.55) {
      note.textContent = `Частка корисного навантаження ${(frac * 100).toFixed(0)}% — ` +
        `дуже амбітно. Реалістично для карбонового планера цього класу 30–45%.`;
    } else {
      note.textContent = '';
    }
  } else {
    $('szFrac').textContent = '—';
    note.textContent = '';
  }

  const D = (SCHEMA && SCHEMA.var_names) ? SCHEMA.var_names.length : 16;
  const pop = Number(RUNTIME.popsize) * D;
  evalCap = Math.floor((Number(RUNTIME.maxiter) + 1) * Number(RUNTIME.popsize) * D * Number(RUNTIME.eval_cap_factor));
  $('szPop').textContent = isFinite(pop) ? String(pop) : '—';
  $('szEvals').textContent = isFinite(evalCap) ? String(evalCap) : '—';
  $('szBudget').textContent = fmt(RUNTIME.time_budget_hours, 2) + ' год';

  // Rough wall-clock estimate. SEC_PER_EVAL was measured on this project at the
  // default mesh resolution; it scales with SPANWISE_RES_MAIN * CHORDWISE_RES_MAIN.
  const SEC_PER_EVAL = 13;
  const w = Number(RUNTIME.workers);
  const procs = w === -1 ? (CPU_COUNT || 1) : Math.max(1, w);
  $('szWorkers').textContent = w === -1
    ? `усі${CPU_COUNT ? ' ' + CPU_COUNT : ''}` : String(procs);

  const hours = (evalCap * SEC_PER_EVAL) / procs / 3600;
  const budget = Number(RUNTIME.time_budget_hours);
  $('szTime').textContent = isFinite(hours)
    ? (hours < 1 ? `${Math.round(hours * 60)} хв` : `${hours.toFixed(1)} год`) : '—';

  const tnote = $('szTimeNote');
  if (isFinite(hours) && isFinite(budget) && hours > budget) {
    tnote.textContent = `Не влізе в бюджет ${budget} год — прогін спиниться раніше, ` +
      `приблизно на ${Math.round(evalCap * budget / hours)} оцінках із ${evalCap}.`;
  } else if (procs === 1 && (CPU_COUNT || 0) > 2) {
    tnote.textContent = `На одному ядрі це ${hours.toFixed(1)} год. ` +
      `Усі ${CPU_COUNT} ядер зроблять те саме за ~${(hours / CPU_COUNT).toFixed(1)} год.`;
  } else {
    tnote.textContent = '';
  }
}

$('szPayTarget').addEventListener('input', recompute);

// ==================================================================
// visibility: basic/advanced mode + search, applied together
// ==================================================================
function applyFilter() {
  const q = $('search').value.trim().toLowerCase();
  const showAdv = $('showAdv').checked;

  // A search query always searches everything, advanced included.
  document.body.classList.toggle('basic', !showAdv && q === '');

  // Scoped to #form on purpose: the requirements tab has its own fields and
  // must not be touched by the basic/advanced toggle or this search box.
  let shown = 0;
  $('form').querySelectorAll('.field').forEach((f) => {
    const matches = q === '' || f.dataset.search.includes(q);
    const allowed = showAdv || q !== '' || !f.classList.contains('adv');
    const visible = matches && allowed;
    f.classList.toggle('hidden', !visible);
    if (visible) shown++;
  });

  const liveGroups = new Set();
  $('form').querySelectorAll('.group').forEach((g) => {
    const any = [...g.querySelectorAll('.field')]
      .some((f) => !f.classList.contains('hidden') &&
                   (showAdv || q !== '' || !f.classList.contains('adv')));
    g.hidden = !any;
    if (any) liveGroups.add(g.dataset.groupCard);
  });

  // keep the sidebar in sync - no navigation to an empty group
  document.querySelectorAll('.gnav').forEach((b) => {
    b.hidden = !liveGroups.has(b.dataset.group);
  });

  const total = $('form').querySelectorAll('.field').length;
  $('advCount').textContent = String(total - PRIMARY.size);
  $('advLabel').textContent = showAdv
    ? `Решта параметрів (показано ${shown} з ${total})`
    : `Решта параметрів (${total - PRIMARY.size})`;
}

$('search').addEventListener('input', applyFilter);
$('showAdv').addEventListener('change', applyFilter);

// ==================================================================
// save / load
// ==================================================================
function setDirty(v) {
  dirty = v;
  $('dirtyBar').classList.toggle('show', v);
  $('btnSave').textContent = v ? 'Зберегти конфіг •' : 'Зберегти конфіг';
}

$('btnSave').addEventListener('click', async () => {
  const r = await window.api.saveConfig(collectConfig());
  if (r.ok) { setDirty(false); toast('Збережено у wing_config.json', 'ok'); }
  else toast('Не збереглося: ' + r.error, 'bad');
});

$('btnSaveAs').addEventListener('click', async () => {
  const r = await window.api.saveConfigAs(collectConfig());
  if (r.ok) toast('Збережено: ' + r.path, 'ok');
  else if (!r.canceled) toast('Помилка: ' + r.error, 'bad');
});

$('btnOpen').addEventListener('click', async () => {
  const r = await window.api.openConfigFrom();
  if (r.canceled) return;
  if (!r.ok) { toast('Помилка: ' + r.error, 'bad'); return; }
  applyConfig(r.config);
  buildForm(); applyFilter(); recompute(); setDirty(true);
  toast('Завантажено (не забудьте «Зберегти конфіг»)', 'ok');
});

$('btnReset').addEventListener('click', () => {
  applyConfig(null);
  buildForm(); applyFilter(); recompute(); setDirty(true);
  toast('Повернуто дефолти зі скрипта — натисніть «Зберегти конфіг»', 'ok');
});

// ==================================================================
// run
// ==================================================================
function setRunning(v, pid) {
  running = v;
  $('btnRun').disabled = v || !!(ACTIVE && ACTIVE.frozen);
  $('btnStop').disabled = !v;
  $('stState').textContent = v ? 'виконується' : 'не запущено';
  $('stPid').textContent = pid || '—';
  if (v) {
    runStartedAt = Date.now();
    clearInterval(runTimer);
    runTimer = setInterval(tickTime, 1000);
    clearInterval(resPollTimer);
    resPollTimer = setInterval(() => {
      if (document.querySelector('#page-results').classList.contains('active')) refreshResults(true);
    }, 6000);
  } else {
    clearInterval(runTimer);
    clearInterval(resPollTimer);
  }
}

function tickTime() {
  const s = Math.floor((Date.now() - runStartedAt) / 1000);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  $('stTime').textContent = (h ? h + ':' : '') + String(m).padStart(h ? 2 : 1, '0') + ':' + String(ss).padStart(2, '0');
}

$('btnRun').addEventListener('click', async () => {
  if (dirty) {
    const r = await window.api.saveConfig(collectConfig());
    if (r.ok) { setDirty(false); appendLog('[ui] конфіг збережено перед запуском', 'ui'); }
  }
  evalCount = 0; bestObj = null; bestLD = null;
  $('stEvals').textContent = '0';
  $('stBest').textContent = '—';
  $('stLD').textContent = '—';
  $('progFill').style.width = '0%';

  const r = await window.api.runStart();
  if (!r.ok) { toast(r.error, 'bad'); return; }
  appendLog(`[ui] запуск: ${r.interpreter} -u NX-2_DE_Optimization.py   (cwd: ${r.cwd})`, 'ui');
  setRunning(true, r.pid);
  document.querySelector('.tab[data-tab="run"]').click();
});

$('btnStop').addEventListener('click', async () => {
  const r = await window.api.runStop();
  if (!r.ok) toast(r.error, 'bad');
  else appendLog('[ui] надіслано сигнал зупинки', 'ui');
});

$('btnClearLog').addEventListener('click', () => { $('log').textContent = ''; });

function logClass(line) {
  if (line.startsWith('[DE eval')) return 'l-eval';
  if (line.startsWith('[config]')) return 'l-cfg';
  if (line.startsWith('[ui]')) return 'l-ui';
  if (line.startsWith('[run]')) return 'l-cfg';
  if (line.startsWith('===') || line.startsWith('[STOP]')) return 'l-hdr';
  return '';
}

function appendLog(line, forceClass) {
  const log = $('log');
  const span = document.createElement('span');
  const cls = forceClass ? 'l-' + forceClass : logClass(line);
  if (cls) span.className = cls;
  span.textContent = line + '\n';
  log.appendChild(span);
  while (log.childNodes.length > 4000) log.removeChild(log.firstChild);
  if ($('autoscroll').checked) log.scrollTop = log.scrollHeight;
}

window.api.onLog(({ line, stream }) => {
  if (stream === 'err') {
    const span = document.createElement('span');
    span.className = 'l-err';
    span.textContent = line + '\n';
    $('log').appendChild(span);
    if ($('autoscroll').checked) $('log').scrollTop = $('log').scrollHeight;
  } else {
    appendLog(line);
  }

  const m = /^\[DE eval (\d+)\]\s+obj=([+-]?[\d.eE+-]+)/.exec(line);
  if (m) {
    evalCount = parseInt(m[1], 10);
    $('stEvals').textContent = evalCap ? `${evalCount} / ${evalCap}` : String(evalCount);
    if (evalCap > 0) $('progFill').style.width = Math.min(100, (evalCount / evalCap) * 100) + '%';
    const obj = parseFloat(m[2]);
    if (isFinite(obj) && (bestObj === null || obj < bestObj)) {
      bestObj = obj;
      $('stBest').textContent = obj.toFixed(4);
      const ld = /LD=([\d.]+)/.exec(line);
      if (ld) { bestLD = parseFloat(ld[1]); $('stLD').textContent = bestLD.toFixed(2); }
    }
  }
});

window.api.onRunState(async (st) => {
  if (st.active) ACTIVE = st.active;
  setRunning(st.running, st.pid);
  if (!st.running) {
    await loadStudies();
    applyFrozen();
    const why = st.code === 0 ? 'завершено успішно'
      : st.signal ? `зупинено (${st.signal})`
      : `завершено з кодом ${st.code}`;
    appendLog(`[ui] ${why}, тривало ${(st.elapsedSec || 0).toFixed(0)} с`, 'ui');
    toast('Розрахунок ' + why, st.code === 0 ? 'ok' : 'bad');
    refreshResults();
  }
});

// ==================================================================
// results
// ==================================================================
$('btnRefresh').addEventListener('click', () => refreshResults());

$('btnOpenFolder').addEventListener('click', () => {
  if (ACTIVE) window.api.openPath(ACTIVE.resultsDir);
});

$('btnClearRes').addEventListener('click', async () => {
  const r = await window.api.clearResults();
  if (r.canceled) return;
  if (!r.ok) { toast(r.error, 'bad'); return; }
  if (r.active) ACTIVE = r.active;
  await loadStudies();
  applyFrozen();
  toast(`Видалено записів: ${r.removed}. Дослідження розморожено.`, 'ok');
  refreshResults();
});

$('sortBy').addEventListener('change', () => renderList());

async function refreshResults(quiet) {
  const r = await window.api.listResults();
  if (!r.ok) { if (!quiet) toast(r.error, 'bad'); return; }
  ROWS = r.rows;
  window.__best = r.best;
  $('resCount').textContent =
    (r.study ? `${r.study} · ` : '') + nEvals(ROWS.length) +
    (r.best ? ` · найкраща ${fmt(r.best.objective, 4)}` : '');
  renderList();
  drawChart();
  if (activeDir) {
    const still = ROWS.some((x) => x.dir === activeDir);
    if (!still) { activeDir = null; $('detail').innerHTML = '<div class="empty">Виберіть оцінку зі списку ліворуч.</div>'; }
  }
}

function renderList() {
  const mode = $('sortBy').value;
  const rows = ROWS.slice();
  if (mode === 'objective') {
    rows.sort((a, b) => (a.objective ?? Infinity) - (b.objective ?? Infinity));
  } else if (mode === 'ld') {
    rows.sort((a, b) => (b.LD ?? -Infinity) - (a.LD ?? -Infinity));
  } else {
    rows.sort((a, b) => (b.eval_id ?? 0) - (a.eval_id ?? 0));
  }

  const bestId = window.__best ? window.__best.eval_id : null;
  const list = $('evalList');
  list.innerHTML = '';
  for (const r of rows.slice(0, 600)) {
    const el = document.createElement('div');
    el.className = 'eval-row'
      + (r.dir === activeDir ? ' active' : '')
      + (r.eval_id === bestId ? ' best' : '')
      + (r.ok ? '' : ' failed');
    el.dataset.dir = r.dir;
    const obj = r.ok ? fmt(r.objective, 3) : 'FAIL';
    const ld = r.ok && r.LD != null ? `L/D ${Number(r.LD).toFixed(2)}` : '';
    el.innerHTML =
      `<span class="eid">#${String(r.eval_id ?? '?').padStart(5, '0')}</span>` +
      `<span><span class="obj">${esc(obj)}</span> <span class="ld">${esc(ld)}</span></span>` +
      `<span class="tags">${(r.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join('')}</span>`;
    list.appendChild(el);
  }
  if (!rows.length) list.innerHTML = '<div class="empty">Результатів ще немає.</div>';
}

$('evalList').addEventListener('click', (e) => {
  const row = e.target.closest('.eval-row');
  if (!row || !row.dataset.dir) return;
  activeDir = row.dataset.dir;
  document.querySelectorAll('.eval-row').forEach((x) => x.classList.toggle('active', x === row));
  showDetail(activeDir);
});

const DETAIL_FIELDS = [
  ['objective', 'Цільова функція', 4],
  ['LD', 'L/D', 3],
  ['CL_at_alpha_star', 'CL', 4],
  ['CD_total', 'CD повний', 5],
  ['CD_profile', 'CD профільний', 5],
  ['CD_induced_used', 'CD індуктивний', 5],
  ['e_from_liftdist_LL', 'Коеф. Освальда e', 3],
  ['alpha_star_deg', 'Кут атаки α*', 2],
  ['static_margin', 'Запас стійкості', 4],
  ['CMalpha_per_rad', 'Cm_alpha', 4],
  ['Cm_at_alpha_star', 'Cm балансування', 4],
  ['Cn_beta_per_rad', 'Cn_beta', 5],
  ['Cl_beta_per_rad', 'Cl_beta', 5],
  ['M_root_at_ycut_Nm', 'Кореневий момент, Н·м', 3],
  ['xcg_frac', 'Центрівка, частка САХ', 3],
  ['c_root_m', 'Коренева хорда, м', 4],
  ['c_tip_m', 'Хорда законцівки, м', 4],
  ['MAC_m', 'САХ, м', 4],
  ['x_max_m', 'Габарит X, м', 4],
  ['sigma_peak_Pa', 'Пікове напруження, Па', 3],
  ['w_tip_m', 'Прогин законцівки, м', 4],
];

async function showDetail(dir) {
  const d = $('detail');
  d.innerHTML = '<div class="empty">Завантаження…</div>';
  const r = await window.api.resultDetail(dir);
  if (!r.ok) { d.innerHTML = `<div class="empty">${esc(r.error)}</div>`; return; }

  d.innerHTML = '';

  if (r.failed) {
    const c = document.createElement('div');
    c.className = 'card';
    c.innerHTML = `<div class="card-head">Оцінка завершилась помилкою</div>` +
      `<div class="card-body"><pre class="raw">${esc(r.failed)}</pre></div>`;
    d.appendChild(c);
  }

  if (r.metrics) {
    const flat = { ...r.metrics };
    flat.xcg_frac = r.metrics?.cg_solve?.xcg_frac_MAC;
    flat.c_root_m = r.metrics?.geom?.c_root_m;
    flat.c_tip_m = r.metrics?.geom?.c_tip_m;
    flat.MAC_m = r.metrics?.geom?.MAC_m;
    flat.x_max_m = r.metrics?.geom?.x_max_m;
    flat.sigma_peak_Pa = r.metrics?.structure?.sigma_peak_Pa;
    flat.w_tip_m = r.metrics?.structure?.w_tip_m;

    const card = document.createElement('div');
    card.className = 'card';
    const items = DETAIL_FIELDS
      .filter(([k]) => flat[k] !== undefined && flat[k] !== null)
      .map(([k, label, dg]) => `<div class="kv-item"><span>${esc(label)}</span><b>${esc(fmt(flat[k], dg))}</b></div>`)
      .join('');
    card.innerHTML =
      `<div class="card-head">${esc(r.metrics.eval_folder || '')}` +
      `<div class="spacer"></div>` +
      `<button class="btn" data-act="open">Відкрити папку</button>` +
      (r.stl
        ? `<button class="btn" data-act="stl">STL (${r.stl.sizeKB} КБ)</button>`
        : `<button class="btn accent" data-act="gen">⬇ Згенерувати STL</button>`) +
      `</div><div class="card-body"><div class="kv">${items}</div></div>`;
    card.querySelector('[data-act="open"]').addEventListener('click', () => window.api.openPath(r.dir));

    const stlBtn = card.querySelector('[data-act="stl"]');
    if (stlBtn) stlBtn.addEventListener('click', () => window.api.reveal(r.stl.path));

    const genBtn = card.querySelector('[data-act="gen"]');
    if (genBtn) genBtn.addEventListener('click', async () => {
      genBtn.disabled = true;
      genBtn.textContent = 'генерую…';
      const res = await window.api.regenerate({
        evalId: r.metrics.eval_id,
        dir: r.dir,                 // main process verifies the file appeared here
      });
      if (res.ok) {
        toast(`STL згенеровано (${res.sizeKB} КБ)`, 'ok');
        showDetail(dir);            // reload so the button becomes "STL (N КБ)"
      } else {
        genBtn.disabled = false;
        genBtn.textContent = '⬇ Згенерувати STL';
        // regenerate.py refuses when the config no longer matches the run
        const why = (res.log || res.error || '').split('\n')
          .find((l) => /НЕ ЗБІГАЄТЬСЯ|ПРОП/.test(l)) || res.error || 'див. лог';
        toast('Не вдалося: ' + why.trim(), 'bad');
      }
    });
    d.appendChild(card);

    const dv = document.createElement('div');
    dv.className = 'card';
    const design = r.metrics.design || {};
    dv.innerHTML =
      `<div class="card-head">Змінні проєктування</div>` +
      `<div class="card-body"><div class="kv">` +
      Object.keys(design).map((k) =>
        `<div class="kv-item"><span>${esc(k)}</span><b>${esc(fmt(design[k], 5))}</b></div>`).join('') +
      `</div></div>`;
    d.appendChild(dv);
  }

  // Charts from curves.json - preferred over the pre-rendered PNGs
  if (r.curves) {
    lineChart._all = [];
    renderCurves(d, r.curves);
  }

  if (r.metrics) {
    const raw = document.createElement('div');
    raw.className = 'card';
    raw.innerHTML = `<div class="card-head">metrics.json</div>` +
      `<div class="card-body"><pre class="raw">${esc(JSON.stringify(r.metrics, null, 2))}</pre></div>`;
    d.appendChild(raw);
  }
}

// ==================================================================
// charts drawn from curves.json (replaces the pre-rendered PNGs)
// ==================================================================
const CH = {
  grid: '#2a3140', text: '#8b95a7', faint: '#5f6878',
  c1: '#4da3ff', c2: '#3fbf7f', c3: '#e0a33e', c4: '#c78bff',
};

/**
 * series: [{x:[], y:[], color, label, dash?, width?}]
 * opts:   {height, equalAspect, invertY, xLabel, yLabel, zeroLine}
 */
function lineChart(host, series, opts) {
  const o = opts || {};
  const cv = document.createElement('canvas');
  cv.className = 'plot';
  host.appendChild(cv);

  const draw = () => {
    const cssW = host.clientWidth || 700;
    const cssH = o.height || 240;
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(cssW * dpr);
    cv.height = Math.round(cssH * dpr);
    cv.style.height = cssH + 'px';
    const g = cv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, cssW, cssH);

    const live = series.filter((s) => s.x && s.x.length && s.y && s.y.length);
    if (!live.length) return;

    let xMin = Infinity, xMax = -Infinity, yMin = Infinity, yMax = -Infinity;
    for (const s of live) {
      for (const v of s.x) { if (v < xMin) xMin = v; if (v > xMax) xMax = v; }
      for (const v of s.y) { if (v < yMin) yMin = v; if (v > yMax) yMax = v; }
    }
    if (o.zeroLine) { yMin = Math.min(yMin, 0); yMax = Math.max(yMax, 0); }
    let xSpan = (xMax - xMin) || 1, ySpan = (yMax - yMin) || 1;
    yMin -= ySpan * 0.08; yMax += ySpan * 0.08; ySpan = yMax - yMin;

    const padL = 56, padR = 12, padT = 10, padB = 30;
    let W = cssW - padL - padR, H = cssH - padT - padB;

    // equal aspect: same metres-per-pixel on both axes (planform drawings)
    let ox = 0, oy = 0;
    if (o.equalAspect) {
      const sc = Math.min(W / xSpan, H / ySpan);
      ox = (W - xSpan * sc) / 2; oy = (H - ySpan * sc) / 2;
      W = xSpan * sc; H = ySpan * sc;
    }
    const X = (v) => padL + ox + ((v - xMin) / xSpan) * W;
    const Y = (v) => o.invertY
      ? padT + oy + ((v - yMin) / ySpan) * H
      : padT + oy + H - ((v - yMin) / ySpan) * H;

    // grid + ticks
    g.strokeStyle = CH.grid; g.lineWidth = 1;
    g.fillStyle = CH.faint; g.font = '10px ui-monospace, monospace';
    for (let i = 0; i <= 4; i++) {
      const yy = padT + oy + (H * i) / 4;
      g.beginPath(); g.moveTo(padL + ox, yy); g.lineTo(padL + ox + W, yy); g.stroke();
      const val = o.invertY ? yMin + (ySpan * i) / 4 : yMax - (ySpan * i) / 4;
      g.fillText(fmt(val, 3), 4, yy + 3);
    }
    for (let i = 0; i <= 4; i++) {
      const xx = padL + ox + (W * i) / 4;
      g.beginPath(); g.moveTo(xx, padT + oy); g.lineTo(xx, padT + oy + H); g.stroke();
      const lbl = fmt(xMin + (xSpan * i) / 4, 3);
      g.fillText(lbl, xx - g.measureText(lbl).width / 2, cssH - 16);
    }

    if (o.zeroLine && yMin < 0 && yMax > 0) {
      g.strokeStyle = '#3d465a'; g.lineWidth = 1;
      g.beginPath(); g.moveTo(padL + ox, Y(0)); g.lineTo(padL + ox + W, Y(0)); g.stroke();
    }

    for (const s of live) {
      g.strokeStyle = s.color || CH.c1;
      g.lineWidth = s.width || 1.8;
      g.setLineDash(s.dash || []);
      g.beginPath();
      for (let i = 0; i < s.x.length; i++) {
        const px = X(s.x[i]), py = Y(s.y[i]);
        if (i === 0) g.moveTo(px, py); else g.lineTo(px, py);
      }
      g.stroke();
      g.setLineDash([]);
    }

    g.fillStyle = CH.text; g.font = '11px sans-serif';
    if (o.xLabel) {
      g.fillText(o.xLabel, padL + ox + W - g.measureText(o.xLabel).width, cssH - 3);
    }
    if (o.yLabel) g.fillText(o.yLabel, 4, cssH - 3);

    // legend
    let lx = padL + ox + 6, ly = padT + 12;
    g.font = '10.5px sans-serif';
    for (const s of live) {
      if (!s.label) continue;
      g.strokeStyle = s.color || CH.c1; g.lineWidth = 2;
      g.setLineDash(s.dash || []);
      g.beginPath(); g.moveTo(lx, ly - 3); g.lineTo(lx + 16, ly - 3); g.stroke();
      g.setLineDash([]);
      g.fillStyle = CH.text;
      g.fillText(s.label, lx + 21, ly);
      lx += 27 + g.measureText(s.label).width;
    }
  };

  draw();
  (lineChart._all ||= []).push(draw);
  return draw;
}

function mirror(y, v) {
  // half-span arrays -> full span, root in the middle
  const ry = [...y].reverse().map((t) => -t);
  const rv = [...v].reverse();
  return { x: ry.concat(y.slice(1)), y: rv.concat(v.slice(1)) };
}

function renderCurves(host, curves) {
  const p = curves.planform, l = curves.lift, st = curves.structure;

  const card = (title, hint) => {
    const c = document.createElement('div');
    c.className = 'card';
    c.innerHTML = `<div class="card-head">${esc(title)}</div>` +
      `<div class="card-body">${hint ? `<div class="plot-hint">${esc(hint)}</div>` : ''}</div>`;
    host.appendChild(c);
    return c.querySelector('.card-body');
  };

  if (p && p.y && p.y.length) {
    const b1 = card('Вид зверху (x–y)', 'x зростає вниз за потоком. Пунктир — лінія стрілоподібності.');
    const ref = mirror(p.y, p.x_ref), le = mirror(p.y, p.x_le), te = mirror(p.y, p.x_te);
    lineChart(b1, [
      { x: ref.x, y: ref.y, color: CH.faint, label: 'лінія стрілоподібності', dash: [5, 4], width: 1.2 },
      { x: le.x, y: le.y, color: CH.c1, label: 'передня кромка' },
      { x: te.x, y: te.y, color: CH.c3, label: 'задня кромка' },
    ], { height: 250, equalAspect: true, invertY: true, xLabel: 'y, м', yLabel: 'x, м' });

    const b2 = card('Вид спереду (y–z)', 'Розхід передньої та задньої кромок показує крутку.');
    const zl = mirror(p.y, p.z_le), zt = mirror(p.y, p.z_te);
    lineChart(b2, [
      { x: zl.x, y: zl.y, color: CH.c1, label: 'передня кромка' },
      { x: zt.x, y: zt.y, color: CH.c3, label: 'задня кромка' },
    ], { height: 200, equalAspect: true, xLabel: 'y, м', yLabel: 'z, м', zeroLine: true });

    const b3 = card('Хорда та крутка по розмаху', null);
    lineChart(b3, [{ x: p.y, y: p.chord, color: CH.c2, label: 'хорда, м' }],
      { height: 160, xLabel: 'y, м', yLabel: 'c, м' });
    lineChart(b3, [{ x: p.y, y: p.twist_deg, color: CH.c4, label: 'крутка, °' }],
      { height: 160, xLabel: 'y, м', yLabel: '°', zeroLine: true });
  }

  if (l && l.y && l.y.length) {
    const b = card('Розподіл підйомної сили', 'Півкрило, від межі фюзеляжу до законцівки. На кінці примусово 0.');
    lineChart(b, [{ x: l.y, y: l.L_prime, color: CH.c1, label: "L′(y), Н/м" }],
      { height: 180, xLabel: 'y, м', yLabel: "Н/м", zeroLine: true });
    lineChart(b, [{ x: l.y, y: l.cl, color: CH.c2, label: 'c_l(y)' }],
      { height: 180, xLabel: 'y, м', yLabel: 'c_l', zeroLine: true });
  }

  if (st && st.y && st.y.length) {
    const b = card('Конструкція (проксі-модель)',
      'УВАГА: жорсткість тут постульована константою, це не розрахунок лонжерона.');
    lineChart(b, [{ x: st.y, y: st.M, color: CH.c3, label: 'момент згину, Н·м' }],
      { height: 160, xLabel: 'y, м', yLabel: 'Н·м', zeroLine: true });
    lineChart(b, [
      { x: st.y, y: st.w_def, color: CH.c1, label: 'прогин, м' },
    ], { height: 160, xLabel: 'y, м', yLabel: 'м', zeroLine: true });
    lineChart(b, [{ x: st.y, y: st.sigma_vm, color: CH.c4, label: 'напруження за Мізесом, Па' }],
      { height: 160, xLabel: 'y, м', yLabel: 'Па' });
  }
}

// ---------------- convergence chart ----------------
function drawChart() {
  const cv = $('chart');
  const pts = ROWS.filter((r) => r.ok && isFinite(r.objective))
    .map((r) => ({ x: r.eval_id, y: r.objective }))
    .sort((a, b) => a.x - b.x);

  const dpr = window.devicePixelRatio || 1;
  const cssW = cv.clientWidth || 1000, cssH = 180;
  cv.width = Math.round(cssW * dpr);
  cv.height = Math.round(cssH * dpr);
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, cssW, cssH);

  if (pts.length < 2) {
    g.fillStyle = '#5f6878';
    g.font = '12px sans-serif';
    g.fillText('Мало даних для графіка', 10, 24);
    return;
  }

  const ys = pts.map((p) => p.y).slice().sort((a, b) => a - b);
  const yMin = ys[0];
  const yMax = ys[Math.min(ys.length - 1, Math.floor(ys.length * 0.85))];
  const span = (yMax - yMin) || 1;
  const xMin = pts[0].x, xMax = pts[pts.length - 1].x;
  const xSpan = (xMax - xMin) || 1;

  const padL = 54, padR = 10, padT = 10, padB = 22;
  const W = cssW - padL - padR, H = cssH - padT - padB;
  const X = (x) => padL + ((x - xMin) / xSpan) * W;
  const Y = (y) => padT + H - ((Math.min(Math.max(y, yMin), yMax) - yMin) / span) * H;

  g.strokeStyle = '#2a3140'; g.lineWidth = 1;
  g.fillStyle = '#5f6878'; g.font = '10px ui-monospace, monospace';
  for (let i = 0; i <= 4; i++) {
    const yy = padT + (H * i) / 4;
    g.beginPath(); g.moveTo(padL, yy); g.lineTo(padL + W, yy); g.stroke();
    const val = yMax - (span * i) / 4;
    g.fillText(fmt(val, 2), 4, yy + 3);
  }

  g.fillStyle = 'rgba(77,163,255,.5)';
  for (const p of pts) {
    const clipped = p.y > yMax;
    g.fillStyle = clipped ? 'rgba(226,86,77,.6)' : 'rgba(77,163,255,.45)';
    g.beginPath(); g.arc(X(p.x), Y(p.y), 1.9, 0, Math.PI * 2); g.fill();
  }

  let best = Infinity;
  g.strokeStyle = '#3fbf7f'; g.lineWidth = 1.8; g.beginPath();
  let started = false;
  for (const p of pts) {
    if (p.y < best) best = p.y;
    const px = X(p.x), py = Y(best);
    if (!started) { g.moveTo(px, py); started = true; } else g.lineTo(px, py);
  }
  g.stroke();

  g.fillStyle = '#5f6878';
  g.fillText(`#${xMin}`, padL, cssH - 7);
  const lbl = `#${xMax}`;
  g.fillText(lbl, padL + W - g.measureText(lbl).width, cssH - 7);
  g.fillStyle = '#3fbf7f';
  g.fillText('зелена лінія — найкраще на цей момент', padL + 60, cssH - 7);
}

window.addEventListener('resize', () => {
  if (ROWS.length) drawChart();
  for (const redraw of (lineChart._all || [])) redraw();
});

boot();
