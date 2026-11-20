# Publisher OTLogger: comandos para Balcones DEV

Primera validación del servicio continuo. Solo CPU/RAM de `idl/kepserver_pcn`.
No aplicar en QA/PRD. Ejecutar cada bloque por separado y detenerse ante errores.
La migración aún no está validada sobre la versión TimescaleDB del clúster.
No genera backups ni reemplaza el ingestor o TimescaleDB.

## 1. Preparar la imagen y los archivos

En una máquina con Docker y el repo actualizado (los cambios del publisher aún
no se han publicado a GitHub), desde la raíz del repositorio:

```bash
docker build --platform linux/amd64 -f publisher/Dockerfile \
  -t otlogger-publisher:0.1.0 .
docker save otlogger-publisher:0.1.0 | gzip > otlogger-publisher-0.1.0.tar.gz
```

Comprueba primero que los nodos de destino son amd64 (paso 2); si no, adaptar
la plataforma antes de construir. Transfiere por BeyondTrust al nodo de Balcones:

- `otlogger-publisher-0.1.0.tar.gz`
- `publisher/migrations/001_cpu_ram_outbox.sql`
- `publisher/k3s/deployment.yaml`, renombrado a `publisher-deployment.yaml`

No uses `publisher-test-job.yaml`: ese es el piloto anterior.

## 2. Confirmar destino y esquema (nodo K3S)

```bash
hostname
sudo k3s kubectl get nodes -o wide
sudo k3s kubectl -n idl get pods -o wide
sudo k3s kubectl -n idl get service timescaledb
sudo k3s kubectl -n idl get pvc
sudo k3s kubectl get service -n observability otel-collector
```

El host observado en la prueba fue `usprmsmovmp01`. Confirma que este acceso
corresponde a Balcones DEV con el equipo antes de modificar la base.

```bash
sudo k3s kubectl -n idl exec -i timescaledb-0 -- \
  psql -X -U postgres -d kepserver_pcn -v ON_ERROR_STOP=1 <<'SQL'
SELECT current_database(), version();
SELECT extversion FROM pg_extension WHERE extname='timescaledb';
SELECT table_name,column_name,data_type,column_default
FROM information_schema.columns
WHERE table_schema='public' AND table_name IN ('cpu_usage','ram_usage')
ORDER BY table_name,ordinal_position;
SELECT hypertable_name,column_name
FROM timescaledb_information.dimensions
WHERE hypertable_name IN ('cpu_usage','ram_usage');
SELECT to_regclass('public.telemetry_outbox');
SELECT pg_size_pretty(pg_database_size(current_database()));
SQL
```

Antes de seguir: CPU debe tener `timestamp`, `usage`; RAM `timestamp`, `total_kb`,
`free_kb`. Ambas particionadas por `timestamp`. Si ya existe `sample_id` o
`telemetry_outbox`, no reapliques la migración; revisar su estado primero.
Revisar espacio libre en el volumen de PostgreSQL antes de habilitar la outbox.

## 3. Importar la imagen y preparar el manifiesto

Desde el directorio donde transferiste los archivos:

```bash
gzip -t otlogger-publisher-0.1.0.tar.gz
sudo k3s ctr images import otlogger-publisher-0.1.0.tar.gz
sudo k3s ctr images list -q | grep otlogger-publisher
```

El manifiesto usa `imagePullPolicy: Never`. Fijar el publisher al nodo donde
importaste la imagen (alternativa: importar en todos los nodos elegibles):

```bash
nano publisher-deployment.yaml
```

Dentro de `spec.template.spec`, al mismo nivel que `containers`, agrega:

```yaml
      nodeSelector:
        kubernetes.io/hostname: usprmsmovmp01
```

Verifica que el nombre corresponda a la columna NAME de `get nodes`.
Confirma imagen `otlogger-publisher:0.1.0`, namespace `idl`, endpoint del Collector
y `OTLOGGER_SOURCE=kepserver_pcn`. No pongas credenciales en el YAML.

## 4. Aplicar una sola vez la migración

Este paso comienza a generar pendientes para nuevas muestras. Requiere permisos
administrativos y puede tomar locks breves. Si falla, no continuar ni repetir
sin revisar el error. Su transacción revierte ante un error.

```bash
sudo k3s kubectl -n idl exec -i timescaledb-0 -- \
  psql -X -U postgres -d kepserver_pcn -v ON_ERROR_STOP=1 \
  < 001_cpu_ram_outbox.sql
```

Comprobar que el ingestor sigue sano:

```bash
sudo k3s kubectl -n idl logs deployment/ingestor --since=2m --tail=50
sudo k3s kubectl -n idl exec timescaledb-0 -- \
  psql -X -U postgres -d kepserver_pcn -v ON_ERROR_STOP=1 \
  -c "SELECT source_table,status,count(*) FROM telemetry_outbox GROUP BY 1,2;"
```

Espera al menos un ciclo del ingestor y vuelve a consultar: deben aparecer
pendientes de CPU/RAM. No se migra ni publica el histórico anterior.

## 5. Crear usuario dedicado y Secret

Abre psql. Si el rol ya existe, detente y revisa sus permisos en lugar de recrearlo.

```bash
sudo k3s kubectl -n idl exec -it timescaledb-0 -- \
  psql -X -U postgres -d kepserver_pcn -v ON_ERROR_STOP=1
```

