# -*- coding: utf-8 -*-
"""
gen_schema.py

Single source of truth for the UI parameter form.

Imports NX-2_DE_Optimization.py, reads the CURRENT value of every exposed
constant straight out of the module, merges it with the metadata table below
(group / label / unit / hint) and writes:

    03_UI/param_schema.json     -> form definition + defaults (read by Electron)
    01_PYTHON/wing_config.json  -> live config (only if it does not exist yet)

Run it again after adding a constant to the metadata table.

Usage:
    py -3 03_UI/tools/gen_schema.py
"""

import os
import sys
import json
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(UI_DIR)
PY_DIR = os.path.join(ROOT, "01_PYTHON")
SCRIPT = os.path.join(PY_DIR, "NX-2_DE_Optimization.py")

SCHEMA_OUT = os.path.join(UI_DIR, "param_schema.json")
CONFIG_OUT = os.path.join(PY_DIR, "wing_config.json")


# ============================================================
# GROUPS (order = tab order in the UI)
# ============================================================
GROUPS = [
    {
        "id": "mode",
        "title": "Режим роботи",
        "hint": "Головний вибір: переробляти обміряне крило NX-2 чи проєктувати нове з вимог. Цей блок зазвичай виставляє 04_SIZING/sizing.py.",
    },
    {
        "id": "requirements",
        "title": "Вимоги та точка проєктування",
        "hint": "Головні числа. Саме вони визначають, яку масу здатне нести крило.",
    },
    {
        "id": "geom_limits",
        "title": "Габаритні обмеження",
        "hint": "Жорсткі межі компонування. Усі в метрах — при зміні розміру крила їх треба масштабувати разом із ним.",
    },
    {
        "id": "stability",
        "title": "Стійкість і центрівка",
        "hint": "Центрівка не входить у геном — вона підбирається всередині кожної оцінки під заданий запас стійкості.",
    },
    {
        "id": "structure",
        "title": "Структура (проксі-модель)",
        "hint": "УВАГА: це не розрахунок лонжерона. Жорсткість EI постулюється константою, а не рахується з перерізу. Числа придатні лише для порівняння варіантів між собою.",
    },
    {
        "id": "objective",
        "title": "Цільова функція",
        "hint": "Ваги термів. W_LD домінує — за замовчуванням L/D дає ~90% значення цільової функції.",
    },
    {
        "id": "penalties",
        "title": "Штрафи та обмеження",
        "hint": "Жорсткі обмеження реалізовані як величезні штрафи (2e8), а не як справжні constraints.",
    },
    {
        "id": "mesh",
        "title": "Сітка VLM",
        "hint": "Прямо впливає на час однієї оцінки. Груба сітка використовується для похідних, основна — для фінального розрахунку.",
    },
    {
        "id": "output",
        "title": "Вивід",
        "hint": "Куда писати результати і що експортувати.",
    },
    {
        "id": "runtime",
        "title": "Differential Evolution",
        "hint": "Розмір популяції × кількість ітерацій × 16 змінних = кількість оцінок. workers=-1 задіює всі ядра.",
    },
]


