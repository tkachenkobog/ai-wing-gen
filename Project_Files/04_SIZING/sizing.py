# -*- coding: utf-8 -*-
"""
sizing.py — підбір точки проєктування з вимог.

Відповідальність цього скрипта:
    вимоги (корисне навантаження, тривалість, спосіб старту)
        -> злітна маса, площа крила, розмах, крейсерська швидкість, CL
        -> габаритні обмеження та нормування
        -> ../01_PYTHON/wing_config.json

Далі оптимізатор (NX-2_DE_Optimization.py) читає цей конфіг і шукає ФОРМУ
крила при вже заданому розмірі. Цей скрипт форму не рахує — тільки розмір.

Що тут є, а чого немає в оптимізаторі:
    * замкнена петля мас (конструкція -> злітна маса -> потрібна площа -> ...)
    * розрахунковий випадок із перевантаженням, а не тільки крейсер 1g
    * підбір лонжерона під міцність (звідси маса конструкції)
    * абсолютні вимоги стійкості замість «не гірше за еталон»

Запуск:
    py -3 04_SIZING/sizing.py
    py -3 04_SIZING/sizing.py --dry-run     # порахувати, конфіг не писати
"""

import os
import sys
import json
import math

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ------------------------------------------------------------------
# Шляхи перевизначаються через оточення — так працюють дослідження.
# Без цих змінних поводиться точно як раніше: читає й пише файли на місцях,
# тож запуск із командного рядка без UI не ламається.
#
#   WING_REQUIREMENTS   вхідні вимоги
#   WING_CONFIG         конфіг для оптимізатора (той самий, що читає оптимізатор)
#   WING_SIZING_REPORT  повний звіт підбору
#   WING_RESULTS_ROOT   куда оптимізатор має писати результати (пишемо в конфіг)
# ------------------------------------------------------------------
def _env_path(name: str, default: str) -> str:
    v = (os.environ.get(name) or "").strip()
    return os.path.abspath(v) if v else default


REQ_PATH = _env_path("WING_REQUIREMENTS", os.path.join(HERE, "requirements.json"))
CONFIG_PATH = _env_path("WING_CONFIG", os.path.join(ROOT, "01_PYTHON", "wing_config.json"))
REPORT_PATH = _env_path("WING_SIZING_REPORT", os.path.join(HERE, "sizing_report.json"))
RESULTS_ROOT_OVERRIDE = (os.environ.get("WING_RESULTS_ROOT") or "").strip()

G = 9.80665

# Максимальна швидкість звалювання за способом старту/посадки [м/с]
STALL_LIMITS = {
    "hand":    11.0,
    "catapult": 16.0,
    "runway":   18.0,
}

# Властивості матеріалів лонжерона
MATERIALS = {
    #                E [Па]    допустиме напруження [Па]   щільність [кг/м³]
    "carbon_ud": dict(E=120e9, sigma_allow=600e6, rho=1600.0),
    "glass":     dict(E=40e9,  sigma_allow=350e6, rho=1900.0),
}


# ============================================================
# ДОПОМІЖНЕ
# ============================================================
def isa_density(alt_m: float) -> float:
    return 1.225 * (1.0 - 2.25577e-5 * max(0.0, alt_m)) ** 4.25588


def strip_comments(obj):
    """Прибрати ключі-пояснення (ті, що починаються з підкреслення)."""
    if isinstance(obj, dict):
        return {k: strip_comments(v) for k, v in obj.items() if not k.startswith("_")}
    return obj


def load_requirements():
    if not os.path.isfile(REQ_PATH):
        sys.exit(f"Не знайдено {REQ_PATH}")
    with open(REQ_PATH, "r", encoding="utf-8") as f:
        return strip_comments(json.load(f))


# ============================================================
# ГЕОМЕТРІЯ ПЛАНФОРМИ (оцінка, для габаритів і лонжерона)
# ============================================================
def planform_chords(S, b, taper):
    """Коренева/законцівкова хорда для трапеції із заданим звуженням."""
    c_root = 2.0 * S / (b * (1.0 + taper))
    c_tip = taper * c_root
    return c_root, c_tip


