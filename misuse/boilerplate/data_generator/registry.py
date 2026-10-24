"""
data_generator/registry.py
=============================
Registry sederhana untuk skenario job. Skenario mendaftarkan diri
lewat `register()` saat modulnya di-import (lihat contoh di
scenarios/misuse_idle_hoarding.py, baris paling bawah file). Fungsi
`discover_scenarios()` meng-import SELURUH modul di folder scenarios/
secara otomatis, sehingga menambah file skenario baru langsung
terdeteksi tanpa mendaftarkan manual di tempat lain.
"""

import importlib
import pkgutil

from scenario_base import Scenario

_REGISTRY = {}


def register(scenario: Scenario):
    """Dipanggil oleh tiap modul skenario saat di-import (self-registration)."""
    if scenario.name in _REGISTRY:
        raise ValueError(f"Skenario dengan nama '{scenario.name}' sudah terdaftar "
                          f"-- pastikan nama unik antar file di scenarios/.")
    _REGISTRY[scenario.name] = scenario


def get(name: str) -> Scenario:
    if name not in _REGISTRY:
        raise KeyError(f"Skenario '{name}' tidak ditemukan. Tersedia: "
                        f"{list(_REGISTRY)}. Sudah panggil discover_scenarios()?")
    return _REGISTRY[name]


def all_scenarios() -> list:
    return list(_REGISTRY.values())


def benign_scenarios() -> list:
    return [s for s in _REGISTRY.values() if s.label == "benign"]


def misuse_scenarios() -> list:
    return [s for s in _REGISTRY.values() if s.label == "misuse"]


def discover_scenarios(package_name: str = "scenarios"):
    """
    Import semua modul di dalam package `scenarios/` supaya efek
    samping `register()` di tiap modul terpicu. Panggil ini SEKALI di
    awal run_experiment.py sebelum memakai all_scenarios()/get().

    TODO (mahasiswa): tidak perlu diubah untuk menambah skenario baru
    -- cukup tambah file baru di scenarios/ mengikuti pola
    template_new_scenario.py, fungsi ini otomatis menemukannya.
    """
    package = importlib.import_module(package_name)
    for _, module_name, _ in pkgutil.iter_modules(package.__path__):
        if module_name.startswith("template_"):
            continue  # kerangka salinan, bukan skenario sungguhan
        importlib.import_module(f"{package_name}.{module_name}")
    return all_scenarios()
