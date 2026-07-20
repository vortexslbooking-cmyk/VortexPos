"""
vortexPOS Cloud — servidor central (API + panel de proveedor).

Arranque local:   uvicorn app.main:app --reload
Panel proveedor:  http://localhost:8000/
Salud:            http://localhost:8000/health
Docs API:         http://localhost:8000/docs
"""
import os
import re
import json
import time
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, insert, update, and_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .db import engine, init_db, tenants, documents, records, new_access_id
from .security import (hash_secret, verify_secret, make_token, read_token)

PROVIDER_EMAIL = os.environ.get("PROVIDER_EMAIL", "admin@vortexpos.local")
PROVIDER_PASSWORD = os.environ.get("PROVIDER_PASSWORD", "vortex-admin")
PIN_RE = re.compile(r"^\d{4,8}$")

# Aviso claro si se arranca en producción con los valores de ejemplo del código.
if PROVIDER_PASSWORD == "vortex-admin":
    print("[vortexPOS] AVISO: PROVIDER_PASSWORD es el valor de ejemplo — "
          "define PROVIDER_EMAIL/PROVIDER_PASSWORD antes de exponer el servidor.")
if os.environ.get("JWT_SECRET") in (None, "", "cambia-esta-clave-en-produccion"):
    print("[vortexPOS] AVISO: JWT_SECRET sin definir — los tokens no son seguros "
          "fuera de desarrollo.")


# ---- Freno anti fuerza-bruta (en memoria; suficiente para un solo proceso) ----
_FAILS: Dict[str, List[float]] = {}
_MAX_FAILS = 8          # intentos fallidos permitidos…
_WINDOW = 300.0         # …en 5 minutos
_LOCK_SECONDS = 60.0    # bloqueo tras superarlos


def throttle_check(bucket: str):
    fails = [t for t in _FAILS.get(bucket, []) if time.monotonic() - t < _WINDOW]
    _FAILS[bucket] = fails
    if len(fails) >= _MAX_FAILS and time.monotonic() - fails[-1] < _LOCK_SECONDS:
        raise HTTPException(429, "Demasiados intentos — espera un minuto y vuelve a probar")


def throttle_fail(bucket: str):
    _FAILS.setdefault(bucket, []).append(time.monotonic())


def throttle_ok(bucket: str):
    _FAILS.pop(bucket, None)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")  # Render/proxies
    return (fwd.split(",")[0].strip() if fwd else None) or (request.client.host if request.client else "?")

app = FastAPI(title="vortexPOS Cloud", version="2.0.0")

