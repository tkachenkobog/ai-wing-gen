'use strict';

const { app, BrowserWindow, ipcMain, dialog, shell } = require('electron');
const os = require('os');
const fs = require('fs');
const fsp = require('fs/promises');
const path = require('path');
const { spawn, execFile } = require('child_process');

// ------------------------------------------------------------------
// Paths
//
// Nothing here points at a study. Every per-study path comes from the active
// study (see the STUDIES section below). The two template files are what a new
// study is seeded from, and they are also what the scripts use when run from a
// terminal with no study involved.
// ------------------------------------------------------------------
const UI_DIR = __dirname;
const ROOT = path.dirname(UI_DIR);
const PY_DIR = path.join(ROOT, '01_PYTHON');
const SCRIPT = path.join(PY_DIR, 'NX-2_DE_Optimization.py');
const REGEN_SCRIPT = path.join(PY_DIR, 'regenerate.py');
const SCHEMA_PATH = path.join(UI_DIR, 'param_schema.json');
const SCHEMA_TOOL = path.join(UI_DIR, 'tools', 'gen_schema.py');

const SIZING_DIR = path.join(ROOT, '04_SIZING');
const SIZING_SCRIPT = path.join(SIZING_DIR, 'sizing.py');

const TPL_REQUIREMENTS = path.join(SIZING_DIR, 'requirements.json');
const TPL_CONFIG = path.join(PY_DIR, 'wing_config.json');

const STUDIES_DIR = path.join(ROOT, '05_STUDIES');
const INDEX_PATH = path.join(STUDIES_DIR, 'index.json');

let win = null;
let child = null;          // running python process
let pyCmd = null;          // { cmd, args, label }
let active = null;         // active study, see setActive()

