#!/usr/bin/env bash
# Bring up the API against a throwaway, pre-seeded database and open the docs.
#
#   scripts/api_demo.sh
#
# For looking at the OpenAPI page and poking the endpoints without needing a TNS
# credential, a real mirror, or a sync. The data is four fabricated objects
# chosen to exercise the interesting geometry — a close pair and the 0/360 seam.
#
# Everything is torn down on exit.
set -euo pipefail

cd "$(dirname "$0")/.."

PORT=${TNS_API_DEMO_PORT:-8000}
PROJECT=tns-api-demo
READER_PW='demo/pw+9=@q'

cleanup() {
    docker rm -f "${PROJECT}-api" "${PROJECT}-db" >/dev/null 2>&1 || true
    docker network rm "${PROJECT}-net" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

echo "building the API image…"
docker build -q -t sarhatabaot/tns-mirror-api:demo api >/dev/null

docker network create "${PROJECT}-net" >/dev/null
docker run -d --rm --name "${PROJECT}-db" --network "${PROJECT}-net" \
    -e POSTGRES_PASSWORD=demo -e POSTGRES_USER=demo -e POSTGRES_DB=demo \
    postgres:16-alpine >/dev/null

echo "waiting for postgres…"
for _ in $(seq 1 60); do
    docker exec "${PROJECT}-db" pg_isready -U demo -d demo >/dev/null 2>&1 && break
    sleep 1
done
sleep 2

echo "applying the published schema and seeding…"
docker exec -i "${PROJECT}-db" psql -q -U demo -d demo < schema/schema_v1.sql >/dev/null
docker exec -i "${PROJECT}-db" psql -q -U demo -d demo >/dev/null <<SQL
INSERT INTO tns_objects (objid,name,ra,"dec","type",redshift,discoverymag,internal_names,reporting_group,source_snapshot,refreshed_at) VALUES
 (1,'AT2026abc',203.10000,10.20000,NULL,NULL,19.4,'ZTF26aaa, ATLAS26x','ZTF',current_date,now()),
 (2,'SN2026xyz',203.10050,10.20000,'SN Ia',0.031,18.1,'ATLAS26x','ATLAS',current_date,now()),
 (3,'AT2026seam',359.99000,0.00000,NULL,NULL,20.0,NULL,'GOTO',current_date,now()),
 (4,'AT2026wrap',0.01000,0.00000,NULL,NULL,20.2,NULL,'GOTO',current_date,now());
UPDATE tns_mirror_meta SET last_full_sync_at=now(), last_delta_sync_at=now(),
  last_source_snapshot=current_date WHERE id=1;
CREATE ROLE tns_ro LOGIN PASSWORD '${READER_PW}';
GRANT CONNECT ON DATABASE demo TO tns_ro;
GRANT USAGE ON SCHEMA public TO tns_ro;
GRANT SELECT ON public.tns_objects TO tns_ro;
GRANT SELECT ON public.tns_mirror_meta TO tns_ro;
SQL

# Connects as the reader, exactly as a deployment would.
docker run -d --rm --name "${PROJECT}-api" --network "${PROJECT}-net" \
    -p "127.0.0.1:${PORT}:8000" \
    -e PGHOST="${PROJECT}-db" -e PGUSER=tns_ro -e PGPASSWORD="${READER_PW}" \
    -e PGDATABASE=demo -e TNS_API_RATE_LIMIT="${TNS_API_RATE_LIMIT:-1000}" \
    ${TNS_API_ROOT_PATH:+-e TNS_API_ROOT_PATH=${TNS_API_ROOT_PATH}} \
    sarhatabaot/tns-mirror-api:demo >/dev/null

prefix=${TNS_API_ROOT_PATH:-}
base="http://127.0.0.1:${PORT}${prefix}"

echo "waiting for the API…"
for _ in $(seq 1 40); do
    curl -sf "${base}/healthz" >/dev/null 2>&1 && break
    sleep 1
done

cat <<EOF

  OpenAPI docs   ${base}/docs
  Raw spec       ${base}/docs/openapi.json

  Try:
    curl '${base}/v1/nearest?ra=203.1004&dec=10.2&radius_arcsec=3'
    curl '${base}/v1/cone?ra=0&dec=0&radius_arcsec=100'      # across the 0/360 seam
    curl '${base}/v1/objects/SN2026xyz'
    curl '${base}/v1/meta'

  Ctrl-C to stop and clean up.

EOF

# Follow the logs so Ctrl-C lands here and the trap tears everything down.
docker logs -f "${PROJECT}-api"
