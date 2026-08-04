"""
Integración con VeriFactu a través de un proveedor homologado (Verifacti).

QUÉ HACE Y QUÉ NO
-----------------
Esto NO convierte a vortexPOS en un sistema de facturación por sí solo. Lo que
hace es enviar cada venta a un proveedor que sí está homologado, que es quien
genera el registro de facturación encadenado y lo remite a la AEAT, y quien
devuelve el QR que hay que imprimir en el ticket.

Mientras `entorno` sea "test", las facturas van al entorno de PRUEBAS de la AEAT
(prewww2.aeat.es) y **no tienen ninguna validez legal**. Para que la tengan hace
falta una clave `vf_prod_` y el alta real del contribuyente.

DÓNDE VIVE LA CLAVE
-------------------
Aquí, en el servidor. Nunca en la app: la app es un HTML que está en la tablet
del bar y cualquiera podría leerlo y emitir facturas en su nombre.

IDEMPOTENCIA
------------
Una venta solo puede facturarse una vez. Lo garantiza la restricción
(tenant_id, record_id) de la tabla `facturas`: si la app reintenta por un corte
de red, se devuelve la factura que ya existía en vez de emitir una duplicada.
Emitir dos veces la misma venta es un problema fiscal, no un fallo estético.
"""
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, insert, update

from .db import engine, tenants, verifactu, facturas

router = APIRouter()