// ------------------------------------------------------------------
// Window
// ------------------------------------------------------------------
function createWindow() {
  win = new BrowserWindow({
    width: 1500,
    height: 980,
    minWidth: 1050,
    minHeight: 700,
    backgroundColor: '#12151a',
    title: 'Wing Generator — NX-2 DE Optimization',
    webPreferences: {
      preload: path.join(UI_DIR, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });
  win.removeMenu();

  // `npm run debug` (or WING_UI_DEBUG=1) mirrors renderer errors into the terminal.
  if (process.env.WING_UI_DEBUG || process.argv.includes('--debug')) {
    win.webContents.on('console-message', (...a) => {
      // Electron changed this signature across majors - handle both shapes.
      const ev = a[0];
      if (ev && typeof ev === 'object' && 'message' in ev) {
        console.log(`[renderer:${ev.level}] ${ev.message} (${ev.sourceId}:${ev.lineNumber})`);
      } else {
        console.log(`[renderer:${a[0]}] ${a[1]} (${a[3]}:${a[2]})`);
      }
    });
    win.webContents.on('did-fail-load', (_e, code, desc, url) =>
      console.log(`[renderer] did-fail-load ${code} ${desc} ${url}`));
    win.webContents.on('preload-error', (_e, p, err) =>
      console.log(`[renderer] preload-error ${p}: ${err}`));
    win.webContents.on('did-finish-load', () => console.log('[renderer] did-finish-load'));
  }

  win.loadFile(path.join(UI_DIR, 'renderer', 'index.html'));
}

app.whenReady().then(createWindow);

app.on('window-all-closed', () => {
  stopChild();
  app.quit();
});

app.on('before-quit', stopChild);

// ------------------------------------------------------------------
// Helpers
// ------------------------------------------------------------------
function send(channel, payload) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
}

async function readJson(file) {
  const txt = await fsp.readFile(file, 'utf-8');
  return JSON.parse(txt);
}

async function readJsonOr(file, fallback = null) {
  try { return await readJson(file); } catch { return fallback; }
}

async function writeJson(file, data) {
  await fsp.mkdir(path.dirname(file), { recursive: true });
  const tmp = file + '.tmp';
  await fsp.writeFile(tmp, JSON.stringify(data, null, 2), 'utf-8');
  await fsp.rename(tmp, file);
}

function exists(p) {
  try { fs.accessSync(p); return true; } catch { return false; }
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

function nowStamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function todayStamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// ==================================================================
// STUDIES
//
// Layout (mirrors 05_STUDIES/studylib.py, which is the CLI/migration side):
//   05_STUDIES/index.json                  last opened study
//   05_STUDIES/<project>/project.json
//   05_STUDIES/<project>/<study>/study.json requirements.json wing_config.json
//                                           sizing_report.json RESULTS/ run.log
// ==================================================================
const TRANSLIT = {
  'а': 'a', 'б': 'b', 'в': 'v', 'г': 'h', 'ґ': 'g', 'д': 'd', 'е': 'e',
  'є': 'ie', 'ж': 'zh', 'з': 'z', 'и': 'y', 'і': 'i', 'ї': 'i', 'й': 'i',
  'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r',
  'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch',
  'ш': 'sh', 'щ': 'shch', 'ь': '', 'ю': 'iu', 'я': 'ia', 'ы': 'y', 'э': 'e', 'ъ': '',
};

function slugify(name, fallback = 'bez-nazvy') {
  const s = String(name || '').trim().toLowerCase();
  let out = '';
  for (const ch of s) {
    if (Object.prototype.hasOwnProperty.call(TRANSLIT, ch)) out += TRANSLIT[ch];
    else if (/[a-z0-9]/.test(ch)) out += ch;
    else out += '-';
  }
  out = out.replace(/-{2,}/g, '-').replace(/^-+|-+$/g, '').slice(0, 60);
  return out || fallback;
}

function uniqueSlug(parentDir, slug) {
  if (!exists(path.join(parentDir, slug))) return slug;
  let n = 2;
  while (exists(path.join(parentDir, `${slug}-${n}`))) n++;
  return `${slug}-${n}`;
}

function studyPaths(projectSlug, studySlug) {
  const dir = path.join(STUDIES_DIR, projectSlug, studySlug);
  return {
    project: projectSlug,
    study: studySlug,
    dir,
    metaPath: path.join(dir, 'study.json'),
    requirements: path.join(dir, 'requirements.json'),
    config: path.join(dir, 'wing_config.json'),
    report: path.join(dir, 'sizing_report.json'),
    results: path.join(dir, 'RESULTS'),
    log: path.join(dir, 'run.log'),
  };
}

/** Cheap scan: how many evals, how many failed, which is best. */
async function scanResults(resultsDir) {
  const out = { evals: 0, failed: 0, best: null };
  let entries;
  try {
    entries = await fsp.readdir(resultsDir, { withFileTypes: true });
  } catch { return out; }

  for (const e of entries) {
    if (!e.isDirectory() || !/^eval_\d+$/.test(e.name)) continue;
    out.evals++;
    if (exists(path.join(resultsDir, e.name, 'FAILED.txt'))) out.failed++;
  }
  const best = await readJsonOr(path.join(resultsDir, 'best_so_far.json'));
  if (best) {
    out.best = {
      objective: best.objective, LD: best.LD,
      eval_folder: best.eval_folder, eval_id: best.eval_id,
    };
  }
  return out;
}

/**
 * Reread study.json and recompute everything derivable from disk.
 *
 * `frozen` is COMPUTED, not just stored: a study is frozen once RESULTS holds at
 * least one eval_* folder. That way the flag can never drift away from what is
 * actually on disk.
 */
async function refreshMeta(projectSlug, studySlug) {
  const p = studyPaths(projectSlug, studySlug);
  const meta = (await readJsonOr(p.metaPath, {})) || {};

  meta.name = meta.name || studySlug;
  meta.slug = studySlug;
  meta.project = projectSlug;

  const res = await scanResults(p.results);
  const cfg = await readJsonOr(p.config);
  const rep = await readJsonOr(p.report);

  meta.frozen = res.evals > 0;
  meta.has_config = !!cfg;
  meta.has_report = !!rep;

  const c = (cfg && cfg.constants) || {};
  const r = (rep && rep.result) || {};
  let AR = null;
  if (c.S_FIXED && c.B_FULL) AR = Math.round((c.B_FULL ** 2 / c.S_FIXED) * 1000) / 1000;

  meta.summary = {
    S_m2: c.S_FIXED ?? null, b_m: c.B_FULL ?? null, AR,
    V_mps: c.V_DESIGN ?? null, CL: c.CL_TARGET ?? null,
    airfoil: c.AIRFOIL_NAME ?? null,
    reference_mode: c.REFERENCE_MODE ?? null,
    stability_mode: c.STABILITY_MODE ?? null,
    mtow_kg: r.mtow_kg ?? null,
    payload_fraction: r.payload_fraction ?? null,
    v_stall_mps: r.v_stall_mps ?? null,
    LD_est: r.LD_est ?? null,
    wing_loading_kgm2: r.wing_loading_kgm2 ?? null,
    evals: res.evals, failed: res.failed, best: res.best,
  };

  const VALID = ['new', 'sized', 'running', 'done', 'stopped', 'failed'];
  if (!VALID.includes(meta.status)) {
    meta.status = res.evals > 0 ? 'stopped' : (rep ? 'sized' : 'new');
  }
  // A "running" study whose process is gone was interrupted
  if (meta.status === 'running' && !child) meta.status = 'stopped';

  await writeJson(p.metaPath, meta);
  return meta;
}

async function listProjects() {
  let entries;
  try {
    entries = await fsp.readdir(STUDIES_DIR, { withFileTypes: true });
  } catch { return []; }

  const projects = [];
  for (const e of entries.sort((a, b) => a.name.localeCompare(b.name))) {
    if (!e.isDirectory() || e.name.startsWith('.') || e.name.startsWith('__')) continue;
    const pdir = path.join(STUDIES_DIR, e.name);
    const pmeta = await readJsonOr(path.join(pdir, 'project.json'));
    if (!pmeta) continue;

    const studies = [];
    let sub;
    try { sub = await fsp.readdir(pdir, { withFileTypes: true }); } catch { sub = []; }
    for (const s of sub.sort((a, b) => b.name.localeCompare(a.name))) {  // newest first
      if (!s.isDirectory()) continue;
      if (!exists(path.join(pdir, s.name, 'study.json'))) continue;
      studies.push(await refreshMeta(e.name, s.name));
    }
    projects.push({
      slug: e.name, name: pmeta.name || e.name,
      note: pmeta.note || '', created_at: pmeta.created_at || null,
      studies,
    });
  }
  return projects;
}

function attach(projectSlug, studySlug, meta) {
  const p = studyPaths(projectSlug, studySlug);
  active = { ...p, meta, frozen: !!meta.frozen };
  return active;
}

async function setActive(projectSlug, studySlug) {
  const p = studyPaths(projectSlug, studySlug);
  if (!exists(p.metaPath)) throw new Error(`немає дослідження ${projectSlug}/${studySlug}`);
  const meta = await refreshMeta(projectSlug, studySlug);
  attach(projectSlug, studySlug, meta);
  const idx = (await readJsonOr(INDEX_PATH, {})) || {};
  idx.active = { project: projectSlug, study: studySlug };
  idx.updated_at = nowStamp();
  await writeJson(INDEX_PATH, idx);
  return active;
}

/** Restore the last opened study; if it is gone, pick the newest available. */
async function restoreActive() {
  const idx = (await readJsonOr(INDEX_PATH, {})) || {};
  const a = idx.active;
  if (a && a.project && a.study && exists(studyPaths(a.project, a.study).metaPath)) {
    return setActive(a.project, a.study);
  }
  const projects = await listProjects();
  for (const p of projects) {
    if (p.studies.length) return setActive(p.slug, p.studies[0].slug);
  }
  active = null;
  return null;
}

async function ensureActive() {
  if (active) {
    // keep frozen/status fresh - RESULTS may have grown since last look
    active.meta = await refreshMeta(active.project, active.study);
    active.frozen = !!active.meta.frozen;
    return active;
  }
  return restoreActive();
}

const NO_STUDY = {
  ok: false,
  error: 'Не вибрано дослідження. Створіть або відкрийте його на вкладці «Дослідження».',
  noStudy: true,
};

const frozenError = (what) => ({
  ok: false,
  frozen: true,
  error: `Дослідження вже прораховане, тому ${what} заблоковано. ` +
         'Щоб змінити параметри — клонуйте дослідження.',
});

function pointConfigAtStudy(config, resultsDir) {
  const cfg = { ...(config || {}) };
  cfg.constants = { ...(cfg.constants || {}) };
  cfg.constants.RESULTS_ROOT = path.resolve(resultsDir);
  return cfg;
}

async function createProject(name, note = '') {
  await fsp.mkdir(STUDIES_DIR, { recursive: true });
  const slug = uniqueSlug(STUDIES_DIR, slugify(name, 'proiekt'));
  const dir = path.join(STUDIES_DIR, slug);
  await fsp.mkdir(dir, { recursive: true });
  const meta = { name: name || slug, slug, note, created_at: nowStamp() };
  await writeJson(path.join(dir, 'project.json'), meta);
  return meta;
}

async function createStudy(projectSlug, name, opts = {}) {
  const pdir = path.join(STUDIES_DIR, projectSlug);
  if (!exists(pdir)) throw new Error(`немає проєкту ${projectSlug}`);

  const base = (name && name.trim()) || 'studiia';
  const slug = uniqueSlug(pdir, `${todayStamp()}_${slugify(base, 'studiia')}`);
  const p = studyPaths(projectSlug, slug);
  await fsp.mkdir(p.results, { recursive: true });

  const srcReq = opts.fromRequirements || TPL_REQUIREMENTS;
  const srcCfg = opts.fromConfig || TPL_CONFIG;
  if (exists(srcReq)) await fsp.copyFile(srcReq, p.requirements);
  if (exists(srcCfg)) {
    await writeJson(p.config, pointConfigAtStudy(await readJsonOr(srcCfg, {}), p.results));
  }

  await writeJson(p.metaPath, {
    name: base, slug, project: projectSlug,
    parent: opts.parent || null,
    status: 'new', frozen: false,
    created_at: nowStamp(),
    sized_at: null, run_started_at: null, run_finished_at: null,
    note: opts.note || '',
  });
  return refreshMeta(projectSlug, slug);
}

// ------------------------------------------------------------------
// Python interpreter detection
// ------------------------------------------------------------------
const PY_CANDIDATES = [
  { cmd: 'py', args: ['-3'], label: 'py -3' },
  { cmd: 'python', args: [], label: 'python' },
  { cmd: 'python3', args: [], label: 'python3' },
];

const PROBE = [
  'import sys, aerosandbox, scipy, numpy',
  'print("PYOK", sys.version.split()[0], aerosandbox.__version__, scipy.__version__, numpy.__version__)',
].join('; ');

function probeOne(cand) {
  return new Promise((resolve) => {
    execFile(cand.cmd, [...cand.args, '-c', PROBE], { timeout: 60000, windowsHide: true },
      (err, stdout) => {
        const out = String(stdout || '');
        if (!err && out.includes('PYOK')) {
          const p = out.trim().split(/\s+/);
          resolve({ ...cand, python: p[1], aerosandbox: p[2], scipy: p[3], numpy: p[4] });
        } else {
          resolve(null);
        }
      });
  });
}

ipcMain.handle('python:detect', async () => {
  for (const cand of PY_CANDIDATES) {
    const hit = await probeOne(cand);
    if (hit) { pyCmd = hit; return { ok: true, ...hit }; }
  }
  return {
    ok: false,
    error: 'Не знайдено інтерпретатора Python зі встановленими aerosandbox / scipy / numpy. ' +
           'Перевірено: ' + PY_CANDIDATES.map((c) => c.label).join(', '),
  };
});

// ------------------------------------------------------------------
// Studies IPC
// ------------------------------------------------------------------
function activeSummary() {
  if (!active) return null;
  return {
    project: active.project, study: active.study,
    dir: active.dir, resultsDir: active.results,
    meta: active.meta, frozen: active.frozen,
  };
}

ipcMain.handle('studies:list', async () => {
  await ensureActive();
  return { ok: true, projects: await listProjects(), active: activeSummary() };
});

ipcMain.handle('studies:current', async () => {
  await ensureActive();
  return { ok: true, active: activeSummary() };
});

ipcMain.handle('studies:select', async (_e, { project, study }) => {
  if (child) return { ok: false, error: 'Спершу зупиніть розрахунок.' };
  try {
    await setActive(project, study);
    return { ok: true, active: activeSummary() };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

ipcMain.handle('studies:createProject', async (_e, { name, note }) => {
  try {
    const p = await createProject(name, note);
    return { ok: true, project: p };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

ipcMain.handle('studies:createStudy', async (_e, { project, name }) => {
  try {
    const meta = await createStudy(project, name);
    await setActive(project, meta.slug);
    return { ok: true, study: meta, active: activeSummary() };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

ipcMain.handle('studies:clone', async (_e, { project, study, name }) => {
  try {
    const src = studyPaths(project, study);
    if (!exists(src.metaPath)) throw new Error('дослідження не знайдено');
    const srcMeta = (await readJsonOr(src.metaPath, {})) || {};
    const meta = await createStudy(project, name || `${srcMeta.name || study} (клон)`, {
      fromRequirements: exists(src.requirements) ? src.requirements : null,
      fromConfig: exists(src.config) ? src.config : null,
      parent: study,
    });
    await setActive(project, meta.slug);
    return { ok: true, study: meta, active: activeSummary() };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

ipcMain.handle('studies:rename', async (_e, { project, study, name }) => {
  try {
    const p = studyPaths(project, study);
    const meta = (await readJsonOr(p.metaPath, {})) || {};
    meta.name = name;
    await writeJson(p.metaPath, meta);
    const fresh = await refreshMeta(project, study);
    if (active && active.project === project && active.study === study) {
      attach(project, study, fresh);
    }
    return { ok: true, study: fresh };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

ipcMain.handle('studies:delete', async (_e, { project, study }) => {
  if (child) return { ok: false, error: 'Спершу зупиніть розрахунок.' };
  const p = studyPaths(project, study);
  const meta = (await readJsonOr(p.metaPath, {})) || {};
  const res = await scanResults(p.results);

  const r = await dialog.showMessageBox(win, {
    type: 'warning',
    buttons: ['Скасувати', 'Видалити дослідження'],
    defaultId: 0, cancelId: 0,
    title: 'Видалити дослідження',
    message: `Видалити дослідження «${meta.name || study}»?`,
    detail: `Буде безповоротно видалено ${nEvals(res.evals)} разом із вхідними даними:\n${p.dir}`,
  });
  if (r.response !== 1) return { ok: false, canceled: true };

  await fsp.rm(p.dir, { recursive: true, force: true });
  if (active && active.project === project && active.study === study) {
    active = null;
    await restoreActive();
  }
  return { ok: true, active: activeSummary() };
});

/** Tail of the study's run.log, so a previous run's output survives a restart. */
ipcMain.handle('studies:readLog', async (_e, ref) => {
  const p = ref && ref.project && ref.study
    ? studyPaths(ref.project, ref.study)
    : (active || null);
  if (!p || !exists(p.log)) return { ok: true, log: '', bytes: 0 };
  try {
    const st = await fsp.stat(p.log);
    const MAX = 512 * 1024;                 // enough for the tail, cheap to send
    const start = Math.max(0, st.size - MAX);
    const fh = await fsp.open(p.log, 'r');
    try {
      const buf = Buffer.alloc(st.size - start);
      await fh.read(buf, 0, buf.length, start);
      let log = buf.toString('utf-8');
      if (start > 0) log = '… (початок логу обрізано)\n' + log.slice(log.indexOf('\n') + 1);
      return { ok: true, log, bytes: st.size, truncated: start > 0 };
    } finally {
      await fh.close();
    }
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

ipcMain.handle('studies:renameProject', async (_e, { project, name }) => {
  try {
    const f = path.join(STUDIES_DIR, project, 'project.json');
    if (!exists(f)) throw new Error('проєкт не знайдено');
    const meta = (await readJsonOr(f, {})) || {};
    meta.name = name;
    await writeJson(f, meta);
    return { ok: true, project: meta };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

ipcMain.handle('studies:deleteProject', async (_e, { project }) => {
  if (child) return { ok: false, error: 'Спершу зупиніть розрахунок.' };
  const pdir = path.join(STUDIES_DIR, project);
  if (!exists(pdir)) return { ok: false, error: 'Проєкт не знайдено.' };

  const pmeta = (await readJsonOr(path.join(pdir, 'project.json'), {})) || {};
  const projects = await listProjects();
  const p = projects.find((x) => x.slug === project);
  const nStudiesCount = p ? p.studies.length : 0;
  const nEvalsCount = p ? p.studies.reduce((n, s) => n + ((s.summary && s.summary.evals) || 0), 0) : 0;

  const r = await dialog.showMessageBox(win, {
    type: 'warning',
    buttons: ['Скасувати', nStudiesCount ? 'Видалити разом із дослідженнями' : 'Видалити проєкт'],
    defaultId: 0, cancelId: 0,
    title: 'Видалити проєкт',
    message: `Видалити проєкт «${pmeta.name || project}»?`,
    detail: nStudies
      ? `Разом із ним буде безповоротно видалено ${nStudies(nStudiesCount)} ` +
        `і ${nEvals(nEvalsCount)}:\n${pdir}`
      : `Проєкт порожній:\n${pdir}`,
  });
  if (r.response !== 1) return { ok: false, canceled: true };

  await fsp.rm(pdir, { recursive: true, force: true });
  if (active && active.project === project) {
    active = null;
    await restoreActive();
  }
  return { ok: true, active: activeSummary() };
});

ipcMain.handle('studies:openFolder', async (_e, { project, study } = {}) => {
  if (project && !study) {
    const err = await shell.openPath(path.join(STUDIES_DIR, project));
    return err ? { ok: false, error: err } : { ok: true };
  }
  const target = project && study ? studyPaths(project, study).dir
    : (active ? active.dir : STUDIES_DIR);
  const err = await shell.openPath(target);
  return err ? { ok: false, error: err } : { ok: true };
});

/** Best curves of several studies, for overlaying on one chart. */
ipcMain.handle('studies:compare', async (_e, list) => {
  const out = [];
  for (const { project, study } of (list || [])) {
    const p = studyPaths(project, study);
    const meta = await readJsonOr(p.metaPath, {});
    const best = await readJsonOr(path.join(p.results, 'best_so_far.json'));
    let curves = null;
    if (best && best.eval_folder) {
      curves = await readJsonOr(path.join(p.results, best.eval_folder, 'curves.json'));
    }
    out.push({
      project, study,
      name: (meta && meta.name) || study,
      summary: (meta && meta.summary) || null,
      best, curves,
    });
  }
  return { ok: true, studies: out };
});

// ------------------------------------------------------------------
// Schema
// ------------------------------------------------------------------
ipcMain.handle('schema:load', async () => {
  if (!exists(SCHEMA_PATH)) {
    return { ok: false, error: 'param_schema.json не знайдено. Виконайте: npm run schema' };
  }
  return { ok: true, schema: await readJson(SCHEMA_PATH) };
});

ipcMain.handle('schema:regen', async () => {
  if (!pyCmd) return { ok: false, error: 'Інтерпретатор Python не визначено.' };
  return new Promise((resolve) => {
    execFile(pyCmd.cmd, [...pyCmd.args, SCHEMA_TOOL],
      { cwd: ROOT, timeout: 300000, windowsHide: true,
        env: { ...process.env, PYTHONIOENCODING: 'utf-8' } },
      (err, stdout, stderr) => {
        if (err) resolve({ ok: false, error: String(stderr || err.message) });
        else resolve({ ok: true, log: String(stdout) });
      });
  });
});

// ------------------------------------------------------------------
// Config (per study)
// ------------------------------------------------------------------
ipcMain.handle('config:load', async () => {
  const a = await ensureActive();
  if (!a) return NO_STUDY;
  if (!exists(a.config)) return { ok: true, config: null, path: a.config, frozen: a.frozen };
  try {
    return { ok: true, config: await readJson(a.config), path: a.config, frozen: a.frozen };
  } catch (e) {
    return { ok: false, error: `wing_config.json пошкоджений: ${e.message}`, path: a.config };
  }
});

ipcMain.handle('config:save', async (_e, config) => {
  const a = await ensureActive();
  if (!a) return NO_STUDY;
  if (a.frozen) return frozenError('зміну параметрів');
  try {
    const withNote = {
      _comment: 'Знімок параметрів дослідження. Читається оптимізатором через WING_CONFIG.',
      _saved_at: new Date().toISOString(),
      ...config,
    };
    // RESULTS_ROOT always belongs to this study, whatever the form says
    await writeJson(a.config, pointConfigAtStudy(withNote, a.results));
    return { ok: true, path: a.config };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

ipcMain.handle('config:saveAs', async (_e, config) => {
  const r = await dialog.showSaveDialog(win, {
    title: 'Зберегти набір параметрів',
    defaultPath: path.join(active ? active.dir : PY_DIR, 'wing_config_preset.json'),
    filters: [{ name: 'JSON', extensions: ['json'] }],
  });
  if (r.canceled || !r.filePath) return { ok: false, canceled: true };
  await fsp.writeFile(r.filePath, JSON.stringify(config, null, 2), 'utf-8');
  return { ok: true, path: r.filePath };
});

ipcMain.handle('config:openFrom', async () => {
  const r = await dialog.showOpenDialog(win, {
    title: 'Завантажити набір параметрів',
    defaultPath: active ? active.dir : PY_DIR,
    properties: ['openFile'],
    filters: [{ name: 'JSON', extensions: ['json'] }],
  });
  if (r.canceled || !r.filePaths.length) return { ok: false, canceled: true };
  try {
    return { ok: true, config: await readJson(r.filePaths[0]), path: r.filePaths[0] };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

// ------------------------------------------------------------------
// Sizing - requirements -> design point (per study)
// ------------------------------------------------------------------
ipcMain.handle('sizing:load', async () => {
  const a = await ensureActive();
  if (!a) return NO_STUDY;
  if (!exists(a.requirements)) {
    return { ok: false, error: `У дослідженні немає requirements.json (${a.requirements})` };
  }
  try {
    // The `_`-prefixed keys are the file's own documentation - keep them.
    return {
      ok: true, requirements: await readJson(a.requirements),
      path: a.requirements, frozen: a.frozen,
      report: await readJsonOr(a.report),
    };
  } catch (e) {
    return { ok: false, error: `requirements.json пошкоджений: ${e.message}` };
  }
});

ipcMain.handle('sizing:save', async (_e, req) => {
  const a = await ensureActive();
  if (!a) return NO_STUDY;
  if (a.frozen) return frozenError('зміну вимог');
  try {
    await writeJson(a.requirements, req);
    return { ok: true, path: a.requirements };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

ipcMain.handle('sizing:run', async (_e, opts) => {
  const a = await ensureActive();
  if (!a) return NO_STUDY;
  if (!pyCmd) return { ok: false, error: 'Інтерпретатор Python не визначено.' };
  if (!exists(SIZING_SCRIPT)) return { ok: false, error: `Не знайдено ${SIZING_SCRIPT}` };
  if (child) return { ok: false, error: 'Спершу зупиніть розрахунок оптимізатора.' };
  const dryRun = !!(opts && opts.dryRun);
  if (a.frozen && !dryRun) return frozenError('перерахунок розміру');

  const args = [...pyCmd.args, SIZING_SCRIPT];
  if (dryRun) args.push('--dry-run');

  return new Promise((resolve) => {
    execFile(pyCmd.cmd, args,
      { cwd: ROOT, timeout: 120000, windowsHide: true, maxBuffer: 8 * 1024 * 1024,
        env: {
          ...process.env,
          PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1',
          WING_REQUIREMENTS: a.requirements,
          WING_CONFIG: a.config,
          WING_SIZING_REPORT: a.report,
          WING_RESULTS_ROOT: a.results,
        } },
      async (err, stdout, stderr) => {
        const log = String(stdout || '') + (stderr ? '\n' + String(stderr) : '');
        if (err && !stdout) {
          resolve({ ok: false, error: String(stderr || err.message), log });
          return;
        }
        let report = null;
        if (!dryRun) {
          report = await readJsonOr(a.report);
          const meta = (await readJsonOr(a.metaPath, {})) || {};
          meta.status = 'sized';
          meta.sized_at = nowStamp();
          await writeJson(a.metaPath, meta);
          active.meta = await refreshMeta(a.project, a.study);
        }
        resolve({ ok: true, log, report, dryRun, active: activeSummary() });
      });
  });
});

// ------------------------------------------------------------------
// Run / stop the optimizer (per study)
// ------------------------------------------------------------------
function stopChild() {
  if (!child) return;
  const pid = child.pid;
  child = null;
  if (process.platform === 'win32') {
    // scipy workers may spawn a whole process tree -> kill it all
    execFile('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true }, () => {});
  } else {
    try { process.kill(-pid, 'SIGKILL'); } catch { try { process.kill(pid, 'SIGKILL'); } catch {} }
  }
}

ipcMain.handle('run:start', async () => {
  const a = await ensureActive();
  if (!a) return NO_STUDY;
  if (child) return { ok: false, error: 'Розрахунок уже виконується.' };
  if (!pyCmd) return { ok: false, error: 'Інтерпретатор Python не визначено.' };
  if (!exists(SCRIPT)) return { ok: false, error: `Не знайдено ${SCRIPT}` };
  if (!exists(a.config)) {
    return { ok: false, error: 'У дослідженні немає wing_config.json — спершу «Підібрати розмір».' };
  }
  if (a.frozen) {
    return {
      ok: false, frozen: true,
      error: 'Дослідження вже містить результати. Щоб рахувати з іншими ' +
             'параметрами — клонуйте його. Щоб перерахувати це — очистіть результати.',
    };
  }

  await fsp.mkdir(a.results, { recursive: true });

  child = spawn(pyCmd.cmd, [...pyCmd.args, '-u', SCRIPT], {
    cwd: PY_DIR,
    windowsHide: true,
    env: {
      ...process.env,
      PYTHONUNBUFFERED: '1',
      PYTHONIOENCODING: 'utf-8',
      WING_CONFIG: a.config,
      WING_REQUIREMENTS: a.requirements,
      WING_SIZING_REPORT: a.report,
      WING_RESULTS_ROOT: a.results,
    },
  });

  const startedAt = Date.now();
  const studyRef = { project: a.project, study: a.study, metaPath: a.metaPath };

  const meta0 = (await readJsonOr(a.metaPath, {})) || {};
  meta0.status = 'running';
  meta0.run_started_at = nowStamp();
  meta0.run_finished_at = null;
  await writeJson(a.metaPath, meta0);

  // Tee stdout into the study folder so the log survives closing the app
  let logStream = null;
  try {
    logStream = fs.createWriteStream(a.log, { flags: 'a' });
    logStream.write(`\n===== ${nowStamp()} запуск (${pyCmd.label}) =====\n`);
  } catch { logStream = null; }

  send('run:state', { running: true, pid: child.pid, startedAt });

  let tail = '';
  const pump = (buf, stream) => {
    const text = buf.toString('utf-8');
    if (logStream) { try { logStream.write(text); } catch {} }
    tail += text;
    const lines = tail.split(/\r?\n/);
    tail = lines.pop();
    for (const line of lines) send('run:log', { line, stream });
  };

  child.stdout.on('data', (b) => pump(b, 'out'));
  child.stderr.on('data', (b) => pump(b, 'err'));

  child.on('close', async (code, signal) => {
    if (tail.trim()) send('run:log', { line: tail, stream: 'out' });
    tail = '';
    child = null;
    if (logStream) {
      try { logStream.write(`===== ${nowStamp()} завершено, код ${code} =====\n`); logStream.end(); } catch {}
    }
    try {
      const meta = (await readJsonOr(studyRef.metaPath, {})) || {};
      meta.status = code === 0 ? 'done' : (signal ? 'stopped' : 'failed');
      meta.run_finished_at = nowStamp();
      await writeJson(studyRef.metaPath, meta);
      const fresh = await refreshMeta(studyRef.project, studyRef.study);
      if (active && active.project === studyRef.project && active.study === studyRef.study) {
        attach(studyRef.project, studyRef.study, fresh);
      }
    } catch {}
    send('run:state', {
      running: false, code, signal,
      elapsedSec: (Date.now() - startedAt) / 1000,
      active: activeSummary(),
    });
  });

  child.on('error', (err) => {
    send('run:log', { line: `[ui] помилка запуску: ${err.message}`, stream: 'err' });
    child = null;
    send('run:state', { running: false, code: -1 });
  });

  return {
    ok: true, pid: child.pid, cwd: PY_DIR, interpreter: pyCmd.label,
    study: `${a.project} / ${a.study}`, results: a.results,
  };
});

ipcMain.handle('run:stop', async () => {
  if (!child) return { ok: false, error: 'Нічого не виконується.' };
  stopChild();
  return { ok: true };
});

ipcMain.handle('run:status', async () => ({ running: !!child, pid: child ? child.pid : null }));

// ------------------------------------------------------------------
// Results (per study)
// ------------------------------------------------------------------
const PICK = [
  'eval_id', 'objective', 'LD', 'CL_at_alpha_star', 'CD_total', 'CD_profile',
  'CD_induced_used', 'e_from_liftdist_LL', 'alpha_star_deg', 'static_margin',
  'CMalpha_per_rad', 'Cm_at_alpha_star', 'Cn_beta_per_rad', 'Cl_beta_per_rad',
  'M_root_at_ycut_Nm', 'timestamp', 'AR', 'span_full_m', 'S_fixed_m2', 'V_mps',
];

ipcMain.handle('results:list', async () => {
  const a = await ensureActive();
  if (!a) return { ...NO_STUDY, rows: [], best: null, reference: null };
  const dir = a.results;
  if (!exists(dir)) return { ok: true, dir, rows: [], best: null, reference: null };

  let names;
  try {
    names = (await fsp.readdir(dir, { withFileTypes: true }))
      .filter((d) => d.isDirectory() && /^eval_\d+$/.test(d.name))
      .map((d) => d.name);
  } catch (e) {
    return { ok: false, error: e.message, dir };
  }

  const rows = [];
  const CHUNK = 64;
  for (let i = 0; i < names.length; i += CHUNK) {
    const batch = names.slice(i, i + CHUNK);
    const got = await Promise.all(batch.map(async (name) => {
      const evalDir = path.join(dir, name);
      const mFile = path.join(evalDir, 'metrics.json');
      const row = { folder: name, dir: evalDir, ok: false };
      try {
        const m = await readJson(mFile);
        row.ok = true;
        for (const k of PICK) if (k in m) row[k] = m[k];
        row.c_root_m = m?.geom?.c_root_m;
        row.c_tip_m = m?.geom?.c_tip_m;
        row.MAC_m = m?.geom?.MAC_m;
        row.x_max_m = m?.geom?.x_max_m;
        row.xcg_frac = m?.cg_solve?.xcg_frac_MAC;
        row.sigma_peak_Pa = m?.structure?.sigma_peak_Pa;
        row.w_tip_m = m?.structure?.w_tip_m;
        row.stab_ok = m?.stability_constraints?.ok;
        row.tags = [];
        if ((m.midspan_chord_penalty || 0) > 1e-6) row.tags.push('MIDCHORD');
        if ((m.xmax_penalty || 0) > 1e-6) row.tags.push('XMAX');
        if ((m.global_min_chord_penalty || 0) > 1e-6) row.tags.push('CMIN');
        if ((m.thin_chord_penalty || 0) > 1e-9) row.tags.push('THIN');
        if ((m.chord_increase_penalty || 0) > 1e-9) row.tags.push('MONO');
        if ((m.tip_taper_penalty || 0) > 1e-6) row.tags.push('TIPTAPER');
        if ((m.tip_twist_penalty || 0) > 1e-6) row.tags.push('TIPTW');
        if (m.cl_bins_bad) row.tags.push('CLBIN');
        if (m?.cg_solve?.clamped) row.tags.push('CGCLAMP');
        if (row.stab_ok === false) row.tags.push('STAB');
      } catch {
        try {
          row.failed = (await fsp.readFile(path.join(evalDir, 'FAILED.txt'), 'utf-8')).slice(0, 400);
        } catch { /* still being written */ }
        const m2 = /^eval_(\d+)$/.exec(name);
        row.eval_id = m2 ? parseInt(m2[1], 10) : null;
      }
      return row;
    }));
    rows.push(...got);
  }

  rows.sort((a2, b2) => (a2.eval_id ?? 0) - (b2.eval_id ?? 0));

  return {
    ok: true, dir, rows,
    best: await readJsonOr(path.join(dir, 'best_so_far.json')),
    reference: await readJsonOr(path.join(dir, 'reference_thresholds.json')),
    study: `${a.project} / ${a.meta.name || a.study}`,
  };
});

ipcMain.handle('results:detail', async (_e, evalDir) => {
  if (!evalDir || !exists(evalDir)) return { ok: false, error: 'Папку не знайдено.' };

  let metrics = null, failed = null, curves = null;
  try { metrics = await readJson(path.join(evalDir, 'metrics.json')); } catch {}
  try { failed = await fsp.readFile(path.join(evalDir, 'FAILED.txt'), 'utf-8'); } catch {}
  // curves.json holds the numbers the charts are drawn from - 2.5 KB instead of
  // 195 KB of pre-rendered PNG, and the renderer can scale/overlay them.
  try { curves = await readJson(path.join(evalDir, 'curves.json')); } catch {}

  let stl = null;
  const stlPath = path.join(evalDir, 'wing_watertight.stl');
  if (exists(stlPath)) {
    const st = await fsp.stat(stlPath);
    stl = { path: stlPath, sizeKB: Math.round(st.size / 1024) };
  }

  return { ok: true, dir: evalDir, metrics, failed, stl, curves };
});

ipcMain.handle('results:regenerate', async (_e, opts) => {
  const a = await ensureActive();
  if (!a) return NO_STUDY;
  if (!pyCmd) return { ok: false, error: 'Інтерпретатор Python не визначено.' };
  if (!exists(REGEN_SCRIPT)) return { ok: false, error: `Не знайдено ${REGEN_SCRIPT}` };

  const args = [...pyCmd.args, REGEN_SCRIPT];
  if (opts && opts.evalId != null) args.push('--eval', String(opts.evalId));
  if (opts && opts.plots) args.push('--plots');

  // Authoritative success check: the file has to actually be there afterwards.
  // Parsing "OK" out of the log is not enough - export_wing_stl() can return
  // silently without writing anything.
  const stlPath = opts && opts.dir ? path.join(opts.dir, 'wing_watertight.stl') : null;
  const before = stlPath && exists(stlPath) ? (await fsp.stat(stlPath)).mtimeMs : null;

  return new Promise((resolve) => {
    execFile(pyCmd.cmd, args,
      { cwd: PY_DIR, timeout: 180000, windowsHide: true, maxBuffer: 4 * 1024 * 1024,
        env: { ...process.env, PYTHONIOENCODING: 'utf-8', WING_CONFIG: a.config } },
      async (err, stdout, stderr) => {
        const log = String(stdout || '') + (stderr ? '\n' + String(stderr) : '');
        if (err && !stdout) {
          resolve({ ok: false, error: String(stderr || err.message), log });
          return;
        }
        if (!stlPath) { resolve({ ok: /^\s*OK\s/m.test(log), log }); return; }

        if (!exists(stlPath)) {
          resolve({ ok: false, error: 'STL не з\'явився у теці варіанта', log });
          return;
        }
        const st = await fsp.stat(stlPath);
        if (before !== null && st.mtimeMs <= before) {
          resolve({ ok: false, error: 'STL не перезаписано', log });
          return;
        }
        resolve({ ok: true, log, sizeKB: Math.round(st.size / 1024) });
      });
  });
});

ipcMain.handle('results:clear', async () => {
  const a = await ensureActive();
  if (!a) return NO_STUDY;
  if (child) return { ok: false, error: 'Спершу зупиніть розрахунок.' };
  if (!exists(a.results)) return { ok: true, removed: 0 };

  const res = await scanResults(a.results);
  const r = await dialog.showMessageBox(win, {
    type: 'warning',
    buttons: ['Скасувати', 'Видалити все'],
    defaultId: 0, cancelId: 0,
    title: 'Очистити результати дослідження',
    message: `Видалити ${nEvals(res.evals)} дослідження «${a.meta.name || a.study}»?`,
    detail: `Вхідні дані залишаться, буде очищено тільки:\n${a.results}\n\n` +
            'Після цього дослідження перестане бути замороженим і його можна ' +
            'буде прорахувати знову.',
  });
  if (r.response !== 1) return { ok: false, canceled: true };

  let removed = 0;
  for (const name of await fsp.readdir(a.results)) {
    await fsp.rm(path.join(a.results, name), { recursive: true, force: true });
    removed++;
  }
  active.meta = await refreshMeta(a.project, a.study);
  active.frozen = !!active.meta.frozen;
  return { ok: true, removed, active: activeSummary() };
});

// ------------------------------------------------------------------
// Shell / dialogs
// ------------------------------------------------------------------
ipcMain.handle('shell:open', async (_e, target) => {
  if (!target) return { ok: false };
  const err = await shell.openPath(target);
  return err ? { ok: false, error: err } : { ok: true };
});

ipcMain.handle('shell:reveal', async (_e, target) => {
  if (target) shell.showItemInFolder(target);
  return { ok: true };
});

ipcMain.handle('paths:info', async () => ({
  root: ROOT,
  pyDir: PY_DIR,
  script: SCRIPT,
  scriptExists: exists(SCRIPT),
  // scipy maps workers=-1 onto multiprocessing.Pool() with os.cpu_count()
  cpuCount: os.cpus().length,
  studiesDir: STUDIES_DIR,
  templateRequirements: TPL_REQUIREMENTS,
  templateConfig: TPL_CONFIG,
  activeStudy: activeSummary(),
  backupExists: exists(path.join(PY_DIR, 'NX-2_DE_Optimization.ORIGINAL.py')),
}));
