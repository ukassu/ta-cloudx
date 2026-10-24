"""
data_generator/run_experiment.py
===================================
Orchestrator GENERIK -- tidak menyebut satu pun nama skenario secara
eksplisit di file ini. Daftar skenario yang dijalankan diambil dari
registry.discover_scenarios() (otomatis menemukan seluruh file di
scenarios/), sehingga menambah/menghapus skenario di folder scenarios/
langsung mengubah dataset yang dihasilkan tanpa menyentuh file ini.

Contoh pemakaian:
    python run_experiment.py --repeats 10 --out-dir data
    python run_experiment.py --repeats 5 --scenarios benign_cnn_training,misuse_idle_hoarding
"""

import argparse
import dataclasses
import glob
import json
import os
import random

import pandas as pd

from registry import discover_scenarios, all_scenarios, get

# Kebijakan sandbox untuk baseline berbasis aturan (R2 idle, R6 sesi, R3
# kuota) -- ditulis ke <out-dir>/policy.json. Skala waktu dikompresi agar
# sesi 5 menit bisa melampaui ambang idle; sesuaikan dengan konfigurasi
# idle culler CloudX bila sudah tersedia (Kajian CATS, Bagian 6.4).
POLICY = {
    "idle_timeout_sec": 120,
    "max_concurrent_sessions": 2,
    "gpu_mem_quota_mb": 512,
}
# ID pengguna pseudonim (Kajian CATS, Bagian 6.3)
USER_POOL = [f"u{i:02d}" for i in range(1, 9)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=10,
                     help="berapa kali seluruh set skenario diulang")
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--interval-sec", type=float, default=5.0)
    ap.add_argument("--scenarios", default=None,
                     help="daftar nama skenario dipisah koma (default: SEMUA "
                          "skenario yang terdaftar di scenarios/)")
    ap.add_argument("--duration-sec", type=int, default=None,
                     help="ganti nilai --duration-sec di default_args setiap "
                          "skenario yang memilikinya (mis. 120 untuk uji cepat)")
    ap.add_argument("--executor", choices=["local", "k8s"], default="local",
                     help="'local': subprocess di mesin ini (job_runner.py). "
                          "'k8s': submit Pod ke cluster kind aktif "
                          "(k8s_job_runner.py) -- kubeconfig harus sudah "
                          "menunjuk ke cluster yang benar.")
    args = ap.parse_args()

    if args.executor == "local":
        from job_runner import run_labeled_job
    else:
        from k8s_job_runner import run_labeled_job

    discover_scenarios()  # import semua modul di scenarios/ -> trigger register()

    if args.scenarios:
        names = [s.strip() for s in args.scenarios.split(",")]
        scenarios = [get(name) for name in names]
    else:
        scenarios = all_scenarios()

    if not scenarios:
        raise RuntimeError(
            "Tidak ada skenario terdaftar -- pastikan folder scenarios/ "
            "berisi file skenario yang memanggil registry.register()."
        )

    print(f"[run_experiment] {len(scenarios)} skenario ditemukan: "
          f"{[s.name for s in scenarios]}")

    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "policy.json"), "w") as f:
        json.dump(POLICY, f, indent=2)
    run_idx = 0

    def with_duration(scenario):
        """Terapkan --duration-sec; timeout ikut diperpanjang agar runner
        tidak berhenti menunggu sebelum job selesai."""
        job_args = list(scenario.default_args)
        if args.duration_sec is not None and "--duration-sec" in job_args:
            duration = int(args.duration_sec * scenario.duration_scale)
            job_args[job_args.index("--duration-sec") + 1] = str(duration)
            scenario = dataclasses.replace(
                scenario,
                timeout_sec=max(scenario.timeout_sec or 0, duration + 30),
            )
        return scenario, job_args

    for repeat in range(args.repeats):
        order = scenarios[:]
        random.shuffle(order)  # acak urutan tiap putaran, hindari bias waktu
        for scenario in order:
            run_idx += 1
            session_id = f"{scenario.name}_{run_idx:04d}"
            scenario, job_args = with_duration(scenario)
            run_labeled_job(
                scenario=scenario,
                session_id=session_id,
                out_dir=args.out_dir,
                interval_sec=args.interval_sec,
                args_override=job_args,
                extra_meta={
                    "user_id": random.choice(USER_POOL),
                    "scenario_code": scenario.code,
                    "label_class": scenario.label_class,
                    **scenario.extra_meta,
                },
            )

    all_csvs = glob.glob(os.path.join(args.out_dir, "*.merged.csv"))
    all_csvs = [f for f in all_csvs if os.path.getsize(f) > 0]
    if not all_csvs:
        print("[run_experiment] tidak ada CSV merged dihasilkan, "
              "dataset gabungan dilewati.")
        return

    df = pd.concat([pd.read_csv(f) for f in all_csvs], ignore_index=True)
    out_path = os.path.join(args.out_dir, "dataset.csv")
    df.to_csv(out_path, index=False)
    print(f"[run_experiment] dataset final: {out_path} ({len(df)} baris, "
          f"{df['session_id'].nunique()} sesi, "
          f"{df.groupby('job_type')['session_id'].nunique().to_dict()})")


if __name__ == "__main__":
    main()
