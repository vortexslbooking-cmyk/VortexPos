#!/usr/bin/env python3
"""
Copia de seguridad de vortexPOS Cloud.

Sin dependencias: solo Python 3 (ya viene en macOS y Linux). Descarga toda la
base de datos del servidor central en un fichero JSON con fecha, y conserva las
últimas N copias borrando las más antiguas.

USO
    export VORTEX_URL=https://vortexpos-cloud.onrender.com
    export VORTEX_EMAIL=tu-email
    export VORTEX_PASSWORD='tu-contraseña'

    python3 backup.py                      # copia en ./copias-seguridad
    python3 backup.py --dir ~/VortexBackups --keep 60
    python3 backup.py --restore copia.json # restaura (no borra nada, solo añade)

AUTOMATIZARLO (una copia diaria a las 04:00, en macOS/Linux):
    crontab -e
    0 4 * * * cd /ruta/a/server && /usr/bin/python3 backup.py >> backup.log 2>&1

IMPORTANTE: el fichero contiene los hash de los PIN de tus clientes. Guárdalo
donde guardarías una contraseña, no en una carpeta compartida.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TIMEOUT = 120  # Render puede tardar ~50 s en despertar si el plan es gratuito


def api(url: str, path: str, payload=None, token: str = "") -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise SystemExit(f"ERROR {e.code} en {path}: {detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"ERROR de red al llamar a {path}: {e.reason}")


def login(url: str, email: str, password: str) -> str:
    tok = api(url, "/api/provider/login", {"email": email, "password": password}).get("token")
    if not tok:
        raise SystemExit("ERROR: el servidor no devolvió token — revisa email y contraseña")
    return tok


def do_backup(url: str, token: str, dest: Path, keep: int) -> Path:
    dump = api(url, "/api/provider/backup", token=token)
    c = dump.get("counts", {})
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    out = dest / f"vortexpos-{stamp}.json"
    out.write_text(json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8")

    kb = out.stat().st_size / 1024
    print(f"OK  {out}  ({kb:.0f} KB) — "
          f"{c.get('tenants', 0)} locales, {c.get('documents', 0)} documentos, "
          f"{c.get('records', 0)} registros")

    if c.get("tenants", 0) == 0:
        print("AVISO: la copia no contiene ningún local. ¿Servidor correcto?")

    # Rotación: se conservan las N más recientes.
    copias = sorted(dest.glob("vortexpos-*.json"))
    for viejo in copias[:-keep] if keep > 0 else []:
        viejo.unlink()
        print(f"    borrada copia antigua: {viejo.name}")
    return out


def do_restore(url: str, token: str, src: Path):
    if not src.exists():
        raise SystemExit(f"ERROR: no existe el fichero {src}")
    dump = json.loads(src.read_text(encoding="utf-8"))
    res = api(url, "/api/provider/restore", dump, token=token)
    a = res.get("added", {})
    print(f"OK  restaurado desde {src.name} — nuevos: "
          f"{a.get('tenants', 0)} locales, {a.get('documents', 0)} documentos, "
          f"{a.get('records', 0)} registros")
    print("    (lo que ya existía se ha respetado: la restauración nunca borra)")


def main():
    p = argparse.ArgumentParser(description="Copia de seguridad de vortexPOS Cloud")
    p.add_argument("--url", default=os.environ.get("VORTEX_URL", "https://vortexpos-cloud.onrender.com"))
    p.add_argument("--email", default=os.environ.get("VORTEX_EMAIL", ""))
    p.add_argument("--password", default=os.environ.get("VORTEX_PASSWORD", ""))
    p.add_argument("--dir", default=os.environ.get("VORTEX_BACKUP_DIR", "copias-seguridad"))
    p.add_argument("--keep", type=int, default=30, help="cuántas copias conservar (0 = todas)")
    p.add_argument("--restore", metavar="FICHERO", help="restaura una copia en el servidor")
    args = p.parse_args()

    if not args.email or not args.password:
        raise SystemExit("ERROR: faltan credenciales. Define VORTEX_EMAIL y VORTEX_PASSWORD "
                         "(o usa --email / --password).")

    print(f"Servidor: {args.url}")
    token = login(args.url, args.email, args.password)

    if args.restore:
        do_restore(args.url, token, Path(args.restore).expanduser())
    else:
        do_backup(args.url, token, Path(args.dir).expanduser(), args.keep)


if __name__ == "__main__":
    sys.exit(main())
