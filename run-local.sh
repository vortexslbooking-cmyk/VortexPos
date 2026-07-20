#!/usr/bin/env bash
# Arranque local de vortexPOS Cloud en un solo comando.
#   ./run-local.sh
set -e
cd "$(dirname "$0")"

# Carga variables de .env si existe (si no, usa valores de desarrollo).
if [ -f .env ]; then set -a; . ./.env; set +a; fi
export JWT_SECRET="${JWT_SECRET:-clave-de-desarrollo-suficientemente-larga-1234567890}"
export PROVIDER_EMAIL="${PROVIDER_EMAIL:-admin@vortexpos.local}"
export PROVIDER_PASSWORD="${PROVIDER_PASSWORD:-vortex-admin}"

echo "Instalando dependencias (solo la primera vez)…"
python3 -m pip install --quiet --user -r requirements.txt

echo ""
echo "  vortexPOS Cloud arrancando…"
echo "  Panel de proveedor:  http://localhost:8000/"
echo "  Usuario:  $PROVIDER_EMAIL"
echo "  Clave:    $PROVIDER_PASSWORD"
echo ""
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
