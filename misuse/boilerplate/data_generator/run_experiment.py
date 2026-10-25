"""
data_generator/run_experiment.py
===================================
Orchestrator GENERIK -- tidak menyebut satu pun nama skenario secara
eksplisit di file ini. Daftar skenario yang dijalankan diambil dari
registry.discover_scenarios() (otomatis menemukan seluruh file di
scenarios/), sehingga menambah/menghapus skenario di folder scenarios/
langsung mengubah dataset yang dihasilkan tanpa menyentuh file ini.

Alur v2:
  1. Susun RENCANA: --sessions sesi dibagi ke benign/misuse sesuai
     --misuse-frac dan Scenario.weight; tiap sesi mendapat parameter
     yang diundi dari Scenario.params (RNG per sesi dari --seed).
  2. Simpan rencana ke <out-dir>/plan.json, lalu jalankan sesi satu per satu.
  3. --resume melanjutkan plan.json yang ada: sesi yang sudah punya
     .merged.csv dilewati.

Contoh pemakaian:
    python run_experiment.py --executor k8s --sessions 300 --out-dir data-v2
    python run_experiment.py --executor k8s --resume --out-dir data-v2
    python run_experiment.py --executor k8s --sessions 15 --duration-sec 60 \\
        --misuse-frac 0.67 --out-dir data-test      # uji cepat
"""

import argparse
import dataclasses
import json
import os
import random
import subprocess
from datetime import datetime, timezone

import pandas as pd

from registry import discover_scenarios, all_scenarios, get
from scenario_base import COMMON_PARAMS

# Kebijakan sandbox untuk baseline berbasis aturan (R2 idle, R6 sesi, R3
# kuota) -- ditulis ke <out-dir>/policy.json. Skala waktu dikompresi agar
# sesi beberapa menit bisa melampaui ambang idle; sesuaikan dengan
# konfigurasi idle culler CloudX bila sudah tersedia (Kajian CATS, Bagian 6.4).
POLICY = {
    "idle_timeout_sec": 120,
    "max_concurrent_sessions": 2,
}
# Kuota memori per sesi = porsi memori GPU node (kolom quota_mem_mb)
QUOTA_FRACS = [0.5, 0.75]
# Profil request default ala JupyterHub; semua sesi punya request agar
# ada/tidaknya request bukan penanda label (R4 memakai profil sendiri).
DEFAULT_REQUEST_PROFILES = [
    {"cpu": "1", "memory": "1Gi"},
    {"cpu": "2", "memory": "2Gi"},
]
# ID pengguna pseudonim (Kajian CATS, Bagian 6.3)
USER_POOL = [f"u{i:02d}" for i in range(1, 9)]
PLAN_VERSION = 2


def detect_mem_budget_mb():
    """Memori GPU node (MB) di mode GPU; selain itu CPU_MEM_BUDGET_MB."""
    if os.environ.get("K8S_REQUEST_GPU", "false").lower() == "true":
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                text=True,
            )
            return int(out.splitlines()[0].strip())
        except (OSError, subprocess.CalledProcessError, ValueError, IndexError):
            print("[run_experiment] WARNING: nvidia-smi gagal, pakai CPU_MEM_BUDGET_MB")
    return int(os.environ.get("CPU_MEM_BUDGET_MB", "4096"))


def sample_value(spec, rng, ctx):
    if callable(spec):
        return spec(rng, ctx)
    if isinstance(spec, tuple) and len(spec) == 2:
        lo, hi = spec
        if isinstance(lo, int) and isinstance(hi, int):
            return rng.randint(lo, hi)
        return round(rng.uniform(lo, hi), 3)
    if isinstance(spec, list):
        return rng.choice(spec)
    return spec


def allocate(n, scenarios):
    """Bagi n sesi ke skenario sebanding Scenario.weight (largest remainder)."""
    total = sum(s.weight for s in scenarios)
    raw = [n * s.weight / total for s in scenarios]
    counts = [int(x) for x in raw]
    by_remainder = sorted(range(len(raw)), key=lambda i: raw[i] - counts[i], reverse=True)
    for i in by_remainder[:n - sum(counts)]:
        counts[i] += 1
    return counts


