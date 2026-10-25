"""
scenarios/benign_cnn_training.py
===================================
Job BENIGN: training CNN kecil pada data sintetis. Pola telemetri yang
diharapkan: GPU utilization naik-turun mengikuti siklus batch/epoch,
I/O muncul saat checkpoint save (bila checkpoint diaktifkan).
"""
import argparse
import time

import torch
import torch.nn as nn
import torch.optim as optim

from scenario_base import LogWriter, Scenario, add_common_args, touch_activity
from registry import register


class SmallCNN(nn.Module):
    def __init__(self, width=32):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv2d(3, width, 3, padding=1), nn.ReLU(),
            nn.Conv2d(width, width * 2, 3, padding=1), nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Flatten(),
            nn.Linear(width * 2 * 16 * 16, 128), nn.ReLU(),
            nn.Linear(128, 10),
        )

    def forward(self, x):
        return self.net(x)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=5)
    ap.add_argument("--steps-per-epoch", type=int, default=100)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--width", type=int, default=32)
    ap.add_argument("--checkpoint-every", type=int, default=1,
                    help="checkpoint tiap N epoch (0 = tidak pernah)")
    ap.add_argument("--duration-sec", type=int, default=0,
                    help=">0: ulangi epoch sampai durasi habis (abaikan --epochs); "
                         "di GPU 5 epoch selesai dalam hitungan detik")
    add_common_args(ap)
    args = ap.parse_args()
    log = LogWriter(args.log_every_sec, args.log_kb)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = SmallCNN(args.width).to(device)
    opt = optim.Adam(model.parameters(), lr=1e-3)
    loss_fn = nn.CrossEntropyLoss()

    start = time.time()
    epoch = 0
    while (time.time() - start < args.duration_sec if args.duration_sec > 0
           else epoch < args.epochs):
        for step in range(args.steps_per_epoch):
            x = torch.randn(args.batch_size, 3, 32, 32, device=device)
            y = torch.randint(0, 10, (args.batch_size,), device=device)
            opt.zero_grad()
            loss = loss_fn(model(x), y)
            loss.backward()
            opt.step()

        if args.checkpoint_every > 0 and (epoch + 1) % args.checkpoint_every == 0:
            # satu file ditimpa: mode durasi bisa ratusan epoch
            torch.save(model.state_dict(), "/tmp/ckpt_last.pt")

        print(f"[benign_cnn_training] epoch={epoch} loss={loss.item():.4f}")
        touch_activity()  # cell training masih berjalan (kernel busy)
        log.tick()
        epoch += 1

    print("[benign_cnn_training] selesai.")


register(Scenario(
    name="benign_cnn_training",
    label="benign",
    script_path="scenarios/benign_cnn_training.py",
    default_args=["--duration-sec", "300", "--steps-per-epoch", "300"],
    timeout_sec=None,  # berhenti sendiri setelah durasi habis
    description="GPU util naik-turun siklik, I/O spike periodik saat checkpoint.",
    params={
        "duration_sec": (180, 900),  # training sah bisa panjang (tumpang-tindih P5)
        "steps_per_epoch": (100, 500),
        "batch_size": [32, 64, 128, 256],
        "width": [16, 32, 64],
        "checkpoint_every": [0, 1, 2, 5],
    },
    queue_waiters=(0, 2),  # job sah tetap bisa berjalan saat cluster ramai
))


if __name__ == "__main__":
    main()
