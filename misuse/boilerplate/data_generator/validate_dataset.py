"""
data_generator/validate_dataset.py
=====================================
Laporan validitas dataset hasil run_experiment.py -- bahan evaluasi TA 1
(Karakterisasi dan Simulasi): apakah data sintetis bervariasi, bebas
kebocoran label struktural, konsisten dengan kebijakan sandbox, dan
apakah perilaku tiap sesi benar-benar sesuai labelnya (cek ground truth).

    python validate_dataset.py data-v2

Laporan dicetak dan disimpan ke <out-dir>/validation_report.txt. Hasil cek
ground truth per sesi ditulis ke <out-dir>/ground_truth_check.csv, dan
baris dari sesi yang lolos ke <out-dir>/dataset_verified.csv.

Kolom label, metadata, dan param_* (ground truth injeksi) TIDAK dipakai
sebagai fitur; fitur = kolom telemetri numerik, diagregasi per sesi
(mean/std/max/min) + jumlah baris.
"""

import argparse
import glob
import json
import os
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.tree import DecisionTreeClassifier

META_COLUMNS = {"timestamp", "session_id", "label", "job_type", "user_id",
                "scenario_code", "label_class"}
KEY_FEATURES = ["cpu_pct", "mem_rss_mb", "gpu_util_pct", "gpu_mem_used_mb_device",
                "last_activity_age_sec", "prom_queue_length", "prom_user_active_sessions"]


class Report:
    def __init__(self):
        self.lines = []
        self.warnings = []

    def __call__(self, text=""):
        print(text)
        self.lines.append(text)

    def warn(self, text):
        self(f"  PERINGATAN: {text}")
        self.warnings.append(text)


def session_features(df, feature_cols):
    g = df.groupby("session_id")
    X = g[feature_cols].agg(["mean", "std", "max", "min"])
    X.columns = [f"{col}_{stat}" for col, stat in X.columns]
    X["n_rows"] = g.size()
    meta = g[["label", "scenario_code", "job_type"]].first()
    return X, meta


def cv_accuracy(model, X, y):
    n_splits = min(5, int(y.value_counts().min()))
    if n_splits < 2:
        return float("nan")
    cv = StratifiedKFold(n_splits, shuffle=True, random_state=0)
    return cross_val_score(model, X, y, cv=cv).mean()


# === Cek ground truth (bagian 6) ===
# Ambang verifikasi, BUKAN parameter simulasi. Ambang kebijakan (idle,
# batas sesi, kuota) diambil dari policy.json dan kolom quota_mem_mb.
WARMUP_SEC = 20        # lewati fase import/inisialisasi job
GPU_UNUSED_PCT = 10    # GPU tidak dipakai: persentil 90 utilisasi <= ini
HOLD_FRAC = 0.9        # memori yang ditahan >= 90% param_hold_mem_mb
OVERPROV_RATIO = 8     # R4: request CPU >= 8x pemakaian rata-rata
BUSY_CPU_PCT = 80      # R7: median CPU ~1 core penuh
LOW_GPU_PCT = 60       # R7: rata-rata utilisasi GPU rendah
HIGH_UTIL_PCT = 60     # S1: median utilisasi tinggi
PERIOD_ACF = 0.3       # S1 berosilasi: autokorelasi pada periode yang disuntikkan
NA = "n/a"             # cek tidak berlaku di mode ini (mis. kuota GPU di mode CPU)
RESULT_TEXT = {True: "ok", False: "gagal", None: "data kurang", NA: "n/a"}


def _col(d, name):
    """Kolom numerik tanpa NaN, atau None bila tidak ada/kosong."""
    if name not in d:
        return None
    s = pd.to_numeric(d[name], errors="coerce").dropna()
    return s if len(s) else None


def _first(d, name):
    s = _col(d, name)
    return None if s is None else float(s.iloc[0])


def _cpu_cores(value):
    """Request CPU Kubernetes ("2", "500m") -> jumlah core."""
    if value is None or pd.isna(value):
        return None
    text = str(value)
    return float(text[:-1]) / 1000 if text.endswith("m") else float(text)


def _negate(result):
    return result if result is None or result is NA else not result