# ============================================================
# PARAMETER METADATA
#   (name, group, label, unit, hint)
# unit "" = dimensionless
# ============================================================
PARAMS = [
    # ---- mode --------------------------------------------------------
    ("REFERENCE_MODE", "mode", "Звідки брати стартову геометрію", "",
     "nx2 = обмір крила NX-2 (як було). requirements = аналітична трапеція з площі й розмаху."),
    ("FIX_ROOT_CHORD", "mode", "Фіксувати кореневу хорду", "",
     "Увімкнено = коренева хорда успадковується від еталона і не змінюється. Вимкніть, щоб вона стала результатом оптимізації."),
    ("STABILITY_MODE", "mode", "Планка стійкості", "",
     "reference = «не гірше за NX-2» (а він шляхово нестійкий, тож вимога пуста). absolute = ваші власні числа нижче."),
    ("CNBETA_MIN_REQ", "mode", "Мінімальний Cn_beta", "1/рад",
     "Шляхова стійкість. Діє лише при STABILITY_MODE = absolute. 0 = нейтрально, більше 0 = стійко."),
    ("CLBETA_MAX_REQ", "mode", "Максимальний Cl_beta", "1/рад",
     "Поперечна стійкість, має бути від'ємним. Діє лише при STABILITY_MODE = absolute."),
    ("INIT_TAPER_RATIO", "mode", "Стартове звуження", "",
     "Хорда законцівки / коренева, для стартової трапеції."),
    ("INIT_SWEEP_LE_DEG", "mode", "Стартова стрілоподібність", "°", ""),
    ("INIT_ROOT_TWIST_DEG", "mode", "Стартова крутка в корені", "°", ""),
    ("INIT_TIP_TWIST_DEG", "mode", "Стартова крутка законцівки", "°",
     "Від'ємна = washout, законцівка розвантажена."),
    ("INIT_DIHEDRAL_DEG", "mode", "Стартовий двогранний кут", "°", ""),

    # ---- requirements ------------------------------------------------
    ("S_FIXED",        "requirements", "Площа крила (обидві половини)", "м²",
     "Фіксована. Разом зі швидкістю й CL визначає підйомну силу."),
    ("B_FULL",         "requirements", "Повний розмах", "м",
     "Фіксований. Подовження AR = B_FULL² / S_FIXED рахується автоматично."),
    ("CL_TARGET",      "requirements", "Крейсерський CL", "",
     "Кут атаки підбирається так, щоб вийшов саме цей CL."),
    ("V_DESIGN",       "requirements", "Крейсерська швидкість", "м/с",
     "Розрахункова швидкість. Підйомна сила ~ V²."),
    ("ALTITUDE_M",     "requirements", "Висота", "м",
     "Використовується для щільності повітря та числа Рейнольдса."),
    ("AIRFOIL_NAME",   "requirements", "Профіль", "",
     "Назва профілю з бази AeroSandbox / UIUC (напр. s5020)."),
    ("Y_CUT",          "requirements", "Межа зони фюзеляжу", "м",
     "Внутрішня частина напіврозмаху виключається зі структури, цільової функції та графіків."),

    # ---- geometric limits --------------------------------------------
    ("Y_MID_CONSTRAINT", "geom_limits", "Станція контролю хорди", "м",
     "У цій точці розмаху перевіряється мінімальна хорда."),
    ("C_MID_MIN",        "geom_limits", "Мін. хорда в цій станції", "м",
     "Жорстке обмеження. Порушення = штраф 2e8."),
    ("X_MAX_LIMIT",      "geom_limits", "Макс. габарит по X", "м",
     "Оцінюється консервативно як max(x_передньої_кромки + хорда)."),
    ("C_GLOBAL_MIN",     "geom_limits", "Мін. хорда будь-де", "м",
     "Жорстке обмеження на всю поверхню крила."),
    ("C_ROOT_MAX_FRAC",  "geom_limits", "Макс. коренева хорда", "× середньої хорди",
     "Діє лише коли коренева хорда вільна. Без цієї межі вона роздувалась до метрів і валила VLM: 290 із 1757 оцінок падало. Фактична стеля = min(це × S/b, X_MAX_LIMIT)."),
    ("C_ROOT_MIN_FRAC",  "geom_limits", "Мін. коренева хорда", "× середньої хорди",
     "Нижня межа кореневої хорди. 1.0 = прямокутне крило."),
    ("W_AREA_ERR",       "penalties",   "Вага похибки площі", "",
     "М'який штраф, коли межа кореневої хорди не дала витримати задану площу. Дає DE градієнт замість обриву."),
    ("C_ABS_MIN_EFF",    "geom_limits", "Бажана мін. хорда (м'яка)", "м",
     "М'який штраф, застосовується лише до внутрішніх 90% напіврозмаху."),
    ("C_ABS_HARD_MIN",   "geom_limits", "Аварійний мінімум хорди", "м",
     "Тільки щоб VLM не зламався на нульовій хорді. Не проєктне обмеження."),
    ("C_MUL_HARD_MIN",   "geom_limits", "Мін. множник хорди", "",
     "Нижня межа для форми хорди у відносних одиницях."),
    ("Y_TAPER_CHECK_FRAC",  "geom_limits", "Точка перевірки звуження", "частка напіврозмаху",
     "Де перевіряти, що хорда не вийшла на «плато» біля законцівки."),
    ("TIP_TAPER_RATIO_MIN", "geom_limits", "Мін. відношення звуження", "",
     "Вимагає c(0.85·напіврозмах) >= це × c(законцівка)."),
    ("TIP_TWIST_MAX_DEG",   "geom_limits", "Макс. крутка законцівки", "°",
     "Штраф, якщо крутка законцівки більша (потрібен washout — від'ємна крутка)."),

    # ---- stability ---------------------------------------------------
    ("SM_TARGET",   "stability", "Цільовий запас стійкості", "",
     "0.05 = 5% САХ. Центрівка підбирається під це значення."),
    ("SM_DEADBAND", "stability", "Мертва зона запасу", "",
     "Відхилення в межах цієї зони не штрафується."),
    ("SM_SCALE",    "stability", "Масштаб штрафу за запас", "", ""),
    ("XCG_MIN",     "stability", "Мін. центрівка", "частка САХ", ""),
    ("XCG_MAX",     "stability", "Макс. центрівка", "частка САХ", ""),
    ("XCG_INIT",    "stability", "Початкова центрівка", "частка САХ",
     "Стартова точка для внутрішнього розв'язувача центрівки."),
    ("BETA_STEP_DEG", "stability", "Крок по ковзанню", "°",
     "Для скінченно-різницевого розрахунку Cn_beta та Cl_beta."),
    ("SLOPE_DALPHA_DEG", "stability", "Крок по куту атаки", "°",
     "Для розрахунку CL_alpha та Cm_alpha."),
    ("CG_MAX_ITERS", "stability", "Макс. ітерацій центрівки", "", ""),
    ("CG_SM_TOL",    "stability", "Допуск по запасу стійкості", "", ""),
    ("ALPHA_SOLVE_MAXITER_COARSE", "stability", "Ітерацій α (груба сітка)", "", ""),
    ("ALPHA_SOLVE_MAXITER_MAIN",   "stability", "Ітерацій α (основна сітка)", "", ""),

    # ---- structure ---------------------------------------------------
    ("EI_ROOT_BASE", "structure", "Жорсткість на згин у корені (EI)", "Н·м²",
     "ПОСТУЛЮЄТЬСЯ, не рахується. По розмаху масштабується як (хорда/коренева)³."),
    ("TORSION_TO_BENDING_RATIO", "structure", "Відношення GJ/EI", "",
     "0.35 = торсійно м'яке крило. Для порівняння: гола труба ~0.77, замкнений кесон вище."),
    ("E_MODULUS", "structure", "Модуль пружності E", "Па",
     "Використовується ЛИШЕ щоб перерахувати постульоване EI назад у момент інерції. Не впливає на жорсткість."),
    ("POISSON_NU", "structure", "Коефіцієнт Пуассона", "", ""),
    ("SECTION_HEIGHT_REL", "structure", "Висота перерізу / хорда", "",
     "0.12 = 12% хорди. Використовується для оцінки напружень."),
    ("EA_REL", "structure", "Положення осі жорсткості", "частка хорди",
     "Захардкоджено. Саме це вам треба зробити змінною, коли додасте два лонжерони."),
    ("AC_REL", "structure", "Положення аеродинамічного фокуса", "частка хорди",
     "Плече крутного моменту = (AC_REL − EA_REL) × хорда."),

    # ---- objective ---------------------------------------------------
    ("W_LD",        "objective", "Вага L/D", "",
     "Домінуючий терм. Саме він робить це аеродинамічним оптимізатором."),
    ("LD_REF",      "objective", "Нормування L/D", "", ""),
    ("W_MROOT",     "objective", "Вага кореневого моменту", "", ""),
    ("MROOT_REF",   "objective", "Нормування кореневого моменту", "Н·м",
     "Треба масштабувати разом із розміром крила, інакше терм втрачає сенс."),
    ("W_3D",        "objective", "Вага 3D-штрафу", "",
     "Карає різницю між тривимірною та проєкційною площею (за двогранний кут)."),
    ("THREED_PEN_REF", "objective", "Нормування 3D-штрафу", "", ""),
    ("W_TRIM",      "objective", "Вага балансування", "", ""),
    ("CM_TRIM_REF", "objective", "Нормування Cm балансування", "", ""),
    ("W_CMA_STAB",  "objective", "Вага стійкості по Cm_alpha", "",
     "Великий штраф за нестійкість (Cm_alpha >= 0)."),
    ("W_SM_TGT",    "objective", "Вага попадання в запас стійкості", "", ""),
    ("W_SANITY",    "objective", "Вага перевірок узгодженості", "", ""),
    ("W_STR_ENERGY","objective", "Вага енергії деформації", "", ""),
    ("W_SIG_PEAK",  "objective", "Вага пікового напруження", "", ""),
    ("W_SIG_UNIFORM","objective", "Вага рівномірності напружень", "", ""),

    # ---- penalties ---------------------------------------------------
    ("W_STAB_CONSTRAINT", "penalties", "Вага обмежень стійкості", "",
     "Cn_beta та Cl_beta порівнюються з еталонним крилом."),
    ("STAB_PENALTY_CAP",  "penalties", "Стеля штрафу стійкості", "", ""),
    ("STAB_TOL",          "penalties", "Допуск обмежень стійкості", "", ""),
    ("W_THIN_CHORD",      "penalties", "Вага штрафу за тонку хорду", "", ""),
    ("W_CHORD_MONO",      "penalties", "Вага штрафу за зростання хорди", "",
     "Хорда мусить не зростати до законцівки."),
    ("W_TIP_TWIST",       "penalties", "Вага штрафу за крутку законцівки", "", ""),
    ("W_MID_CHORD_HARD",  "penalties", "Жорсткий штраф: хорда в станції", "",
     "2e8 = фактично жорстке обмеження."),
    ("W_XMAX_HARD",       "penalties", "Жорсткий штраф: габарит X", "", ""),
    ("W_TIP_TAPER_HARD",  "penalties", "Жорсткий штраф: звуження", "", ""),
    ("W_GLOBAL_CHORD_HARD","penalties", "Жорсткий штраф: глобальна мін. хорда", "", ""),
    ("CL_BINS_MISMATCH_TOL","penalties", "Допуск неузгодженості CL", "",
     "Порівнює CL від VLM із CL, проінтегрованим по розподілу підйомної сили."),
    ("CL_BINS_MISMATCH_PEN","penalties", "Штраф за неузгодженість CL", "", ""),

    # ---- mesh --------------------------------------------------------
    ("NUM_SECTIONS_PER_SEMISPAN", "mesh", "Перерізів на напіврозмах", "",
     "Кількість станцій геометрії."),
    ("SPANWISE_RES_MAIN",  "mesh", "Панелей по розмаху (основна)", "", ""),
    ("CHORDWISE_RES_MAIN", "mesh", "Панелей по хорді (основна)", "", ""),
    ("SPANWISE_RES_COARSE","mesh", "Панелей по розмаху (груба)", "",
     "Груба сітка використовується для похідних — швидко."),
    ("CHORDWISE_RES_COARSE","mesh","Панелей по хорді (груба)", "", ""),
    ("LL_N_FOURIER_TERMS", "mesh", "Членів ряду Фур'є", "",
     "Для індуктивного опору за теорією несучої лінії."),
    ("LL_N_THETA_SAMPLES", "mesh", "Точок інтегрування θ", "", ""),

    # ---- output ------------------------------------------------------
    ("RESULTS_ROOT",  "output", "Папка результатів", "",
     "Відносно 01_PYTHON. Для кожного запуску краще своя папка."),
    ("EXPORT_STL",    "output", "Експортувати STL на кожній оцінці", "",
     "340 мс і 546 КБ на варіант = 1.9 ГБ і 21 хв за повний прогін. Вимкніть і беріть STL потім через regenerate.py."),
    ("EXPORT_CURVES", "output", "Писати curves.json", "",
     "0.5 мс і 2.5 КБ на варіант — числа, з яких UI малює графіки. Тримайте увімкненим."),
    ("STL_N_AIRFOIL", "output", "Точок на профіль у STL", "", ""),
]


