# -*- coding: utf-8 -*-
"""
VL_Optimization_DE.py

Differential Evolution optimizer for a wing-only MDO problem using:
- AeroSandbox VLM (aerodynamics)
- 2D section-based profile drag (NeuralFoil if available; safe fallback)
- Simple 1D beam postprocess for bending/torsion + stress proxies
- CG solved internally to target SM=5% (CG not in genome)

Stability constraints (reference-based):
    Cn_beta >= Cn_beta_ref
    Cl_beta <= Cl_beta_ref

"Evolutionary" structure terms:
    1) Strain energy proxy
    2) Peak stress proxy
    3) Constant-stress tendency CV

- Export watertight STL for every evaluation (full 3D wing including airfoil + twist + sweep + dihedral).
- Parallel DE using all CPUs (SciPy workers=-1), Windows spawn safe.
- Chord monotone NON-INCREASING by construction (no chord repair step, ever).

FIXES (this revision):
- Root chord is HARD-FIXED to reference (CROOT_REF) after reference is computed/loaded.
- Prevents ultra-thin wings via:
    * higher chord multiplier floor
    * explicit thin-chord penalty on region y>=Y_CUT
- NEW HARD CONSTRAINTS (implemented as *very large penalties*):
    * chord at y=0.40 m must be >= 0.10 m
    * max x-extent of wing (conservative TE = x_le + chord) must be <= 0.45 m
"""

import os

# Limit BLAS/OpenMP threads per process to avoid oversubscription when using multiprocessing.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

import re
import json
import time
import struct
import random
import numpy as np

import matplotlib
matplotlib.use("Agg")  # safer for multiprocessing + file output
import matplotlib.pyplot as plt
from matplotlib.path import Path

import aerosandbox as asb

from scipy.optimize import differential_evolution
from scipy.interpolate import PchipInterpolator
from scipy.spatial import Delaunay


# Optional PyVista for structure screenshot
try:
    import pyvista as pv
except Exception:
    pv = None


# ============================================================
# OUTPUTS
# ============================================================
RESULTS_ROOT = "RESULTS"
os.makedirs(RESULTS_ROOT, exist_ok=True)

# Parallel-safe eval-id counter + best tracking using filesystem locks
_EVAL_LOCK_DIR = os.path.join(RESULTS_ROOT, "_lock_eval_counter")
_EVAL_COUNTER_PATH = os.path.join(RESULTS_ROOT, "_eval_counter.txt")

_BEST_LOCK_DIR = os.path.join(RESULTS_ROOT, "_lock_best")
_BEST_PATH = os.path.join(RESULTS_ROOT, "best_so_far.json")

_REF_PATH = os.path.join(RESULTS_ROOT, "reference_thresholds.json")
_REF_LOADED = False


# ============================================================
# FIXED REQUIREMENTS
# ============================================================
S_FIXED = 0.15          # [m^2] projected planform area (both halves)
B_FULL  = 1.2           # [m] full span (FIXED)
Y_TIP   = 0.5 * B_FULL  # [m] semispan
AR_FIXED = (B_FULL ** 2) / S_FIXED

CL_TARGET = 0.4         # [-] design CL
V_DESIGN = 12.0         # [m/s]

# Ignore inner span for objective/structure/plots (fuselage region)
Y_CUT = 0.10            # [m] only use y >= Y_CUT for structure/objective/plots

# --- NEW hard constraints (implemented via large penalties) ---
Y_MID_CONSTRAINT = 0.40   # [m]
C_MID_MIN = 0.09          # [m] chord(y=0.40) >= 0.09
X_MAX_LIMIT = 0.45        # [m] max x extent must be <= 0.45
W_MID_CHORD_HARD = 2.0e8  # huge penalty weight
W_XMAX_HARD      = 2.0e8  # huge penalty weight

# --- NEW: anti-plateau tip constraint ---
Y_TAPER_CHECK_FRAC = 0.85     # check chord around 85% semispan
TIP_TAPER_RATIO_MIN = 1.10    # require c(0.85*Ytip) >= 1.10 * c_tip
W_TIP_TAPER_HARD = 2.0e8      # huge penalty -> treated as hard constraint

# --- NEW hard constraint: global minimum chord anywhere on wing ---
C_GLOBAL_MIN = 0.04            # [m] if any chord < this => huge penalty
W_GLOBAL_CHORD_HARD = 2.0e8    # huge penalty weight (treat as hard constraint)

# ============================================================
# GLOBAL SETTINGS
# ============================================================
AIRFOIL_NAME = "s5020"
ALTITUDE_M = 0.0

# Discretization (main)
NUM_SECTIONS_PER_SEMISPAN = 15
SPANWISE_RES_MAIN  = 14
CHORDWISE_RES_MAIN = 6

# Coarse mesh for derivatives (fast)
SPANWISE_RES_COARSE  = 8
CHORDWISE_RES_COARSE = 4

# Structure (postprocess only)
EI_ROOT_BASE = 2.0e3
TORSION_TO_BENDING_RATIO = 0.35
E_MODULUS  = 50e9
POISSON_NU = 0.30
G_MODULUS  = E_MODULUS / (2.0 * (1.0 + POISSON_NU))
SECTION_HEIGHT_REL = 0.12
EA_REL = 0.35
AC_REL = 0.25
DEFLECTION_SCALE_VIS = 50.0

# Visual-only clamp
CLAMP_TORSION_FOR_PLOT = True
PHI_VIS_CLAMP_DEG = 20.0

# Lateral derivatives
BETA_STEP_DEG = 1.0

# Static margin targeting (CG solved internally)
SM_TARGET   = 0.05
SM_DEADBAND = 0.005
SM_SCALE    = 0.02
XCG_MIN = 0.05
XCG_MAX = 0.60
XCG_INIT = 0.25

# CG solver + slope estimation around alpha*
CG_MAX_ITERS = 4
CG_SM_TOL = 0.0025
SLOPE_DALPHA_DEG = 1.5
ALPHA_SOLVE_MAXITER_COARSE = 18
ALPHA_SOLVE_MAXITER_MAIN   = 12

# z(y) control locations
Y_CTRL_FRACS_Z = [0.20, 0.45, 0.70, 0.90, 1]
# chord/twist/LE control locations
Y_CTRL_FRACS_3 = [1/3, 2/3, 1.0]

# --- Lift integration consistency sanity ---
CL_BINS_MISMATCH_TOL = 0.08
CL_BINS_MISMATCH_PEN = 150.0

# --- Induced drag from lift distribution (lifting-line Fourier) ---
LL_N_FOURIER_TERMS = 25
LL_N_THETA_SAMPLES = 800

# Objective normalizations
LD_REF = 15.0
MROOT_REF = 2.0
THREED_PEN_REF = 0.10
CM_TRIM_REF = 0.05

# Objective weights (L/D first)
W_LD       = 15.0
W_MROOT    = 1.0
W_3D       = 0.3
W_TRIM     = 0.5
W_CMA_STAB = 15.0
W_SM_TGT   = 6.0
W_SANITY   = 2.0

# Structure weights (keep, but not driving)
W_STR_ENERGY   = 0.5
W_SIG_PEAK     = 0.5
W_SIG_UNIFORM  = 0.3

# Stability constraint penalty controls
W_STAB_CONSTRAINT = 10.0
STAB_PENALTY_CAP  = 1.0e4
STAB_TOL          = 1e-6

# Set from reference at startup (must be loaded in workers too):
CNBETA_MIN = None   # Cn_beta >= CNBETA_MIN
CLBETA_MAX = None   # Cl_beta <= CLBETA_MAX

# Structure reference scales (set from reference at startup / loaded in workers):
STR_ENERGY_REF = None
SIGMA_REF      = None
SIGMA_CV_REF   = None

# Reference root chord (loaded from reference_thresholds.json)
CROOT_REF = None   # [m] reference root chord (from the reference design)

# STL export settings
EXPORT_STL = True
STL_N_AIRFOIL = 180
STL_PATH_NAME = "wing_watertight.stl"

# Root-chord equality (now HARD-FIXED in geometry once reference is available)
# Kept as debug metric only:
CROOT_TOL = 0.0005

# --- Chord floors (reworked to avoid tip plateau artifacts) ---
# Hard floor only for numerical safety (very small)
C_MUL_HARD_MIN = 0.05         # multiplier floor, much lower than before
C_ABS_HARD_MIN = 0.015        # absolute hard floor, just to prevent VLM pathologies

# Soft "effective region" thickness requirement (objective penalty, not clamping)
C_ABS_MIN_EFF = 0.040         # keep your 4 cm preference
W_THIN_CHORD = 9000.0         # stronger, but applied only to inner 90% semispan

# --- Fix B: penalty for ANY chord increase anywhere ---
W_CHORD_MONO = 30000.0  # strong; tune 1e4..1e5

# --- NEW: washout preference ---
TIP_TWIST_MAX_DEG = 0.0     # penalize if tip twist is > 0 deg (positive twist)
W_TIP_TWIST = 4.0

# ============================================================
# DESIGN VARIABLES (16 vars)
# ============================================================
VAR_NAMES = [
    "SWEEP_LE_DEG",
    "ROOT_TWIST_DEG",
    "X1_OFF", "X2_OFF", "X3_OFF",          # LE offsets [m]

    # Chord monotone-by-construction:
    "C1_MUL",                               # chord multiplier at y=1/3
    "DC2",                                  # >=0 drop from C1->C2
    "DC3",                                  # >=0 drop from C2->C3

    "TW1_DEG", "TW2_DEG", "TW3_DEG",        # twist at control points [deg]
    "Z1_FRAC", "Z2_FRAC", "Z3_FRAC", "Z4_FRAC", "Z5_FRAC",  # z(y) controls
]

BOUNDS = {
    "SWEEP_LE_DEG":    (0.0, 25.0),
    "ROOT_TWIST_DEG":  (-5.0, 5.0),
    "X1_OFF":          (0.03, 0.10),
    "X2_OFF":          (0, 0.001),
    "X3_OFF":          (0.03, 0.20),

    # chord params
    "C1_MUL": (0.22, 1.25),   # keep reasonable, not too tiny
    "DC2":             (0.0,  1.25),
    "DC3":             (0.0,  1.25),

    "TW1_DEG":         (-12.0, 1.0),
    "TW2_DEG":         (-12.0, 1.0),
    "TW3_DEG":         (-12.0, 1.0),
    "Z1_FRAC":         (0, 0.2),
    "Z2_FRAC":         (0, 0.2),
    "Z3_FRAC":         (0, 0.2),
    "Z4_FRAC":         (0, 0.2),
    "Z5_FRAC":         (0, 0.2),
}
DE_BOUNDS = [BOUNDS[n] for n in VAR_NAMES]

def de_objective(x):
    # Top-level function => picklable => works with workers=-1 on Windows
    return objective_wrapper(x, velocity=V_DESIGN)


# ============================================================
# PARALLEL-SAFE LOCKS / COUNTER / BEST
# ============================================================
def _safe_mkdir_lock(lock_dir: str, stale_seconds: float = 600.0):
    """Acquire a lock via atomic os.mkdir(). If stale, attempt cleanup."""
    while True:
        try:
            os.mkdir(lock_dir)
            return
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lock_dir)
                if age > stale_seconds:
                    try:
                        os.rmdir(lock_dir)
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(0.01 + 0.02 * random.random())


def _release_lock(lock_dir: str):
    try:
        os.rmdir(lock_dir)
    except Exception:
        pass


def initialize_eval_counter_from_existing():
    """Initialize the eval counter to max(existing eval_xxxxx folders)."""
    max_id = 0
    try:
        for name in os.listdir(RESULTS_ROOT):
            m = re.match(r"^eval_(\d+)$", name)
            if m:
                max_id = max(max_id, int(m.group(1)))
    except Exception:
        pass

    _safe_mkdir_lock(_EVAL_LOCK_DIR)
    try:
        current = 0
        if os.path.isfile(_EVAL_COUNTER_PATH):
            try:
                with open(_EVAL_COUNTER_PATH, "r", encoding="utf-8") as f:
                    current = int(f.read().strip() or "0")
            except Exception:
                current = 0

        newv = max(current, max_id)
        with open(_EVAL_COUNTER_PATH, "w", encoding="utf-8") as f:
            f.write(str(int(newv)))
    finally:
        _release_lock(_EVAL_LOCK_DIR)


def allocate_eval_id() -> int:
    """Allocate a unique sequential eval id across processes."""
    _safe_mkdir_lock(_EVAL_LOCK_DIR)
    try:
        cur = 0
        if os.path.isfile(_EVAL_COUNTER_PATH):
            try:
                with open(_EVAL_COUNTER_PATH, "r", encoding="utf-8") as f:
                    cur = int(f.read().strip() or "0")
            except Exception:
                cur = 0
        cur += 1
        with open(_EVAL_COUNTER_PATH, "w", encoding="utf-8") as f:
            f.write(str(int(cur)))
        return int(cur)
    finally:
        _release_lock(_EVAL_LOCK_DIR)