class SessionView:
    """Satu sesi beserta konteks yang dibutuhkan cek ground truth."""

    def __init__(self, d, policy, interval_sec):
        self.d = d.sort_values("session_age_sec")
        self.steady = self.d[self.d["session_age_sec"] > WARMUP_SEC]
        self.policy = policy
        self.interval_sec = interval_sec
        self.gpu_mode = _col(d, "gpu_util_pct") is not None
        # kuota per sesi (v2) atau kuota global policy.json (v1)
        self.quota_mb = _first(d, "quota_mem_mb") or policy.get("gpu_mem_quota_mb")

    def util(self):
        """Utilisasi beban utama: GPU di mode GPU, CPU di mode CPU."""
        return _col(self.steady, "gpu_util_pct" if self.gpu_mode else "cpu_pct")


def chk_gpu_unused(v):
    s = v.util()
    return None if s is None else bool(s.quantile(0.9) <= GPU_UNUSED_PCT)


def chk_mem_held(v):
    hold = _first(v.d, "param_hold_mem_mb")
    mem = _col(v.steady, "gpu_mem_used_mb_device" if v.gpu_mode else "mem_rss_mb")
    if hold is None or mem is None:
        return None
    return bool(mem.median() >= HOLD_FRAC * hold)


def chk_over_quota(v):
    if not v.gpu_mode:
        return NA   # kuota memori GPU; di mode CPU alokasi masuk RAM
    mem = _col(v.d, "gpu_mem_used_mb_device")
    if mem is None or v.quota_mb is None:
        return None
    return bool(mem.max() > v.quota_mb)


def chk_within_quota(v):
    return _negate(chk_over_quota(v))


def chk_idle(v):
    s = _col(v.d, "last_activity_age_sec")
    limit = v.policy.get("idle_timeout_sec")
    return None if s is None or limit is None else bool(s.max() > limit)


def chk_not_idle(v):
    return _negate(chk_idle(v))


def chk_sessions_over(v):
    s = _col(v.d, "prom_user_active_sessions")
    limit = v.policy.get("max_concurrent_sessions")
    return None if s is None or limit is None else bool(s.max() > limit)


def chk_sessions_within(v):
    return _negate(chk_sessions_over(v))


def chk_queue(v):
    if not v.gpu_mode:
        return NA   # waiter hanya dibuat di mode GPU
    q = _col(v.d, "prom_queue_length")
    return None if q is None else bool(q.max() >= 1)


def chk_queue_rising(v):
    if not v.gpu_mode:
        return NA
    q = _col(v.d, "prom_queue_length")
    if q is None or len(q) < 6:
        return None
    third = len(q) // 3
    return bool(q.max() >= 1 and q.iloc[-third:].mean() > q.iloc[:third].mean())


def chk_overprov(v):
    request = (_cpu_cores(v.d["param_request_cpu"].iloc[0])
               if "param_request_cpu" in v.d else None)
    if request is None:
        request = _first(v.d, "prom_cpu_request_cores")
    cpu = _col(v.steady, "cpu_pct")
    if request is None or cpu is None:
        return None
    return bool(request / max(cpu.mean() / 100, 0.01) >= OVERPROV_RATIO)


def chk_no_activity(v):
    if _col(v.d, "last_activity_age_sec") is None:
        return None
    # tanpa touch_activity(), sidecar memakai waktu mulai proses sebagai aktivitas terakhir
    gap = (v.d["session_age_sec"] - v.d["last_activity_age_sec"]).abs()
    return bool(gap.max() <= 1.0)


def chk_no_later_activity(v):
    s = _col(v.steady, "last_activity_age_sec")
    if s is None or len(s) < 2:
        return None
    # touch_activity() me-reset umur aktivitas; setelah fase awal tidak boleh ada reset
    return bool((s.diff().dropna() >= -0.5).all())


def chk_cpu_busy(v):
    s = _col(v.steady, "cpu_pct")
    return None if s is None else bool(s.median() >= BUSY_CPU_PCT)


def chk_gpu_low(v):
    if not v.gpu_mode:
        return NA
    s = _col(v.steady, "gpu_util_pct")
    return None if s is None else bool(s.mean() <= LOW_GPU_PCT)


