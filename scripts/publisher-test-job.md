# Prueba OTLP desde Balcones DEV

El Job envía un único lote de CPU/RAM desde un pod en `idl`. No instala
otro Collector, no lee la base directamente ni modifica el ingestor.
Confirma con Néstor que el receptor de métricas esté habilitado y que
la imagen del manifiesto esté disponible para el clúster. Si el entorno
no puede descargarla, usa una imagen Python 3.12 aprobada y disponible.

Desde Balcones DEV, con los archivos de scripts en el mismo directorio:

```bash
export OTLOGGER_TEST_STATE="$HOME/otlogger-collector-pilot-$(date +%s)"
bash test-publisher-balcones.sh capture

sudo k3s kubectl -n idl create configmap otlogger-publisher-test-input \
  --from-file=send-test-otlp.py \
  --from-file="$OTLOGGER_TEST_STATE/pending/cpu_usage.csv" \
  --from-file="$OTLOGGER_TEST_STATE/pending/ram_usage.csv"

sudo k3s kubectl create -f publisher-test-job.yaml
sudo k3s kubectl -n idl logs -f job/otlogger-publisher-test --pod-running-timeout=60s
sudo k3s kubectl -n idl get job otlogger-publisher-test
```

Ejecuta los pasos por separado y detente si alguno falla. La ConfigMap
contiene una copia temporal de la muestra de CPU/RAM. El Job no mueve
los CSV en el nodo. Si la muestra tiene una hora o más, captura una nueva
en otro directorio; el sender no modifica timestamps.

El éxito esperado es `Lote confirmado por el receptor OTLP` y un Job
completado. Pídele a Néstor corroborar recepción de
`service.name=otlogger-publisher-test` y las métricas
`otlogger.cpu.usage`, `otlogger.ram.total` y `otlogger.ram.free`.
El ACK no demuestra persistencia en Dynatrace.

Tras revisar los logs, elimina los recursos de prueba antes de repetir:

```bash
sudo k3s kubectl -n idl delete job otlogger-publisher-test
sudo k3s kubectl -n idl delete configmap otlogger-publisher-test-input
```