def chord_at(y, b, c_root, c_tip):
    """Хорда на відстані y від кореня (лінійне звуження)."""
    semi = 0.5 * b
    t = min(max(y / semi, 0.0), 1.0)
    return c_root + (c_tip - c_root) * t


# ============================================================
# ЛОНЖЕРОН: підбір під розрахункове навантаження
# ============================================================
def size_spar(mtow, b, c_root, tc, n_limit, n_ult, mat, st):
    """
    Підбирає площу полиць лонжерона за ТРЬОМА критеріями і бере максимум:

      1) міцність  — M = sigma_allow * A * h при розрахунковому перевантаженні
      2) жорсткість — прогин законцівки не більше tip_deflection_frac * напіврозмах
                       при експлуатаційному перевантаженні
      3) технологія — мінімальна площа полиці (кількість шарів * товщина шару)

    На малому масштабі майже завжди виграє критерій 3: кореневий момент такий
    малий, що міцнісна полиця виходить тонша за один шар тканини. Саме тому
    підбір ЛИШЕ за міцністю дає нефізично легкий лонжерон.

    Модель перерізу: дві полиці, розділені висотою h. Полиці звужуються до
    законцівки, тому ефективний об'єм ~45% від постійного перерізу.
    Стінка додає ~30% до маси полиць.
    """
    semi = 0.5 * b
    y_cp = 0.42 * semi                     # центр тиску напівкрила
    h = 0.85 * tc * c_root                 # висота лонжерона в корені

    M_ult = n_ult * (mtow * G / 2.0) * y_cp
    M_1g = (mtow * G / 2.0) * y_cp

    # --- 1) міцність ---
    A_strength = M_ult / max(mat["sigma_allow"] * h, 1e-12)

    # --- 2) жорсткість: консоль із зосередженою силою в центрі тиску ---
    P = n_limit * (mtow * G / 2.0)
    delta_max = st["tip_deflection_frac"] * semi
    EI_req = P * y_cp ** 2 * (3.0 * semi - y_cp) / (6.0 * max(delta_max, 1e-12))
    # EI = 2 * E * A * (h/2)^2  для двох полиць
    A_stiff = EI_req / max(2.0 * mat["E"] * (0.5 * h) ** 2, 1e-12)

    # --- 3) технологічний мінімум ---
    A_min = st["spar_cap_min_area_mm2"] * 1e-6

    A_cap = max(A_strength, A_stiff, A_min)
    driver = {A_strength: "міцність", A_stiff: "жорсткість", A_min: "технологія"}[A_cap]

    m_caps = 2.0 * 2.0 * A_cap * semi * 0.45 * mat["rho"]
    m_web = 0.30 * m_caps
    m_spar = m_caps + m_web

    EI_actual = 2.0 * mat["E"] * A_cap * (0.5 * h) ** 2
    delta_actual = P * y_cp ** 2 * (3.0 * semi - y_cp) / (6.0 * max(EI_actual, 1e-12))

    return dict(
        m_spar_kg=m_spar,
        cap_area_m2=A_cap,
        cap_area_mm2=A_cap * 1e6,
        cap_thickness_mm=1000.0 * A_cap / 0.025,   # при ширині полиці 25 мм
        spar_height_m=h,
        M_root_ultimate_Nm=M_ult,
        M_root_1g_Nm=M_1g,
        EI_root_Nm2=EI_actual,
        tip_deflection_m=delta_actual,
        tip_deflection_frac=delta_actual / max(semi, 1e-12),
        driver=driver,
        A_strength_mm2=A_strength * 1e6,
        A_stiffness_mm2=A_stiff * 1e6,
        A_min_mm2=A_min * 1e6,
    )