def chk_high_load(v):
    s = v.util()
    return None if s is None else bool(s.median() >= HIGH_UTIL_PCT)


def chk_periodic(v):
    period = _first(v.d, "param_period_sec") or 60.0   # default skenario
    lag = int(round(period / v.interval_sec))
    s = v.util()
    if s is None or lag < 2 or len(s) < 2 * lag:
        return None   # sesi terlalu pendek untuk dua periode
    return bool(s.reset_index(drop=True).autocorr(lag) >= PERIOD_ACF)


# Cek per kode skenario (definisi di scenarios/README.md); override per job_type.
GT_CHECKS = {
    "R1": {"gpu_tidak_dipakai": chk_gpu_unused, "memori_tertahan": chk_mem_held,
           "di_bawah_kuota": chk_within_quota, "ada_antrean": chk_queue,
           "tidak_melewati_idle": chk_not_idle},
    "R2": {"melewati_idle": chk_idle},
    "R3": {"melewati_kuota": chk_over_quota},
    "R4": {"request_jauh_di_atas_pemakaian": chk_overprov},
    "R5": {"tanpa_aktivitas": chk_no_activity, "gpu_tidak_dipakai": chk_gpu_unused,
           "melewati_idle": chk_idle},
    "R6": {"sesi_melewati_batas": chk_sessions_over},
    "R7": {"cpu_penuh": chk_cpu_busy, "gpu_rendah": chk_gpu_low},
    "P5": {"antrean_naik": chk_queue_rising},
    "S1": {"beban_tinggi": chk_high_load, "tanpa_aktivitas_lanjutan": chk_no_later_activity},
    "BENIGN": {"di_bawah_kuota": chk_within_quota, "tidak_melewati_idle": chk_not_idle,
               "sesi_dalam_batas": chk_sessions_within},
}
GT_CHECKS_BY_JOB = {
    # beban rata-ratanya tidak harus tinggi; yang dicek periodisitasnya
    "misuse_disguised_pattern": {"pola_periodik": chk_periodic,
                                 "tanpa_aktivitas_lanjutan": chk_no_later_activity},
}


def ground_truth_table(df, policy, interval_sec):
    """Satu baris per sesi: hasil tiap cek dan status (lolos / gagal /
    tidak lengkap / tanpa cek)."""
    rows = {}
    for session_id, d in df.groupby("session_id"):
        job, code = d["job_type"].iloc[0], d["scenario_code"].iloc[0]
        checks = GT_CHECKS_BY_JOB.get(job, GT_CHECKS.get(code, {}))
        view = SessionView(d, policy, interval_sec)
        results = {name: fn(view) for name, fn in checks.items()}
        applicable = {k: r for k, r in results.items() if r is not NA}
        failed = [k for k, r in applicable.items() if r is False]
        missing = [k for k, r in applicable.items() if r is None]
        status = ("tanpa cek" if not applicable else "gagal" if failed
                  else "tidak lengkap" if missing else "lolos")
        rows[session_id] = {"job_type": job, "scenario_code": code, "status": status,
                            "cek_gagal": ",".join(failed),
                            "cek_data_kurang": ",".join(missing),
                            **{k: RESULT_TEXT[r] for k, r in results.items()}}
    return pd.DataFrame.from_dict(rows, orient="index").rename_axis("session_id")


