# -*- coding: utf-8 -*-
"""
studylib.py — спільна робота з проєктами та дослідженнями.

Структура на диску:

    05_STUDIES/
      index.json                  останнє відкрите дослідження
      <проєкт>/
        project.json
        <дослідження>/
          study.json              метадані та зведення
          requirements.json       знімок вхідних вимог
          wing_config.json        знімок; RESULTS_ROOT = абсолютний шлях на ./RESULTS
          sizing_report.json      вивід підбору
          RESULTS/                eval_*, best_so_far.json, reference_thresholds.json
          run.log                 вивід прогону

Цим модулем користується migrate_legacy.py. UI має власну реалізацію того самого
розкладу в 03_UI/main.js — щоб не платити запуском Python за кожен клік.
"""

import os
import re
import json
import time
import shutil
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

STUDIES_DIR = HERE
INDEX_PATH = os.path.join(HERE, "index.json")

# Шаблони, з яких народжується нове дослідження
TPL_REQUIREMENTS = os.path.join(ROOT, "04_SIZING", "requirements.json")
TPL_CONFIG = os.path.join(ROOT, "01_PYTHON", "wing_config.json")

STATUSES = ("new", "sized", "running", "done", "stopped", "failed")


# ============================================================
# ІМЕНА
# ============================================================
_TRANSLIT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'h', 'ґ': 'g', 'д': 'd', 'е': 'e',
    'є': 'ie', 'ж': 'zh', 'з': 'z', 'и': 'y', 'і': 'i', 'ї': 'i', 'й': 'i',
    'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r',
    'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch',
    'ш': 'sh', 'щ': 'shch', 'ь': '', 'ю': 'iu', 'я': 'ia', 'ы': 'y', 'э': 'e', 'ъ': '',
}


def slugify(name: str, fallback: str = "bez-nazvy") -> str:
    """Назва -> безпечне ім'я теки. Кирилиця транслітерується."""
    s = (name or "").strip().lower()
    out = []
    for ch in s:
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isalnum() and ch.isascii():
            out.append(ch)
        elif ch in " _-.":
            out.append("-")
        else:
            # решта (напр. інші алфавіти) — прибираємо діакритику й пробуємо ascii
            d = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode()
            out.append(d.lower() if d.isalnum() else "-")
    s = "".join(out)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    s = s[:60]
    return s or fallback


def unique_slug(parent_dir: str, slug: str) -> str:
    """Додати -2, -3 … якщо тека вже існує."""
    if not os.path.exists(os.path.join(parent_dir, slug)):
        return slug
    n = 2
    while os.path.exists(os.path.join(parent_dir, f"{slug}-{n}")):
        n += 1
    return f"{slug}-{n}"


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return time.strftime("%Y-%m-%d")


# ============================================================
# ЧИТАННЯ / ЗАПИС JSON
# ============================================================
def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ============================================================
# ШЛЯХИ ДОСЛІДЖЕННЯ
# ============================================================
def study_dir(project_slug: str, study_slug: str) -> str:
    return os.path.join(STUDIES_DIR, project_slug, study_slug)


def study_paths(project_slug: str, study_slug: str) -> dict:
    d = study_dir(project_slug, study_slug)
    return {
        "project": project_slug,
        "study": study_slug,
        "dir": d,
        "meta": os.path.join(d, "study.json"),
        "requirements": os.path.join(d, "requirements.json"),
        "config": os.path.join(d, "wing_config.json"),
        "report": os.path.join(d, "sizing_report.json"),
        "results": os.path.join(d, "RESULTS"),
        "log": os.path.join(d, "run.log"),
    }


# ============================================================
# ЗВЕДЕННЯ ПО РЕЗУЛЬТАТАХ
# ============================================================
def scan_results(results_dir: str) -> dict:
    """Порахувати оцінки та витягти найкращу. Дешево — без читання всіх metrics."""
    out = {"evals": 0, "failed": 0, "best": None}
    if not os.path.isdir(results_dir):
        return out

    try:
        names = os.listdir(results_dir)
    except OSError:
        return out

    for name in names:
        if not re.match(r"^eval_\d+$", name):
            continue
        out["evals"] += 1
        if os.path.isfile(os.path.join(results_dir, name, "FAILED.txt")):
            out["failed"] += 1

    best = read_json(os.path.join(results_dir, "best_so_far.json"))
    if isinstance(best, dict):
        out["best"] = {
            "objective": best.get("objective"),
            "LD": best.get("LD"),
            "eval_folder": best.get("eval_folder"),
            "eval_id": best.get("eval_id"),
        }
    return out


