"""
data_generator/telemetry_sidecar.py
=====================================
Berjalan sebagai SIDECAR CONTAINER di dalam Pod yang sama dengan job
container, memanfaatkan `shareProcessNamespace: true` pada Pod spec
supaya sidecar bisa melihat proses job lewat psutil walau beda
container. Dengan K8S_REQUEST_GPU=true, runner memberi sidecar akses
nvidia-smi sehingga kolom gpu_* level device terisi (di WSL2 kolom
gpu_mem_used_mb_process tetap 0: nvidia-smi tidak melaporkan proses).
Tanpa GPU, kolom GPU bernilai None (nvidia-smi tidak ada), TIDAK
menyebabkan error, tapi juga tidak memberi sinyal. Sinyal misuse tetap terekam lewat
CPU/memori/IO, karena torch.device("cuda" if ... else "cpu") pada
skenario otomatis fallback ke CPU -- alokasi "GPU memory" pada
misuse_idle_hoarding, misuse_zombie_job, dsb otomatis menjadi alokasi
RAM biasa (skenario memakai torch.zeros agar halaman memori benar-benar
terpakai), sehingga tetap tertangkap oleh kolom mem_rss_mb.

Metrik Prometheus TIDAK diambil di sini: k8s_job_runner.py melakukan
query_range dari host setelah job selesai, supaya traffic query sidecar
tidak ikut terhitung di metrik network Pod.

Kolom konteks sesi (Kajian CATS, indikator R1/R2/R3):
  session_age_sec        umur proses job
  last_activity_age_sec  detik sejak touch_activity() terakhir (ACTIVITY_FILE);
                         sama dengan session_age_sec bila tidak pernah ada aktivitas
  gpu_alloc_sec          detik-GPU teralokasi sejauh ini (session_age x GPU_REQUESTED)

Environment variables (diisi dari Pod spec):
  SESSION_ID, LABEL, JOB_TYPE, JOB_CMD_MATCH, INTERVAL_SEC, GPU_REQUESTED,
  ACTIVITY_FILE, EXTRA_META_JSON (lihat k8s_job_runner.py)
"""

import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import psutil

OUT_PATH = "/data/telemetry.csv"
ACTIVITY_FILE = os.environ.get("ACTIVITY_FILE", "/data/last_activity")
POLL_FOR_TARGET_SEC = 30


def _run(cmd):
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return ""


def get_gpu_device_stats():
    """Tanpa GPU: nvidia-smi tidak ada di PATH -> _run() gagal senyap,
    seluruh kolom GPU bernilai None. Tidak menyebabkan error di baris
    manapun -- ini SENGAJA didesain graceful degradation."""
    out = _run(["nvidia-smi",
                 "--query-gpu=utilization.gpu,utilization.memory,memory.used,memory.total",
                 "--format=csv,noheader,nounits"])
    if not out:
        return {"gpu_util_pct": None, "gpu_mem_util_pct": None,
                 "gpu_mem_used_mb": None, "gpu_mem_total_mb": None}
    parts = [p.strip() for p in out.split(",")]
    return {"gpu_util_pct": float(parts[0]), "gpu_mem_util_pct": float(parts[1]),
             "gpu_mem_used_mb": float(parts[2]), "gpu_mem_total_mb": float(parts[3])}


def get_process_gpu_memory_mb(pids):
    out = _run(["nvidia-smi", "--query-compute-apps=pid,used_memory",
                 "--format=csv,noheader,nounits"])
    if not out:
        return 0.0
    total, pid_set = 0.0, set(pids)
    for line in out.splitlines():
        try:
            pid_str, mem_str = [x.strip() for x in line.split(",")]
            if int(pid_str) in pid_set:
                total += float(mem_str)
        except Exception:
            continue
    return total


def find_target_root_pid(cmd_match):
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(p.info["cmdline"] or [])
            if cmd_match in cmdline:
                return p.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def collect_pid_tree(root_pid):
    pids = [root_pid]
    try:
        root = psutil.Process(root_pid)
        pids += [c.pid for c in root.children(recursive=True)]
    except psutil.NoSuchProcess:
        pass
    return pids


def target_is_running(root_pid):
    try:
        status = psutil.Process(root_pid).status()
    except psutil.NoSuchProcess:
        return False
    return status not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)


# cpu_percent(interval=None) membandingkan dengan pemanggilan sebelumnya
# pada OBJEK Process yang sama; objek baru selalu mengembalikan 0.0.
# Karena itu objek disimpan antar iterasi.
_PROCS = {}