# ============================================================
# ГОЛОВНА ПЕТЛЯ МАС
# ============================================================
def converge_mass(req, verbose=True):
    pay = req["payload"]
    mis = req["mission"]
    lau = req["launch"]
    lim = req["geometry_limits"]
    st = req["structure"]
    pro = req["propulsion"]
    fix = req["fixed_masses"]
    aer = req["aero_estimate"]

    rho = isa_density(mis["altitude_m"])
    mat = MATERIALS.get(st["material"])
    if mat is None:
        sys.exit(f"Невідомий матеріал '{st['material']}'. Доступні: {list(MATERIALS)}")

    v_stall_max = STALL_LIMITS.get(lau["method"])
    if v_stall_max is None:
        sys.exit(f"Невідомий спосіб старту '{lau['method']}'. Доступні: {list(STALL_LIMITS)}")

    n_ult = st["load_factor_limit"] * st["safety_factor"]
    m_fixed = (pay["mass_kg"] + fix["fuselage_kg"] + fix["avionics_kg"]
               + fix["recovery_kg"] + fix["misc_kg"])

    # Навантаження на крило з умови звалювання [Н/м²]
    wing_loading = 0.5 * rho * v_stall_max ** 2 * lau["CL_max"]

    mtow = m_fixed * 2.0        # початкове припущення
    history = []

    for it in range(1, 41):
        # --- геометрія з поточної маси ---
        S = mtow * G / wing_loading
        b = math.sqrt(lim["aspect_ratio_target"] * S)
        span_clipped = b > lim["span_max_m"]
        if span_clipped:
            b = lim["span_max_m"]
        AR = b * b / S

        c_root, c_tip = planform_chords(S, b, lim["taper_ratio"])

        # --- крейсерський режим ---
        v_cruise = math.sqrt(2.0 * mtow * G / (rho * S * mis["cruise_CL"]))
        CD = aer["CD0"] + mis["cruise_CL"] ** 2 / (math.pi * AR * aer["oswald_e"])
        LD = mis["cruise_CL"] / CD

        # --- потужність і батарея ---
        p_shaft = mtow * G * v_cruise / LD          # потрібна тягова потужність [Вт]
        p_elec = p_shaft / pro["total_efficiency"]
        energy_wh = p_elec * (mis["endurance_min"] / 60.0)
        m_batt = energy_wh / (pro["battery_wh_per_kg"] * pro["battery_usable_fraction"])
        m_prop = p_elec * pro["mass_per_watt_kg"]

        # --- конструкція ---
        m_skin = st["skin_mass_per_area_kgm2"] * S
        spar = size_spar(mtow, b, c_root, lim["thickness_ratio"],
                         st["load_factor_limit"], n_ult, mat, st)
        m_struct = m_skin + spar["m_spar_kg"]

        mtow_new = m_fixed + m_struct + m_batt + m_prop
        err = abs(mtow_new - mtow) / max(mtow, 1e-9)
        history.append(dict(iter=it, mtow=mtow_new, S=S, b=b, err=err))

        mtow = 0.5 * (mtow + mtow_new)   # згладжування для стійкості

        if err < 1e-5:
            break
    else:
        print("! Петля мас не зійшлася за 40 ітерацій — перевірте вимоги "
              "(надто велике навантаження або надто мала питома енергія батареї).")

    # --- фінальний перерахунок на збіжній масі ---
    S = mtow * G / wing_loading
    b = min(math.sqrt(lim["aspect_ratio_target"] * S), lim["span_max_m"])
    span_clipped = math.sqrt(lim["aspect_ratio_target"] * S) > lim["span_max_m"]
    AR = b * b / S
    c_root, c_tip = planform_chords(S, b, lim["taper_ratio"])
    v_cruise = math.sqrt(2.0 * mtow * G / (rho * S * mis["cruise_CL"]))
    v_stall = math.sqrt(2.0 * mtow * G / (rho * S * lau["CL_max"]))
    CD = aer["CD0"] + mis["cruise_CL"] ** 2 / (math.pi * AR * aer["oswald_e"])
    LD = mis["cruise_CL"] / CD
    p_shaft = mtow * G * v_cruise / LD
    p_elec = p_shaft / pro["total_efficiency"]
    m_batt = (p_elec * mis["endurance_min"] / 60.0) / (
        pro["battery_wh_per_kg"] * pro["battery_usable_fraction"])
    m_prop = p_elec * pro["mass_per_watt_kg"]
    m_skin = st["skin_mass_per_area_kgm2"] * S
    spar = size_spar(mtow, b, c_root, lim["thickness_ratio"],
                         st["load_factor_limit"], n_ult, mat, st)

    return dict(
        converged_iters=len(history),
        rho=rho, n_ult=n_ult,
        wing_loading_Nm2=wing_loading,
        wing_loading_kgm2=wing_loading / G,
        mtow_kg=mtow,
        S_m2=S, b_m=b, AR=AR,
        span_clipped=span_clipped,
        c_root_m=c_root, c_tip_m=c_tip,
        v_cruise_mps=v_cruise, v_stall_mps=v_stall,
        cruise_CL=mis["cruise_CL"], CD_est=CD, LD_est=LD,
        power_shaft_W=p_shaft, power_elec_W=p_elec,
        masses=dict(
            payload=pay["mass_kg"],
            fuselage=fix["fuselage_kg"],
            skin=m_skin,
            spar=spar["m_spar_kg"],
            battery=m_batt,
            propulsion=m_prop,
            avionics=fix["avionics_kg"],
            recovery=fix["recovery_kg"],
            misc=fix["misc_kg"],
        ),
        spar=spar,
        payload_fraction=pay["mass_kg"] / mtow,
        material=st["material"],
    )