def summary_from_config(config: dict) -> dict:
    """Точка проєктування прямо з конфігу дослідження."""
    c = (config or {}).get("constants", {}) or {}
    s, b = c.get("S_FIXED"), c.get("B_FULL")
    ar = None
    try:
        if s and b:
            ar = round((float(b) ** 2) / float(s), 3)
    except Exception:
        pass
    return {
        "S_m2": s, "b_m": b, "AR": ar,
        "V_mps": c.get("V_DESIGN"), "CL": c.get("CL_TARGET"),
        "airfoil": c.get("AIRFOIL_NAME"),
        "reference_mode": c.get("REFERENCE_MODE"),
        "stability_mode": c.get("STABILITY_MODE"),
    }


def summary_from_report(report: dict) -> dict:
    """Маси та режим із повного звіту підбору."""
    r = (report or {}).get("result", {}) or {}
    keys = ("mtow_kg", "payload_fraction", "v_cruise_mps", "v_stall_mps",
            "LD_est", "wing_loading_kgm2", "power_elec_W", "c_root_m", "c_tip_m")
    return {k: r.get(k) for k in keys if r.get(k) is not None}


def refresh_meta(project_slug: str, study_slug: str) -> dict:
    """
    Перечитати study.json і перерахувати похідні поля з диска.

    frozen обчислюється, а не тільки зберігається: дослідження заморожене, якщо
    в RESULTS уже є хоч одна тека eval_*. Так метадані не можуть розійтися з
    тим, що насправді лежить на диску.
    """
    p = study_paths(project_slug, study_slug)
    meta = read_json(p["meta"], {}) or {}

    meta.setdefault("name", study_slug)
    meta["slug"] = study_slug
    meta["project"] = project_slug

    res = scan_results(p["results"])
    cfg = read_json(p["config"])
    rep = read_json(p["report"])

    meta["frozen"] = res["evals"] > 0
    meta["has_config"] = cfg is not None
    meta["has_report"] = rep is not None

    summary = {}
    summary.update(summary_from_config(cfg))
    summary.update(summary_from_report(rep))
    summary.update({k: res[k] for k in ("evals", "failed", "best")})
    meta["summary"] = summary

    if meta.get("status") not in STATUSES:
        if res["evals"] > 0:
            meta["status"] = "stopped"
        elif rep is not None:
            meta["status"] = "sized"
        else:
            meta["status"] = "new"

    write_json(p["meta"], meta)
    return meta


# ============================================================
# ПЕРЕЛІК
# ============================================================
def list_projects() -> list:
    if not os.path.isdir(STUDIES_DIR):
        return []
    out = []
    for name in sorted(os.listdir(STUDIES_DIR)):
        d = os.path.join(STUDIES_DIR, name)
        if not os.path.isdir(d) or name.startswith(".") or name.startswith("__"):
            continue
        pmeta = read_json(os.path.join(d, "project.json"))
        if pmeta is None:
            continue  # не проєкт (напр. __pycache__)
        studies = []
        for sname in sorted(os.listdir(d)):
            sd = os.path.join(d, sname)
            if not os.path.isdir(sd):
                continue
            if not os.path.isfile(os.path.join(sd, "study.json")):
                continue
            studies.append(refresh_meta(name, sname))
        out.append({
            "slug": name,
            "name": pmeta.get("name", name),
            "note": pmeta.get("note", ""),
            "created_at": pmeta.get("created_at"),
            "studies": studies,
        })
    return out


def get_index() -> dict:
    return read_json(INDEX_PATH, {}) or {}


def set_active(project_slug: str, study_slug: str):
    idx = get_index()
    idx["active"] = {"project": project_slug, "study": study_slug}
    idx["updated_at"] = now()
    write_json(INDEX_PATH, idx)


def get_active() -> dict | None:
    a = (get_index() or {}).get("active")
    if not isinstance(a, dict):
        return None
    p, s = a.get("project"), a.get("study")
    if not p or not s:
        return None
    if not os.path.isfile(study_paths(p, s)["meta"]):
        return None
    return {"project": p, "study": s}


