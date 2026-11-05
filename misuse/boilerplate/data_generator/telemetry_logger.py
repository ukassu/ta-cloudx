"""
data_generator/telemetry_logger.py
=====================================
Merekam telemetri CPU/memori/GPU/I/O sebuah sesi job (proses + seluruh
child process-nya) pada interval tetap, menuliskannya sebagai CSV
berlabel. GENERIK terhadap skenario apapun -- tidak tahu dan tidak
peduli skenario mana yang dijalankan, hanya butuh root_pid + metadata
label/job_type/extra_meta dari job_runner.py.
"""

import csv
import os
import subprocess
import threading
import time
from datetime import datetime, timezone

import psutil


def _run(cmd):
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return ""


def get_gpu_device_stats(gpu_index=0):
    out = _run([
        "nvidia-smi", f"--id={gpu_index}",
        "--query-gpu=utilization.gpu,utilization.memory,memory.used,memory.total",
        "--format=csv,noheader,nounits",
    ])
    if not out:
        return {"gpu_util_pct": None, "gpu_mem_util_pct": None,
                 "gpu_mem_used_mb": None, "gpu_mem_total_mb": None}
    parts = [p.strip() for p in out.split(",")]
    return {
        "gpu_util_pct": float(parts[0]), "gpu_mem_util_pct": float(parts[1]),
        "gpu_mem_used_mb": float(parts[2]), "gpu_mem_total_mb": float(parts[3]),
    }


def get_process_gpu_memory_mb(pids):
    out = _run(["nvidia-smi", "--query-compute-apps=pid,used_memory",
                 "--format=csv,noheader,nounits"])
    total = 0.0
    if not out:
        return total
    pid_set = set(pids)
    for line in out.splitlines():
        try:
            pid_str, mem_str = [x.strip() for x in line.split(",")]
            if int(pid_str) in pid_set:
                total += float(mem_str)
        except Exception:
            continue
    return total


def _collect_pid_tree(root_pid):
    pids = [root_pid]
    try:
        root = psutil.Process(root_pid)
        pids += [p.pid for p in root.children(recursive=True)]
    except psutil.NoSuchProcess:
        pass
    return pids


def get_process_cpu_mem_io(pids):
    cpu_pct = mem_rss_mb = io_read_mb = io_write_mb = 0.0
    alive = 0
    for pid in pids:
        try:
            p = psutil.Process(pid)
            cpu_pct += p.cpu_percent(interval=None)
            mem_rss_mb += p.memory_info().rss / (1024 ** 2)
            io = p.io_counters()
            io_read_mb += io.read_bytes / (1024 ** 2)
            io_write_mb += io.write_bytes / (1024 ** 2)
            alive += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {"cpu_pct": cpu_pct, "mem_rss_mb": mem_rss_mb,
             "io_read_mb": io_read_mb, "io_write_mb": io_write_mb,
             "num_alive_processes": alive}


class TelemetryLogger:
    """
    Parameters
    ----------
    root_pid : int      PID proses utama job.
    session_id : str    ID unik sesi.
    label : str          "benign" / "misuse" (dari Scenario.label).
    job_type : str        Nama skenario (dari Scenario.name).
    extra_meta : dict     Kolom metadata tambahan opsional -- dipakai
                          skenario yang punya metadata khusus (mis.
                          scenarios/misuse_quota_exceed.py butuh kolom
                          claimed_quota_mb).
    """

    FIELDNAMES = [
        "timestamp", "session_id", "label", "job_type",
        "cpu_pct", "mem_rss_mb", "io_read_mb", "io_write_mb",
        "num_alive_processes", "gpu_util_pct", "gpu_mem_util_pct",
        "gpu_mem_used_mb_device", "gpu_mem_used_mb_process", "gpu_mem_total_mb",
    ]

    def __init__(self, root_pid, session_id, label, job_type, out_path,
                 interval_sec=5.0, gpu_index=0, extra_meta=None):
        self.root_pid = root_pid
        self.session_id = session_id
        self.label = label
        self.job_type = job_type
        self.out_path = out_path
        self.interval_sec = interval_sec
        self.gpu_index = gpu_index
        self.extra_meta = extra_meta or {}
        self._stop_flag = threading.Event()
        self._thread = None

        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        fieldnames = self.FIELDNAMES + list(self.extra_meta.keys())
        self._file = open(out_path, "w", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=fieldnames)
        self._writer.writeheader()

        for pid in _collect_pid_tree(root_pid):
            try:
                psutil.Process(pid).cpu_percent(interval=None)
            except Exception:
                pass

    def _loop(self):
        next_sample = time.time()
        while not self._stop_flag.is_set():
            pids = _collect_pid_tree(self.root_pid)
            if not pids:
                break
            # waktu sampel dicatat sebelum query nvidia-smi yang lamanya bervariasi
            sampled_at = datetime.now(timezone.utc)
            proc_stats = get_process_cpu_mem_io(pids)
            gpu_dev = get_gpu_device_stats(self.gpu_index)
            gpu_proc_mem = get_process_gpu_memory_mb(pids)

            row = {
                "timestamp": sampled_at.isoformat(),
                "session_id": self.session_id, "label": self.label,
                "job_type": self.job_type,
                "cpu_pct": round(proc_stats["cpu_pct"], 3),
                "mem_rss_mb": round(proc_stats["mem_rss_mb"], 3),
                "io_read_mb": round(proc_stats["io_read_mb"], 3),
                "io_write_mb": round(proc_stats["io_write_mb"], 3),
                "num_alive_processes": proc_stats["num_alive_processes"],
                "gpu_util_pct": gpu_dev["gpu_util_pct"],
                "gpu_mem_util_pct": gpu_dev["gpu_mem_util_pct"],
                "gpu_mem_used_mb_device": gpu_dev["gpu_mem_used_mb"],
                "gpu_mem_used_mb_process": round(gpu_proc_mem, 3),
                "gpu_mem_total_mb": gpu_dev["gpu_mem_total_mb"],
            }
            row.update(self.extra_meta)
            self._writer.writerow(row)
            self._file.flush()

            if proc_stats["num_alive_processes"] == 0:
                break
            # jadwal tetap, sama dengan telemetry_sidecar.py
            next_sample = max(next_sample + self.interval_sec, time.time())
            self._stop_flag.wait(max(0.0, next_sample - time.time()))

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_flag.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_sec + 2)
        self._file.close()
