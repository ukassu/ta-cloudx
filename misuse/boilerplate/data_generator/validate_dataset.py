"""
data_generator/validate_dataset.py
=====================================
Laporan validitas dataset hasil run_experiment.py -- bahan evaluasi TA 1
(Karakterisasi dan Simulasi): apakah data sintetis bervariasi, bebas
kebocoran label struktural, dan konsisten dengan kebijakan sandbox.

    python validate_dataset.py data-v2

Laporan dicetak dan disimpan ke <out-dir>/validation_report.txt.

Kolom label, metadata, dan param_* (ground truth injeksi) TIDAK dipakai
sebagai fitur; fitur = kolom telemetri numerik, diagregasi per sesi
(mean/std/max/min) + jumlah baris.
"""

import argparse
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
    if "max_concurrent_sessions" in policy and "prom_user_active_sessions" in df:
        over = by["prom_user_active_sessions"].max() > policy["max_concurrent_sessions"]
        out(f"melewati batas {policy['max_concurrent_sessions']} sesi: "
            f"R6 {over[y_code == 'R6'].mean():.0%}, benign {over[benign].mean():.0%}")
    if "quota_mem_mb" in df and "gpu_mem_used_mb_device" in df:
        over = by["gpu_mem_used_mb_device"].max() > by["quota_mem_mb"].first()
        out(f"melewati kuota memori GPU: R3 {over[y_code == 'R3'].mean():.0%}, "
            f"benign {over[benign].mean():.0%}")
        if over[benign].mean() > 0.05:
            out.warn("> 5% sesi benign melampaui kuota -- label R3 jadi ambigu")

    out(f"\n## Kesimpulan: {len(out.warnings)} peringatan")
    for w in out.warnings:
        out(f"- {w}")
    with open(os.path.join(args.out_dir, "validation_report.txt"), "w") as f:
        f.write("\n".join(out.lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
