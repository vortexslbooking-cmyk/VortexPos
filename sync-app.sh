#!/usr/bin/env bash
# Publica la última versión de vortexpos.html en TODOS los destinos:
# nube (PWA /app/), proyecto Android y proyecto Windows.
#   ./sync-app.sh
set -e
cd "$(dirname "$0")"
SRC="../vortexpos.html"
if [ ! -f "$SRC" ]; then
  echo "No encuentro $SRC (ejecuta este script desde la carpeta server del proyecto)"
  exit 1
fi
cp "$SRC" app/static/vortexpos.html
cp "$SRC" apps/android/www/index.html
cp "$SRC" apps/windows/www/index.html
echo "✓ App sincronizada: nube (PWA) + Android + Windows"
echo "  Sube los cambios al repo: la PWA se actualiza al desplegar y los"
echo "  workflows de GitHub Actions reconstruyen APK y EXE automáticamente."
