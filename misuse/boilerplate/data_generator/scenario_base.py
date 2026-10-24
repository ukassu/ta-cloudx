"""
data_generator/scenario_base.py
==================================
Kontrak untuk SATU skenario job (benign ATAU misuse). Setiap skenario
adalah sebuah script Python yang bisa dijalankan berdiri sendiri
(`python nama_file.py --arg ...`) DAN mendaftarkan dirinya ke registry
(lihat registry.py) supaya bisa ditemukan otomatis oleh run_experiment.py.

Menambah jenis misuse baru = membuat satu file baru di scenarios/
mengikuti pola ini -- TIDAK PERLU mengubah registry.py, job_runner.py,
atau run_experiment.py sama sekali. Lihat
scenarios/template_new_scenario.py untuk kerangka siap-pakai.
"""

import os
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Kode taksonomi yang dipakai CATS (Kajian Misuse Detection CloudX v5,
# Bagian 2-4). "BENIGN" untuk job wajar.
BENIGN_CODE = "BENIGN"
LABEL_CLASSES = ("K1", "K2", "K3")

ACTIVITY_FILE = os.environ.get("ACTIVITY_FILE", "/data/last_activity")


def touch_activity():
    """Tandai aktivitas notebook (kernel menjalankan cell / pengguna
    berinteraksi). Sidecar membaca mtime file ini sebagai
    `last_activity_age_sec` -- analog last activity pada JupyterHub idle
    culler. Gagal senyap bila /data tidak ada (eksekusi lokal)."""
    try:
        with open(ACTIVITY_FILE, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


@dataclass
class Scenario:
    name: str
    """Identifier unik skenario, dipakai sebagai job_type di dataset
    output (kolom `job_type`) dan sebagai penanda proses untuk
    telemetry logger. Gunakan snake_case, mis. "misuse_idle_hoarding"."""

    label: str
    """WAJIB salah satu: "benign" atau "misuse"."""

    script_path: str
    """Path ke script Python skenario ini -- relatif terhadap folder
    scenarios/ untuk eksekusi lokal, atau path di dalam image container
    (mis. "/app/scenarios/misuse_idle_hoarding.py") untuk eksekusi K8s."""

    default_args: List[str] = field(default_factory=list)
    """Argumen CLI default saat dijalankan run_experiment.py, mis.
    ["--duration-sec", "600"]. Bisa di-override per pemanggilan."""

    timeout_sec: Optional[int] = None
    """Batas waktu paksa (detik). WAJIB diisi untuk skenario yang tidak
    berhenti sendiri (mis. idle-hoarding, zombie job) -- runner akan
    mem-force-kill proses setelah durasi ini. None = biarkan skenario
    berhenti sendiri (skenario yang memang punya durasi internal)."""

    description: str = ""
    """Penjelasan singkat pola telemetri yang diharapkan dari skenario
    ini -- membantu dokumentasi otomatis & debugging, tidak dipakai
    logika runner."""

    code: str = BENIGN_CODE
    """Kode taksonomi CATS: R1-R7, P1-P6, S1-S7; "BENIGN" untuk benign.
    Ditulis ke kolom `scenario_code`."""

    label_class: str = "-"
    """Kelas respons: K1 (inefisien tapi sah), K2 (melanggar kebijakan),
    K3 (berbahaya); "-" untuk benign. Ditulis ke kolom `label_class`."""

    duration_scale: float = 1.0
    """Pengali --duration-sec dari run_experiment.py, mis. 3.0 untuk
    skenario yang memang berdurasi panjang (P5 monopolisasi)."""

    queue_waiters: Tuple[int, int] = (0, 0)
    """Rentang (min, max) jumlah job GPU pengguna lain yang ikut antre
    selama sesi; diundi per sesi. Hanya berlaku di mode GPU
    (K8S_REQUEST_GPU=true) -- waiter Pending karena GPU dipegang sesi ini,
    sehingga queue_length / queue_wait_max_sec di Prometheus nyata."""

    queue_arrival: str = "start"
    """"start": semua waiter datang di awal sesi; "spread": datang
    bertahap sepanjang sesi (antrean dan waktu tunggu terus naik)."""

    sibling_sessions: int = 0
    """Jumlah sesi notebook paralel lain milik pengguna yang sama (R6).
    Runner menjalankannya sebagai Pod CPU ringan selama sesi utama."""

    job_resources: dict = field(default_factory=dict)
    """`resources` Kubernetes untuk container job, mis.
    {"requests": {"cpu": "4", "memory": "2Gi"}} untuk R4. nvidia.com/gpu
    ditambahkan otomatis oleh runner di mode GPU."""

    extra_meta: dict = field(default_factory=dict)
    """Metadata statis skenario yang ikut menjadi kolom CSV, mis.
    {"claimed_quota_mb": 512} untuk R3."""

    def __post_init__(self):
        if self.label not in ("benign", "misuse"):
            raise ValueError(
                f"Scenario '{self.name}': label harus 'benign' atau "
                f"'misuse', dapat '{self.label}'"
            )
        if self.label == "benign":
            if self.code != BENIGN_CODE or self.label_class != "-":
                raise ValueError(
                    f"Scenario '{self.name}': benign harus code="
                    f"'{BENIGN_CODE}' dan label_class='-'"
                )
        elif self.code == BENIGN_CODE or self.label_class not in LABEL_CLASSES:
            raise ValueError(
                f"Scenario '{self.name}': misuse wajib punya code taksonomi "
                f"(mis. 'R1') dan label_class salah satu {LABEL_CLASSES}"
            )
        if self.queue_arrival not in ("start", "spread"):
            raise ValueError(
                f"Scenario '{self.name}': queue_arrival harus 'start' atau 'spread'"
            )