RUNTIME_PARAMS = [
    ("popsize",      "runtime", "Розмір популяції (×16 змінних)", "",
     "Реальна популяція = popsize × 16."),
    ("maxiter",      "runtime", "Максимум ітерацій DE", "", ""),
    ("workers",      "runtime", "Процесів", "",
     "-1 = усі ядра. Код до цього готовий, але за замовчуванням стоїть 1."),
    ("seed",         "runtime", "Seed", "",
     "Фіксований seed = повторюваний результат."),
    ("strategy",     "runtime", "Стратегія DE", "",
     "best1bin, rand1bin, best2bin тощо."),
    ("mutation_min", "runtime", "Мутація (мін)", "", ""),
    ("mutation_max", "runtime", "Мутація (макс)", "", ""),
    ("recombination","runtime", "Рекомбінація", "", ""),
    ("polish",       "runtime", "Фінальне доведення (polish)", "",
     "Локальна оптимізація найкращої точки після DE."),
    ("time_budget_hours", "runtime", "Бюджет часу", "год",
     "Зупиняє DE після цього часу незалежно від ітерацій."),
    ("eval_cap_factor",   "runtime", "Коефіцієнт стелі оцінок", "",
     "Стеля = (maxiter+1) × popsize × 16 × це."),
]


BOUND_LABELS = {
    "SWEEP_LE_DEG":   ("Стрілоподібність передньої кромки", "°"),
    "ROOT_TWIST_DEG": ("Крутка в корені", "°"),
    "X1_OFF":         ("Зсув передньої кромки @ 1/3", "м"),
    "X2_OFF":         ("Зсув передньої кромки @ 2/3", "м"),
    "X3_OFF":         ("Зсув передньої кромки @ законцівка", "м"),
    "C1_MUL":         ("Множник хорди @ 1/3", ""),
    "DC2":            ("Спад хорди 1/3 → 2/3", ""),
    "DC3":            ("Спад хорди 2/3 → законцівка", ""),
    "TW1_DEG":        ("Крутка @ 1/3", "°"),
    "TW2_DEG":        ("Крутка @ 2/3", "°"),
    "TW3_DEG":        ("Крутка @ законцівка", "°"),
    "Z1_FRAC":        ("Підйом z @ 20% напіврозмаху", "частка напіврозмаху"),
    "Z2_FRAC":        ("Підйом z @ 45%", "частка напіврозмаху"),
    "Z3_FRAC":        ("Підйом z @ 70%", "частка напіврозмаху"),
    "Z4_FRAC":        ("Підйом z @ 90%", "частка напіврозмаху"),
    "Z5_FRAC":        ("Підйом z @ законцівка", "частка напіврозмаху"),
}