def _process(pid):
    proc = _PROCS.get(pid)
    if proc is None:
        proc = psutil.Process(pid)
        proc.cpu_percent(interval=None)  # baseline; nilai pertama selalu 0.0
        _PROCS[pid] = proc
    return proc


def get_process_cpu_mem_io(pids):
    cpu_pct = mem_rss_mb = io_read_mb = io_write_mb = 0.0
    alive = 0
    for pid in pids:
        try:
            p = _process(pid)
            cpu_pct += p.cpu_percent(interval=None)
            mem_rss_mb += p.memory_info().rss / (1024 ** 2)
            io = p.io_counters()
            io_read_mb += io.read_bytes / (1024 ** 2)
            io_write_mb += io.write_bytes / (1024 ** 2)
            alive += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            _PROCS.pop(pid, None)
            continue
    return {"cpu_pct": cpu_pct, "mem_rss_mb": mem_rss_mb,
             "io_read_mb": io_read_mb, "io_write_mb": io_write_mb,
             "num_alive_processes": alive}


def get_session_context(root_pid, gpu_requested):
    now = time.time()
    try:
        started = psutil.Process(root_pid).create_time()
    except psutil.NoSuchProcess:
        started = now
    try:
        last_activity = os.path.getmtime(ACTIVITY_FILE)
    except OSError:
        last_activity = started
    session_age = now - started
    return {"session_age_sec": round(session_age, 1),
            "last_activity_age_sec": round(now - max(last_activity, started), 1),
            "gpu_alloc_sec": round(session_age * gpu_requested, 1)}


def main():
    session_id = os.environ["SESSION_ID"]
    label = os.environ["LABEL"]
    job_type = os.environ["JOB_TYPE"]
    cmd_match = os.environ["JOB_CMD_MATCH"]
    interval_sec = float(os.environ.get("INTERVAL_SEC", "5"))
    extra_meta = json.loads(os.environ.get("EXTRA_META_JSON", "{}"))
    gpu_requested = int(os.environ.get("GPU_REQUESTED", "0"))

    print(f"[telemetry_sidecar] menunggu proses target (match='{cmd_match}')...",
          flush=True)
    start_wait = time.time()
    root_pid = None
    while time.time() - start_wait < POLL_FOR_TARGET_SEC:
        root_pid = find_target_root_pid(cmd_match)
        if root_pid is not None:
            break
        time.sleep(1)

    if root_pid is None:
        print("[telemetry_sidecar] proses target tidak ditemukan, keluar.", flush=True)
        sys.exit(1)

    print(f"[telemetry_sidecar] target ditemukan pid={root_pid}, mulai rekam.",
          flush=True)

    fieldnames = [
        "timestamp", "session_id", "label", "job_type",
        "cpu_pct", "mem_rss_mb", "io_read_mb", "io_write_mb",
        "num_alive_processes", "gpu_util_pct", "gpu_mem_util_pct",
        "gpu_mem_used_mb_device", "gpu_mem_used_mb_process",
        "session_age_sec", "last_activity_age_sec", "gpu_alloc_sec",
    ] + list(extra_meta.keys())

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for pid in collect_pid_tree(root_pid):
            try:
                _process(pid)
            except psutil.NoSuchProcess:
                pass
        time.sleep(interval_sec)

        while True:
            if not target_is_running(root_pid):
                print("[telemetry_sidecar] proses target selesai, berhenti.",
                      flush=True)
                break
            pids = collect_pid_tree(root_pid)
            if not pids:
                break

            proc_stats = get_process_cpu_mem_io(pids)
            gpu_dev = get_gpu_device_stats()
            gpu_proc_mem = get_process_gpu_memory_mb(pids)

            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id, "label": label, "job_type": job_type,
                "cpu_pct": round(proc_stats["cpu_pct"], 3),
                "mem_rss_mb": round(proc_stats["mem_rss_mb"], 3),
                "io_read_mb": round(proc_stats["io_read_mb"], 3),
                "io_write_mb": round(proc_stats["io_write_mb"], 3),
                "num_alive_processes": proc_stats["num_alive_processes"],
                "gpu_util_pct": gpu_dev["gpu_util_pct"],
                "gpu_mem_util_pct": gpu_dev["gpu_mem_util_pct"],
                "gpu_mem_used_mb_device": gpu_dev["gpu_mem_used_mb"],
                "gpu_mem_used_mb_process": round(gpu_proc_mem, 3),
            }
            row.update(get_session_context(root_pid, gpu_requested))
            row.update(extra_meta)
            writer.writerow(row)
            f.flush()
            time.sleep(interval_sec)

    with open("/data/DONE", "w") as f:
        f.write("done")

    # Keep the Pod running until the runner copies telemetry.csv.
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