def get_eval_counter_value() -> int:
    try:
        with open(_EVAL_COUNTER_PATH, "r", encoding="utf-8") as f:
            return int(f.read().strip() or "0")
    except Exception:
        return 0


def update_best_so_far_locked(metrics: dict):
    """Parallel-safe best_so_far.json update."""
    obj = float(metrics.get("objective", float("inf")))
    if not np.isfinite(obj):
        return

    _safe_mkdir_lock(_BEST_LOCK_DIR)
    try:
        best_obj = float("inf")
        if os.path.isfile(_BEST_PATH):
            try:
                with open(_BEST_PATH, "r", encoding="utf-8") as f:
                    best = json.load(f)
                best_obj = float(best.get("objective", float("inf")))
            except Exception:
                best_obj = float("inf")

        if obj < best_obj:
            best_record = dict(metrics)
            best_record["best_update_timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(_BEST_PATH, "w", encoding="utf-8") as f:
                json.dump(best_record, f, indent=2)
    finally:
        _release_lock(_BEST_LOCK_DIR)


# ============================================================
# REFERENCE THRESHOLDS LOADING (IMPORTANT FOR MULTIPROCESS)
# ============================================================
def ensure_reference_loaded():
    """Workers (Windows spawn) must load CNBETA/CLBETA + structure refs + CROOT_REF from disk."""
    global _REF_LOADED
    global CNBETA_MIN, CLBETA_MAX, STR_ENERGY_REF, SIGMA_REF, SIGMA_CV_REF, CROOT_REF

    if _REF_LOADED:
        return

    if not os.path.isfile(_REF_PATH):
        return

    try:
        with open(_REF_PATH, "r", encoding="utf-8") as f:
            ref = json.load(f)

        if "Cn_beta_ref_per_rad" in ref:
            CNBETA_MIN = float(ref["Cn_beta_ref_per_rad"])
        if "Cl_beta_ref_per_rad" in ref:
            CLBETA_MAX = float(ref["Cl_beta_ref_per_rad"])

        if "c_root_ref_m" in ref:
            CROOT_REF = float(ref["c_root_ref_m"])

        sref = ref.get("structure_ref", {})
        if "U_total_ref" in sref:
            STR_ENERGY_REF = float(sref["U_total_ref"])
        if "sigma_peak_ref_Pa" in sref:
            SIGMA_REF = float(sref["sigma_peak_ref_Pa"])
        if "sigma_cv_ref" in sref:
            SIGMA_CV_REF = float(sref["sigma_cv_ref"])

        _REF_LOADED = True
    except Exception:
        _REF_LOADED = False


# ============================================================
# UTILITIES
# ============================================================
def safe_get(d, keys, default=np.nan):
    for k in keys:
        if k in d:
            return d[k]
    return default


def safe_grad(y, x):
    y = np.asarray(y); x = np.asarray(x)
    g = np.empty_like(y)
    g[1:-1] = (y[2:] - y[:-2]) / (x[2:] - x[:-2])
    g[0]    = (y[1] - y[0]) / (x[1] - x[0])
    g[-1]   = (y[-1] - y[-2]) / (x[-1] - x[-2])
    return g


def clip_to_bounds_dict(design):
    out = {}
    for k, v in design.items():
        if k in BOUNDS:
            lo, hi = BOUNDS[k]
            out[k] = float(np.clip(float(v), lo, hi))
        else:
            out[k] = float(v)
    return out


def vec_to_design(x):
    return {name: float(val) for name, val in zip(VAR_NAMES, x)}


def design_to_vec(design):
    return np.array([float(design[n]) for n in VAR_NAMES], dtype=float)


def geometry_to_wind_rotation_matrix(op_point):
    ex_w = np.array(op_point.convert_axes(1.0, 0.0, 0.0, from_axes="geometry", to_axes="wind"))
    ey_w = np.array(op_point.convert_axes(0.0, 1.0, 0.0, from_axes="geometry", to_axes="wind"))
    ez_w = np.array(op_point.convert_axes(0.0, 0.0, 1.0, from_axes="geometry", to_axes="wind"))
    return np.vstack([ex_w, ey_w, ez_w]).T


def dynamic_pressure(op_point):
    try:
        return float(op_point.dynamic_pressure())
    except Exception:
        rho = 1.225
        V = float(op_point.velocity)
        return 0.5 * rho * V * V


def get_forces_and_points(vlm_obj):
    if hasattr(vlm_obj, "forces_geometry"):
        Fg = np.asarray(vlm_obj.forces_geometry)
    elif hasattr(vlm_obj, "panel_forces_geometry"):
        Fg = np.asarray(vlm_obj.panel_forces_geometry)
    elif hasattr(vlm_obj, "panel_forces"):
        Fg = np.asarray(vlm_obj.panel_forces)
    else:
        raise AttributeError("No per-panel forces on VLM object.")

    if hasattr(vlm_obj, "collocation_points"):
        P = np.asarray(vlm_obj.collocation_points)
    elif hasattr(vlm_obj, "panel_collocation_points"):
        P = np.asarray(vlm_obj.panel_collocation_points)
    else:
        raise AttributeError("No panel collocation points on VLM object.")
    return Fg, P


def make_vlm_uniform(airplane, op, spanwise_res, chordwise_res):
    try:
        return asb.VortexLatticeMethod(
            airplane=airplane,
            op_point=op,
            spanwise_resolution=spanwise_res,
            chordwise_resolution=chordwise_res,
            spanwise_spacing_function=np.linspace,
            chordwise_spacing_function=np.linspace
        )
    except TypeError:
        try:
            return asb.VortexLatticeMethod(
                airplane=airplane,
                op_point=op,
                spanwise_resolution=spanwise_res,
                chordwise_resolution=chordwise_res
            )
        except TypeError:
            return asb.VortexLatticeMethod(
                airplane=airplane,
                op_point=op,
                mesh_n_spanwise=spanwise_res,
                mesh_n_chordwise=chordwise_res
            )


def pchip_from_root_and_ctrl(ys, y_tip, root_value, y_ctrl_fracs, ctrl_values):
    y_ctrl = np.array([f * y_tip for f in y_ctrl_fracs], dtype=float)
    xp = np.concatenate([[0.0], y_ctrl])
    fp = np.concatenate([[float(root_value)], np.array(ctrl_values, dtype=float)])
    f = PchipInterpolator(xp, fp, extrapolate=True)
    return f(ys)


def z_from_controls(ys, y_tip, z_root, y_ctrl_fracs, z_ctrl_fracs):
    y_ctrl = np.array([f * y_tip for f in y_ctrl_fracs], dtype=float)
    z_ctrl = np.array([zf * y_tip for zf in z_ctrl_fracs], dtype=float)
    xp = np.concatenate([[0.0], y_ctrl])
    fp = np.concatenate([[float(z_root)], z_ctrl])
    f = PchipInterpolator(xp, fp, extrapolate=True)
    return f(ys)


def compute_mac_ybar_xle(ys, chord, x_le, S_full):
    ys = np.asarray(ys, dtype=float)
    c  = np.asarray(chord, dtype=float)
    x  = np.asarray(x_le, dtype=float)

    I_c2 = np.trapezoid(c**2, x=ys)
    MAC = (4.0 / max(S_full, 1e-12)) * I_c2

    I_yc2 = np.trapezoid(ys * (c**2), x=ys)
    y_bar = (4.0 / max(S_full * max(MAC, 1e-12), 1e-12)) * I_yc2

    I_xc2 = np.trapezoid(x * (c**2), x=ys)
    x_le_mac = (4.0 / max(S_full * max(MAC, 1e-12), 1e-12)) * I_xc2

    return float(MAC), float(y_bar), float(x_le_mac)


def augment_span_to_tip(centers, L_prime, cl_local, ys_geom, chord_geom, y_tip):
    centers = np.asarray(centers, dtype=float)
    L_prime = np.asarray(L_prime, dtype=float)
    cl_local = np.asarray(cl_local, dtype=float)

    y0 = 0.0
    yT = float(y_tip)

    L0 = float(L_prime[0]) if len(L_prime) > 0 else 0.0
    cl0 = float(cl_local[0]) if len(cl_local) > 0 else 0.0

    y_aug  = np.concatenate([[y0], centers, [yT]])
    L_aug  = np.concatenate([[L0], L_prime, [0.0]])
    cl_aug = np.concatenate([[cl0], cl_local, [0.0]])

    chord_aug = np.interp(y_aug, ys_geom, chord_geom)
    return y_aug, L_aug, cl_aug, chord_aug


# ============================================================
# STL EXPORT (watertight, full wing)
# ============================================================
def _get_airfoil_coords_any_version(airfoil: asb.Airfoil) -> np.ndarray:
    """Return Nx2 array [x, z] from AeroSandbox Airfoil across versions."""
    if hasattr(airfoil, "coordinates") and not callable(getattr(airfoil, "coordinates")):
        coords = np.asarray(getattr(airfoil, "coordinates"))
        if coords.ndim == 2 and coords.shape[1] >= 2:
            return coords[:, :2]

    if hasattr(airfoil, "coordinates") and callable(getattr(airfoil, "coordinates")):
        fn = getattr(airfoil, "coordinates")
        for kwargs in ({}, {"n": 200}, {"n_points": 200}, {"N": 200}):
            try:
                coords = np.asarray(fn(**kwargs))
                if coords.ndim == 2 and coords.shape[1] >= 2:
                    return coords[:, :2]
            except Exception:
                continue

    for name in ("get_coordinates", "coords"):
        if hasattr(airfoil, name) and callable(getattr(airfoil, name)):
            fn = getattr(airfoil, name)
            coords = np.asarray(fn())
            if coords.ndim == 2 and coords.shape[1] >= 2:
                return coords[:, :2]

    raise RuntimeError("Could not extract airfoil coordinates from AeroSandbox Airfoil object.")


def _resample_closed_curve_xy(points_xy: np.ndarray, n: int) -> np.ndarray:
    """Resample a closed 2D curve to n points (no duplicated endpoint)."""
    pts = np.asarray(points_xy, dtype=float)
    if pts.shape[0] < 8:
        return pts

    if np.linalg.norm(pts[0] - pts[-1]) < 1e-10:
        pts = pts[:-1]

    pts_c = np.vstack([pts, pts[0]])
    seg = np.linalg.norm(np.diff(pts_c, axis=0), axis=1)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(s[-1])
    if total < 1e-12:
        return pts

    s_new = np.linspace(0.0, total, n + 1)[:-1]
    x = np.interp(s_new, s, pts_c[:, 0])
    y = np.interp(s_new, s, pts_c[:, 1])
    return np.column_stack([x, y])


def _write_binary_stl(path: str, triangles: np.ndarray):
    """Write triangles (N,3,3) to a binary STL."""
    tris = np.asarray(triangles, dtype=np.float32)
    n = int(tris.shape[0])

    header = b"VL_Optimization_DE watertight wing STL"
    header = header[:80].ljust(80, b" ")

    with open(path, "wb") as f:
        f.write(header)
        f.write(struct.pack("<I", n))
        for tri in tris:
            v1, v2, v3 = tri[0], tri[1], tri[2]
            nrm = np.cross(v2 - v1, v3 - v1)
            nn = float(np.linalg.norm(nrm))
            if nn > 1e-20:
                nrm = nrm / nn
            else:
                nrm = np.array([0.0, 0.0, 0.0], dtype=np.float32)

            f.write(struct.pack("<3f", float(nrm[0]), float(nrm[1]), float(nrm[2])))
            f.write(struct.pack("<3f", float(v1[0]), float(v1[1]), float(v1[2])))
            f.write(struct.pack("<3f", float(v2[0]), float(v2[1]), float(v2[2])))
            f.write(struct.pack("<3f", float(v3[0]), float(v3[1]), float(v3[2])))
            f.write(struct.pack("<H", 0))


def _triangulate_cap(points3d: np.ndarray, outward_y_sign: float) -> np.ndarray:
    """Cap a tip by Delaunay triangulation in (x,z), filtering triangles inside the airfoil polygon."""
    pts = np.asarray(points3d, dtype=float)
    xz = pts[:, [0, 2]]

    poly = xz
    if np.linalg.norm(poly[0] - poly[-1]) > 1e-12:
        poly_closed = np.vstack([poly, poly[0]])
    else:
        poly_closed = poly
    path = Path(poly_closed)

    try:
        tri = Delaunay(xz)
        simplices = tri.simplices
    except Exception:
        tris = []
        for i in range(1, len(pts) - 1):
            tris.append([pts[0], pts[i], pts[i + 1]])
        tris = np.asarray(tris, dtype=float)
        for k in range(tris.shape[0]):
            v1, v2, v3 = tris[k]
            nrm = np.cross(v2 - v1, v3 - v1)
            if np.sign(nrm[1]) != np.sign(outward_y_sign):
                tris[k] = np.array([v1, v3, v2])
        return tris

    tris = []
    for s in simplices:
        c2 = np.mean(xz[s], axis=0)
        if path.contains_point(c2):
            tris.append(pts[s])
    if not tris:
        return np.zeros((0, 3, 3), dtype=float)

    tris = np.asarray(tris, dtype=float)
    for k in range(tris.shape[0]):
        v1, v2, v3 = tris[k]
        nrm = np.cross(v2 - v1, v3 - v1)
        if np.sign(nrm[1]) != np.sign(outward_y_sign):
            tris[k] = np.array([v1, v3, v2])

    return tris


def export_wing_stl(eval_dir: str, geom: dict, n_airfoil: int = STL_N_AIRFOIL):
    """
    Export watertight STL of full wing:
    - loft airfoil sections with chord + twist + sweep + dihedral
    - symmetric full wing (left + right)
    - cap both tips
    """
    if not EXPORT_STL:
        return

    try:
        af = asb.Airfoil(AIRFOIL_NAME)
        coords = _get_airfoil_coords_any_version(af)  # Nx2 [x,z]
        coords = _resample_closed_curve_xy(coords, int(n_airfoil))
        x_af = coords[:, 0]
        z_af = coords[:, 1]
    except Exception as e:
        with open(os.path.join(eval_dir, "STL_FAILED.txt"), "w", encoding="utf-8") as f:
            f.write(f"Airfoil coordinate extraction failed:\n{e}\n")
        return

    ys    = np.asarray(geom["ys"], dtype=float)
    x_le  = np.asarray(geom["x_le"], dtype=float)
    z_le  = np.asarray(geom["z_le"], dtype=float)
    chord = np.asarray(geom["chord"], dtype=float)
    twist = np.deg2rad(np.asarray(geom["twist"], dtype=float))

    right_sections = []
    for xi, yi, zi, ci, ti in zip(x_le, ys, z_le, chord, twist):
        X = ci * x_af
        Z = ci * z_af

        Xt =  X * np.cos(ti) + Z * np.sin(ti)
        Zt = -X * np.sin(ti) + Z * np.cos(ti)

        Xg = xi + Xt
        Yg = np.full_like(Xg, yi)
        Zg = zi + Zt

        right_sections.append(np.column_stack([Xg, Yg, Zg]))

    left_sections = []
    for sec in right_sections[::-1][1:]:
        secm = sec.copy()
        secm[:, 1] *= -1.0
        left_sections.append(secm)

    sections = left_sections + right_sections
    n_sec = len(sections)
    n_pts = sections[0].shape[0]

    inside_point = np.array([x_le[0] + 0.25 * chord[0], 0.0, z_le[0]], dtype=float)

    tris = []

    for i in range(n_sec - 1):
        A = sections[i]
        B = sections[i + 1]
        for j in range(n_pts):
            j2 = (j + 1) % n_pts
            v00 = A[j]
            v01 = A[j2]
            v10 = B[j]
            v11 = B[j2]

            t1 = np.array([v00, v10, v11], dtype=float)
            t2 = np.array([v00, v11, v01], dtype=float)

            for t in (t1, t2):
                v1, v2, v3 = t
                nrm = np.cross(v2 - v1, v3 - v1)
                cen = (v1 + v2 + v3) / 3.0
                if np.dot(nrm, (inside_point - cen)) > 0:
                    t[:] = np.array([v1, v3, v2], dtype=float)

            tris.append(t1)
            tris.append(t2)

    left_tip  = sections[0]
    right_tip = sections[-1]

    tris_left_cap  = _triangulate_cap(left_tip,  outward_y_sign=-1.0)
    tris_right_cap = _triangulate_cap(right_tip, outward_y_sign=+1.0)

    if tris_left_cap.size > 0:
        tris.extend(list(tris_left_cap))
    if tris_right_cap.size > 0:
        tris.extend(list(tris_right_cap))

    tris = np.asarray(tris, dtype=float)
    if tris.ndim != 3 or tris.shape[1:] != (3, 3) or tris.shape[0] < 50:
        with open(os.path.join(eval_dir, "STL_FAILED.txt"), "w", encoding="utf-8") as f:
            f.write("STL triangulation produced too few triangles.\n")
        return

    stl_path = os.path.join(eval_dir, STL_PATH_NAME)
    try:
        _write_binary_stl(stl_path, tris)
    except Exception as e:
        with open(os.path.join(eval_dir, "STL_FAILED.txt"), "w", encoding="utf-8") as f:
            f.write(f"STL write failed:\n{e}\n")


# ============================================================
# INDUCED DRAG FROM LIFT DISTRIBUTION (lifting-line Fourier)
# ============================================================
def compute_cdi_from_lift_distribution_LL(
    y_semispan, Lprime_semispan, q, V, b_full, AR,
    n_terms=LL_N_FOURIER_TERMS, n_theta=LL_N_THETA_SAMPLES
):
    y = np.asarray(y_semispan, dtype=float)
    Lp = np.asarray(Lprime_semispan, dtype=float)

    ok = np.isfinite(y) & np.isfinite(Lp)
    y = y[ok]; Lp = Lp[ok]
    if len(y) < 5:
        return dict(CDi=np.nan, e=np.nan, CL_LL=np.nan, A=None)

    order = np.argsort(y)
    y = y[order]; Lp = Lp[order]

    q = float(q); V = float(V)
    if (not np.isfinite(q)) or q <= 1e-12 or (not np.isfinite(V)) or V <= 1e-12:
        return dict(CDi=np.nan, e=np.nan, CL_LL=np.nan, A=None)

    Gamma = Lp * V / (2.0 * q)

    theta = np.linspace(0.0, np.pi, int(n_theta))
    y_theta = 0.5 * float(b_full) * np.abs(np.cos(theta))
    Gamma_theta = np.interp(y_theta, y, Gamma, left=float(Gamma[0]), right=0.0)

    N = int(n_terms)
    if N < 3:
        N = 3

    A = np.zeros(N, dtype=float)
    denom = float(b_full) * float(V) * np.pi
    if denom <= 1e-18:
        return dict(CDi=np.nan, e=np.nan, CL_LL=np.nan, A=None)

    for n in range(1, N + 1):
        integrand = Gamma_theta * np.sin(n * theta)
        A[n - 1] = np.trapezoid(integrand, x=theta) / denom

    A1 = float(A[0])
    CL_LL = float(np.pi * float(AR) * A1)

    sum_n = float(np.sum((np.arange(1, N + 1, dtype=float)) * (A ** 2)))
    CDi = float(np.pi * float(AR) * sum_n) if sum_n > 0 else np.nan
    e = float((A1 ** 2) / sum_n) if (np.isfinite(A1) and sum_n > 1e-18) else np.nan

    return dict(CDi=CDi, e=e, CL_LL=CL_LL, A=A)


# ============================================================
# STABILITY CONSTRAINT (reference-based)
# ============================================================
def stability_constraint_penalty(Cn_beta, Cl_beta):
    global CNBETA_MIN, CLBETA_MAX

    if CNBETA_MIN is None or CLBETA_MAX is None:
        return 0.0, True, 0.0, 0.0

    if not (np.isfinite(Cn_beta) and np.isfinite(Cl_beta)):
        return float(STAB_PENALTY_CAP), False, float("nan"), float("nan")

    v_cnb = max(0.0, (CNBETA_MIN - Cn_beta) - STAB_TOL)
    v_clb = max(0.0, (Cl_beta - CLBETA_MAX) - STAB_TOL)

    scale_cnb = max(abs(CNBETA_MIN), 1e-2)
    scale_clb = max(abs(CLBETA_MAX), 5e-2)

    pen = W_STAB_CONSTRAINT * ((v_cnb / scale_cnb) ** 2 + (v_clb / scale_clb) ** 2)
    pen = float(min(pen, STAB_PENALTY_CAP))
    ok = (v_cnb <= 0.0) and (v_clb <= 0.0)
    return pen, ok, float(v_cnb), float(v_clb)


# ============================================================
# LIFT DISTRIBUTION
# ============================================================
def compute_semispan_lift_distribution_binned(vlm_obj, y_tip, n_bins, y_geom_stations, chord_geom, S_ref):
    Fg, P = get_forces_and_points(vlm_obj)
    y_all = P[:, 1]
    R = geometry_to_wind_rotation_matrix(vlm_obj.op_point)
    Fw = Fg @ R.T
    Lp = -Fw[:, 2]  # lift

    mask = y_all >= -1e-12
    y_r, L_r = y_all[mask], Lp[mask]

    edges = np.linspace(0.0, y_tip, n_bins + 1)
    centers = 0.5 * (edges[:-1] + edges[1:])
    dy = float(y_tip / n_bins)

    L_strip = np.zeros(n_bins)
    idx = np.digitize(y_r, edges) - 1
    for Li, k in zip(L_r, idx):
        if 0 <= k < n_bins:
            L_strip[k] += Li

    L_prime = L_strip / max(dy, 1e-12)
    c_local = np.interp(centers, y_geom_stations, chord_geom)

    q = dynamic_pressure(vlm_obj.op_point)
    cl_local = L_prime / (q * np.maximum(c_local, 1e-9))

    y_aug, L_aug, cl_aug, chord_aug = augment_span_to_tip(
        centers, L_prime, cl_local, y_geom_stations, chord_geom, y_tip
    )

    L_total = 2.0 * np.trapezoid(L_aug, x=y_aug)
    CL_from_bins = L_total / (q * S_ref)

    return centers, L_prime, cl_local, q, CL_from_bins, y_aug, L_aug, cl_aug, chord_aug


# ============================================================
# STRUCTURE
# ============================================================
def structural_response_1D(y, Lprime, chord_bins):
    y = np.asarray(y, dtype=float)
    Lprime = np.asarray(Lprime, dtype=float)
    chord_bins = np.asarray(chord_bins, dtype=float)

    if len(y) < 3 or (not np.all(np.diff(y) > 0)):
        raise ValueError("structural_response_1D: y must be increasing with >=3 points.")

    V = np.zeros_like(y)
    M = np.zeros_like(y)
    for i in range(len(y) - 2, -1, -1):
        dy = y[i+1] - y[i]
        V[i] = V[i+1] + 0.5 * (Lprime[i] + Lprime[i+1]) * dy
        M[i] = M[i+1] + 0.5 * (V[i] + V[i+1]) * dy

    lever = (AC_REL - EA_REL) * chord_bins
    tprime = Lprime * lever
    T = np.zeros_like(y)
    for i in range(len(y) - 2, -1, -1):
        dy = y[i+1] - y[i]
        T[i] = T[i+1] + 0.5 * (tprime[i] + tprime[i+1]) * dy

    expc = 3.0
    c_root_val = float(chord_bins[0])
    EI = EI_ROOT_BASE * (np.maximum(chord_bins, 1e-9) / max(c_root_val, 1e-9)) ** expc
    GJ = (TORSION_TO_BENDING_RATIO * EI_ROOT_BASE) * (np.maximum(chord_bins, 1e-9) / max(c_root_val, 1e-9)) ** expc

    kappa = M / np.maximum(EI, 1e-12)
    theta = np.zeros_like(y)
    w_def = np.zeros_like(y)
    for i in range(len(y) - 1):
        dy = y[i+1] - y[i]
        theta[i+1] = theta[i] + 0.5 * (kappa[i] + kappa[i+1]) * dy
        w_def[i+1] = w_def[i] + 0.5 * (theta[i] + theta[i+1]) * dy

    phi_prime = T / np.maximum(GJ, 1e-12)
    phi = np.zeros_like(y)
    for i in range(len(y) - 1):
        dy = y[i+1] - y[i]
        phi[i+1] = phi[i] + 0.5 * (phi_prime[i] + phi_prime[i+1]) * dy

    I = np.maximum(EI / E_MODULUS, 1e-20)
    J = np.maximum(GJ / G_MODULUS, 1e-20)
    h = SECTION_HEIGHT_REL * chord_bins
    y_max = 0.5 * h
    sigma_bend = np.abs(M) * y_max / I
    r_tors = 0.5 * h
    tau_tors = np.abs(T) * r_tors / J
    sigma_vm = np.sqrt(sigma_bend**2 + 3.0 * tau_tors**2)

    U_bend = float(np.trapezoid((M**2) / np.maximum(EI, 1e-12), x=y))
    U_tors = float(np.trapezoid((T**2) / np.maximum(GJ, 1e-12), x=y))
    U_total = float(U_bend + U_tors)

    sigma_peak = float(np.max(sigma_vm)) if len(sigma_vm) else float("nan")
    sigma_mean = float(np.mean(sigma_vm)) if len(sigma_vm) else float("nan")
    sigma_cv = float(np.std(sigma_vm) / sigma_mean) if (np.isfinite(sigma_mean) and sigma_mean > 1e-12) else float("nan")

    return dict(
        M=M, V=V, T=T,
        w_def=w_def, phi=phi,
        sigma_vm=sigma_vm,
        EI=EI, GJ=GJ,
        U_bend=U_bend, U_tors=U_tors, U_total=U_total,
        sigma_peak=sigma_peak,
        sigma_cv=sigma_cv
    )


# ============================================================
# GEOMETRY BUILD (FIXED ROOT CHORD + ANTI-THIN)
# ============================================================
def _build_chord_distribution_fixed_root(ys, chord_shape_base, c_root_ref, S_target):
    """
    Build chord(y) while enforcing:
      - chord(0) = c_root_ref (exact)
      - total projected area = S_target (exact, unless degenerate)
    using a "pivot scaling" about the root:
      chord_shape(y) = 1 + k*(shape_base(y)-1)

    This preserves monotonicity if k>=0 and shape_base is monotone non-increasing.
    """
    ys = np.asarray(ys, dtype=float)
    s0 = np.asarray(chord_shape_base, dtype=float)

    # Ensure root of shape is exactly 1.0
    if len(s0) > 0:
        s0[0] = 1.0

    # Enforce minimum multiplier floor
    s0 = np.maximum(s0, C_MUL_HARD_MIN)

    c_root_ref = float(c_root_ref)
    S_target = float(S_target)

    # target integral of chord_shape over semispan
    I_target = S_target / (2.0 * max(c_root_ref, 1e-12))
    J = float(np.trapezoid(s0 - 1.0, x=ys))

    if abs(J) < 1e-12:
        chord_shape = s0.copy()
    else:
        k = (I_target - float(ys[-1])) / J
        # Require non-negative scaling to preserve monotonic behavior robustly
        k = float(np.clip(k, 0.0, 50.0))
        chord_shape = 1.0 + k * (s0 - 1.0)

    chord_shape = np.maximum(chord_shape, C_MUL_HARD_MIN)
    chord = c_root_ref * chord_shape

    # Hard clamp to prevent pathological tiny chord
    chord = np.maximum(chord, C_ABS_HARD_MIN)
    chord[0] = c_root_ref  # enforce exact root chord

    # Recompute area (should match very closely)
    S_proj = 2.0 * float(np.trapezoid(chord, x=ys))
    return chord, S_proj



def build_geometry_from_design(design):
    ensure_reference_loaded()

    sweep = float(design["SWEEP_LE_DEG"])
    root_tw = float(design["ROOT_TWIST_DEG"])

    x_ctrl = [float(design["X1_OFF"]), float(design["X2_OFF"]), float(design["X3_OFF"])]

    # ---- Chord monotone by construction (non-increasing outboard) ----
    c1 = float(design["C1_MUL"])
    dc2 = float(design["DC2"])
    dc3 = float(design["DC3"])

    # CRITICAL FIX: use C_MUL_HARD_MIN (C_MUL_MIN does not exist anymore)
    c2 = max(C_MUL_HARD_MIN, c1 - max(0.0, dc2))
    c3 = max(C_MUL_HARD_MIN, c2 - max(0.0, dc3))
    c_ctrl = [c1, c2, c3]

    tw_ctrl = [float(design["TW1_DEG"]), float(design["TW2_DEG"]), float(design["TW3_DEG"])]
    z_ctrl_fracs = [float(design[f"Z{i}_FRAC"]) for i in range(1, 6)]

    ys = np.linspace(0.0, Y_TIP, NUM_SECTIONS_PER_SEMISPAN + 1)

    # Reference sweep line
    x_ref = np.tan(np.deg2rad(sweep)) * ys

    # LE offsets (root pinned to 0)
    x_off = pchip_from_root_and_ctrl(
        ys=ys, y_tip=Y_TIP, root_value=0.0,
        y_ctrl_fracs=Y_CTRL_FRACS_3, ctrl_values=x_ctrl
    )
    x_le = x_ref + x_off

    # Base chord shape multipliers (root=1)
    chord_shape_base = pchip_from_root_and_ctrl(
        ys=ys, y_tip=Y_TIP, root_value=1.0,
        y_ctrl_fracs=Y_CTRL_FRACS_3, ctrl_values=c_ctrl
    )
    chord_shape_base = np.maximum(chord_shape_base, C_MUL_HARD_MIN)
    chord_shape_base[0] = 1.0

    # Root chord hard-fixed once reference exists; enforce projected area exactly
    if (CROOT_REF is not None) and np.isfinite(CROOT_REF) and (float(CROOT_REF) > 1e-9):
        chord, S_proj = _build_chord_distribution_fixed_root(
            ys=ys,
            chord_shape_base=chord_shape_base,
            c_root_ref=float(CROOT_REF),
            S_target=float(S_FIXED)
        )
    else:
        # Reference not yet known (first run): fall back to original scale-to-area approach
        denom = 2.0 * np.trapezoid(chord_shape_base, x=ys)
        scale = S_FIXED / max(denom, 1e-12)
        chord = scale * chord_shape_base
        chord = np.maximum(chord, C_ABS_HARD_MIN)
        S_proj = 2.0 * np.trapezoid(chord, x=ys)

    # Twist
    twist = pchip_from_root_and_ctrl(
        ys=ys, y_tip=Y_TIP, root_value=root_tw,
        y_ctrl_fracs=Y_CTRL_FRACS_3, ctrl_values=tw_ctrl
    )

    # Dihedral z(y)
    z_le = z_from_controls(ys, Y_TIP, 0.0, Y_CTRL_FRACS_Z, z_ctrl_fracs)

    # 3D area estimate
    dz_dy = safe_grad(z_le, ys)
    S_3Dest = 2.0 * np.trapezoid(chord * np.sqrt(1.0 + dz_dy**2), x=ys)

    MAC, y_bar, x_le_mac = compute_mac_ybar_xle(ys, chord, x_le, S_full=float(S_proj))

    return dict(
        ys=ys, chord=chord, twist=twist, x_le=x_le, z_le=z_le,
        x_ref=x_ref, x_off=x_off,
        S_proj=float(S_proj), S_3Dest=float(S_3Dest),
        MAC=float(MAC), y_bar=float(y_bar), x_le_mac=float(x_le_mac),
        y_tip=float(Y_TIP), y_cut=float(Y_CUT),
        sweep_le_deg_ref=float(sweep),
        le_offset_max_m=float(np.max(np.abs(x_off))),
        chord_repaired=False,
        taper_eff=float(chord[-1] / max(chord[0], 1e-12))
    )


def build_airplane_from_geom_and_xcg(geom, xcg_frac):
    xcg_frac = float(np.clip(float(xcg_frac), XCG_MIN, XCG_MAX))
    x_cg = float(geom["x_le_mac"] + xcg_frac * geom["MAC"])

    wing_airfoil = asb.Airfoil(AIRFOIL_NAME)
    xsecs = [
        asb.WingXSec(
            xyz_le=[float(xi), float(yi), float(zi)],
            chord=float(ci),
            twist=float(ti),
            airfoil=wing_airfoil
        )
        for xi, yi, zi, ci, ti in zip(geom["x_le"], geom["ys"], geom["z_le"], geom["chord"], geom["twist"])
    ]

    airplane = asb.Airplane(
        name="DE Wing",
        xyz_ref=[x_cg, 0.0, 0.0],
        wings=[asb.Wing(name="Main Wing", symmetric=True, xsecs=xsecs)],
        fuselages=[]
    )
    return airplane, x_cg


def tip_taper_hard_penalty(geom) -> dict:
    """
    Prevent a 'flat' outboard chord plateau (which makes TE ~ parallel to LE near tip).
    Enforce c(y=Y_TAPER_CHECK_FRAC*Ytip) >= TIP_TAPER_RATIO_MIN * c_tip
    """
    ys = np.asarray(geom["ys"], dtype=float)
    c  = np.asarray(geom["chord"], dtype=float)

    yq = float(Y_TAPER_CHECK_FRAC) * float(geom["y_tip"])
    c_q = float(np.interp(yq, ys, c))
    c_tip = float(c[-1])

    if (not np.isfinite(c_q)) or (not np.isfinite(c_tip)) or c_tip <= 1e-9:
        return dict(c_q=float("nan"), c_tip=float("nan"), ratio=float("nan"),
                    penalty=float(W_TIP_TAPER_HARD))

    ratio = c_q / c_tip
    if ratio >= float(TIP_TAPER_RATIO_MIN) - 1e-12:
        return dict(c_q=c_q, c_tip=c_tip, ratio=ratio, penalty=0.0)

    d = float(TIP_TAPER_RATIO_MIN - ratio)
    pen = float(W_TIP_TAPER_HARD * (d / max(float(TIP_TAPER_RATIO_MIN), 1e-12))**2)
    return dict(c_q=c_q, c_tip=c_tip, ratio=ratio, penalty=pen)


def tip_twist_penalty(geom) -> dict:
    """
    Penalize positive (or insufficiently negative) tip twist.
    """
    tw = np.asarray(geom["twist"], dtype=float)
    tip = float(tw[-1]) if len(tw) else float("nan")

    if not np.isfinite(tip):
        return dict(tip_twist_deg=float("nan"), penalty=25.0)

    if tip <= float(TIP_TWIST_MAX_DEG) + 1e-12:
        return dict(tip_twist_deg=tip, penalty=0.0)

    d = float(tip - float(TIP_TWIST_MAX_DEG))
    pen = float(W_TIP_TWIST * (d / 3.0)**2)
    return dict(tip_twist_deg=tip, penalty=pen)


# ============================================================
# VLM RUNS + SOLVERS
# ============================================================
def run_vlm(airplane, velocity, alpha_deg, beta_deg=0.0, coarse=False):
    op = asb.OperatingPoint(velocity=float(velocity), alpha=float(alpha_deg), beta=float(beta_deg))
    if coarse:
        vlm = make_vlm_uniform(airplane, op, SPANWISE_RES_COARSE, CHORDWISE_RES_COARSE)
    else:
        vlm = make_vlm_uniform(airplane, op, SPANWISE_RES_MAIN, CHORDWISE_RES_MAIN)
    aero = vlm.run()
    return vlm, aero, op


def solve_alpha_for_CL(airplane, velocity, CL_target, alpha_lo=-6.0, alpha_hi=16.0, max_iter=12, coarse=False):
    def eval_CL(alpha):
        vlm, aero, _ = run_vlm(airplane, velocity, alpha, beta_deg=0.0, coarse=coarse)
        CL = float(safe_get(aero, ["CL", "C_L"], np.nan))
        return CL, vlm, aero

    CL_lo, _, _ = eval_CL(alpha_lo)
    CL_hi, _, _ = eval_CL(alpha_hi)

    if not (np.isfinite(CL_lo) and np.isfinite(CL_hi)):
        raise RuntimeError("CL bracket eval failed (non-finite).")

    if (CL_lo - CL_target) * (CL_hi - CL_target) > 0:
        alpha_lo2, alpha_hi2 = -12.0, 24.0
        CL_lo, _, _ = eval_CL(alpha_lo2)
        CL_hi, _, _ = eval_CL(alpha_hi2)
        alpha_lo, alpha_hi = alpha_lo2, alpha_hi2
        if (CL_lo - CL_target) * (CL_hi - CL_target) > 0:
            raise RuntimeError(
                f"Could not bracket CL_target={CL_target} (CL@lo={CL_lo:.3f}, CL@hi={CL_hi:.3f})."
            )

    a0, a1 = alpha_lo, alpha_hi
    f0 = CL_lo - CL_target
    f1 = CL_hi - CL_target

    last = None
    for _ in range(max_iter):
        if abs(f1 - f0) < 1e-12:
            break
        a2 = a1 - f1 * (a1 - a0) / (f1 - f0)
        CL2, vlm2, aero2 = eval_CL(a2)
        if not np.isfinite(CL2):
            break
        f2 = CL2 - CL_target
        last = (a2, vlm2, aero2, f2)
        if abs(f2) < 5e-3:
            return float(a2), (vlm2, aero2)
        a0, f0 = a1, f1
        a1, f1 = a2, f2

    if last is None:
        raise RuntimeError("Alpha solve failed.")
    return float(a1), (last[1], last[2])


def compute_slopes_and_SM_about_alpha(airplane, velocity, alpha_center_deg, dalpha_deg=SLOPE_DALPHA_DEG):
    a0 = float(alpha_center_deg)
    da = float(dalpha_deg)
    if da <= 0:
        da = 1.0

    _, aero_p, _ = run_vlm(airplane, velocity, a0 + da, beta_deg=0.0, coarse=True)
    _, aero_m, _ = run_vlm(airplane, velocity, a0 - da, beta_deg=0.0, coarse=True)

    CL_p = float(safe_get(aero_p, ["CL", "C_L"], np.nan))
    CL_m = float(safe_get(aero_m, ["CL", "C_L"], np.nan))
    Cm_p = float(safe_get(aero_p, ["Cm", "C_m", "CM"], np.nan))
    Cm_m = float(safe_get(aero_m, ["Cm", "C_m", "CM"], np.nan))

    da_rad = np.deg2rad(da)
    if not (np.isfinite(CL_p) and np.isfinite(CL_m) and np.isfinite(Cm_p) and np.isfinite(Cm_m)) or abs(da_rad) < 1e-12:
        return np.nan, np.nan, np.nan

    CLalpha = float((CL_p - CL_m) / (2.0 * da_rad))
    CMalpha = float((Cm_p - Cm_m) / (2.0 * da_rad))

    if not np.isfinite(CLalpha) or abs(CLalpha) < 1e-9 or not np.isfinite(CMalpha):
        return np.nan, CLalpha, CMalpha

    SM = float(-CMalpha / CLalpha)
    return SM, CLalpha, CMalpha


def solve_cg_for_sm(geom, velocity, sm_target=SM_TARGET):
    x0 = float(XCG_INIT)
    airplane0, _ = build_airplane_from_geom_and_xcg(geom, x0)

    alpha_star_c, _ = solve_alpha_for_CL(
        airplane0, velocity, CL_TARGET,
        max_iter=ALPHA_SOLVE_MAXITER_COARSE,
        coarse=True
    )

    def eval_SM_at_xcg(xcg_frac):
        ap, _ = build_airplane_from_geom_and_xcg(geom, xcg_frac)
        SM, CLalpha, CMalpha = compute_slopes_and_SM_about_alpha(ap, velocity, alpha_star_c, dalpha_deg=SLOPE_DALPHA_DEG)
        return SM, CLalpha, CMalpha

    SM0, CLalpha0, CMalpha0 = eval_SM_at_xcg(x0)
    if (not np.isfinite(SM0)) or (not np.isfinite(geom["MAC"])) or geom["MAC"] <= 1e-9:
        clamped0 = (x0 <= XCG_MIN + 1e-12) or (x0 >= XCG_MAX - 1e-12)
        return x0, SM0, CLalpha0, CMalpha0, clamped0, float(alpha_star_c)

    x1 = float(np.clip(x0 + (SM0 - sm_target), XCG_MIN, XCG_MAX))
    SM1, CLalpha1, CMalpha1 = eval_SM_at_xcg(x1)

    if np.isfinite(SM1) and abs(SM1 - sm_target) <= CG_SM_TOL:
        clamped1 = (x1 <= XCG_MIN + 1e-12) or (x1 >= XCG_MAX - 1e-12)
        return x1, SM1, CLalpha1, CMalpha1, clamped1, float(alpha_star_c)

    xa, fa = x0, (SM0 - sm_target)
    xb, fb = x1, (SM1 - sm_target)
    best = (x1, SM1, CLalpha1, CMalpha1)

    for _ in range(max(CG_MAX_ITERS - 2, 0)):
        if not (np.isfinite(fa) and np.isfinite(fb)) or abs(fb - fa) < 1e-9:
            break
        x2 = xb - fb * (xb - xa) / (fb - fa)
        x2 = float(np.clip(x2, XCG_MIN, XCG_MAX))
        SM2, CLalpha2, CMalpha2 = eval_SM_at_xcg(x2)
        if np.isfinite(SM2):
            best = (x2, SM2, CLalpha2, CMalpha2)
            if abs(SM2 - sm_target) <= CG_SM_TOL:
                xb, fb = x2, (SM2 - sm_target)
                break
            xa, fa = xb, fb
            xb, fb = x2, (SM2 - sm_target)
        else:
            break

    x_best, SM_best, CLalpha_best, CMalpha_best = best
    clamped = (x_best <= XCG_MIN + 1e-12) or (x_best >= XCG_MAX - 1e-12)
    return float(x_best), float(SM_best), float(CLalpha_best), float(CMalpha_best), bool(clamped), float(alpha_star_c)


def compute_lateral_derivatives(airplane, velocity, alpha_deg):
    db = float(BETA_STEP_DEG)
    _, aero_p, _ = run_vlm(airplane, velocity, alpha_deg, beta_deg=+db, coarse=True)
    _, aero_m, _ = run_vlm(airplane, velocity, alpha_deg, beta_deg=-db, coarse=True)

    Cn_p = float(safe_get(aero_p, ["Cn", "C_n"], np.nan))
    Cn_m = float(safe_get(aero_m, ["Cn", "C_n"], np.nan))
    Cl_p = float(safe_get(aero_p, ["Cl", "C_l"], np.nan))
    Cl_m = float(safe_get(aero_m, ["Cl", "C_l"], np.nan))

    db_rad = np.deg2rad(db)
    Cn_beta = (Cn_p - Cn_m) / (2 * db_rad)
    Cl_beta = (Cl_p - Cl_m) / (2 * db_rad)
    return float(Cn_beta), float(Cl_beta)

# ============================================================
# 2D profile drag (NeuralFoil if available; fallback-safe)
# ============================================================
def estimate_profile_cd_2d(geom, alpha_star_deg, velocity_mps, atmosphere):
    ys = np.asarray(geom["ys"], dtype=float)
    c  = np.asarray(geom["chord"], dtype=float)
    tw = np.asarray(geom["twist"], dtype=float)

    try:
        rho = float(atmosphere.density())
    except Exception:
        rho = 1.225
    try:
        mu = float(atmosphere.dynamic_viscosity())
    except Exception:
        mu = 1.81e-5
    try:
        a = float(atmosphere.speed_of_sound())
    except Exception:
        a = 340.0

    V = float(velocity_mps)
    mach = abs(V) / max(a, 1e-9)

    alpha_eff_deg = float(alpha_star_deg) + tw
    Re = rho * V * np.maximum(c, 1e-6) / max(mu, 1e-12)

    af = asb.Airfoil(AIRFOIL_NAME)
    Cd2d = np.full_like(ys, np.nan, dtype=float)
    ok = False
    try:
        out = af.get_aero_from_neuralfoil(alpha=alpha_eff_deg, Re=Re, mach=mach)
        if isinstance(out, dict):
            if "Cd" in out:
                Cd2d = np.asarray(out["Cd"], dtype=float); ok = True
            elif "CD" in out:
                Cd2d = np.asarray(out["CD"], dtype=float); ok = True
    except Exception:
        ok = False

    if (not ok) or (not np.all(np.isfinite(Cd2d))):
        return float(0.010)

    integrand = Cd2d * c
    CD_profile = (2.0 / max(float(geom["S_proj"]), 1e-12)) * float(np.trapezoid(integrand, x=ys))
    if (not np.isfinite(CD_profile)) or (CD_profile <= 0):
        CD_profile = 0.010
    return float(CD_profile)


# ============================================================
# PLOTS / ARTIFACTS
# ============================================================
def save_lift_plot(eval_dir, y, Lp, cl):
    y = np.asarray(y, dtype=float)
    Lp = np.asarray(Lp, dtype=float)
    cl = np.asarray(cl, dtype=float)

    ok = np.isfinite(y) & np.isfinite(Lp) & np.isfinite(cl)
    y, Lp, cl = y[ok], Lp[ok], cl[ok]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 8), sharex=True)
    if len(y) >= 4 and np.all(np.diff(y) > 0):
        y_f = np.linspace(y.min(), y.max(), 200)
        Lp_s = PchipInterpolator(y, Lp)(y_f)
        cl_s = PchipInterpolator(y, cl)(y_f)
        ax1.plot(y_f, Lp_s, "-", lw=1.5); ax1.plot(y, Lp, "o", ms=3)
        ax2.plot(y_f, cl_s, "-", lw=1.5); ax2.plot(y, cl, "o", ms=3)
    else:
        ax1.plot(y, Lp, "-o", lw=1, ms=3)
        ax2.plot(y, cl, "-o", lw=1, ms=3)

    ax1.set_ylabel("L'(y) [N/m]"); ax1.grid(True, linestyle=":", linewidth=0.8)
    ax2.set_xlabel("y [m] (right semispan, masked; includes tip=0)")
    ax2.set_ylabel("c_l(y) [-]"); ax2.grid(True, linestyle=":", linewidth=0.8)

    fig.suptitle("Spanwise Lift Distribution (Right Semispan, y>=Y_CUT) — tip forced to 0")
    fig.tight_layout()
    fig.savefig(os.path.join(eval_dir, "lift_distribution.png"), dpi=160)
    plt.close(fig)