# ============================================================
# СТВОРЕННЯ
# ============================================================
def create_project(name: str, note: str = "") -> dict:
    slug = unique_slug(STUDIES_DIR, slugify(name, "proiekt"))
    d = os.path.join(STUDIES_DIR, slug)
    os.makedirs(d, exist_ok=True)
    meta = {"name": name or slug, "slug": slug, "note": note, "created_at": now()}
    write_json(os.path.join(d, "project.json"), meta)
    return meta


def _point_config_at_study(config: dict, results_dir: str) -> dict:
    """Прописати в конфігу абсолютний шлях на RESULTS цього дослідження."""
    cfg = dict(config or {})
    cfg.setdefault("constants", {})
    cfg["constants"]["RESULTS_ROOT"] = os.path.abspath(results_dir)
    return cfg


def create_study(project_slug: str, name: str,
                 from_requirements: str = None,
                 from_config: str = None,
                 parent: str = None) -> dict:
    """
    Нове дослідження. Вхідні дані беруться з переданих файлів, інакше — із
    шаблонів (04_SIZING/requirements.json, 01_PYTHON/wing_config.json).
    """
    pdir = os.path.join(STUDIES_DIR, project_slug)
    if not os.path.isdir(pdir):
        raise FileNotFoundError(f"немає проєкту {project_slug}")

    base = name.strip() if name and name.strip() else "doslidzhennia"
    slug = unique_slug(pdir, f"{today()}_{slugify(base, 'doslidzhennia')}")
    p = study_paths(project_slug, slug)
    os.makedirs(p["dir"], exist_ok=True)
    os.makedirs(p["results"], exist_ok=True)

    src_req = from_requirements or TPL_REQUIREMENTS
    src_cfg = from_config or TPL_CONFIG

    if os.path.isfile(src_req):
        shutil.copy2(src_req, p["requirements"])
    if os.path.isfile(src_cfg):
        write_json(p["config"], _point_config_at_study(read_json(src_cfg, {}), p["results"]))

    meta = {
        "name": base,
        "slug": slug,
        "project": project_slug,
        "parent": parent,
        "status": "new",
        "frozen": False,
        "created_at": now(),
        "sized_at": None,
        "run_started_at": None,
        "run_finished_at": None,
        "note": "",
    }
    write_json(p["meta"], meta)
    return refresh_meta(project_slug, slug)


def clone_study(project_slug: str, study_slug: str, new_name: str = None) -> dict:
    """
    Клон вхідних даних у нове дослідження. RESULTS НЕ копіюються — саме тому
    клон і потрібен: змінити параметри, не зачепивши прорахований результат.
    """
    src = study_paths(project_slug, study_slug)
    if not os.path.isfile(src["meta"]):
        raise FileNotFoundError(f"немає дослідження {project_slug}/{study_slug}")

    src_meta = read_json(src["meta"], {}) or {}
    name = new_name or f"{src_meta.get('name', study_slug)} (клон)"

    return create_study(
        project_slug, name,
        from_requirements=src["requirements"] if os.path.isfile(src["requirements"]) else None,
        from_config=src["config"] if os.path.isfile(src["config"]) else None,
        parent=study_slug,
    )


def set_status(project_slug: str, study_slug: str, status: str, **stamps):
    p = study_paths(project_slug, study_slug)
    meta = read_json(p["meta"], {}) or {}
    if status in STATUSES:
        meta["status"] = status
    for k, v in stamps.items():
        meta[k] = v
    write_json(p["meta"], meta)
    return refresh_meta(project_slug, study_slug)


def rename_study(project_slug: str, study_slug: str, new_name: str):
    p = study_paths(project_slug, study_slug)
    meta = read_json(p["meta"], {}) or {}
    meta["name"] = new_name
    write_json(p["meta"], meta)
    return refresh_meta(project_slug, study_slug)


def delete_study(project_slug: str, study_slug: str):
    d = study_dir(project_slug, study_slug)
    if os.path.isdir(d):
        shutil.rmtree(d)
    a = get_active()
    if a and a["project"] == project_slug and a["study"] == study_slug:
        idx = get_index()
        idx.pop("active", None)
        write_json(INDEX_PATH, idx)