# ============================================================
# ЕМІСІЯ КОНФІГУ ДЛЯ ОПТИМІЗАТОРА
# ============================================================
def build_config_patch(req, r):
    """Тільки ті ключі, за які відповідає sizing. Решту конфігу не чіпаємо."""
    lim = req["geometry_limits"]
    stab = req["stability_requirements"]
    pay = req["payload"]

    b, S = r["b_m"], r["S_m2"]
    semi = 0.5 * b
    c_root, c_tip = r["c_root_m"], r["c_tip_m"]

    # Зона фюзеляжу: половина ширини відсіку плюс невеликий перехід
    y_cut = min(0.5 * pay["bay_width_m"] * 1.25, 0.30 * semi)

    # Станція контролю хорди — 2/3 напіврозмаху
    y_mid = 0.667 * semi
    c_mid = chord_at(y_mid, b, c_root, c_tip)

    patch = {}
    if RESULTS_ROOT_OVERRIDE:
        # Дослідження веде власну теку результатів. Абсолютний шлях, бо оптимізатор
        # запускається з 01_PYTHON і відносний вказав би не туди.
        patch["RESULTS_ROOT"] = os.path.abspath(RESULTS_ROOT_OVERRIDE)

    return {
        **patch,
        # ---- точка проєктування ----
        "S_FIXED": round(S, 5),
        "B_FULL": round(b, 4),
        "V_DESIGN": round(r["v_cruise_mps"], 3),
        "CL_TARGET": round(r["cruise_CL"], 4),
        "ALTITUDE_M": req["mission"]["altitude_m"],

        # ---- зона фюзеляжу ----
        "Y_CUT": round(y_cut, 4),

        # ---- габаритні обмеження, масштабовані під новий розмір ----
        "Y_MID_CONSTRAINT": round(y_mid, 4),
        "C_MID_MIN": round(0.80 * c_mid, 4),
        "C_GLOBAL_MIN": round(0.60 * c_tip, 4),
        "C_ABS_MIN_EFF": round(0.75 * c_tip, 4),
        "C_ABS_HARD_MIN": round(0.25 * c_tip, 4),
        "X_MAX_LIMIT": round(1.70 * c_root, 4),

        # ---- нормування цільової функції під новий масштаб ----
        "MROOT_REF": round(r["spar"]["M_root_1g_Nm"], 3),
        "LD_REF": round(r["LD_est"], 2),

        # ---- структурна проксі-модель: узгодити з підібраним лонжероном ----
        "EI_ROOT_BASE": round(r["spar"]["EI_root_Nm2"], 1),
        "E_MODULUS": MATERIALS[r["material"]]["E"],
        "SECTION_HEIGHT_REL": lim["thickness_ratio"],

        # ---- РЕЖИМ: проєктувати з вимог, а не переробляти NX-2 ----
        "REFERENCE_MODE": "requirements",
        "FIX_ROOT_CHORD": False,
        "INIT_TAPER_RATIO": lim["taper_ratio"],

        # ---- АБСОЛЮТНІ вимоги стійкості (замість «не гірше за NX-2») ----
        "STABILITY_MODE": "absolute",
        "CNBETA_MIN_REQ": stab["Cn_beta_min_per_rad"],
        "CLBETA_MAX_REQ": stab["Cl_beta_max_per_rad"],
        "SM_TARGET": stab["static_margin"],
    }