def save_wing_views(eval_dir, geom):
    ys = np.asarray(geom["ys"], dtype=float)
    x_ref = np.asarray(geom["x_ref"], dtype=float)
    x_le = np.asarray(geom["x_le"], dtype=float)
    z_le = np.asarray(geom["z_le"], dtype=float)
    c = np.asarray(geom["chord"], dtype=float)
    tw_rad = np.deg2rad(np.asarray(geom["twist"], dtype=float))

    x_te = x_le + c * np.cos(tw_rad)
    z_te = z_le - c * np.sin(tw_rad)

    y_full = np.concatenate([-ys[::-1], ys[1:]])
    xref_full = np.concatenate([x_ref[::-1], x_ref[1:]])
    xle_full  = np.concatenate([x_le[::-1], x_le[1:]])
    xte_full  = np.concatenate([x_te[::-1], x_te[1:]])
    zle_full  = np.concatenate([z_le[::-1], z_le[1:]])
    zte_full  = np.concatenate([z_te[::-1], z_te[1:]])

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))

    ax1.plot(y_full, xref_full, "--", lw=1.5, label="Ref sweep line (LE)")
    ax1.plot(y_full, xle_full, "-", lw=2.0, label="Actual LE")
    ax1.plot(y_full, xte_full, "-", lw=2.0, label="TE (includes cos(twist))")
    ax1.set_aspect("equal", adjustable="box")
    ax1.set_xlabel("y [m]"); ax1.set_ylabel("x [m]")
    ax1.set_title("Top view (x-y)")
    ax1.grid(True, linestyle=":", linewidth=0.8)
    ax1.legend()

    ax2.plot(y_full, zle_full, "-", lw=2.0, label="LE")
    ax2.plot(y_full, zte_full, "-", lw=2.0, label="TE (shows twist)")
    ax2.axhline(0.0, lw=1)
    ax2.set_aspect("equal", adjustable="box")
    ax2.set_xlabel("y [m]"); ax2.set_ylabel("z [m]")
    ax2.set_title("Front view (y-z)")
    ax2.grid(True, linestyle=":", linewidth=0.8)
    ax2.legend()

    fig.suptitle("Wing 2-View Drawing (ref sweep + twist-visible front view)")
    fig.tight_layout()
    fig.savefig(os.path.join(eval_dir, "wing_views.png"), dpi=170)
    plt.close(fig)