# CORS: las tablets sirven la app desde otro origen (archivo local, otra URL) y
# llaman a esta API. La API está protegida por token, así que se permite el origen
# de las apps. Restríngelo con ALLOWED_ORIGINS (separados por comas) en producción.
_origins = os.environ.get("ALLOWED_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _origins.strip() == "*" else [o.strip() for o in _origins.split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

PANEL_HTML = (Path(__file__).parent / "panel.html").read_text(encoding="utf-8")

# Crea las tablas al importar el módulo (sirve tanto para uvicorn como para tests).
init_db()


@app.on_event("startup")
def _startup():
    init_db()


def now():
    return datetime.now(timezone.utc)


def iso(dt) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def parse_iso(s: Optional[str]):
    if not s:
        return now()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return now()


# ---------------------------------------------------------------- Auth deps
def _auth(authorization: Optional[str]) -> Dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Falta el token de autenticación")
    data = read_token(authorization.split(" ", 1)[1])
    if not data:
        raise HTTPException(401, "Token no válido o caducado")
    return data


def require_provider(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    data = _auth(authorization)
    if data.get("role") != "provider":
        raise HTTPException(403, "Se requieren permisos de proveedor")
    return data


def require_device(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    data = _auth(authorization)
    if data.get("role") != "device":
        raise HTTPException(403, "Se requiere un dispositivo autenticado")
    return data


# ---------------------------------------------------------------- Modelos
class ProviderLogin(BaseModel):
    email: str
    password: str


class DeviceLogin(BaseModel):
    license_key: str
    pin: str


class DeviceActivate(BaseModel):
    access_id: str
    pin: str


class TenantCreate(BaseModel):
    business_name: str
    plan: str = "Pro"
    pin: str
    notes: str = ""


class TenantPatch(BaseModel):
    plan: Optional[str] = None
    status: Optional[str] = None
    business_name: Optional[str] = None
    notes: Optional[str] = None
    pin: Optional[str] = None


class DocIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    payload: Any = Field(alias="json")           # el cliente envía la clave "json"
    updated_at: Optional[str] = None


class RecordIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    kind: str
    record_id: str
    payload: Any = Field(alias="json")
    created_at: Optional[str] = None


class SyncIn(BaseModel):
    documents: Dict[str, DocIn] = {}
    records: List[RecordIn] = []
    # Las tablets no necesitan re-descargar su historial en cada sync: por defecto
    # la respuesta solo lleva documentos (carta/config). Un cliente que sí quiera
    # el histórico (p. ej. una tablet nueva restaurando) lo pide explícitamente.
    pull_records: bool = False


# ---------------------------------------------------------------- Salud / panel
@app.get("/health")
def health():
    return {"ok": True, "service": "vortexpos-cloud", "time": iso(now())}


@app.get("/", response_class=HTMLResponse)
def panel():
    return PANEL_HTML


# ---------------------------------------------------------------- PWA (/app/)
# El servidor sirve la propia app vortexPOS como PWA instalable: el cliente
# entra en /app/, pulsa "Instalar" y la usa con icono propio y sin conexión.
# Al actualizar static/vortexpos.html aquí, todos los locales reciben la
# versión nueva en su siguiente arranque con internet.
STATIC_DIR = Path(__file__).parent / "static"
_APP_ASSETS = {
    "manifest.webmanifest": "application/manifest+json",
    "sw.js": "application/javascript",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "icon-512-maskable.png": "image/png",
}


@app.get("/app")
def app_redirect():
    return RedirectResponse("/app/", status_code=307)


@app.get("/app/", response_class=HTMLResponse)
def app_shell():
    f = STATIC_DIR / "vortexpos.html"
    if not f.exists():
        raise HTTPException(404, "App no publicada en este servidor")
    return f.read_text(encoding="utf-8")


@app.get("/app/{asset}")
def app_asset(asset: str):
    if asset not in _APP_ASSETS:          # lista blanca: nada de rutas arbitrarias
        raise HTTPException(404, "Recurso no encontrado")
    f = STATIC_DIR / asset
    if not f.exists():
        raise HTTPException(404, "Recurso no encontrado")
    return FileResponse(f, media_type=_APP_ASSETS[asset])


# ---------------------------------------------------------------- Provider auth
@app.post("/api/provider/login")
def provider_login(body: ProviderLogin, request: Request):
    bucket = "prov:" + client_ip(request)
    throttle_check(bucket)
    # compare_digest exige ASCII en str: se compara en bytes para admitir
    # contraseñas con ñ, tildes o símbolos sin provocar un error 500.
    ok = (secrets.compare_digest(body.email.strip().lower().encode(),
                                 PROVIDER_EMAIL.strip().lower().encode())
          and secrets.compare_digest(body.password.encode(), PROVIDER_PASSWORD.encode()))
    if not ok:
        throttle_fail(bucket)
        raise HTTPException(401, "Credenciales de proveedor incorrectas")
    throttle_ok(bucket)
    return {"token": make_token({"role": "provider", "email": PROVIDER_EMAIL})}


# ---------------------------------------------------------------- Provider: tenants
def _tenant_public(row) -> Dict[str, Any]:
    return {
        "id": row.id, "license_key": row.license_key,
        "access_id": getattr(row, "access_id", None),
        "business_name": row.business_name,
        "plan": row.plan, "status": row.status, "notes": row.notes,
        "created_at": iso(row.created_at), "last_seen": iso(row.last_seen),
    }


@app.get("/api/provider/tenants")
def list_tenants(_=Depends(require_provider)):
    out = []
    with engine.begin() as cx:
        rows = cx.execute(select(tenants).order_by(tenants.c.created_at.desc())).all()
        for r in rows:
            sales = cx.execute(
                select(records.c.json).where(and_(
                    records.c.tenant_id == r.id, records.c.kind == "sale"))
            ).all()
            total = 0.0
            for (j,) in sales:
                try:
                    total += float(json.loads(j).get("total", 0) or 0)
                except Exception:
                    pass
            t = _tenant_public(r)
            t["sales_count"] = len(sales)
            t["sales_total"] = round(total, 2)
            out.append(t)
    return {"tenants": out}


@app.post("/api/provider/tenants")
def create_tenant(body: TenantCreate, _=Depends(require_provider)):
    if not PIN_RE.match(body.pin or ""):
        raise HTTPException(400, "El PIN debe tener de 4 a 8 dígitos (solo números)")
    if not body.business_name.strip():
        raise HTTPException(400, "El nombre del negocio es obligatorio")
    tid = "t_" + secrets.token_hex(6)
    license_key = "VTX-" + "-".join(secrets.token_hex(2).upper() for _ in range(3))
    with engine.begin() as cx:
        # ID de acceso único (reintenta si colisiona, cosa improbable)
        for _ in range(10):
            access_id = new_access_id()
            if not cx.execute(select(tenants.c.id).where(
                    tenants.c.access_id == access_id)).first():
                break
        cx.execute(insert(tenants).values(
            id=tid, license_key=license_key, access_id=access_id,
            pin_hash=hash_secret(body.pin),
            business_name=body.business_name, plan=body.plan, status="Activo",
            notes=body.notes, created_at=now(),
        ))
    return {"id": tid, "license_key": license_key, "access_id": access_id, "pin": body.pin,
            "business_name": body.business_name, "plan": body.plan, "status": "Activo"}


@app.patch("/api/provider/tenants/{tid}")
def patch_tenant(tid: str, body: TenantPatch, _=Depends(require_provider)):
    vals: Dict[str, Any] = {}
    for f in ("plan", "status", "business_name", "notes"):
        v = getattr(body, f)
        if v is not None:
            vals[f] = v
    if body.pin:
        if not PIN_RE.match(body.pin):
            raise HTTPException(400, "El PIN debe tener de 4 a 8 dígitos (solo números)")
        vals["pin_hash"] = hash_secret(body.pin)
    if not vals:
        raise HTTPException(400, "Nada que actualizar")
    with engine.begin() as cx:
        res = cx.execute(update(tenants).where(tenants.c.id == tid).values(**vals))
        if res.rowcount == 0:
            raise HTTPException(404, "Local no encontrado")
    return {"ok": True, "updated": list(vals.keys())}


class MenuPut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    payload: Any = Field(alias="json")


@app.get("/api/provider/tenants/{tid}/menu")
def get_menu(tid: str, _=Depends(require_provider)):
    """Carta actual del local (la última sincronizada o la guardada por el proveedor)."""
    with engine.begin() as cx:
        row = cx.execute(select(tenants).where(tenants.c.id == tid)).first()
        if not row:
            raise HTTPException(404, "Local no encontrado")
        doc = cx.execute(select(documents).where(and_(
            documents.c.tenant_id == tid, documents.c.doc_key == "menu"))).first()
    if not doc:
        return {"menu": [], "updated_at": None}
    try:
        menu = json.loads(doc.json)
    except Exception:
        menu = []
    return {"menu": menu, "updated_at": iso(doc.updated_at)}


@app.put("/api/provider/tenants/{tid}/menu")
def put_menu(tid: str, body: MenuPut, _=Depends(require_provider)):
    """
    El proveedor guarda la carta del local. Se marca con la hora del servidor,
    de modo que las tablets la adoptan en su próxima sincronización (Last-Write-Wins).
    """
    menu = body.payload
    if not isinstance(menu, list):
        raise HTTPException(400, "La carta debe ser una lista de categorías")
    clean = []
    for c in menu:
        if not isinstance(c, dict):
            continue
        cat = str(c.get("cat", "")).strip()
        if not cat:
            continue
        station = c.get("station") if c.get("station") in ("cocina", "barra") else "cocina"
        items = []
        for it in (c.get("items") or []):
            if not isinstance(it, dict):
                continue
            name = str(it.get("name", "")).strip()
            if not name:
                continue
            try:
                price = max(0.0, float(it.get("price", 0) or 0))
            except (TypeError, ValueError):
                price = 0.0
            item = {"id": it.get("id") or secrets.token_hex(4), "name": name, "price": price}
            if it.get("off"):
                item["off"] = True
            items.append(item)
        clean.append({"cat": cat, "station": station, "items": items})

    ts = now()
    payload = json.dumps(clean, ensure_ascii=False)
    with engine.begin() as cx:
        row = cx.execute(select(tenants).where(tenants.c.id == tid)).first()
        if not row:
            raise HTTPException(404, "Local no encontrado")
        existing = cx.execute(select(documents.c.updated_at).where(and_(
            documents.c.tenant_id == tid, documents.c.doc_key == "menu"))).first()
        if existing is None:
            cx.execute(insert(documents).values(
                tenant_id=tid, doc_key="menu", json=payload, updated_at=ts))
        else:
            cx.execute(update(documents).where(and_(
                documents.c.tenant_id == tid, documents.c.doc_key == "menu"
            )).values(json=payload, updated_at=ts))
    return {"ok": True, "updated_at": iso(ts), "categorias": len(clean)}


@app.get("/api/provider/tenants/{tid}/closures")
def tenant_closures(tid: str, _=Depends(require_provider)):
    """Historial de cierres Z de un local, con su arqueo de efectivo y descuadre."""
    with engine.begin() as cx:
        row = cx.execute(select(tenants).where(tenants.c.id == tid)).first()
        if not row:
            raise HTTPException(404, "Local no encontrado")
        recs = cx.execute(select(records.c.json).where(and_(
            records.c.tenant_id == tid, records.c.kind == "closure"))).all()
    out = []
    for (j,) in recs:
        try:
            c = json.loads(j)
        except Exception:
            continue
        s = c.get("summary", {}) or {}
        k = c.get("cash", {}) or {}
        out.append({
            "z": c.get("z"), "from": c.get("from"), "to": c.get("to"),
            "total": s.get("total", 0), "tickets": s.get("tickets", 0), "items": s.get("items", 0),
            "efectivo": (s.get("byMethod") or {}).get("efectivo", 0),
            "tarjeta": (s.get("byMethod") or {}).get("tarjeta", 0),
            "fondoInicial": k.get("fondoInicial"), "ventasEfectivo": k.get("ventasEfectivo"),
            "entradas": k.get("entradas"), "salidas": k.get("salidas"),
            "esperado": k.get("esperado"), "contado": k.get("contado"),
            "descuadre": k.get("descuadre"), "fondoSiguiente": k.get("fondoSiguiente"),
        })
    out.sort(key=lambda c: (c["z"] is None, -(c["z"] or 0)))  # más reciente primero
    return {"closures": out}


@app.get("/api/provider/tenants/{tid}/summary")
def tenant_summary(tid: str, _=Depends(require_provider)):
    with engine.begin() as cx:
        row = cx.execute(select(tenants).where(tenants.c.id == tid)).first()
        if not row:
            raise HTTPException(404, "Local no encontrado")
        recs = cx.execute(select(records.c.kind, records.c.json)
                          .where(records.c.tenant_id == tid)).all()
    total = 0.0
    by_method: Dict[str, float] = {}
    by_product: Dict[str, Dict[str, float]] = {}
    n_sales = 0
    n_closures = 0
    for kind, j in recs:
        try:
            d = json.loads(j)
        except Exception:
            continue
        if kind == "sale":
            n_sales += 1
            t = float(d.get("total", 0) or 0)
            total += t
            m = d.get("method", "—")
            by_method[m] = round(by_method.get(m, 0) + t, 2)
            for it in d.get("items", []):
                nm = it.get("name", "?")
                p = by_product.setdefault(nm, {"qty": 0, "amount": 0.0})
                p["qty"] += it.get("qty", 0)
                p["amount"] = round(p["amount"] + it.get("price", 0) * it.get("qty", 0), 2)
        elif kind == "closure":
            n_closures += 1
    top = sorted(by_product.items(), key=lambda kv: kv[1]["qty"], reverse=True)[:8]
    return {
        "tenant": _tenant_public(row),
        "sales_total": round(total, 2), "sales_count": n_sales, "closures": n_closures,
        "by_method": by_method,
        "top_products": [{"name": n, **v} for n, v in top],
    }


# ---------------------------------------------------------------- Device auth
@app.post("/api/device/activate")
def device_activate(body: DeviceActivate, request: Request):
    """
    Activación de un dispositivo con el ID de acceso corto + PIN que entrega
    el proveedor. Devuelve la licencia y un token: a partir de aquí la app
    trabaja sola y el cliente nunca necesita escribir la licencia larga.
    """
    bucket = "act:" + client_ip(request)
    throttle_check(bucket)
    aid = (body.access_id or "").strip().upper()
    with engine.begin() as cx:
        row = cx.execute(select(tenants).where(tenants.c.access_id == aid)).first()
    if not row or not verify_secret(body.pin, row.pin_hash):
        throttle_fail(bucket)
        raise HTTPException(401, "ID de acceso o PIN incorrectos")
    if row.status in ("Suspendido", "Baja"):
        raise HTTPException(403, f"Licencia {row.status.lower()} — contacta con el proveedor")
    throttle_ok(bucket)
    token = make_token({"role": "device", "tenant_id": row.id, "license": row.license_key})
    return {"token": token, "license_key": row.license_key, "tenant": _tenant_public(row)}


@app.post("/api/device/login")
def device_login(body: DeviceLogin, request: Request):
    bucket = "dev:" + client_ip(request)
    throttle_check(bucket)
    with engine.begin() as cx:
        row = cx.execute(select(tenants).where(
            tenants.c.license_key == body.license_key.strip())).first()
    if not row or not verify_secret(body.pin, row.pin_hash):
        throttle_fail(bucket)
        raise HTTPException(401, "Licencia o PIN incorrectos")
    throttle_ok(bucket)
    if row.status in ("Suspendido", "Baja"):
        raise HTTPException(403, f"Licencia {row.status.lower()} — contacta con el proveedor")
    token = make_token({"role": "device", "tenant_id": row.id, "license": row.license_key})
    return {"token": token, "tenant": _tenant_public(row)}


# ---------------------------------------------------------------- Sincronización
@app.post("/api/sync")
def sync(body: SyncIn, dev=Depends(require_device)):
    """
    Offline-first: el dispositivo envía sus cambios (push) y recibe el estado
    autoritativo (pull) en la misma llamada.
      · documents -> Last-Write-Wins por updated_at (carta, config, mesas).
      · records   -> upsert idempotente (ventas/cierres nunca se pierden ni duplican).
    """
    tid = dev["tenant_id"]
    is_sqlite = engine.url.get_backend_name() == "sqlite"

    with engine.begin() as cx:
        # revalida estado (una licencia suspendida no sincroniza)
        trow = cx.execute(select(tenants).where(tenants.c.id == tid)).first()
        if not trow or trow.status in ("Suspendido", "Baja"):
            raise HTTPException(403, "Licencia no activa")

        # PUSH documents (LWW)
        for key, doc in body.documents.items():
            ts = parse_iso(doc.updated_at)
            existing = cx.execute(select(documents.c.updated_at).where(and_(
                documents.c.tenant_id == tid, documents.c.doc_key == key))).first()
            payload = json.dumps(doc.payload, ensure_ascii=False)
            if existing is None:
                cx.execute(insert(documents).values(
                    tenant_id=tid, doc_key=key, json=payload, updated_at=ts))
            else:
                cur = existing[0]
                if cur.tzinfo is None:
                    cur = cur.replace(tzinfo=timezone.utc)
                if ts >= cur:
                    cx.execute(update(documents).where(and_(
                        documents.c.tenant_id == tid, documents.c.doc_key == key
                    )).values(json=payload, updated_at=ts))

        # PUSH records (idempotente: ignora duplicados por id, en una sola sentencia)
        for rec in body.records:
            payload = json.dumps(rec.payload, ensure_ascii=False)
            ts = parse_iso(rec.created_at)
            values = dict(tenant_id=tid, kind=rec.kind, record_id=rec.record_id,
                          json=payload, created_at=ts)
            ins = sqlite_insert(records) if is_sqlite else pg_insert(records)
            cx.execute(ins.values(**values).on_conflict_do_nothing(
                index_elements=["tenant_id", "kind", "record_id"]))

        cx.execute(update(tenants).where(tenants.c.id == tid).values(last_seen=now()))

        # PULL: documentos siempre (carta/config); el histórico solo si se pide.
        docs_out = {}
        for r in cx.execute(select(documents).where(documents.c.tenant_id == tid)).all():
            docs_out[r.doc_key] = {"json": json.loads(r.json), "updated_at": iso(r.updated_at)}
        recs_out = []
        if body.pull_records:
            for r in cx.execute(select(records).where(records.c.tenant_id == tid)
                                .order_by(records.c.created_at)).all():
                recs_out.append({"kind": r.kind, "record_id": r.record_id,
                                 "json": json.loads(r.json), "created_at": iso(r.created_at)})

    return {"documents": docs_out, "records": recs_out, "server_time": iso(now())}