def build_plan(scenarios, args, mem_budget_mb):
    benign = [s for s in scenarios if s.label == "benign"]
    misuse = [s for s in scenarios if s.label == "misuse"]
    if benign and misuse:
        n_misuse = round(args.sessions * args.misuse_frac)
        groups = [(benign, args.sessions - n_misuse), (misuse, n_misuse)]
    else:
        groups = [(benign or misuse, args.sessions)]

    order = []
    for group, n in groups:
        for scenario, count in zip(group, allocate(n, group)):
            if count == 0:
                print(f"[run_experiment] WARNING: {scenario.name} mendapat 0 sesi "
                      f"-- naikkan --sessions")
            order += [scenario] * count
    random.Random(args.seed).shuffle(order)

    sessions = []
    for idx, scenario in enumerate(order, start=1):
        rng = random.Random(f"{args.seed}:{idx}")
        ctx = {"mem_budget_mb": mem_budget_mb,
               "quota_mem_mb": int(rng.choice(QUOTA_FRACS) * mem_budget_mb)}
        specs = {**COMMON_PARAMS, **scenario.params}
        params = {name: sample_value(spec, rng, ctx) for name, spec in specs.items()}
        if args.duration_sec is not None:   # mode uji: durasi tetap
            params["duration_sec"] = int(args.duration_sec * scenario.duration_scale)
        elif "duration_sec" in params:
            params["duration_sec"] = max(30, int(params["duration_sec"] * args.duration_scale))
        sessions.append({
            "session_id": f"{scenario.name}_{idx:04d}",
            "scenario": scenario.name,
            "user_id": rng.choice(USER_POOL),
            "quota_mem_mb": ctx["quota_mem_mb"],
            "params": params,
            "queue_waiters": rng.randint(*scenario.queue_waiters),
            "sibling_sessions": rng.randint(*scenario.sibling_sessions),
            "requests": rng.choice(scenario.request_profiles or DEFAULT_REQUEST_PROFILES),
        })
    return sessions