BASE = "https://api.verifacti.com"
CREAR = BASE + "/verifactu/create"
TIMEOUT = 25


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _provider(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    from .main import require_provider
    return require_provider(authorization)


# ---------------------------------------------------------------- Configuración
class ConfigIn(BaseModel):
    api_key: Optional[str] = None
    nif: Optional[str] = None
    serie: Optional[str] = None
    activo: Optional[bool] = None
    entorno: Optional[str] = None


def _config_publica(r) -> Dict[str, Any]:
    """
    La clave NUNCA se devuelve entera, ni siquiera al proveedor autenticado.
    Se enseña solo el prefijo, que es lo único que hace falta para saber si es
    de pruebas o de producción y si está puesta.
    """
    k = r.api_key or ""
    return {
        "tenant_id": r.tenant_id,
        "clave_puesta": bool(k),
        "clave_pista": (k[:11] + "…" + k[-4:]) if len(k) > 18 else ("puesta" if k else ""),
        "nif": r.nif, "serie": r.serie, "ultimo_numero": r.ultimo_numero,
        "activo": bool(r.activo), "entorno": r.entorno,
        "avisa": ("Entorno de PRUEBAS: estas facturas no tienen validez legal."
                  if r.entorno != "produccion" else ""),
    }


@router.get("/api/provider/tenants/{tid}/verifactu")
def ver_config(tid: str, _=Depends(_provider)):
    with engine.begin() as cx:
        if not cx.execute(select(tenants.c.id).where(tenants.c.id == tid)).first():
            raise HTTPException(404, "Local no encontrado")
        r = cx.execute(select(verifactu).where(verifactu.c.tenant_id == tid)).first()
    if not r:
        return {"tenant_id": tid, "clave_puesta": False, "activo": False,
                "entorno": "test", "serie": "A", "ultimo_numero": 0,
                "avisa": "Sin configurar: este local no emite facturas."}
    return _config_publica(r)


@router.put("/api/provider/tenants/{tid}/verifactu")
def guardar_config(tid: str, body: ConfigIn, _=Depends(_provider)):
    if body.entorno is not None and body.entorno not in ("test", "produccion"):
        raise HTTPException(400, "entorno debe ser 'test' o 'produccion'")
    if body.api_key is not None and body.api_key.strip():
        k = body.api_key.strip()
        if not (k.startswith("vf_test_") or k.startswith("vf_prod_")):
            raise HTTPException(400, "La clave debe empezar por vf_test_ o vf_prod_")
    ts = _ahora()
    with engine.begin() as cx:
        if not cx.execute(select(tenants.c.id).where(tenants.c.id == tid)).first():
            raise HTTPException(404, "Local no encontrado")
        existe = cx.execute(select(verifactu).where(verifactu.c.tenant_id == tid)).first()
        vals = {k: v for k, v in {
            "api_key": (body.api_key or "").strip() if body.api_key is not None else None,
            "nif": (body.nif or "").strip().upper() if body.nif is not None else None,
            "serie": (body.serie or "").strip().upper() if body.serie is not None else None,
            "activo": body.activo, "entorno": body.entorno,
        }.items() if v is not None}
        vals["updated_at"] = ts

        # No se deja activar sin lo mínimo: sin clave o sin NIF, activarlo solo
        # serviría para que cada cobro fallara delante del cliente.
        futuro = {**({} if not existe else {
            "api_key": existe.api_key, "nif": existe.nif}), **vals}
        if futuro.get("activo") and not (futuro.get("api_key") and futuro.get("nif")):
            raise HTTPException(400, "Para activar hacen falta la clave y el NIF del local")

        if existe:
            cx.execute(update(verifactu).where(verifactu.c.tenant_id == tid).values(**vals))
        else:
            cx.execute(insert(verifactu).values(
                tenant_id=tid, api_key=vals.get("api_key", ""), nif=vals.get("nif", ""),
                serie=vals.get("serie", "A"), ultimo_numero=0,
                activo=bool(vals.get("activo", False)),
                entorno=vals.get("entorno", "test"), updated_at=ts))
        r = cx.execute(select(verifactu).where(verifactu.c.tenant_id == tid)).first()
    return _config_publica(r)


# ---------------------------------------------------------------- Emisión
def _llamar_proveedor(clave: str, cuerpo: Dict[str, Any]) -> Dict[str, Any]:
    req = urllib.request.Request(
        CREAR, data=json.dumps(cuerpo).encode(), method="POST",
        headers={"Authorization": "Bearer " + clave,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return {"ok": True, "datos": json.loads(r.read().decode())}
    except urllib.error.HTTPError as e:
        detalle = e.read()[:400].decode("utf-8", "ignore")
        return {"ok": False, "error": f"HTTP {e.code}: {detalle}"}
    except Exception as e:
        # Un corte de red no puede perder la factura: queda pendiente y se
        # reintenta. Por eso se distingue el fallo de red del rechazo.
        return {"ok": False, "error": f"sin respuesta del proveedor ({type(e).__name__})",
                "reintentable": True}


class EmitirIn(BaseModel):
    record_id: str
    importe_total: float
    base_imponible: Optional[float] = None
    tipo_iva: int = 21
    descripcion: str = "Consumicion"


def _factura_publica(r) -> Dict[str, Any]:
    return {"id": r.id, "serie": r.serie, "numero": r.numero, "importe": r.importe,
            "estado": r.estado, "uuid": r.uuid_proveedor, "url_aeat": r.url_aeat,
            "qr": r.qr_base64, "error": r.error}


@router.post("/api/device/verifactu/emitir")
def emitir(body: EmitirIn, dev=Depends(lambda authorization=Header(None):
                                       __import__("app.main", fromlist=["require_device"])
                                       .require_device(authorization))):
    """
    La llama la APP del bar tras cobrar. La app nunca ve la clave: manda la venta
    y recibe el QR ya generado.
    """
    tid = dev["tenant_id"]
    with engine.begin() as cx:
        cfg = cx.execute(select(verifactu).where(verifactu.c.tenant_id == tid)).first()
        if not cfg or not cfg.activo:
            raise HTTPException(409, "Este local no tiene la facturacion activada")
        if not cfg.api_key or not cfg.nif:
            raise HTTPException(409, "Falta la clave o el NIF del local")

        # Idempotencia: si esa venta ya se facturó, se devuelve la de antes.
        ya = cx.execute(select(facturas).where(
            (facturas.c.tenant_id == tid) &
            (facturas.c.record_id == body.record_id))).first()
        if ya:
            return {"repetida": True, "factura": _factura_publica(ya)}

        numero = (cfg.ultimo_numero or 0) + 1
        cx.execute(update(verifactu).where(verifactu.c.tenant_id == tid)
                   .values(ultimo_numero=numero, updated_at=_ahora()))
        serie, clave, entorno = cfg.serie or "A", cfg.api_key, cfg.entorno

    base = body.base_imponible
    if base is None:
        base = round(body.importe_total / (1 + body.tipo_iva / 100.0), 2)
    cuota = round(body.importe_total - base, 2)

    cuerpo = {
        "serie": serie, "numero": str(numero).zfill(4),
        "fecha_expedicion": _ahora().strftime("%d-%m-%Y"),
        "tipo_factura": "F2",                      # simplificada: el ticket de un bar
        "descripcion": body.descripcion[:120],
        "importe_total": f"{body.importe_total:.2f}",
        "lineas": [{"base_imponible": f"{base:.2f}",
                    "tipo_impositivo": str(body.tipo_iva),
                    "cuota_repercutida": f"{cuota:.2f}"}],
    }
    res = _llamar_proveedor(clave, cuerpo)
    d = res.get("datos") or {}
    ts = _ahora()
    fid = "fac_" + body.record_id[-12:] + str(numero)

    with engine.begin() as cx:
        cx.execute(insert(facturas).values(
            id=fid[:40], tenant_id=tid, record_id=body.record_id,
            serie=serie, numero=str(numero).zfill(4), importe=body.importe_total,
            estado=(d.get("estado") or "pendiente") if res["ok"] else "error",
            uuid_proveedor=d.get("uuid", ""), url_aeat=d.get("url", ""),
            qr_base64=d.get("qr", ""), error="" if res["ok"] else res["error"][:900],
            created_at=ts))
        r = cx.execute(select(facturas).where(facturas.c.id == fid[:40])).first()

    if not res["ok"]:
        # 502 y no 500: el fallo es del proveedor, no nuestro. La app lo distingue
        # para reintentar en vez de dar el cobro por perdido.
        raise HTTPException(502, f"El proveedor rechazo la factura: {res['error'][:200]}")
    return {"repetida": False, "entorno": entorno, "factura": _factura_publica(r)}


@router.get("/api/provider/tenants/{tid}/facturas")
def listar(tid: str, limit: int = 100, _=Depends(_provider)):
    with engine.begin() as cx:
        rows = cx.execute(select(facturas).where(facturas.c.tenant_id == tid)
                          .order_by(facturas.c.created_at.desc())
                          .limit(min(limit, 500))).all()
    return {"facturas": [_factura_publica(r) for r in rows]}