def screenshot_structure(eval_dir, geom, y_struct_abs, w_def, phi_rad, sigma_vm):
    if pv is None:
        return

    y_struct_abs = np.asarray(y_struct_abs, dtype=float)
    y_plot = np.linspace(float(y_struct_abs[0]), float(geom["y_tip"]), 80)

    ys = geom["ys"]
    x_le = geom["x_le"]
    z_le = geom["z_le"]
    twist_deg = geom["twist"]

    xle_p   = np.interp(y_plot, ys, x_le)
    zle_p   = np.interp(y_plot, ys, z_le)
    twist_p = np.deg2rad(np.interp(y_plot, ys, twist_deg))
    chord_p = np.interp(y_plot, ys, geom["chord"])

    w_p   = np.interp(y_plot, y_struct_abs, w_def)
    phi_p = np.interp(y_plot, y_struct_abs, phi_rad)
    sig_p = np.interp(y_plot, y_struct_abs, sigma_vm)

    if CLAMP_TORSION_FOR_PLOT:
        phi_p = np.clip(phi_p, -np.deg2rad(PHI_VIS_CLAMP_DEG), np.deg2rad(PHI_VIS_CLAMP_DEG))

    n_span = len(y_plot)
    n_chord = 30
    xi = np.linspace(0.0, 1.0, n_chord)

    def make_surface(deflected=False):
        X = np.zeros((n_chord, n_span))
        Y = np.zeros_like(X)
        Z = np.zeros_like(X)
        for j in range(n_span):
            c  = chord_p[j]
            tw = twist_p[j] + (phi_p[j] if deflected else 0.0)
            x0 = xle_p[j]
            z0 = zle_p[j] + ((DEFLECTION_SCALE_VIS * w_p[j]) if deflected else 0.0)
            X[:, j] = x0 + xi * c * np.cos(tw)
            Y[:, j] = y_plot[j]
            Z[:, j] = z0 + xi * c * np.sin(tw)
        return pv.StructuredGrid(X[:, :, None], Y[:, :, None], Z[:, :, None])

    grid_undef = make_surface(False)
    grid_def   = make_surface(True)

    stress_grid = np.tile(sig_p[None, :], (n_chord, 1))
    grid_def["von_mises_Pa"] = stress_grid.ravel(order="F")

    p = pv.Plotter(off_screen=True, window_size=(1400, 900))
    p.add_mesh(grid_undef, color="gray", style="wireframe", opacity=0.35)
    p.add_mesh(grid_def, scalars="von_mises_Pa", cmap="viridis", show_edges=False, opacity=0.95)
    p.add_scalar_bar(title=f"von Mises [Pa] (defl x{DEFLECTION_SCALE_VIS:g})")
    p.add_axes()
    p.show_bounds(grid="back", location="outer", all_edges=True)

    try:
        p.screenshot(os.path.join(eval_dir, "structure_view.png"))
    except Exception:
        pass
    try:
        p.close()
    except Exception:
        pass