def to_cli(params):
    cli = []
    for name, value in params.items():
        cli += [f"--{name.replace('_', '-')}", str(value)]
    return cli


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=300,
                     help="jumlah total sesi dalam rencana")
    ap.add_argument("--misuse-frac", type=float, default=0.15,
                     help="porsi sesi misuse (sisanya benign)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--resume", action="store_true",
                     help="lanjutkan plan.json di --out-dir, lewati sesi yang selesai")
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--interval-sec", type=float, default=5.0)
    ap.add_argument("--scenarios", default=None,
                     help="daftar nama skenario dipisah koma (default: SEMUA "
                          "skenario yang terdaftar di scenarios/)")
    ap.add_argument("--duration-scale", type=float, default=1.0,
                     help="pengali durasi yang diundi, mis. 0.5 untuk run lebih singkat")
    ap.add_argument("--duration-sec", type=int, default=None,
                     help="mode uji: durasi tetap untuk semua sesi (x Scenario.duration_scale)")
    ap.add_argument("--executor", choices=["local", "k8s"], default="local",
                     help="'local': subprocess di mesin ini (job_runner.py). "
                          "'k8s': submit Pod ke cluster aktif "
                          "(k8s_job_runner.py) -- kubeconfig harus sudah "
                          "menunjuk ke cluster yang benar.")
    args = ap.parse_args()

    code_sha = None
    if args.executor == "local":
        from job_runner import run_labeled_job
    else:
        from k8s_job_runner import CODE_SOURCE, code_sha256, run_labeled_job
        if CODE_SOURCE == "configmap":
            code_sha = code_sha256()

    discover_scenarios()  # import semua modul di scenarios/ -> trigger register()

    os.makedirs(args.out_dir, exist_ok=True)
    plan_path = os.path.join(args.out_dir, "plan.json")
    if args.resume:
        if not os.path.exists(plan_path):
            raise SystemExit(f"--resume: {plan_path} tidak ada")
        with open(plan_path) as f:
            plan = json.load(f)
        print(f"[run_experiment] melanjutkan rencana {plan_path} "
              f"(argumen rencana di CLI diabaikan)")
        if code_sha and plan.get("code_sha256") and plan["code_sha256"] != code_sha:
            print("[run_experiment] WARNING: kode skenario berubah sejak rencana dibuat; "
                  "sesi lanjutan memakai kode baru (dataset tidak lagi seragam)")
    else:
        if os.path.exists(plan_path):
            raise SystemExit(f"{args.out_dir} sudah berisi plan.json -- pakai --resume "
                             f"atau --out-dir baru")
        if args.scenarios:
            scenarios = [get(name.strip()) for name in args.scenarios.split(",")]
        else:
            scenarios = all_scenarios()
        if not scenarios:
            raise RuntimeError(
                "Tidak ada skenario terdaftar -- pastikan folder scenarios/ "
                "berisi file skenario yang memanggil registry.register()."
            )
        mem_budget_mb = detect_mem_budget_mb()
        plan = {
            "version": PLAN_VERSION,
            "created": datetime.now(timezone.utc).isoformat(),
            "args": vars(args),
            "mem_budget_mb": mem_budget_mb,
            "code_sha256": code_sha,
            "sessions": build_plan(scenarios, args, mem_budget_mb),
        }
        with open(plan_path, "w") as f:
            json.dump(plan, f, indent=1)
        with open(os.path.join(args.out_dir, "policy.json"), "w") as f:
            json.dump({**POLICY, "mem_budget_mb": mem_budget_mb,
                       "quota_fracs": QUOTA_FRACS}, f, indent=2)

    sessions = plan["sessions"]
    counts = pd.Series([s["scenario"] for s in sessions]).value_counts().to_dict()
    print(f"[run_experiment] {len(sessions)} sesi, memori GPU/node "
          f"{plan['mem_budget_mb']} MB: {counts}")

    try:
        for idx, entry in enumerate(sessions, start=1):
            session_id = entry["session_id"]
            merged_path = os.path.join(args.out_dir, f"{session_id}.merged.csv")
            if os.path.exists(merged_path) and os.path.getsize(merged_path) > 0:
                continue
            for suffix in (".csv", ".prometheus.csv"):  # sisa sesi yang terputus
                partial = os.path.join(args.out_dir, session_id + suffix)
                if os.path.exists(partial):
                    os.remove(partial)

            scenario = get(entry["scenario"])
            params = entry["params"]
            if "duration_sec" in params:
                scenario = dataclasses.replace(scenario, timeout_sec=int(params["duration_sec"]) + 60)
            print(f"[run_experiment] sesi {idx}/{len(sessions)}: {session_id}")
            run_labeled_job(
                scenario=scenario,
                session_id=session_id,
                out_dir=args.out_dir,
                interval_sec=args.interval_sec,
                args_override=to_cli(params),
                extra_meta={
                    "user_id": entry["user_id"],
                    "scenario_code": scenario.code,
                    "label_class": scenario.label_class,
                    "quota_mem_mb": entry["quota_mem_mb"],
                    **{f"param_{name}": value for name, value in params.items()},
                    "param_queue_waiters": entry["queue_waiters"],
                    "param_sibling_sessions": entry["sibling_sessions"],
                    "param_request_cpu": entry["requests"]["cpu"],
                    "param_request_memory": entry["requests"]["memory"],
                    **scenario.extra_meta,
                },
                n_waiters=entry["queue_waiters"],
                n_siblings=entry["sibling_sessions"],
                job_requests=entry["requests"],
            )
    except KeyboardInterrupt:
        print(f"\n[run_experiment] dihentikan. Lanjutkan dengan:\n"
              f"  python run_experiment.py --executor {args.executor} --resume "
              f"--out-dir {args.out_dir}")
        raise SystemExit(130)

    merged = [os.path.join(args.out_dir, f"{s['session_id']}.merged.csv") for s in sessions]
    merged = [f for f in merged if os.path.exists(f) and os.path.getsize(f) > 0]
    if not merged:
        print("[run_experiment] tidak ada CSV merged dihasilkan, "
              "dataset gabungan dilewati.")
        return

    df = pd.concat([pd.read_csv(f) for f in merged], ignore_index=True)
    out_path = os.path.join(args.out_dir, "dataset.csv")
    df.to_csv(out_path, index=False)
    print(f"[run_experiment] dataset final: {out_path} ({len(df)} baris, "
          f"{df['session_id'].nunique()} sesi, "
          f"{df.groupby('job_type')['session_id'].nunique().to_dict()})")
    print(f"[run_experiment] validasi: python validate_dataset.py {args.out_dir}")


if __name__ == "__main__":
    main()