Dentro de psql:

```sql
CREATE ROLE otlogger_publisher LOGIN;
\password otlogger_publisher
GRANT CONNECT ON DATABASE kepserver_pcn TO otlogger_publisher;
GRANT USAGE ON SCHEMA public TO otlogger_publisher;
GRANT SELECT,UPDATE,DELETE ON public.telemetry_outbox TO otlogger_publisher;
\q
```

Introduce una contraseña nueva mediante `\password`; no incluirla en comandos
SQL, capturas ni archivos versionados. Los triggers usan los permisos del
escritor del ingestor: si no es postgres, validar que pueda INSERT en outbox.

Crear el Secret desde un archivo temporal protegido, sin poner contraseña en
argumentos de procesos (ejecutar este bloque en bash):

```bash
bash <<'BASH'
set -euo pipefail
umask 077
secret_file=$(mktemp)
trap 'rm -f "$secret_file"' EXIT
read -r -s -p 'Contraseña de otlogger_publisher: ' publisher_password </dev/tty
printf '\n' >/dev/tty
printf 'PGHOST=timescaledb\nPGPORT=5432\nPGDATABASE=kepserver_pcn\nPGUSER=otlogger_publisher\nPGPASSWORD=%s\n' \
  "$publisher_password" > "$secret_file"
unset publisher_password
sudo k3s kubectl -n idl create secret generic otlogger-publisher-db \
  --from-env-file="$secret_file"
BASH
```

Esto asume el Service `timescaledb` en `idl`, confirmado en el paso 2. Si el
Secret ya existe, no borrarlo sin revisar qué workload lo consume.

## 6. Desplegar y observar

```bash
sudo k3s kubectl apply --dry-run=server -f publisher-deployment.yaml
sudo k3s kubectl apply -f publisher-deployment.yaml
sudo k3s kubectl -n idl rollout status deployment/otlogger-publisher --timeout=180s
sudo k3s kubectl -n idl logs -f deployment/otlogger-publisher --tail=50
```

Ctrl+C cierra el seguimiento de logs, no el servicio. Buscar lotes con
`outcome=collector_accepted` y errores vacíos. Un pod Running no confirma envío.

```bash
sudo k3s kubectl -n idl exec -i timescaledb-0 -- \
  psql -X -U postgres -d kepserver_pcn -v ON_ERROR_STOP=1 <<'SQL'
SELECT status,count(*),min(created_at),max(collector_accepted_at)
FROM telemetry_outbox GROUP BY status;
SELECT source_table,sample_timestamp,status,attempts,last_error
FROM telemetry_outbox ORDER BY created_at DESC LIMIT 15;
SELECT pg_size_pretty(pg_total_relation_size('telemetry_outbox'));
SQL
```

## 7. Verificar en Dynatrace con Néstor/Ángel

Buscar las métricas con rango reciente:

```dql
timeseries cpu = avg(otlogger.cpu.usage), interval: 1m
```

```dql
timeseries ram_total = avg(otlogger.ram.total), interval: 1m
```

```dql
timeseries ram_free = avg(otlogger.ram.free), interval: 1m
```

El nombre de servicio ahora es `otlogger-publisher`, no
`otlogger-publisher-test`. Puede haber puntos de ambas pruebas en el mismo rango.
El ACK local no garantiza persistencia en Dynatrace.

## 8. Probar reinicio del publisher

Solo después de confirmar recepción continua:

```bash
sudo k3s kubectl -n idl rollout restart deployment/otlogger-publisher
sudo k3s kubectl -n idl rollout status deployment/otlogger-publisher --timeout=180s
sudo k3s kubectl -n idl logs deployment/otlogger-publisher --since=3m
```

Repetir las consultas de estados y verificar continuidad en Dynatrace.
Para probar desconexión del Collector, coordinar con Néstor: no detener el
Collector compartido de otros módulos. Preparar un fallo aislado del publisher.

## Detener la prueba

```bash
sudo k3s kubectl -n idl scale deployment/otlogger-publisher --replicas=0
```

Esto detiene envíos, pero los triggers siguen acumulando pendientes. No dejarlo
así indefinidamente. Si se decide deshabilitar la captura de pendientes,
coordinar el siguiente cambio administrativo (no borra datos existentes):

```bash
sudo k3s kubectl -n idl exec -i timescaledb-0 -- \
  psql -X -U postgres -d kepserver_pcn -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;
SET LOCAL lock_timeout='5s';
ALTER TABLE cpu_usage DISABLE TRIGGER cpu_outbox;
ALTER TABLE ram_usage DISABLE TRIGGER ram_outbox;
COMMIT;
SQL
```

No se crean pendientes durante ese intervalo. Para reanudar la captura hay que
habilitar explícitamente ambos triggers. No borrar outbox, PVC o base.

## Límites de esta versión DEV

- Accepted se limpia en lotes tras 48 horas; no modifica histórico.
- Pendientes mayores a una hora pasan a expired sin cambiar timestamps.
- Expired/rejected no se borran solos. Revisar espacio y definir su tratamiento.
- Outbox sin límite automático de tamaño: supervisar crecimiento.
- La prueba continua de resiliencia y el límite de capacidad siguen pendientes.
