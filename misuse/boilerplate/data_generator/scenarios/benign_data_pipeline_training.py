"""
scenarios/benign_data_pipeline_training.py
=============================================
Job BENIGN: training dengan pipeline data nyata -- shard dataset dibaca
dari disk, diaugmentasi di CPU, lalu dilatih di GPU; tiap epoch ditutup
evaluasi dan checkpoint. Utilisasi GPU turun saat fase data loading
dan I/O baca muncul berkala. Pembanding untuk R1 ("periksa fase data
loading") dan R7: penurunan GPU di sini singkat dan sah.
"""
import argparse
import os
import time

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim

from scenario_base import Scenario, touch_activity
from registry import register

DATA_DIR = "/tmp/dataset"


def make_shards(n_shards, samples_per_shard):
    os.makedirs(DATA_DIR, exist_ok=True)
    paths = []
    for i in range(n_shards):
        path = os.path.join(DATA_DIR, f"shard_{i:02d}.npy")
        np.save(path, np.random.rand(samples_per_shard, 3, 32, 32).astype(np.float32))
        paths.append(path)
    return paths


def read_shard(path):
    data = np.load(path)
    # buang dari page cache agar epoch berikutnya benar-benar membaca disk
    fd = os.open(path, os.O_RDONLY)
    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    os.close(fd)
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=600)
    ap.add_argument("--shards", type=int, default=8)
    ap.add_argument("--samples-per-shard", type=int, default=2048)
    ap.add_argument("--batch-size", type=int, default=128)
    args = ap.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    touch_activity()
    paths = make_shards(args.shards, args.samples_per_shard)
    model = nn.Sequential(
        nn.Conv2d(3, 32, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
        nn.Conv2d(32, 64, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
        nn.Flatten(), nn.Linear(64 * 8 * 8, 10),
    ).to(device)
    opt = optim.SGD(model.parameters(), lr=0.01)
    loss_fn = nn.CrossEntropyLoss()

    start = time.time()
    epoch = 0
    while time.time() - start < args.duration_sec:
        model.train()
        for path in paths:
            if time.time() - start >= args.duration_sec:
                break
            data = read_shard(path)
            data = np.flip(data, axis=3) if epoch % 2 else data   # augmentasi CPU
            data = (data - data.mean()) / (data.std() + 1e-6)
            x_all = torch.from_numpy(np.ascontiguousarray(data))
            for i in range(0, len(x_all), args.batch_size):
                x = x_all[i:i + args.batch_size].to(device)
                y = torch.randint(0, 10, (len(x),), device=device)
                opt.zero_grad()
                loss_fn(model(x), y).backward()
                opt.step()
            touch_activity()
        model.eval()
        with torch.no_grad():   # fase evaluasi
            for _ in range(20):
                model(torch.randn(args.batch_size, 3, 32, 32, device=device))
        torch.save(model.state_dict(), "/tmp/ckpt_last.pt")
        print(f"[benign_data_pipeline_training] epoch={epoch}")
        epoch += 1

    print("[benign_data_pipeline_training] selesai.")


register(Scenario(
    name="benign_data_pipeline_training",
    label="benign",
    script_path="scenarios/benign_data_pipeline_training.py",
    default_args=["--duration-sec", "600"],
    timeout_sec=None,
    description="GPU tinggi dengan dip berkala saat baca shard dari disk + "
                "augmentasi CPU; I/O baca periodik, checkpoint tiap epoch.",
    queue_waiters=(0, 2),
))


if __name__ == "__main__":
    main()