# ============================================================
# OBJECTIVE HELPERS (ANTI-THIN + SM + NEW HARD CONSTRAINTS)
# ============================================================
def chord_increase_penalty(chord):
    c = np.asarray(chord, dtype=float)
    dc = np.diff(c)
    inc = np.maximum(0.0, dc)  # positive means chord increases outboard
    if not np.any(inc > 0):
        return 0.0
    # Normalize by root chord so the weight is meaningful across scales
    return float(W_CHORD_MONO * np.mean((inc / max(c[0], 1e-12))**2))


def static_margin_target_penalty(SM):
    if not np.isfinite(SM):
        return 10.0
    err = abs(SM - SM_TARGET) - SM_DEADBAND
    if err <= 0:
        return 0.0
    return (err / max(SM_SCALE, 1e-12)) ** 2


def thin_chord_penalty(geom) -> float:
    """
    Penalize thin wings in the effective region:
      - starts at Y_CUT
      - stops at 90% semispan (allow sharp taper to the tip)
    """
    ys = np.asarray(geom["ys"], dtype=float)
    c  = np.asarray(geom["chord"], dtype=float)

    mask = (ys >= float(Y_CUT) - 1e-12) & (ys <= 0.90 * float(Y_TIP) + 1e-12)
    if np.sum(mask) < 2:
        return 0.0

    c_eff = c[mask]
    deficit = np.maximum(0.0, float(C_ABS_MIN_EFF) - c_eff)
    if not np.any(deficit > 0):
        return 0.0

    dn = deficit / max(float(C_ABS_MIN_EFF), 1e-12)
    mean_dn = float(np.mean(dn))
    max_dn  = float(np.max(dn))

    return float(W_THIN_CHORD * (mean_dn**2 + 0.5 * max_dn**2))



