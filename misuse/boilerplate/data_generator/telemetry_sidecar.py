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

io_read_mb/io_write_mb dari psutil io_counters(); di kernel tanpa /proc/<pid>/io
(mis. WSL2 6.18) diambil dari io.stat cgroup container job (lihat HAS_PROC_IO).

gpu_util_pct/gpu_mem_util_pct = rata-rata bacaan nvidia-smi tiap 200 ms selama
interval sampling (GpuSampler), bukan satu bacaan sesaat: utilization.gpu hanya
mencakup jendela pendek (~1/6 s di WSL2), sehingga satu bacaan per 5 s ter-alias
(uji di PC lab: beban duty 74% terbaca 96%, burst 6% terbaca 0%).

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
import threading
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


def _round(value):
    return None if value is None else round(value, 3)


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


class GpuSampler:
    """nvidia-smi persisten (-lms) di thread latar. interval_stats() memberi
    rata-rata utilisasi sejak pemanggilan sebelumnya dan memori terakhir, atau
    None bila tidak ada bacaan (tanpa GPU / nvidia-smi gagal -> pemanggil
    memakai get_gpu_device_stats())."""

    def __init__(self, period_ms=200):
        self._rows = []
        self._lock = threading.Lock()
        try:
            self._proc = subprocess.Popen(
                ["nvidia-smi", "-i", "0",
                 "--query-gpu=utilization.gpu,utilization.memory,memory.used,memory.total",
                 "--format=csv,noheader,nounits", "-lms", str(period_ms)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError:
            self._proc = None
            return
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self._proc.stdout:
            try:
                row = [float(p) for p in line.split(",")[:4]]
            except ValueError:
                continue   # mis. "[N/A]"
            if len(row) == 4:
                with self._lock:
                    self._rows.append(row)

    def interval_stats(self):
        with self._lock:
            rows, self._rows = self._rows, []
        if not rows:
            return None
        n = len(rows)
        return {"gpu_util_pct": round(sum(r[0] for r in rows) / n, 1),
                "gpu_mem_util_pct": round(sum(r[1] for r in rows) / n, 1),
                "gpu_mem_used_mb": rows[-1][2], "gpu_mem_total_mb": rows[-1][3]}

    def stop(self):
        if self._proc is not None:
            self._proc.terminate()


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


# Kernel tanpa CONFIG_TASK_IO_ACCOUNTING (mis. kernel WSL2 6.18 di PC lab) tidak
# punya /proc/<pid>/io, sehingga psutil tidak menyediakan Process.io_counters().
HAS_PROC_IO = hasattr(psutil.Process, "io_counters")


def get_cgroup_io_mb(pid):
    """(read_mb, write_mb) kumulatif dari io.stat cgroup container proses pid --
    byte yang benar-benar sampai ke storage, sumber yang sama dengan
    container_fs_{reads,writes}_bytes_total cAdvisor. Dibaca lewat /proc/<pid>/root
    karena sidecar berada di cgroup namespace lain. None bila tidak terbaca."""
    rbytes = wbytes = 0
    try:
        with open(f"/proc/{pid}/root/sys/fs/cgroup/io.stat") as f:
            for line in f:                      # "8:48 rbytes=.. wbytes=.. rios=.."
                for field in line.split()[1:]:
                    key, _, value = field.partition("=")
                    if key == "rbytes":
                        rbytes += int(value)
                    elif key == "wbytes":
                        wbytes += int(value)
    except (OSError, ValueError):
        return None
    return rbytes / (1024 ** 2), wbytes / (1024 ** 2)


def get_process_cpu_mem_io(pids):
    cpu_pct = mem_rss_mb = io_read_mb = io_write_mb = 0.0
    alive = 0
    for pid in pids:
        try:
            p = _process(pid)
            cpu_pct += p.cpu_percent(interval=None)
            mem_rss_mb += p.memory_info().rss / (1024 ** 2)
            if HAS_PROC_IO:
                io = p.io_counters()
                io_read_mb += io.read_bytes / (1024 ** 2)
                io_write_mb += io.write_bytes / (1024 ** 2)
            alive += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            _PROCS.pop(pid, None)
            continue
    if not HAS_PROC_IO:
        # Semua proses job ada di cgroup container yang sama; pids[0] = proses root job
        io = get_cgroup_io_mb(pids[0]) if pids else None
        io_read_mb, io_write_mb = io if io else (None, None)
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

    print(f"[telemetry_sidecar] target ditemukan pid={root_pid}, mulai rekam "
          f"(sumber io: {'psutil' if HAS_PROC_IO else 'cgroup io.stat'}).", flush=True)

    fieldnames = [
        "timestamp", "session_id", "label", "job_type",
        "cpu_pct", "mem_rss_mb", "io_read_mb", "io_write_mb",
        "num_alive_processes", "gpu_util_pct", "gpu_mem_util_pct",
        "gpu_mem_used_mb_device", "gpu_mem_used_mb_process", "gpu_mem_total_mb",
        "session_age_sec", "last_activity_age_sec", "gpu_alloc_sec",
    ] + list(extra_meta.keys())

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        gpu_sampler = GpuSampler()   # mulai sekarang: interval pertama ikut dirata-rata
        for pid in collect_pid_tree(root_pid):
            try:
                _process(pid)
            except psutil.NoSuchProcess:
                pass
        time.sleep(interval_sec)
        next_sample = time.time()

        while True:
            if not target_is_running(root_pid):
                print("[telemetry_sidecar] proses target selesai, berhenti.",
                      flush=True)
                break
            pids = collect_pid_tree(root_pid)
            if not pids:
                break

            # waktu sampel dicatat sebelum query nvidia-smi yang lamanya bervariasi
            sampled_at = datetime.now(timezone.utc)
            proc_stats = get_process_cpu_mem_io(pids)
            gpu_dev = gpu_sampler.interval_stats() or get_gpu_device_stats()
            gpu_proc_mem = get_process_gpu_memory_mb(pids)

            row = {
                "timestamp": sampled_at.isoformat(),
                "session_id": session_id, "label": label, "job_type": job_type,
                "cpu_pct": round(proc_stats["cpu_pct"], 3),
                "mem_rss_mb": round(proc_stats["mem_rss_mb"], 3),
                "io_read_mb": _round(proc_stats["io_read_mb"]),
                "io_write_mb": _round(proc_stats["io_write_mb"]),
                "num_alive_processes": proc_stats["num_alive_processes"],
                "gpu_util_pct": gpu_dev["gpu_util_pct"],
                "gpu_mem_util_pct": gpu_dev["gpu_mem_util_pct"],
                "gpu_mem_used_mb_device": gpu_dev["gpu_mem_used_mb"],
                "gpu_mem_used_mb_process": round(gpu_proc_mem, 3),
                "gpu_mem_total_mb": gpu_dev["gpu_mem_total_mb"],
            }
            row.update(get_session_context(root_pid, gpu_requested))
            row.update(extra_meta)
            writer.writerow(row)
            f.flush()
            # Jadwal tetap: lama query nvidia-smi (lebih lambat saat GPU idle)
            # tidak menggeser periode sampling, jadi jumlah baris per sesi
            # hanya bergantung pada durasi, bukan skenario.
            next_sample = max(next_sample + interval_sec, time.time())
            time.sleep(max(0.0, next_sample - time.time()))
        gpu_sampler.stop()

    with open("/data/DONE", "w") as f:
        f.write("done")

    # Keep the Pod running until the runner copies telemetry.csv.
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
