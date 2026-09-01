# -*- coding: utf-8 -*-
"""
regenerate.py — перевипуск STL і креслення для вже порахованих варіантів.

Сенс: під час пошуку STL не пишеться (EXPORT_STL = false),
бо на повний прогін це 2.6 ГБ і ~38 хвилин на файли, з яких ви подивитесь
одиниці. Геометрія повністю відновлюється з вектора змінних, збереженого в
metrics.json, тож STL можна отримати будь-коли й без повторного рахунку.

Використання:
    py -3 regenerate.py                 # найкращий варіант (best_so_far.json)
    py -3 regenerate.py --eval 126      # конкретний варіант
    py -3 regenerate.py --top 5         # 5 найкращих за цільовою функцією
    py -3 regenerate.py --all           # всі (обережно: місце на диску)
    py -3 regenerate.py --force         # писати навіть при розбіжності геометрії

ВАЖЛИВО. Геометрія залежить від wing_config.json. Якщо після прогону ви
перезапускали sizing.py або міняли площу/розмах, відновлена геометрія НЕ
відповідатиме тій, що була порахована. Скрипт це перевіряє сам: порівнює
відновлену площу, кореневу хорду, хорду законцівки й САХ із записаними в
metrics.json, і відмовляється писати при розбіжності.

PNG тут не відтворюються — рендер матплотлібом із проєкту прибраний, графіки
малює UI із curves.json.
"""

import os
import sys
import json
import glob
import argparse
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "NX-2_DE_Optimization.py")
CONFIG = os.path.join(HERE, "wing_config.json")

# Скільки допускаємо розбіжності при звірці геометрії
TOL_REL = 1e-6


