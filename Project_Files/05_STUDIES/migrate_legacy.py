# -*- coding: utf-8 -*-
"""
migrate_legacy.py — перенести наявні результати у перше дослідження.

До появи досліджень усе було глобальним: одні requirements.json, один
wing_config.json, одна тека 01_PYTHON/RESULTS. Цей скрипт складає з них перше
дослідження, щоб прорахований результат не загубився.

Що робить:
  1. створює проєкт і дослідження
  2. копіює requirements.json, wing_config.json, sizing_report.json
  3. ПЕРЕНОСИТЬ 01_PYTHON/RESULTS у теку дослідження (os.rename — миттєво)
  4. прописує в конфігу дослідження абсолютний RESULTS_ROOT
  5. звіряє кількість тек до і після; при розбіжності відкочує перенесення

Нічого не видаляє. Шаблони (04_SIZING/requirements.json, 01_PYTHON/wing_config.json)
залишаються на місці — з них народжуються нові дослідження.

Запуск:
    py -3 05_STUDIES/migrate_legacy.py
    py -3 05_STUDIES/migrate_legacy.py --dry-run
    py -3 05_STUDIES/migrate_legacy.py --project "Носитель 3 кг" --study "AR 8, катапульта"
"""

import os
import re
import sys
import json
import shutil
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import studylib as S

LEGACY_RESULTS = os.path.join(S.ROOT, "01_PYTHON", "RESULTS")
LEGACY_REQ = os.path.join(S.ROOT, "04_SIZING", "requirements.json")
LEGACY_CFG = os.path.join(S.ROOT, "01_PYTHON", "wing_config.json")
LEGACY_REPORT = os.path.join(S.ROOT, "04_SIZING", "sizing_report.json")

# Порожня тека, що могла лишитися від запусків із іншого каталогу
STRAY_RESULTS = os.path.join(S.ROOT, "RESULTS")


def count_evals(d):
    if not os.path.isdir(d):
        return 0
    try:
        return sum(1 for n in os.listdir(d) if re.match(r"^eval_\d+$", n))
    except OSError:
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="Носитель 3 кг")
    ap.add_argument("--study", default="Перенесено з RESULTS")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    before = count_evals(LEGACY_RESULTS)

    print()
    print("=" * 64)
    print("  МІГРАЦІЯ У ДОСЛІДЖЕННЯ")
    print("=" * 64)
    print(f"  джерело     {LEGACY_RESULTS}")
    print(f"  оцінок      {before}")
    for label, p in (("requirements", LEGACY_REQ), ("wing_config", LEGACY_CFG),
                     ("sizing_report", LEGACY_REPORT)):
        print(f"  {label:13s} {'є' if os.path.isfile(p) else 'немає'}")

    if before == 0 and not os.path.isfile(LEGACY_CFG):
        print()
        print("  Переносити нічого. Вихід.")
        return

    if args.dry_run:
        print()
        print("  [DRY RUN] нічого не змінено")
        return

    # --- проєкт ---
    projects = S.list_projects()
    proj = next((p for p in projects if p["name"] == args.project), None)
    if proj is None:
        proj = S.create_project(args.project, note="Створено міграцією зі старої структури")
        print(f"\n  проєкт створено: {proj['name']}  ({proj['slug']})")
    else:
        print(f"\n  проєкт уже є: {proj['name']}  ({proj['slug']})")

    # --- дослідження ---
    meta = S.create_study(
        proj["slug"], args.study,
        from_requirements=LEGACY_REQ if os.path.isfile(LEGACY_REQ) else None,
        from_config=LEGACY_CFG if os.path.isfile(LEGACY_CFG) else None,
    )
    p = S.study_paths(proj["slug"], meta["slug"])
    print(f"  дослідження створено: {meta['name']}  ({meta['slug']})")

    if os.path.isfile(LEGACY_REPORT):
        shutil.copy2(LEGACY_REPORT, p["report"])
        print("  sizing_report.json скопійовано")

    # --- перенесення результатів ---
    if before > 0:
        # create_study уже зробив порожню RESULTS — приберемо, щоб rename спрацював
        try:
            if os.path.isdir(p["results"]) and not os.listdir(p["results"]):
                os.rmdir(p["results"])
        except OSError:
            pass

        moved = False
        if not os.path.exists(p["results"]):
            try:
                os.rename(LEGACY_RESULTS, p["results"])
                moved = True
                print("  RESULTS перенесено (os.rename)")
            except OSError as e:
                print(f"  rename не вдався ({e}); копіюю…")

        if not moved:
            shutil.copytree(LEGACY_RESULTS, p["results"], dirs_exist_ok=True)
            print("  RESULTS скопійовано")

        after = count_evals(p["results"])
        print(f"  оцінок на новому місці: {after}")

        if after != before:
            print()
            print(f"  !! РОЗБІЖНІСТЬ: було {before}, стало {after}")
            if moved:
                try:
                    os.rename(p["results"], LEGACY_RESULTS)
                    print("  перенесення відкочено, старе місце відновлено")
                except OSError as e:
                    print(f"  ВІДКОТИТИ НЕ ВДАЛОСЯ ({e}) — перевірте обидві теки вручну")
            print("  дослідження лишилось створеним, але без результатів")
            sys.exit(1)

    os.makedirs(p["results"], exist_ok=True)

    # --- конфіг має вказувати на RESULTS дослідження ---
    cfg = S.read_json(p["config"], {}) or {}
    cfg.setdefault("constants", {})
    old_root = cfg["constants"].get("RESULTS_ROOT")
    cfg["constants"]["RESULTS_ROOT"] = os.path.abspath(p["results"])
    cfg["_migrated_from"] = os.path.abspath(LEGACY_RESULTS)
    S.write_json(p["config"], cfg)
    print(f"  RESULTS_ROOT: {old_root!r} -> {cfg['constants']['RESULTS_ROOT']}")

    # --- метадані ---
    S.set_status(proj["slug"], meta["slug"], "stopped",
                 note="Прогін перервано збоєм файлового лока (виправлено). "
                      "Перенесено зі старої структури.")
    meta = S.refresh_meta(proj["slug"], meta["slug"])
    S.set_active(proj["slug"], meta["slug"])

    # --- прибрати порожню пустушку в корені, якщо є ---
    if os.path.isdir(STRAY_RESULTS) and count_evals(STRAY_RESULTS) == 0:
        try:
            shutil.rmtree(STRAY_RESULTS)
            print("  прибрано порожню Project_Files/RESULTS")
        except OSError:
            pass

    # --- звіт ---
    s = meta["summary"]
    print()
    print("  ЗВЕДЕННЯ ДОСЛІДЖЕННЯ")
    print("  " + "-" * 60)
    print(f"  оцінок / впало     {s.get('evals')} / {s.get('failed')}")
    if s.get("best"):
        b = s["best"]
        print(f"  найкраща           {b.get('eval_folder')}  "
              f"цільова {b.get('objective')}  L/D {b.get('LD')}")
    print(f"  площа / розмах     {s.get('S_m2')} м²  /  {s.get('b_m')} м   AR {s.get('AR')}")
    print(f"  швидкість / CL     {s.get('V_mps')} м/с  /  {s.get('CL')}")
    if s.get("mtow_kg"):
        print(f"  злітна маса        {s.get('mtow_kg')} кг")
    print()
    print(f"  тека дослідження: {p['dir']}")
    print(f"  активне дослідження записано в {S.INDEX_PATH}")
    print()
    print("  Старе місце (01_PYTHON/RESULTS) тепер порожнє або відсутнє — так і має бути.")
    print()


if __name__ == "__main__":
    main()