def _check_counts(rows, column):
    """"cek_a x3, cek_b x1" dari kolom daftar cek dipisah koma."""
    names = rows[column].str.split(",").explode()
    return ", ".join(f"{name} x{n}" for name, n in names[names != ""].value_counts().items())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    args = ap.parse_args()
    out = Report()

    df = pd.read_csv(os.path.join(args.out_dir, "dataset.csv"))
    policy, plan = {}, None
    if os.path.exists(os.path.join(args.out_dir, "policy.json")):
        policy = json.load(open(os.path.join(args.out_dir, "policy.json")))
    if os.path.exists(os.path.join(args.out_dir, "plan.json")):
        plan = json.load(open(os.path.join(args.out_dir, "plan.json")))

    feature_cols = [c for c in df.select_dtypes("number").columns
                    if c not in META_COLUMNS and not c.startswith("param_")]
    X, meta = session_features(df, feature_cols)

    # === 1. Ringkasan ===
    out(f"# Validasi dataset: {args.out_dir}")
    out(f"\n## 1. Ringkasan")
    out(f"{len(df)} baris, {len(meta)} sesi"
        + (f" dari {len(plan['sessions'])} sesi dalam plan.json" if plan else ""))
    out(meta["label"].value_counts().to_string())
    counts = meta.groupby(["scenario_code", "job_type"]).size()
    out(counts.to_string())
    if plan and len(meta) < len(plan["sessions"]):
        out.warn(f"{len(plan['sessions']) - len(meta)} sesi belum selesai (pakai --resume)")
    if counts.min() < 5:
        out.warn("ada skenario dengan < 5 sesi; evaluasi per skenario kurang stabil")
    rows = X["n_rows"]
    out(f"baris/sesi: min {rows.min()}, median {rows.median():.0f}, max {rows.max()}")

    # === 2. Kolom kosong ===
    out("\n## 2. Kolom kosong")
    na = df[feature_cols].isna().mean()
    out(na[na > 0.01].round(3).to_string() if (na > 0.01).any() else "tidak ada kolom > 1% kosong")
    na_by_scenario = df.groupby("job_type")[feature_cols].apply(lambda d: d.isna().mean())
    spread = (na_by_scenario.max() - na_by_scenario.min())
    for col in spread[spread > 0.5].index:
        filled = na_by_scenario.index[na_by_scenario[col] < 0.1].tolist()
        out.warn(f"{col} hanya terisi di {filled} -- pola kosong bisa membocorkan label")

    # === 3. Variasi antar sesi ===
    out("\n## 3. Variasi antar sesi (koefisien variasi rata-rata sesi)")
    keys = [c for c in KEY_FEATURES if c in df.columns]
    per_session = df.groupby(["job_type", "session_id"])[keys].mean()
    grouped = per_session.groupby(level=0)
    cv = (grouped.std() / grouped.mean().abs().replace(0, np.nan)).round(2)
    out(cv.to_string())
    flat = cv[["gpu_util_pct", "cpu_pct"]].max(axis=1) if {"gpu_util_pct", "cpu_pct"} <= set(cv) else None
    if flat is not None:
        for name in flat[flat < 0.05].index:
            out.warn(f"{name}: sesi-sesinya nyaris identik (CV < 0.05)")

    # === 4. Uji kebocoran & tingkat kesulitan ===
    out("\n## 4. Uji kebocoran dan tingkat kesulitan (akurasi cross-validation)")
    y_code = meta["scenario_code"]
    baseline = y_code.value_counts(normalize=True).max()
    out(f"baseline (tebak kelas terbanyak) scenario_code: {baseline:.3f}")
    structure = pd.concat([X.drop(columns="n_rows").isna().astype(int), X[["n_rows"]]], axis=1)
    leak = cv_accuracy(DecisionTreeClassifier(random_state=0), structure, y_code)
    out(f"hanya pola kosong + jumlah baris -> scenario_code: {leak:.3f}")
    if leak > max(baseline + 0.15, 0.3):
        out.warn("struktur data (bukan nilai telemetri) cukup untuk menebak skenario")
    Xf = X.fillna(-1)
    for depth in (2, 3):
        acc = cv_accuracy(DecisionTreeClassifier(max_depth=depth, random_state=0), Xf, y_code)
        out(f"decision tree depth={depth} -> scenario_code: {acc:.3f}")
    rf = RandomForestClassifier(300, random_state=0)
    for target in ("label", "scenario_code"):
        acc = cv_accuracy(rf, Xf, meta[target])
        out(f"random forest -> {target}: {acc:.3f}")
        if acc >= 0.99:
            out.warn(f"{target} terpisah sempurna -- dataset kemungkinan terlalu mudah")
    rf.fit(Xf, meta["label"])
    top = pd.Series(rf.feature_importances_, index=Xf.columns).nlargest(8)
    out("fitur terpenting (label) -- periksa apakah artefak simulasi:")
    out(top.round(3).to_string())

    # === 5. Konsistensi dengan kebijakan sandbox ===
    out("\n## 5. Konsistensi dengan policy.json (baseline berbasis aturan)")
    by = df.groupby("session_id")
    benign = meta["label"] == "benign"
    if "idle_timeout_sec" in policy and "last_activity_age_sec" in df:
        idle = by["last_activity_age_sec"].max() > policy["idle_timeout_sec"]
        out(f"melewati ambang idle {policy['idle_timeout_sec']} s: "
            f"R2 {idle[y_code == 'R2'].mean():.0%}, benign {idle[benign].mean():.0%}")
    else:
        out.warn("ambang idle (policy.json) atau kolom last_activity_age_sec tidak ada "
                 "-- cek idle dilewati")
    if "max_concurrent_sessions" in policy and "prom_user_active_sessions" in df:
        over = by["prom_user_active_sessions"].max() > policy["max_concurrent_sessions"]
        out(f"melewati batas {policy['max_concurrent_sessions']} sesi: "
            f"R6 {over[y_code == 'R6'].mean():.0%}, benign {over[benign].mean():.0%}")
    else:
        out.warn("batas sesi (policy.json) atau kolom prom_user_active_sessions tidak ada "
                 "-- cek sesi paralel dilewati")
    # kuota per sesi (v2) atau kuota global policy.json (dataset v1)
    quota = (by["quota_mem_mb"].first() if "quota_mem_mb" in df
             else policy.get("gpu_mem_quota_mb"))
    if quota is not None and "gpu_mem_used_mb_device" in df \
            and df["gpu_mem_used_mb_device"].notna().any():
        over = by["gpu_mem_used_mb_device"].max() > quota
        out(f"melewati kuota memori GPU: R3 {over[y_code == 'R3'].mean():.0%}, "
            f"benign {over[benign].mean():.0%}")
        if over[benign].mean() > 0.05:
            out.warn("> 5% sesi benign melampaui kuota -- label R3 jadi ambigu")
    else:
        out.warn("kuota memori GPU tidak diketahui atau data tanpa GPU -- cek kuota dilewati")

    # === 6. Cek ground truth per sesi ===
    out("\n## 6. Cek ground truth per sesi")
    out("Tiap sesi diuji terhadap definisi skenarionya (scenarios/README.md); "
        "hanya sesi 'lolos' yang masuk dataset_verified.csv.")
    failures = []
    for path in sorted(glob.glob(os.path.join(args.out_dir, "*.failed.json"))):
        with open(path) as f:
            failures.append(json.load(f))
    if failures:
        out.warn(f"{len(failures)} sesi gagal dijalankan, tidak ada di dataset: "
                 + "; ".join(f"{x['session_id']} ({x['reason']})" for x in failures))
    interval_sec = (plan["args"]["interval_sec"] if plan
                    else df.groupby("session_id")["session_age_sec"].diff().median())
    gt = ground_truth_table(df, policy, interval_sec)
    out(gt.groupby(["scenario_code", "job_type"])["status"]
        .value_counts().unstack(fill_value=0).to_string())
    for job, rows in gt[gt["status"] != "lolos"].groupby("job_type"):
        failed, missing = _check_counts(rows, "cek_gagal"), _check_counts(rows, "cek_data_kurang")
        detail = "; ".join(filter(None, [failed and f"gagal: {failed}",
                                         missing and f"data kurang: {missing}"]))
        out.warn(f"{job}: {len(rows)}/{(gt['job_type'] == job).sum()} sesi tidak lolos "
                 f"cek ground truth ({detail or rows['status'].iloc[0]})")
    gt.to_csv(os.path.join(args.out_dir, "ground_truth_check.csv"))
    verified = df[df["session_id"].isin(gt.index[gt["status"] == "lolos"])]
    verified.to_csv(os.path.join(args.out_dir, "dataset_verified.csv"), index=False)
    out(f"dataset_verified.csv: {verified['session_id'].nunique()} dari {len(gt)} sesi")

    out(f"\n## Kesimpulan: {len(out.warnings)} peringatan")
    for w in out.warnings:
        out(f"- {w}")
    with open(os.path.join(args.out_dir, "validation_report.txt"), "w") as f:
        f.write("\n".join(out.lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