def load_module():
    if not os.path.isfile(SCRIPT):
        sys.exit(f"Не знайдено {SCRIPT}")
    os.environ.setdefault("WING_CONFIG", CONFIG)
    spec = importlib.util.spec_from_file_location("nx2_de_opt", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nx2_de_opt"] = mod
    spec.loader.exec_module(mod)
    return mod


def results_dir(mod):
    r = mod.RESULTS_ROOT
    return r if os.path.isabs(r) else os.path.join(HERE, r)


def read_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def check_geometry(mod, geom, metrics):
    """
    Звірити відновлену геометрію із записаною. Повертає список розбіжностей.
    Це головний захист: якщо конфіг змінився, ми пишемо не те крило.
    """
    rec = metrics.get("geom", {}) or {}
    pairs = [
        ("площа",             float(geom["S_proj"]),      rec.get("S_proj_m2")),
        ("коренева хорда",    float(geom["chord"][0]),    rec.get("c_root_m")),
        ("хорда законцівки",  float(geom["chord"][-1]),   rec.get("c_tip_m")),
        ("САХ",               float(geom["MAC"]),         rec.get("MAC_m")),
    ]
    bad = []
    for name, got, want in pairs:
        if want is None:
            continue
        want = float(want)
        denom = max(abs(want), 1e-12)
        if abs(got - want) / denom > TOL_REL:
            bad.append(f"{name}: відновлено {got:.6f}, у metrics.json {want:.6f}")
    return bad


def regenerate_one(mod, eval_dir, force):
    name = os.path.basename(eval_dir)
    mpath = os.path.join(eval_dir, "metrics.json")
    if not os.path.isfile(mpath):
        return name, "немає metrics.json (варіант провалився?)", False

    try:
        metrics = read_json(mpath)
    except Exception as e:
        return name, f"metrics.json не читається: {e}", False

    design = metrics.get("design")
    if not isinstance(design, dict):
        return name, "у metrics.json немає вектора design", False

    try:
        geom = mod.build_geometry_from_design(design)
    except Exception as e:
        return name, f"геометрія не будується: {e}", False

    bad = check_geometry(mod, geom, metrics)
    if bad and not force:
        return name, "ГЕОМЕТРІЯ НЕ ЗБІГАЄТЬСЯ — " + "; ".join(bad), False

    made = []
    stl = os.path.join(eval_dir, mod.STL_PATH_NAME)
    failed_marker = os.path.join(eval_dir, "STL_FAILED.txt")

    # Прибрати старий маркер помилки, щоб не прийняти його за свіжий
    try:
        if os.path.isfile(failed_marker):
            os.remove(failed_marker)
    except Exception:
        pass

    before = os.path.getmtime(stl) if os.path.isfile(stl) else None

    # ВАЖЛИВО: export_wing_stl() на початку робить `if not EXPORT_STL: return`.
    # Під час пошуку експорт вимкнений — і та сама перевірка тихо блокувала
    # перевипуск. Тут експорт потрібен завжди, тож вмикаємо його примусово.
    mod.EXPORT_STL = True

    try:
        mod.export_wing_stl(eval_dir, geom, n_airfoil=mod.STL_N_AIRFOIL)
    except Exception as e:
        return name, f"експорт STL впав: {e}", False

    if os.path.isfile(failed_marker):
        try:
            why = open(failed_marker, encoding="utf-8").read().strip().replace("\n", " ")
        except Exception:
            why = "див. STL_FAILED.txt"
        return name, f"експорт STL не вдався: {why}", False

    # Не звітувати про успіх, не переконавшись, що файл справді з'явився
    if not os.path.isfile(stl):
        return name, "STL не створено (файлу немає після експорту)", False
    if before is not None and os.path.getmtime(stl) <= before:
        return name, "STL не перезаписано (файл не змінився)", False

    made.append(f"STL {os.path.getsize(stl) // 1024} КБ")

    # curves.json відновлюється з геометрії (крім розподілу підйомної сили,
    # який без повторного прогону VLM не відтворити — беремо збережений, якщо є)
    if not os.path.isfile(os.path.join(eval_dir, "curves.json")):
        try:
            zeros = [0.0] * len(geom["ys"])
            mod.save_curves(eval_dir, geom, geom["ys"], zeros, zeros)
            made.append("curves.json (без розподілу підйомної сили)")
        except Exception as e:
            made.append(f"curves.json не вдався: {e}")

    note = ", ".join(made)
    if bad and force:
        note += "  [!! записано попри розбіжність геометрії]"
    return name, note, True


def pick_targets(args, rdir):
    """Які папки обробляти."""
    all_dirs = sorted(glob.glob(os.path.join(rdir, "eval_*")))
    if not all_dirs:
        sys.exit(f"У {rdir} немає жодного eval_*")

    if args.all:
        return all_dirs

    if args.eval is not None:
        d = os.path.join(rdir, f"eval_{int(args.eval):05d}")
        if not os.path.isdir(d):
            sys.exit(f"Не знайдено {d}")
        return [d]

    if args.top:
        scored = []
        for d in all_dirs:
            try:
                m = read_json(os.path.join(d, "metrics.json"))
                obj = float(m.get("objective", float("inf")))
                if obj == obj:  # not NaN
                    scored.append((obj, d))
            except Exception:
                pass
        scored.sort(key=lambda t: t[0])
        return [d for _, d in scored[: int(args.top)]]

    # за замовчуванням — найкращий
    bpath = os.path.join(rdir, "best_so_far.json")
    if os.path.isfile(bpath):
        try:
            folder = read_json(bpath).get("eval_folder")
            if folder:
                d = os.path.join(rdir, folder)
                if os.path.isdir(d):
                    return [d]
        except Exception:
            pass
    sys.exit("Немає best_so_far.json — вкажіть --eval N, --top N або --all")


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--eval", type=int, help="номер варіанта")
    ap.add_argument("--top", type=int, help="N найкращих за цільовою функцією")
    ap.add_argument("--all", action="store_true", help="усі варіанти")
    ap.add_argument("--force", action="store_true",
                    help="писати навіть якщо геометрія не збігається з metrics.json")
    args = ap.parse_args()

    mod = load_module()
    rdir = results_dir(mod)
    if not os.path.isdir(rdir):
        sys.exit(f"Не знайдено папку результатів {rdir}")

    targets = pick_targets(args, rdir)

    print()
    print(f"  результати: {rdir}")
    print(f"  до обробки: {len(targets)}")
    print(f"  точка проєктування конфігу: S={mod.S_FIXED:.5f} м²  b={mod.B_FULL:.4f} м")
    print("  " + "-" * 62)

    ok = fail = 0
    for d in targets:
        name, note, good = regenerate_one(mod, d, args.force)
        mark = "OK  " if good else "ПРОП"
        print(f"  {mark} {name:14s} {note}")
        ok += good
        fail += (not good)

    print("  " + "-" * 62)
    print(f"  зроблено {ok}, пропущено {fail}")
    if fail:
        print()
        print("  Якщо причина — розбіжність геометрії, значить конфіг змінився після")
        print("  прогону (найчастіше через повторний запуск sizing.py). Або поверніть")
        print("  попередню точку проєктування, або перезапустіть прогін.")
    print()


if __name__ == "__main__":
    main()
