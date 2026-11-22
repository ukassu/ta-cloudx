#!/usr/bin/env bash
# data_generator/prometheus_archive.sh
# =====================================
# Sajikan arsip TSDB Prometheus sebuah run dengan Prometheus lokal (Docker),
# agar metrik yang tidak masuk dataset.csv (OTel, Inspektor Gadget, NetObserv)
# tetap bisa di-query setelah cluster dimatikan. TSDB Prometheus di cluster
# berupa emptyDir: hilang saat down-all.sh dan tersimpan paling lama 10 hari.
#
#   ./prometheus_archive.sh serve <arsip_dir> [port]   # default port 19090
#   ./prometheus_archive.sh stop
#
# <arsip_dir>/tsdb       salinan asli TSDB (tidak pernah disentuh)
# <arsip_dir>/work/tsdb  salinan kerja yang dipakai container (dibuat otomatis;
#                        Prometheus menulis WAL/lock ke direktori datanya)
#
# Lalu, misalnya:
#   PROMETHEUS_URL=http://127.0.0.1:19090 python fetch_supporting_metrics.py <out_dir>
set -euo pipefail

NAME="${PROM_ARCHIVE_NAME:-prom-archive}"
IMAGE="quay.io/prometheus/prometheus:v3.16.0"

case "${1:-}" in
  serve)
    ARCHIVE="$(realpath "${2:?pemakaian: $0 serve <arsip_dir> [port]}")"
    PORT="${3:-19090}"
    [ -d "$ARCHIVE/tsdb" ] || { echo "tidak ada $ARCHIVE/tsdb" >&2; exit 1; }
    if [ ! -d "$ARCHIVE/work/tsdb" ]; then
      echo "[archive] membuat salinan kerja $ARCHIVE/work/tsdb ..."
      mkdir -p "$ARCHIVE/work" && cp -a "$ARCHIVE/tsdb" "$ARCHIVE/work/tsdb"
    fi
    # scrape_interval 1 jam: Prometheus ini hanya untuk query, tidak men-scrape apa pun
    printf 'global:\n  scrape_interval: 1h\n' > "$ARCHIVE/prometheus.yml"
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --name "$NAME" --user "$(id -u):$(id -g)" -p "127.0.0.1:$PORT:9090" \
      -v "$ARCHIVE/work/tsdb":/prometheus \
      -v "$ARCHIVE/prometheus.yml":/etc/prometheus/prometheus.yml:ro \
      "$IMAGE" --config.file=/etc/prometheus/prometheus.yml \
      --storage.tsdb.path=/prometheus --storage.tsdb.retention.time=10y >/dev/null
    for _ in $(seq 90); do
      if curl -sf --max-time 3 "http://127.0.0.1:$PORT/-/ready" >/dev/null; then
        echo "[archive] siap: http://127.0.0.1:$PORT (container $NAME)"
        exit 0
      fi
      sleep 2
    done
    echo "[archive] belum siap setelah 180 s; cek: docker logs $NAME" >&2
    exit 1
    ;;
  stop)
    docker rm -f "$NAME" >/dev/null && echo "[archive] container $NAME dihentikan"
    ;;
  *)
    echo "pemakaian: $0 serve <arsip_dir> [port] | stop" >&2
    exit 2
    ;;
esac