def bounds_patch(r):
    """
    Межі зсувів передньої кромки задані в МЕТРАХ, тож при зміні розміру крила їх
    обов'язково масштабувати — інакше оптимізатор не дотягнеться до потрібної
    стрілоподібності.

    Нижня межа саме 0: в оригіналі там стояло 0.03 і 0.03, через що ПРЯМА
    передня кромка була недосяжна — планформа завжди виходила вигнутою, як у
    NX-2. Тепер пряма трапеція є допустимим варіантом, а вигин — необов'язковим.
    """
    c_root = r["c_root_m"]
    return {
        "X1_OFF": [0.0, round(0.35 * c_root, 4)],
        "X2_OFF": [0.0, round(0.35 * c_root, 4)],
        "X3_OFF": [0.0, round(0.70 * c_root, 4)],
    }


def merge_into_config(patch, bpatch, dry_run=False):
    cfg = {}
    if os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)

    cfg.setdefault("constants", {})
    cfg.setdefault("bounds", {})
    cfg.setdefault("runtime", {})

    changes = []
    for k, v in patch.items():
        old = cfg["constants"].get(k)
        if old != v:
            changes.append((k, old, v))
        cfg["constants"][k] = v
    for k, v in bpatch.items():
        old = cfg["bounds"].get(k)
        if old != v:
            changes.append((k, old, v))
        cfg["bounds"][k] = v

    cfg["_sized_by"] = "04_SIZING/sizing.py"

    if not dry_run:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)

    return changes