def load_module():
    """Import NX-2_DE_Optimization.py despite the dash in its filename."""
    if not os.path.isfile(SCRIPT):
        print(f"ERROR: not found: {SCRIPT}")
        sys.exit(1)

    # Make sure no existing config file colours the "defaults" we read.
    os.environ["WING_CONFIG"] = os.path.join(HERE, "_no_such_config.json")

    spec = importlib.util.spec_from_file_location("nx2_de_opt", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nx2_de_opt"] = mod
    spec.loader.exec_module(mod)
    return mod


def py_type(v):
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    return "str"


def main():
    print("importing NX-2_DE_Optimization.py (this pulls in AeroSandbox, takes a moment) ...")
    mod = load_module()
    print("import OK")

    fields = []
    missing = []
    for name, group, label, unit, hint in PARAMS:
        if not hasattr(mod, name):
            missing.append(name)
            continue
        val = getattr(mod, name)
        fields.append({
            "name": name,
            "group": group,
            "label": label,
            "unit": unit,
            "hint": hint,
            "type": py_type(val),
            "default": val,
        })

    runtime_defaults = dict(mod.RUNTIME_CFG)
    runtime_fields = []
    for name, group, label, unit, hint in RUNTIME_PARAMS:
        if name not in runtime_defaults:
            missing.append("runtime." + name)
            continue
        val = runtime_defaults[name]
        runtime_fields.append({
            "name": name,
            "group": group,
            "label": label,
            "unit": unit,
            "hint": hint,
            "type": py_type(val),
            "default": val,
        })

    bound_fields = []
    for name in mod.VAR_NAMES:
        lo, hi = mod.BOUNDS[name]
        label, unit = BOUND_LABELS.get(name, (name, ""))
        bound_fields.append({
            "name": name,
            "label": label,
            "unit": unit,
            "default": [float(lo), float(hi)],
        })

    schema = {
        "generated_from": os.path.relpath(SCRIPT, ROOT).replace("\\", "/"),
        "groups": GROUPS,
        "constants": fields,
        "runtime": runtime_fields,
        "bounds": bound_fields,
        "var_names": list(mod.VAR_NAMES),
    }

    os.makedirs(os.path.dirname(SCHEMA_OUT), exist_ok=True)
    with open(SCHEMA_OUT, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2, ensure_ascii=False)
    print(f"wrote {SCHEMA_OUT}  ({len(fields)} constants, "
          f"{len(runtime_fields)} runtime, {len(bound_fields)} bounds)")

    if missing:
        print("WARNING: not found in module: " + ", ".join(missing))

    if not os.path.isfile(CONFIG_OUT):
        cfg = {
            "_comment": "Written by the Electron UI. Delete this file to fall back "
                        "to the constants hardcoded in NX-2_DE_Optimization.py.",
            "constants": {f["name"]: f["default"] for f in fields},
            "bounds": {b["name"]: b["default"] for b in bound_fields},
            "runtime": {r["name"]: r["default"] for r in runtime_fields},
        }
        with open(CONFIG_OUT, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        print(f"wrote {CONFIG_OUT} (defaults)")
    else:
        print(f"kept existing {CONFIG_OUT}")


if __name__ == "__main__":
    main()