def midspan_chord_hard_penalty(geom) -> dict:
    """
    HARD constraint: chord(y=Y_MID_CONSTRAINT) >= C_MID_MIN.
    Implemented as a huge penalty so DE treats it as infeasible.
    """
    ys = np.asarray(geom["ys"], dtype=float)
    c  = np.asarray(geom["chord"], dtype=float)

    yq = float(Y_MID_CONSTRAINT)
    c_mid = float(np.interp(yq, ys, c))

    if (not np.isfinite(c_mid)) or (c_mid < 0):
        return dict(c_mid=float("nan"), penalty=float(W_MID_CHORD_HARD))

    if c_mid >= float(C_MID_MIN) - 1e-12:
        return dict(c_mid=c_mid, penalty=0.0)

    d = float(C_MID_MIN - c_mid)
    pen = float(W_MID_CHORD_HARD * (d / max(float(C_MID_MIN), 1e-12))**2)
    return dict(c_mid=c_mid, penalty=pen)


def xmax_hard_penalty(geom) -> dict:
    """
    HARD constraint: max x extent must be <= X_MAX_LIMIT.
    Use conservative TE estimate: x_max = max(x_le + chord).
    """
    x_le = np.asarray(geom["x_le"], dtype=float)
    c    = np.asarray(geom["chord"], dtype=float)

    x_max = float(np.max(x_le + c)) if len(x_le) else float("nan")

    if (not np.isfinite(x_max)):
        return dict(x_max=float("nan"), penalty=float(W_XMAX_HARD))

    if x_max <= float(X_MAX_LIMIT) + 1e-12:
        return dict(x_max=x_max, penalty=0.0)

    d = float(x_max - float(X_MAX_LIMIT))
    pen = float(W_XMAX_HARD * (d / max(float(X_MAX_LIMIT), 1e-12))**2)
    return dict(x_max=x_max, penalty=pen)


def compute_objective(metrics):
    global STR_ENERGY_REF, SIGMA_REF, SIGMA_CV_REF

    tip_tw_pen = float(metrics.get("tip_twist_penalty", 0.0))
    tip_taper_pen = float(metrics.get("tip_taper_penalty", 0.0))

    LD = metrics["LD"]
    M_root = metrics["M_root_at_ycut_Nm"]
    threeD_pen = metrics["threeD_penalty"]
    SM = metrics["static_margin"]
    Cm_star = metrics["Cm_at_alpha_star"]
    CMalpha = metrics["CMalpha_per_rad"]

    sanity_pen = metrics.get("sanity_penalty", 0.0)
    stab_con_pen = metrics.get("stability_constraint_penalty", 0.0)
    thin_pen = metrics.get("thin_chord_penalty", 0.0)
    mono_pen = float(metrics.get("chord_increase_penalty", 0.0))

    mid_pen  = float(metrics.get("midspan_chord_penalty", 0.0))
    xmax_pen = float(metrics.get("xmax_penalty", 0.0))

    # NEW: global min chord hard penalty
    globalmin_pen = float(metrics.get("global_min_chord_penalty", 0.0))

    U_total = metrics.get("structure", {}).get("U_total", np.nan)
    sigma_peak = metrics.get("structure", {}).get("sigma_peak_Pa", np.nan)
    sigma_cv = metrics.get("structure", {}).get("sigma_cv", np.nan)

    energy_ref = STR_ENERGY_REF if (STR_ENERGY_REF is not None and STR_ENERGY_REF > 1e-12) else 1.0
    sigma_ref  = SIGMA_REF if (SIGMA_REF is not None and SIGMA_REF > 1e-12) else 1.0
    sigcv_ref  = SIGMA_CV_REF if (SIGMA_CV_REF is not None and SIGMA_CV_REF > 1e-6) else 0.10

    LD_term = 10.0 if not np.isfinite(LD) else -(LD / max(LD_REF, 1e-12))
    mroot_term = abs(M_root) / max(MROOT_REF, 1e-12)
    threeD_term = threeD_pen / max(THREED_PEN_REF, 1e-12)
    trim_term = abs(Cm_star) / max(CM_TRIM_REF, 1e-12) if np.isfinite(Cm_star) else 10.0

    if not np.isfinite(CMalpha):
        cma_pen = 5.0
    elif CMalpha >= 0.0:
        cma_pen = (1.0 + CMalpha / 0.2) ** 2
    else:
        cma_pen = 0.0

    sm_pen = static_margin_target_penalty(SM)

    energy_term = (U_total / max(energy_ref, 1e-12)) if np.isfinite(U_total) else 10.0
    sigma_term  = (sigma_peak / max(sigma_ref, 1e-12)) if np.isfinite(sigma_peak) else 10.0
    sigcv_term  = (sigma_cv / max(sigcv_ref, 1e-6)) if np.isfinite(sigma_cv) else 10.0

    return float(
        W_LD * LD_term +
        W_MROOT * mroot_term +
        W_3D * threeD_term +
        W_TRIM * trim_term +
        W_CMA_STAB * cma_pen +
        W_SM_TGT * sm_pen +
        W_SANITY * sanity_pen +
        stab_con_pen +
        thin_pen +
        mono_pen +
        mid_pen +
        xmax_pen +
        globalmin_pen +          # <-- NEW
        W_STR_ENERGY * energy_term +
        W_SIG_PEAK * sigma_term +
        W_SIG_UNIFORM * sigcv_term +
        tip_tw_pen +
        tip_taper_pen
    )


def global_min_chord_hard_penalty(geom) -> dict:
    """
    HARD constraint: if chord(y) anywhere is < C_GLOBAL_MIN, apply huge penalty.
    Implemented as a very large penalty so DE treats it as infeasible.
    """
    c = np.asarray(geom["chord"], dtype=float)
    if c.size == 0 or (not np.all(np.isfinite(c))):
        return dict(c_min=float("nan"), penalty=float(W_GLOBAL_CHORD_HARD))

    c_min = float(np.min(c))
    if c_min >= float(C_GLOBAL_MIN) - 1e-12:
        return dict(c_min=c_min, penalty=0.0)

    d = float(C_GLOBAL_MIN - c_min)
    pen = float(W_GLOBAL_CHORD_HARD * (d / max(float(C_GLOBAL_MIN), 1e-12))**2)
    return dict(c_min=c_min, penalty=pen)