# ============================================================
# ЗВІТ
# ============================================================
def report(req, r, changes, dry_run):
    m = r["masses"]
    line = "-" * 62
    print()
    print("=" * 62)
    print("  ПІДБІР ТОЧКИ ПРОЄКТУВАННЯ")
    print("=" * 62)
    print(f"  збіглося за {r['converged_iters']} ітерацій")
    print()
    print("  МАСИ")
    print(line)
    for name, label in [("payload", "корисне навантаження"), ("fuselage", "корпус/гондола"),
                        ("skin", "обшивка крила"), ("spar", "лонжерон"),
                        ("battery", "батарея"), ("propulsion", "силова група"),
                        ("avionics", "авіоніка+серво"), ("recovery", "парашут/шасі"),
                        ("misc", "інше")]:
        if m[name] <= 0:
            continue
        print(f"  {label:24s} {m[name]:7.3f} кг   {100*m[name]/r['mtow_kg']:5.1f}%")
    print(line)
    print(f"  {'ЗЛІТНА МАСА':24s} {r['mtow_kg']:7.3f} кг")
    print(f"  {'частка корисного':24s} {100*r['payload_fraction']:7.1f} %")
    print()
    print("  КРИЛО")
    print(line)
    print(f"  площа                    {r['S_m2']:7.4f} м²")
    print(f"  розмах                   {r['b_m']:7.3f} м"
          + ("   <- обрізано за span_max" if r["span_clipped"] else ""))
    print(f"  подовження AR            {r['AR']:7.2f}")
    print(f"  навантаження на крило    {r['wing_loading_kgm2']:7.2f} кг/м²")
    print(f"  коренева хорда (оцінка)  {r['c_root_m']:7.4f} м")
    print(f"  хорда законцівки         {r['c_tip_m']:7.4f} м")
    print()
    print("  РЕЖИМ")
    print(line)
    print(f"  крейсер                  {r['v_cruise_mps']:7.2f} м/с  ({r['v_cruise_mps']*3.6:.0f} км/год)")
    print(f"  звалювання               {r['v_stall_mps']:7.2f} м/с  ({r['v_stall_mps']*3.6:.0f} км/год)")
    print(f"  L/D (оцінка)             {r['LD_est']:7.2f}")
    print(f"  потужність (електр.)     {r['power_elec_W']:7.0f} Вт")
    print()
    print("  ЛОНЖЕРОН")
    print(line)
    s = r["spar"]
    print(f"  розрахункове перевантаж. {r['n_ult']:7.2f}  (експл. * запас)")
    print(f"  кореневий момент, розрах.{s['M_root_ultimate_Nm']:7.1f} Н·м")
    print(f"  кореневий момент, 1g     {s['M_root_1g_Nm']:7.1f} Н·м")
    print(f"  висота лонжерона         {1000*s['spar_height_m']:7.1f} мм")
    print(f"  площа полиці             {s['cap_area_mm2']:7.1f} мм²"
          f"  (= {s['cap_thickness_mm']:.2f} мм при ширині 25 мм)")
    print(f"  визначальний критерій    {s['driver']:>7s}")
    print(f"     за міцністю           {s['A_strength_mm2']:7.2f} мм²")
    print(f"     за жорсткістю         {s['A_stiffness_mm2']:7.2f} мм²")
    print(f"     технологічний мінімум {s['A_min_mm2']:7.2f} мм²")
    print(f"  прогин законцівки        {1000*s['tip_deflection_m']:7.1f} мм"
          f"  ({100*s['tip_deflection_frac']:.1f}% напіврозмаху)")
    print(f"  EI в корені              {s['EI_root_Nm2']:7.1f} Н·м²")
    print()
    print(f"  ЗАПИС У {os.path.relpath(CONFIG_PATH, ROOT)}"
          + ("  [DRY RUN — не записано]" if dry_run else ""))
    print(line)
    if not changes:
        print("  без змін")
    for k, old, new in changes:
        o = "—" if old is None else (f"{old}" if not isinstance(old, float) else f"{old:g}")
        print(f"  {k:24s} {o:>14s}  ->  {new}")
    print()


def main():
    dry_run = "--dry-run" in sys.argv
    req = load_requirements()
    r = converge_mass(req)

    patch = build_config_patch(req, r)
    bpatch = bounds_patch(r)
    changes = merge_into_config(patch, bpatch, dry_run=dry_run)

    report(req, r, changes, dry_run)

    if not dry_run:
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(dict(requirements=req, result=r, config_patch=patch,
                           bounds_patch=bpatch), f, indent=2, ensure_ascii=False)
        print(f"  повний звіт: {os.path.relpath(REPORT_PATH, ROOT)}")
        print()

    # Попередження, які варто бачити
    if r["span_clipped"]:
        print("  ! Розмах обрізано за span_max — фактичне подовження нижче бажаного.")
    if r["payload_fraction"] > 0.5:
        print(f"  ! Частка корисного {100*r['payload_fraction']:.0f}% — оптимістично. "
              "Перевірте skin_mass_per_area_kgm2 та avionics_kg.")
    if r["v_stall_mps"] > STALL_LIMITS[req["launch"]["method"]] + 0.5:
        print("  ! Швидкість звалювання вища за ліміт способу старту.")
    print()


if __name__ == "__main__":
    main()
