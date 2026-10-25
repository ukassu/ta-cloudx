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

Variasi per sesi (v2): `Scenario.params` berisi rentang parameter yang
diundi run_experiment.py untuk SETIAP sesi, lalu dioper sebagai argumen
CLI (`duration_sec` -> `--duration-sec`) dan dicatat sebagai kolom
`param_*`. Format nilai:
  (lo, hi)          uniform; int bila keduanya int
  [a, b, c]         pilih salah satu
  callable(rng, ctx) dihitung, mis. mem_frac(0.3, 0.6) = porsi VRAM
  nilai lain        tetap
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

# Parameter yang berlaku untuk semua skenario (bisa ditimpa per skenario):
# log berkala agar "tidak menulis ke disk" bukan penanda label.
COMMON_PARAMS = {
    "log_every_sec": [0, 0, 15, 30, 60],
    "log_kb": (16, 256),
}


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


def mem_frac(lo, hi):
    """Parameter MB sebesar porsi [lo, hi] dari memori GPU node (VRAM),
    atau CPU_MEM_BUDGET_MB di mode CPU -- sama-sama aman di GPU 4 GB
    maupun 16 GB."""
    return lambda rng, ctx: int(rng.uniform(lo, hi) * ctx["mem_budget_mb"])


def session_quota():
    """Kuota memori sesi ini (kolom quota_mem_mb, diundi per sesi)."""
    return lambda rng, ctx: ctx["quota_mem_mb"]


def add_common_args(ap):
    ap.add_argument("--log-every-sec", type=float, default=0,
                    help="tulis log berkala tiap N detik (0 = tidak)")
    ap.add_argument("--log-kb", type=int, default=64,
                    help="ukuran tiap tulisan log (KB)")


class LogWriter:
    """Tulis blok log berkala ke disk (fsync agar terhitung sebagai I/O)."""

    def __init__(self, every_sec, kb, path="/tmp/job.log"):
        self.every_sec, self.kb, self.path = every_sec, kb, path
        self.next_at = time.time() + every_sec

    def tick(self):
        if self.every_sec <= 0 or time.time() < self.next_at:
            return
        with open(self.path, "ab") as f:
            f.write(os.urandom(self.kb * 1024))
            f.flush()
            os.fsync(f.fileno())
        if os.path.getsize(self.path) > 64 * 1024 * 1024:
            os.remove(self.path)  # rotasi sederhana
        self.next_at = time.time() + self.every_sec


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
    """Argumen CLI saat skenario dijalankan tanpa rencana run_experiment
    (pemanggilan langsung run_labeled_job). run_experiment.py memakai
    `params`."""

    timeout_sec: Optional[int] = None
    """Batas waktu paksa (detik) untuk pemanggilan tanpa rencana.
    run_experiment.py menghitungnya dari duration_sec yang diundi."""

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

    params: dict = field(default_factory=dict)
    """Rentang parameter yang diundi per sesi (lihat docstring modul).
    Wajib memuat `duration_sec`; digabung dengan COMMON_PARAMS."""

    weight: float = 1.0
    """Bobot relatif terhadap skenario lain dengan label yang sama saat
    run_experiment.py membagi jumlah sesi."""

    duration_scale: float = 1.0
    """Pengali durasi tetap saat run_experiment.py --duration-sec dipakai
    (mode uji), mis. 3.0 untuk P5 monopolisasi."""

    queue_waiters: Tuple[int, int] = (0, 0)
    """Rentang (min, max) jumlah job GPU pengguna lain yang ikut antre
    selama sesi; diundi per sesi. Hanya berlaku di mode GPU
    (K8S_REQUEST_GPU=true) -- waiter Pending karena GPU dipegang sesi ini,
    sehingga queue_length / queue_wait_max_sec di Prometheus nyata."""

    queue_arrival: str = "start"
    """"start": semua waiter datang di awal sesi; "spread": datang
    bertahap sepanjang sesi (antrean dan waktu tunggu terus naik)."""

    sibling_sessions: Tuple[int, int] = (0, 0)
    """Rentang (min, max) sesi notebook paralel lain milik pengguna yang
    sama (R6). Runner menjalankannya sebagai Pod CPU ringan."""

    request_profiles: Optional[list] = None
    """Pilihan `requests` container job, mis. [{"cpu": "6", "memory": "4Gi"}]
    untuk R4. None = profil default JupyterHub di run_experiment.py, agar
    semua sesi punya request (ada/tidaknya request bukan penanda label)."""

    extra_meta: dict = field(default_factory=dict)
    """Metadata statis skenario yang ikut menjadi kolom CSV."""

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
        if self.params and "duration_sec" not in self.params:
            raise ValueError(f"Scenario '{self.name}': params wajib memuat duration_sec")
