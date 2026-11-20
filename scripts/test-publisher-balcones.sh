#!/usr/bin/env bash
# Manual pilot: capture one CPU/RAM batch and test persistent retry to a local sink.
set -euo pipefail

mode=${1:-}
case "$mode" in capture|fail|send|send-otlp|status) ;; *) echo "Uso: bash $0 {capture|fail|send|send-otlp|status}" >&2; exit 2;; esac
state=${OTLOGGER_TEST_STATE:-"$HOME/otlogger-publisher-test"}
umask 077
mkdir -p "$state"
lock="$state/.lock"
mkdir "$lock" 2>/dev/null || { echo "Otra ejecución está usando $state" >&2; exit 1; }
tmp=''
cleanup() { if [[ -n "$tmp" ]]; then rm -rf -- "$tmp"; fi; rmdir -- "$lock"; }
trap cleanup EXIT

case "$mode" in
  capture)
    [[ ! -e "$state/pending" && ! -e "$state/received" ]] || {
      echo 'Ya existe un lote. Usa otro directorio OTLOGGER_TEST_STATE para una prueba nueva.' >&2; exit 1;
    }
    tmp=$(mktemp -d "$state/.capture.XXXXXX")
    for table in cpu_usage ram_usage; do
      sudo k3s kubectl -n idl exec timescaledb-0 -- \
        psql -X -q -U postgres -d kepserver_pcn -v ON_ERROR_STOP=1 \
        -c "BEGIN READ ONLY; SET LOCAL statement_timeout = '20s';
            COPY (SELECT * FROM public.${table} WHERE timestamp IS NOT NULL
                  ORDER BY timestamp DESC LIMIT 5) TO STDOUT WITH (FORMAT CSV, HEADER TRUE);
            COMMIT;" > "$tmp/$table.csv"
      [[ $(wc -l < "$tmp/$table.csv") -ge 2 ]] || { echo "Sin muestras: $table" >&2; exit 1; }
    done
    mv -- "$tmp" "$state/pending"
    tmp=''
    echo "Lote guardado en $state/pending"
    ;;
  fail)
    [[ -d "$state/pending" ]] || { echo 'Primero ejecuta capture.' >&2; exit 1; }
    echo 'Fallo simulado: el lote sigue pendiente.' >&2
    exit 1
    ;;
  send|send-otlp)
    if [[ -d "$state/received" && ! -e "$state/pending" ]]; then
      echo 'El receptor local ya tiene el lote.'
    else
      [[ -d "$state/pending" && ! -e "$state/received" ]] || { echo 'No hay un lote pendiente válido.' >&2; exit 1; }
      if [[ "$mode" == send-otlp ]]; then
        script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
        python3 "$script_dir/send-test-otlp.py" "$state/pending" --port "${OTLOGGER_TEST_PORT:-4318}"
      fi
      mv -- "$state/pending" "$state/received"
      echo "Lote archivado en $state/received ($mode)"
    fi
    ;;
  status)
    for dir in pending received; do
      if [[ -d "$state/$dir" ]]; then
        printf '%s: ' "$dir"
        cksum "$state/$dir/"*.csv
      else
        printf '%s: sin lote\n' "$dir"
      fi
    done
    ;;
esac