# ============================================================
# EVALUATION
# ============================================================
def evaluate_design(design, eval_id, velocity=V_DESIGN):
    ensure_reference_loaded()

    eval_dir = os.path.join(RESULTS_ROOT, f"eval_{eval_id:05d}")
    os.makedirs(eval_dir, exist_ok=True)

    geom = build_geometry_from_design(design)

    mono_pen = chord_increase_penalty(geom["chord"])

    # NEW washout + tip plateau penalties
    twpen = tip_twist_penalty(geom)
    tappen = tip_taper_hard_penalty(geom)

    tip_tw_pen = float(twpen["penalty"])
    tip_taper_pen = float(tappen["penalty"])
    tip_tw_deg = float(twpen.get("tip_twist_deg", float("nan")))
    tip_ratio = float(tappen.get("ratio", float("nan")))

    # --- hard constraints as huge penalties ---
    midc = midspan_chord_hard_penalty(geom)
    xmx  = xmax_hard_penalty(geom)
    mid_pen = float(midc["penalty"])
    xmax_pen = float(xmx["penalty"])
    c_mid = float(midc.get("c_mid", float("nan")))
    x_max = float(xmx.get("x_max", float("nan")))

    # --- NEW: global minimum chord hard constraint ---
    gmin = global_min_chord_hard_penalty(geom)
    globalmin_pen = float(gmin["penalty"])
    c_min_global = float(gmin.get("c_min", float("nan")))

    # Export STL (never fail eval if STL fails)
    try:
        export_wing_stl(eval_dir, geom, n_airfoil=STL_N_AIRFOIL)
    except Exception:
        pass

    # CG solve
    xcg_frac, SM, CLalpha, CMalpha, cg_clamped, alpha_star_coarse = solve_cg_for_sm(geom, velocity, SM_TARGET)
    airplane, x_cg = build_airplane_from_geom_and_xcg(geom, xcg_frac)

    # Main alpha solve
    alpha_star_deg, (vlm, aero) = solve_alpha_for_CL(
        airplane, velocity, CL_TARGET, max_iter=ALPHA_SOLVE_MAXITER_MAIN, coarse=False
    )

    CL = float(safe_get(aero, ["CL", "C_L"], np.nan))
    Cm_star = float(safe_get(aero, ["Cm", "C_m", "CM"], np.nan))
    CDi_raw = float(safe_get(aero, ["CDi", "C_Di", "CD_induced", "CD"], np.nan))

    (y_cent, L_prime, cl_local, q, CL_from_bins,
     y_aug, L_aug, cl_aug, chord_aug) = compute_semispan_lift_distribution_binned(
        vlm, geom["y_tip"], SPANWISE_RES_MAIN, geom["ys"], geom["chord"], geom["S_proj"]
    )

    ll = compute_cdi_from_lift_distribution_LL(
        y_semispan=y_aug, Lprime_semispan=L_aug, q=q, V=velocity, b_full=B_FULL, AR=AR_FIXED
    )
    CDi_ll = float(ll.get("CDi", np.nan))
    e_ll = float(ll.get("e", np.nan))
    CL_ll = float(ll.get("CL_LL", np.nan))

    CDi_elliptic = float((CL**2) / (np.pi * max(AR_FIXED, 1e-12))) if np.isfinite(CL) else float("nan")

    cd_ll_bad = False
    if (not np.isfinite(CDi_ll)) or (CDi_ll <= 0.0):
        cd_ll_bad = True
        CDi_ll = CDi_elliptic

    CDi_used = float(CDi_ll) if np.isfinite(CDi_ll) else float("nan")

    # Structure region: y >= Y_CUT
    mask = y_aug >= float(Y_CUT)
    if np.sum(mask) < 4:
        mask = np.ones_like(y_aug, dtype=bool)

    y_abs = y_aug[mask]
    L_eff = L_aug[mask]
    cl_eff = cl_aug[mask]
    c_eff = chord_aug[mask]
    y_eff = y_abs - float(y_abs[0])

    struct = structural_response_1D(y_eff, L_eff, c_eff)
    M_root_at_ycut = float(struct["M"][0])

    Cn_beta, Cl_beta = compute_lateral_derivatives(airplane, velocity, alpha_star_deg)
    stab_con_pen, stab_ok, v_cnb, v_clb = stability_constraint_penalty(Cn_beta, Cl_beta)

    atmo = asb.Atmosphere(altitude=ALTITUDE_M)
    CD_profile = float(estimate_profile_cd_2d(geom, alpha_star_deg, velocity, atmo))

    # CL-binning sanity
    cl_sanity_pen = 0.0
    cl_bins_bad = False
    if np.isfinite(CL) and np.isfinite(CL_from_bins):
        if abs(CL_from_bins - CL) > CL_BINS_MISMATCH_TOL:
            cl_bins_bad = True
            cl_sanity_pen += CL_BINS_MISMATCH_PEN
    else:
        cl_bins_bad = True
        cl_sanity_pen += CL_BINS_MISMATCH_PEN

    CD_total = CD_profile + float(max(CDi_used, 0.0)) if np.isfinite(CDi_used) else float("nan")
    LD = float(CL / CD_total) if np.isfinite(CL) and np.isfinite(CD_total) and CD_total > 1e-12 else np.nan

    threeD_penalty = float((geom["S_3Dest"] / max(geom["S_proj"], 1e-12)) - 1.0)
    sanity_penalty = float(cl_sanity_pen)

    # Anti-thin penalty (your existing effective-region soft penalty)
    thin_pen = float(thin_chord_penalty(geom))

    # Extra debug: min chord in effective region
    ys_g = np.asarray(geom["ys"], dtype=float)
    c_g  = np.asarray(geom["chord"], dtype=float)
    mask_g = ys_g >= float(Y_CUT) - 1e-12
    cmin_eff = float(np.min(c_g[mask_g])) if np.any(mask_g) else float(np.min(c_g))

    metrics = {
        "eval_id": int(eval_id),
        "eval_folder": os.path.basename(eval_dir),
        "eval_path": eval_dir,
        "design": design,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),

        "V_mps": float(velocity),
        "S_fixed_m2": float(S_FIXED),
        "span_full_m": float(B_FULL),
        "AR": float(AR_FIXED),

        "alpha_star_deg": float(alpha_star_deg),
        "alpha_star_coarse_deg_for_SM": float(alpha_star_coarse),
        "CL_target": float(CL_TARGET),
        "CL_at_alpha_star": float(CL) if np.isfinite(CL) else float("nan"),

        "CDi_raw_vlm": float(CDi_raw) if np.isfinite(CDi_raw) else float("nan"),
        "CDi_from_liftdist_LL": float(CDi_ll) if np.isfinite(CDi_ll) else float("nan"),
        "CDi_elliptic_e1": float(CDi_elliptic) if np.isfinite(CDi_elliptic) else float("nan"),
        "e_from_liftdist_LL": float(e_ll) if np.isfinite(e_ll) else float("nan"),
        "CL_from_liftdist_LL": float(CL_ll) if np.isfinite(CL_ll) else float("nan"),
        "CD_induced_used": float(CDi_used) if np.isfinite(CDi_used) else float("nan"),
        "cd_ll_bad": bool(cd_ll_bad),

        "CD_profile": float(CD_profile),
        "CD_total": float(CD_total) if np.isfinite(CD_total) else float("nan"),
        "LD": float(LD) if np.isfinite(LD) else float("nan"),

        "CL_from_bins": float(CL_from_bins) if np.isfinite(CL_from_bins) else float("nan"),
        "cl_bins_bad": bool(cl_bins_bad),

        "y_cut_m": float(Y_CUT),
        "M_root_at_ycut_Nm": float(M_root_at_ycut),

        "Cm_at_alpha_star": float(Cm_star) if np.isfinite(Cm_star) else float("nan"),
        "threeD_penalty": float(threeD_penalty),

        "Cn_beta_per_rad": float(Cn_beta),
        "Cl_beta_per_rad": float(Cl_beta),

        "stability_constraints": {
            "Cn_beta_min_per_rad": float(CNBETA_MIN) if CNBETA_MIN is not None else None,
            "Cl_beta_max_per_rad": float(CLBETA_MAX) if CLBETA_MAX is not None else None,
            "Cn_beta_violation": float(v_cnb),
            "Cl_beta_violation": float(v_clb),
            "ok": bool(stab_ok),
            "penalty": float(stab_con_pen),
        },
        "stability_constraint_penalty": float(stab_con_pen),

        "static_margin": float(SM) if np.isfinite(SM) else float("nan"),
        "CLalpha_per_rad": float(CLalpha) if np.isfinite(CLalpha) else float("nan"),
        "CMalpha_per_rad": float(CMalpha) if np.isfinite(CMalpha) else float("nan"),

        "cg_solve": {
            "xcg_frac_MAC": float(xcg_frac),
            "x_cg_m": float(x_cg),
            "clamped": bool(cg_clamped),
            "xcg_min": float(XCG_MIN),
            "xcg_max": float(XCG_MAX),
            "SM_target": float(SM_TARGET),
            "SM_tol_used": float(CG_SM_TOL),
            "slope_dalpha_deg": float(SLOPE_DALPHA_DEG),
        },

        "geom": {
            "S_proj_m2": float(geom["S_proj"]),
            "S_3Dest_m2": float(geom["S_3Dest"]),
            "MAC_m": float(geom["MAC"]),
            "x_le_mac_m": float(geom["x_le_mac"]),
            "c_root_m": float(geom["chord"][0]),
            "c_tip_m": float(geom["chord"][-1]),
            "c_min_eff_m": float(cmin_eff),
            "taper_eff": float(geom["taper_eff"]),
            "sweep_le_deg_ref": float(geom["sweep_le_deg_ref"]),
            "le_offset_max_m": float(geom["le_offset_max_m"]),
            "c_root_ref_m": float(CROOT_REF) if (CROOT_REF is not None and np.isfinite(CROOT_REF)) else None,
            "c_root_tol_m": float(CROOT_TOL),
            "c_abs_min_eff_m": float(C_ABS_MIN_EFF),
            "c_abs_hard_min_m": float(C_ABS_HARD_MIN),

            "y_mid_constraint_m": float(Y_MID_CONSTRAINT),
            "c_mid_m": float(c_mid) if np.isfinite(c_mid) else float("nan"),
            "c_mid_min_m": float(C_MID_MIN),
            "x_max_m": float(x_max) if np.isfinite(x_max) else float("nan"),
            "x_max_limit_m": float(X_MAX_LIMIT),

            # NEW debug fields
            "c_min_global_m": float(c_min_global) if np.isfinite(c_min_global) else float("nan"),
            "c_global_min_limit_m": float(C_GLOBAL_MIN),
        },

        "structure": {
            "U_bend": float(struct["U_bend"]),
            "U_tors": float(struct["U_tors"]),
            "U_total": float(struct["U_total"]),
            "sigma_peak_Pa": float(struct["sigma_peak"]),
            "sigma_cv": float(struct["sigma_cv"]) if np.isfinite(struct["sigma_cv"]) else float("nan"),
            "w_tip_m": float(struct["w_def"][-1]) if len(struct["w_def"]) else float("nan"),
            "phi_tip_rad": float(struct["phi"][-1]) if len(struct["phi"]) else float("nan"),
        },

        "sanity": {
            "cl_sanity_pen": float(cl_sanity_pen),
            "sanity_penalty": float(sanity_penalty),
        },
        "sanity_penalty": float(sanity_penalty),

        "thin_chord_penalty": float(thin_pen),
        "chord_increase_penalty": float(mono_pen),

        "midspan_chord_penalty": float(mid_pen),
        "xmax_penalty": float(xmax_pen),

        "global_min_chord_penalty": float(globalmin_pen),

        "tip_twist_deg": float(tip_tw_deg) if np.isfinite(tip_tw_deg) else float("nan"),
        "tip_twist_penalty": float(tip_tw_pen),
        "tip_taper_ratio": float(tip_ratio) if np.isfinite(tip_ratio) else float("nan"),
        "tip_taper_penalty": float(tip_taper_pen),
    }

    metrics["objective"] = compute_objective(metrics)

    save_lift_plot(eval_dir, y_abs, L_eff, cl_eff)
    save_wing_views(eval_dir, geom)
    screenshot_structure(eval_dir, geom, y_abs, struct["w_def"], struct["phi"], struct["sigma_vm"])

    with open(os.path.join(eval_dir, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    return metrics



def objective_wrapper(x, velocity=V_DESIGN):
    ensure_reference_loaded()

    eval_id = allocate_eval_id()
    design = vec_to_design(x)

    try:
        metrics = evaluate_design(design, eval_id=eval_id, velocity=velocity)
        obj = float(metrics["objective"])
    except Exception as e:
        eval_dir = os.path.join(RESULTS_ROOT, f"eval_{eval_id:05d}")
        os.makedirs(eval_dir, exist_ok=True)
        with open(os.path.join(eval_dir, "FAILED.txt"), "w", encoding="utf-8") as f:
            f.write(str(e))
        return 1e9

    update_best_so_far_locked(metrics)

    sm  = metrics.get("static_margin", float("nan"))
    cm  = metrics.get("Cm_at_alpha_star", float("nan"))
    cma = metrics.get("CMalpha_per_rad", float("nan"))
    cnb = metrics.get("Cn_beta_per_rad", float("nan"))
    clb = metrics.get("Cl_beta_per_rad", float("nan"))
    ld  = metrics.get("LD", float("nan"))
    m0  = metrics.get("M_root_at_ycut_Nm", float("nan"))
    xcg = metrics.get("cg_solve", {}).get("xcg_frac_MAC", float("nan"))

    clamped = metrics.get("cg_solve", {}).get("clamped", False)
    clbad   = metrics.get("cl_bins_bad", False)
    stab_ok = metrics.get("stability_constraints", {}).get("ok", True)
    cdllbad = metrics.get("cd_ll_bad", False)

    # NEW constraint flags
    midpen = metrics.get("midspan_chord_penalty", 0.0)
    xmaxpen = metrics.get("xmax_penalty", 0.0)

    tags = []
    if clamped: tags.append("CGCLAMP")
    if clbad: tags.append("CLBINBAD")
    if not stab_ok: tags.append("STABCONSTR")
    if cdllbad: tags.append("CDLLBAD")
    if metrics.get("thin_chord_penalty", 0.0) > 1e-9: tags.append("THINPEN")
    if midpen > 1e-6: tags.append("MIDCHORD")
    if xmaxpen > 1e-6: tags.append("XMAX")
    if metrics.get("tip_twist_penalty", 0.0) > 1e-6: tags.append("TIPTW")
    if metrics.get("tip_taper_penalty", 0.0) > 1e-6: tags.append("TIPTAPER")
    tag_str = (" [" + ",".join(tags) + "]") if tags else ""
    gminpen = metrics.get("global_min_chord_penalty", 0.0)
    if gminpen > 1e-6: tags.append("C<0.06")

    U = metrics.get("structure", {}).get("U_total", float("nan"))
    sigp = metrics.get("structure", {}).get("sigma_peak_Pa", float("nan"))
    sigcv = metrics.get("structure", {}).get("sigma_cv", float("nan"))
    cdi_used = metrics.get("CD_induced_used", float("nan"))
    e_ll = metrics.get("e_from_liftdist_LL", float("nan"))
    cmin_eff = metrics.get("geom", {}).get("c_min_eff_m", float("nan"))
    c_mid = metrics.get("geom", {}).get("c_mid_m", float("nan"))
    x_max = metrics.get("geom", {}).get("x_max_m", float("nan"))

    print(
        f"[DE eval {eval_id:05d}] obj={obj:+.4f} | LD={ld:.2f} | "
        f"|M(y={Y_CUT:.2f})|={abs(m0):.3f} | "
        f"Cnβ={cnb:+.5f} | Clβ={clb:+.5f} | "
        f"CDi_LL={cdi_used:.6f} | e_LL={e_ll:.3f} | "
        f"U={U:.3e} | sig_peak={sigp:.3e} | sig_CV={sigcv:.3f} | "
        f"cmin_eff={cmin_eff:.3f} | c@0.40={c_mid:.3f} | xmax={x_max:.3f} | "
        f"SM={sm:.3f} | Cm*={cm:+.3f} | Cmalpha={cma:+.3f} | "
        f"alpha*={metrics.get('alpha_star_deg', float('nan')):+.2f} | "
        f"xcg={xcg:.2f}MAC{tag_str}"
    )

    return obj


# ============================================================
# INITIAL GUESS FROM MY NX-2 ORIGINAL DESIGN
# ============================================================
Y_STATIONS_REF = np.array([0.000, 0.150, 0.300, 0.450, 0.600, 0.650, 0.700, 0.750, 0.770], dtype=float)
C_STATIONS_REF = np.array([0.414, 0.209, 0.165, 0.142, 0.119, 0.111, 0.098, 0.073, 0.054], dtype=float)
XOFF_STATIONS_REF = np.array([0.032, 0.137, 0.218, 0.296, 0.374, 0.402, 0.442, 0.507, 0.546], dtype=float)
DIHEDRAL_STATIONS_DEG_REF = np.array([13.6, 5.3, 1.2, -1.1, -1.5, 10.9, 31.7, 50.5, 50.5], dtype=float)
TWIST_STATIONS_DEG_REF = np.array([0.00, -0.45, -1.18, -2.05, -3.14, -3.90, -5.89, -8.63, -8.28], dtype=float)


def _integrate_dihedral_to_z(ys, dihedral_deg):
    ys = np.asarray(ys, dtype=float)
    dihedral_rad = np.deg2rad(np.asarray(dihedral_deg, dtype=float))
    z = np.zeros_like(ys)
    for i in range(1, len(ys)):
        dy = ys[i] - ys[i-1]
        slope = np.tan(0.5 * (dihedral_rad[i] + dihedral_rad[i-1]))
        z[i] = z[i-1] + slope * dy
    return z


def initial_guess_from_reference_stations():
    y_ref = Y_STATIONS_REF.copy()
    c_ref = C_STATIONS_REF.copy()
    xle_ref = XOFF_STATIONS_REF.copy()
    tw_ref = TWIST_STATIONS_DEG_REF.copy()
    dih_ref = DIHEDRAL_STATIONS_DEG_REF.copy()

    y_tip_ref = float(y_ref[-1])
    s_y = Y_TIP / max(y_tip_ref, 1e-12)

    y = y_ref * s_y
    xle = (xle_ref - xle_ref[0]) * s_y
    zle = _integrate_dihedral_to_z(y_ref, dih_ref) * s_y
    tw = tw_ref.copy()

    fx = PchipInterpolator(y, xle, extrapolate=True)
    fc = PchipInterpolator(y, c_ref, extrapolate=True)
    ftw = PchipInterpolator(y, tw, extrapolate=True)
    fz = PchipInterpolator(y, zle, extrapolate=True)

    denom = float(np.sum(y**2))
    k = float(np.sum(y * xle) / denom) if denom > 1e-12 else 0.0
    sweep_deg_unclipped = float(np.rad2deg(np.arctan(k)))
    sweep_deg = float(np.clip(sweep_deg_unclipped, *BOUNDS["SWEEP_LE_DEG"]))

    tan_sweep = float(np.tan(np.deg2rad(sweep_deg)))
    y_ctrl3 = np.array([f * Y_TIP for f in Y_CTRL_FRACS_3], dtype=float)
    x_off_ctrl = fx(y_ctrl3) - tan_sweep * y_ctrl3

    c_root = float(fc(0.0))
    c_ctrl = fc(y_ctrl3)
    c_mul_ctrl = np.maximum(c_ctrl / max(c_root, 1e-12), 1e-6)

    # Convert (C1,C2,C3) -> (C1, DC2, DC3) (monotone drops)
    c1 = float(c_mul_ctrl[0])
    c2 = float(c_mul_ctrl[1])
    c3 = float(c_mul_ctrl[2])

    tw_root = float(ftw(0.0))
    tw_ctrl = ftw(y_ctrl3)

    y_ctrlz = np.array([f * Y_TIP for f in Y_CTRL_FRACS_Z], dtype=float)
    z_ctrl = fz(y_ctrlz)
    z_fracs = np.asarray(z_ctrl / max(Y_TIP, 1e-12), dtype=float)

    design0 = {
        "SWEEP_LE_DEG": sweep_deg,
        "ROOT_TWIST_DEG": tw_root,
        "X1_OFF": float(x_off_ctrl[0]),
        "X2_OFF": float(x_off_ctrl[1]),
        "X3_OFF": float(x_off_ctrl[2]),

        "C1_MUL": float(c1),
        "DC2": float(max(0.0, c1 - c2)),
        "DC3": float(max(0.0, c2 - c3)),

        "TW1_DEG": float(tw_ctrl[0]),
        "TW2_DEG": float(tw_ctrl[1]),
        "TW3_DEG": float(tw_ctrl[2]),
    }
    for i in range(5):
        design0[f"Z{i+1}_FRAC"] = float(z_fracs[i])

    design0 = clip_to_bounds_dict(design0)

    with open(os.path.join(RESULTS_ROOT, "initial_guess_from_ref.json"), "w", encoding="utf-8") as f:
        json.dump(design0, f, indent=2)

    return design0


# ============================================================
# REFERENCE-BASED THRESHOLDS + STRUCTURE SCALES
# ============================================================
def compute_reference_thresholds_and_scales(design_ref, velocity=V_DESIGN):
    global CNBETA_MIN, CLBETA_MAX, STR_ENERGY_REF, SIGMA_REF, SIGMA_CV_REF, CROOT_REF, _REF_LOADED

    # IMPORTANT:
    # At this point CROOT_REF is None, so geometry will be built with fallback "scale-to-area".
    # Then we define CROOT_REF from that reference geometry's actual root chord.
    geom = build_geometry_from_design(design_ref)
    CROOT_REF = float(geom["chord"][0])

    xcg_frac, SM, CLalpha, CMalpha, cg_clamped, alpha_star_coarse = solve_cg_for_sm(geom, velocity, SM_TARGET)
    airplane, _ = build_airplane_from_geom_and_xcg(geom, xcg_frac)

    alpha_star_deg, (vlm, aero) = solve_alpha_for_CL(
        airplane, velocity, CL_TARGET,
        max_iter=ALPHA_SOLVE_MAXITER_MAIN,
        coarse=False
    )

    (_, _, _, q, _, y_aug, L_aug, cl_aug, chord_aug) = compute_semispan_lift_distribution_binned(
        vlm, geom["y_tip"], SPANWISE_RES_MAIN, geom["ys"], geom["chord"], geom["S_proj"]
    )

    mask = y_aug >= float(Y_CUT)
    if np.sum(mask) < 4:
        mask = np.ones_like(y_aug, dtype=bool)

    y_abs = y_aug[mask]
    L_eff = L_aug[mask]
    c_eff = chord_aug[mask]
    y_eff = y_abs - float(y_abs[0])

    struct = structural_response_1D(y_eff, L_eff, c_eff)

    Cn_beta_ref, Cl_beta_ref = compute_lateral_derivatives(airplane, velocity, alpha_star_deg)
    CNBETA_MIN = float(Cn_beta_ref)
    CLBETA_MAX = float(Cl_beta_ref)

    STR_ENERGY_REF = float(struct["U_total"]) if np.isfinite(struct["U_total"]) and struct["U_total"] > 0 else 1.0
    SIGMA_REF = float(struct["sigma_peak"]) if np.isfinite(struct["sigma_peak"]) and struct["sigma_peak"] > 0 else 1.0
    SIGMA_CV_REF = float(struct["sigma_cv"]) if np.isfinite(struct["sigma_cv"]) and struct["sigma_cv"] > 1e-6 else 0.10

    ref_blob = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "design_ref": design_ref,
        "alpha_star_deg": float(alpha_star_deg),
        "alpha_star_coarse_deg_for_SM": float(alpha_star_coarse),
        "Cn_beta_ref_per_rad": float(CNBETA_MIN),
        "Cl_beta_ref_per_rad": float(CLBETA_MAX),
        "c_root_ref_m": float(CROOT_REF),
        "structure_ref": {
            "U_total_ref": float(STR_ENERGY_REF),
            "sigma_peak_ref_Pa": float(SIGMA_REF),
            "sigma_cv_ref": float(SIGMA_CV_REF),
        }
    }

    with open(_REF_PATH, "w", encoding="utf-8") as f:
        json.dump(ref_blob, f, indent=2)

    _REF_LOADED = True

    print("\n=== Reference thresholds set ===")
    print(f"Cnβ_ref = {CNBETA_MIN:+.6f} (constraint: Cnβ >= this)")
    print(f"Clβ_ref = {CLBETA_MAX:+.6f} (constraint: Clβ <= this)")
    print(f"c_root_ref = {CROOT_REF:.6f} m (HARD-FIXED thereafter)")
    print(f"U_ref = {STR_ENERGY_REF:.3e} (normalization)")
    print(f"σ_ref = {SIGMA_REF:.3e} (normalization)")
    print(f"CVσ_ref = {SIGMA_CV_REF:.3f} (normalization)")
    print("Saved: reference_thresholds.json")


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    initialize_eval_counter_from_existing()

    TIME_BUDGET_HOURS = 24
    TIME_BUDGET_S = TIME_BUDGET_HOURS * 3600.0
    t0 = time.time()

    design0 = initial_guess_from_reference_stations()
    x0 = design_to_vec(design0)

    with open(os.path.join(RESULTS_ROOT, "initial_x0_vector.json"), "w", encoding="utf-8") as f:
        json.dump({k: float(v) for k, v in zip(VAR_NAMES, x0)}, f, indent=2)

    # Compute and persist thresholds + CROOT_REF from the reference design
    # ------------------------------------------------------------
    # FORCE FRESH REFERENCE (avoid stale reference_thresholds.json)
    # ------------------------------------------------------------
    try:
        if os.path.isfile(_REF_PATH):
            os.remove(_REF_PATH)
    except Exception:
        pass
    
    _REF_LOADED = False
    CNBETA_MIN = None
    CLBETA_MAX = None
    STR_ENERGY_REF = None
    SIGMA_REF = None
    SIGMA_CV_REF = None
    CROOT_REF = None
    
    # Compute and persist thresholds + CROOT_REF from the reference design
    compute_reference_thresholds_and_scales(design0, velocity=V_DESIGN)


    D = len(VAR_NAMES)
    popsize = 10
    maxiter = 20

    MAX_EVALS = int((maxiter + 1) * popsize * D * 1.10)

    def de_callback(xk, convergence):
        elapsed = time.time() - t0
        if elapsed >= TIME_BUDGET_S:
            print(f"\n[STOP] Time budget reached: {elapsed/3600.0:.2f} h (limit {TIME_BUDGET_HOURS:.2f} h)")
            return True
        if get_eval_counter_value() >= MAX_EVALS:
            print(f"\n[STOP] Eval cap reached: {get_eval_counter_value()} (cap {MAX_EVALS})")
            return True
        return False

    def run_de_with_fallback():
        try:
            return differential_evolution(
                func=de_objective,
                bounds=DE_BOUNDS,
                strategy="best1bin",
                maxiter=maxiter,
                popsize=popsize,
                tol=0.0,
                atol=0.0,
                mutation=(0.5, 1.0),
                recombination=0.8,
                polish=True,
                init="latinhypercube",
                x0=x0,
                callback=de_callback,
                updating="deferred",
                workers=1,   # set -1 if you want true parallel (ensure all code is spawn-safe)
                seed=2,
                disp=True
            )
        except TypeError:
            # Older SciPy fallback
            n_pop = popsize * D
            init_pop = np.zeros((n_pop, D), dtype=float)
            init_pop[0, :] = x0

            lows = np.array([b[0] for b in DE_BOUNDS], dtype=float)
            highs = np.array([b[1] for b in DE_BOUNDS], dtype=float)
            rng = np.random.default_rng(2)
            init_pop[1:, :] = lows + (highs - lows) * rng.random((n_pop - 1, D))

            return differential_evolution(
                func=de_objective,
                bounds=DE_BOUNDS,
                strategy="best1bin",
                maxiter=maxiter,
                popsize=popsize,
                tol=0.0,
                atol=0.0,
                mutation=(0.5, 1.0),
                recombination=0.8,
                polish=True,
                init=init_pop,
                callback=de_callback,
                updating="deferred",
                workers=1,
                seed=2,
                disp=True
            )

    result = run_de_with_fallback()

    print("\n=== differential_evolution DONE ===")
    print(result)

    best_x = result.x
    best_design = {name: float(val) for name, val in zip(VAR_NAMES, best_x)}
    with open(os.path.join(RESULTS_ROOT, "best_design_from_de.json"), "w", encoding="utf-8") as f:
        json.dump(best_design, f, indent=2)

    print("\nBest design variables:")
    for k in VAR_NAMES:
        print(f" {k}: {best_design[k]}")
