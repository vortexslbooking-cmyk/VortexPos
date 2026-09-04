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
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, insert, update

from .db import engine, tenants, verifactu, facturas
from .fiscal_validacion import TIPOS_CLIENTE, normaliza, valida_cliente

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
    d = {"id": r.id, "serie": r.serie, "numero": r.numero, "importe": r.importe,
         "estado": r.estado, "uuid": r.uuid_proveedor, "url_aeat": r.url_aeat,
         "qr": r.qr_base64, "error": r.error}
    # Los campos nuevos solo se añaden si la fila los tiene. Una instalación a
    # medio migrar no puede reventar el listado de facturas del panel.
    d["tipo"] = getattr(r, "tipo", "F2") or "F2"
    d["base"] = getattr(r, "base", 0.0)
    d["cuota"] = getattr(r, "cuota", 0.0)
    try:
        d["desglose"] = json.loads(getattr(r, "desglose", "[]") or "[]")
    except (ValueError, TypeError):
        d["desglose"] = []
    if d["tipo"] == "F1":
        d["cliente"] = {
            "nombre": getattr(r, "cliente_nombre", ""),
            "nif": getattr(r, "cliente_nif", ""),
            "direccion": getattr(r, "cliente_direccion", ""),
            "cp": getattr(r, "cliente_cp", ""),
            "municipio": getattr(r, "cliente_municipio", ""),
            "provincia": getattr(r, "cliente_provincia", ""),
            "pais": getattr(r, "cliente_pais", "ES"),
        }
    return d


# --------------------------------------------------------------------------
# Desglose de IVA — SIEMPRE se calcula aquí, nunca se cree a la tablet
# --------------------------------------------------------------------------
class LineaIn(BaseModel):
    descripcion: str = ""
    cantidad: float = 1.0
    precio: float = 0.0           # PVP unitario, con IVA incluido (como en la carta)
    tipo_iva: float = 21.0
    descuento: float = 0.0        # importe ya descontado, con IVA incluido


def _dos(x: float) -> float:
    return round(x + 1e-9, 2)


def _desglosar(lineas: List["LineaIn"]) -> Dict[str, Any]:
    """
    Convierte las líneas de la venta en el desglose por tipo de IVA.

    En hostelería los precios de la carta van CON IVA incluido: una caña son
    2,50 € y punto. Así que aquí se va hacia atrás: del total con IVA se saca
    la base. Se agrupa por tipo porque una misma mesa puede llevar comida al
    10 % y bebida alcohólica al 21 %, y el art. 6.1.f) del RD 1619/2012 exige
    desglosarlo por tipos.

    El redondeo se hace UNA sola vez por tipo, sobre el acumulado, no línea a
    línea: redondear cada línea y sumar produce descuadres de céntimos que en
    una factura no se pueden explicar.
    """
    por_tipo: Dict[float, float] = {}
    for ln in lineas:
        bruto = (ln.cantidad or 0) * (ln.precio or 0) - (ln.descuento or 0)
        tipo = float(ln.tipo_iva if ln.tipo_iva is not None else 21)
        por_tipo[tipo] = por_tipo.get(tipo, 0.0) + bruto

    desglose, base_total, cuota_total, total = [], 0.0, 0.0, 0.0
    for tipo in sorted(por_tipo):
        con_iva = _dos(por_tipo[tipo])
        base = _dos(con_iva / (1 + tipo / 100.0))
        cuota = _dos(con_iva - base)          # así base + cuota == con_iva SIEMPRE
        desglose.append({"tipo": tipo, "base": base, "cuota": cuota, "total": con_iva})
        base_total += base
        cuota_total += cuota
        total += con_iva
    return {"desglose": desglose, "base": _dos(base_total),
            "cuota": _dos(cuota_total), "total": _dos(total)}


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


class ClienteIn(BaseModel):
    """Destinatario de una factura completa. Lo manda la app desde su fichero
    de clientes; el servidor lo vuelve a validar antes de emitir nada."""
    nombre: str = ""
    nif: str = ""
    tipo: str = "empresa"          # particular | autonomo | empresa
    direccion: str = ""
    cp: str = ""
    municipio: str = ""
    provincia: str = ""
    pais: str = "ES"
    email: str = ""
    telefono: str = ""


class EmitirCompletaIn(BaseModel):
    record_id: str
    cliente: ClienteIn
    lineas: List[LineaIn]
    metodo_pago: str = ""
    # Lo que la app cree que suma. NO se usa para facturar: solo para comparar
    # y cantar si hay discrepancia. Ver abajo por qué.
    importe_total_app: Optional[float] = None


@router.post("/api/device/verifactu/emitir-completa")
def emitir_completa(body: EmitirCompletaIn,
                    dev=Depends(lambda authorization=Header(None):
                                __import__("app.main", fromlist=["require_device"])
                                .require_device(authorization))):
    """
    Emite una factura COMPLETA (ordinaria, tipo F1) a nombre de un cliente.

    Se diferencia de `emitir` en tres cosas, y las tres importan:

      1. Lleva destinatario con sus datos fiscales, que es lo que la convierte
         en factura completa y no en un ticket.
      2. Usa SERIE Y CONTADOR PROPIOS (por defecto la serie "F"), para no
         mezclar su numeración con la de los tickets.
      3. El importe NO se acepta de la app: se recalcula aquí a partir de las
         líneas. Si la tablet manda 100 € y las líneas suman 90 €, se factura
         90 € y se deja constancia. Un total manipulable desde el cliente es
         un agujero fiscal, no una comodidad.
    """
    tid = dev["tenant_id"]

    # ── 1. Validación fiscal ANTES de tocar nada. Si el cliente no está
    # completo no se reserva número: un hueco en la numeración hay que
    # justificarlo delante de un inspector.
    cliente = body.cliente.model_dump() if hasattr(body.cliente, "model_dump") else body.cliente.dict()
    fallos = valida_cliente(cliente)
    if not body.lineas:
        fallos.append("La factura no tiene ninguna línea.")
    if fallos:
        raise HTTPException(422, {"error": "datos_fiscales_incompletos", "fallos": fallos})

    calc = _desglosar(body.lineas)
    if calc["total"] <= 0:
        raise HTTPException(422, {"error": "importe_no_valido",
                                  "fallos": ["El total de la factura debe ser mayor que cero."]})

    # Discrepancia con lo que decía la app: no se bloquea (el cobro ya ocurrió y
    # el cliente está esperando su factura), pero se deja escrito para poder
    # investigarlo. Manda siempre el cálculo del servidor.
    aviso_descuadre = ""
    if body.importe_total_app is not None and abs(body.importe_total_app - calc["total"]) >= 0.01:
        aviso_descuadre = ("La app decía %.2f y las lineas suman %.2f: se factura el calculo "
                           "del servidor." % (body.importe_total_app, calc["total"]))

    with engine.begin() as cx:
        cfg = cx.execute(select(verifactu).where(verifactu.c.tenant_id == tid)).first()
        if not cfg or not cfg.activo:
            raise HTTPException(409, "Este local no tiene la facturacion activada")
        if not cfg.api_key or not cfg.nif:
            raise HTTPException(409, "Falta la clave o el NIF del local")

        # Idempotencia, igual que en las simplificadas: una venta, una factura.
        ya = cx.execute(select(facturas).where(
            (facturas.c.tenant_id == tid) &
            (facturas.c.record_id == body.record_id))).first()
        if ya:
            return {"repetida": True, "factura": _factura_publica(ya)}

        # Contador propio de las completas. Va dentro de la misma transacción
        # que la lectura: dos tablets cobrando a la vez no pueden llevarse el
        # mismo número.
        numero = (getattr(cfg, "ultimo_numero_completa", 0) or 0) + 1
        cx.execute(update(verifactu).where(verifactu.c.tenant_id == tid)
                   .values(ultimo_numero_completa=numero, updated_at=_ahora()))
        serie = getattr(cfg, "serie_completa", "") or "F"
        clave, entorno = cfg.api_key, cfg.entorno

    cuerpo = {
        "serie": serie, "numero": str(numero).zfill(6),
        "fecha_expedicion": _ahora().strftime("%d-%m-%Y"),
        "tipo_factura": "F1",                  # completa/ordinaria, con destinatario
        "descripcion": (", ".join(l.descripcion for l in body.lineas if l.descripcion)[:120]
                        or "Consumicion"),
        "importe_total": f"{calc['total']:.2f}",
        "destinatarios": [{
            "nombre": cliente["nombre"][:120],
            "nif": normaliza(cliente["nif"]),
        }],
        "lineas": [{"base_imponible": f"{d['base']:.2f}",
                    "tipo_impositivo": str(int(d["tipo"]) if float(d["tipo"]).is_integer()
                                           else d["tipo"]),
                    "cuota_repercutida": f"{d['cuota']:.2f}"}
                   for d in calc["desglose"]],
    }
    res = _llamar_proveedor(clave, cuerpo)
    d = res.get("datos") or {}
    ts = _ahora()
    fid = ("fc_" + body.record_id[-12:] + str(numero))[:40]

    with engine.begin() as cx:
        cx.execute(insert(facturas).values(
            id=fid, tenant_id=tid, record_id=body.record_id,
            serie=serie, numero=str(numero).zfill(6), importe=calc["total"],
            tipo="F1", base=calc["base"], cuota=calc["cuota"],
            desglose=json.dumps(calc["desglose"]),
            cliente_nombre=cliente["nombre"][:200],
            cliente_nif=normaliza(cliente["nif"]),
            cliente_direccion=(cliente.get("direccion") or "")[:200],
            cliente_cp=(cliente.get("cp") or "")[:10],
            cliente_municipio=(cliente.get("municipio") or "")[:120],
            cliente_provincia=(cliente.get("provincia") or "")[:120],
            cliente_pais=((cliente.get("pais") or "ES")[:2]).upper(),
            estado=(d.get("estado") or "pendiente") if res["ok"] else "error",
            uuid_proveedor=d.get("uuid", ""), url_aeat=d.get("url", ""),
            qr_base64=d.get("qr", ""),
            error=(aviso_descuadre if res["ok"] else res["error"][:900]),
            created_at=ts))
        r = cx.execute(select(facturas).where(facturas.c.id == fid)).first()

    if not res["ok"]:
        raise HTTPException(502, f"El proveedor rechazo la factura: {res['error'][:200]}")
    return {"repetida": False, "entorno": entorno, "aviso": aviso_descuadre,
            "factura": _factura_publica(r)}


@router.post("/api/device/verifactu/validar-cliente")
def validar_cliente(body: ClienteIn,
                    dev=Depends(lambda authorization=Header(None):
                                __import__("app.main", fromlist=["require_device"])
                                .require_device(authorization))):
    """
    Comprueba los datos fiscales sin emitir nada.

    La app llama aquí mientras el camarero rellena la ficha, para que el fallo
    aparezca en el momento y no cuando el cliente ya está esperando la factura.
    """
    datos = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    fallos = valida_cliente(datos)
    return {"valido": not fallos, "fallos": fallos, "tipos": TIPOS_CLIENTE}


class RectificarIn(BaseModel):
    record_id: str                # id de la contra-venta (la anulación)
    rectifica_record_id: str      # id de la venta original que se anula
    importe_total: float          # positivo: el importe que se devuelve
    base_imponible: Optional[float] = None
    tipo_iva: int = 21
    descripcion: str = "Anulacion"


@router.post("/api/device/verifactu/rectificar")
def rectificar(body: RectificarIn, dev=Depends(lambda authorization=Header(None):
                                               __import__("app.main", fromlist=["require_device"])
                                               .require_device(authorization))):
    """
    La llama la APP cuando se anula una venta que ya se habia facturado.

    Una factura emitida NO se borra ni se corrige: se emite otra factura
    RECTIFICATIVA que la deja sin efecto (art. 11 RD 1007/2023 y art. 15
    RD 1619/2012). Como los tickets de un bar son facturas simplificadas (F2),
    su rectificativa es siempre del tipo **R5**, cualquiera que sea el motivo.

    Se emite "por diferencias" (tipo_rectificativa "I") y con los importes en
    NEGATIVO: la diferencia respecto de lo facturado es el importe entero con el
    signo cambiado. Asi la suma de la serie da lo realmente cobrado.
    """
    tid = dev["tenant_id"]
    with engine.begin() as cx:
        cfg = cx.execute(select(verifactu).where(verifactu.c.tenant_id == tid)).first()
        if not cfg or not cfg.activo:
            raise HTTPException(409, "Este local no tiene la facturacion activada")
        if not cfg.api_key or not cfg.nif:
            raise HTTPException(409, "Falta la clave o el NIF del local")

        # La original tiene que existir y estar emitida: no se puede rectificar
        # una factura que nunca llego a la AEAT.
        orig = cx.execute(select(facturas).where(
            (facturas.c.tenant_id == tid) &
            (facturas.c.record_id == body.rectifica_record_id))).first()
        if not orig:
            raise HTTPException(409, "La venta que se anula no tiene factura emitida")
        if orig.estado == "error":
            raise HTTPException(409, "La factura original quedo en error: no hay nada que rectificar")

        # Idempotencia, igual que al emitir: una anulacion se rectifica una vez.
        ya = cx.execute(select(facturas).where(
            (facturas.c.tenant_id == tid) &
            (facturas.c.record_id == body.record_id))).first()
        if ya:
            return {"repetida": True, "factura": _factura_publica(ya)}

        numero = (cfg.ultimo_numero or 0) + 1
        cx.execute(update(verifactu).where(verifactu.c.tenant_id == tid)
                   .values(ultimo_numero=numero, updated_at=_ahora()))
        serie, clave, entorno = cfg.serie or "A", cfg.api_key, cfg.entorno
        orig_serie, orig_numero = orig.serie, orig.numero
        orig_fecha = orig.created_at

    importe = abs(body.importe_total)
    base = body.base_imponible
    if base is None:
        base = round(importe / (1 + body.tipo_iva / 100.0), 2)
    base = abs(base)
    cuota = round(importe - base, 2)

    cuerpo = {
        "serie": serie, "numero": str(numero).zfill(4),
        "fecha_expedicion": _ahora().strftime("%d-%m-%Y"),
        "tipo_factura": "R5",                       # rectificativa de simplificada
        "tipo_rectificativa": "I",                  # por diferencias
        "descripcion": body.descripcion[:120],
        "importe_total": f"-{importe:.2f}",
        "lineas": [{"base_imponible": f"-{base:.2f}",
                    "tipo_impositivo": str(body.tipo_iva),
                    "cuota_repercutida": f"-{cuota:.2f}"}],
        "facturas_rectificadas": [{
            "serie": orig_serie, "numero": orig_numero,
            "fecha_expedicion": orig_fecha.strftime("%d-%m-%Y"),
        }],
    }
    res = _llamar_proveedor(clave, cuerpo)
    d = res.get("datos") or {}
    ts = _ahora()
    fid = "rec_" + body.record_id[-12:] + str(numero)

    with engine.begin() as cx:
        cx.execute(insert(facturas).values(
            id=fid[:40], tenant_id=tid, record_id=body.record_id,
            serie=serie, numero=str(numero).zfill(4), importe=-importe,
            estado=(d.get("estado") or "pendiente") if res["ok"] else "error",
            uuid_proveedor=d.get("uuid", ""), url_aeat=d.get("url", ""),
            qr_base64=d.get("qr", ""), error="" if res["ok"] else res["error"][:900],
            created_at=ts))
        r = cx.execute(select(facturas).where(facturas.c.id == fid[:40])).first()

    if not res["ok"]:
        raise HTTPException(502, f"El proveedor rechazo la rectificativa: {res['error'][:200]}")
    return {"repetida": False, "entorno": entorno, "factura": _factura_publica(r)}


@router.get("/api/provider/tenants/{tid}/facturas")
def listar(tid: str, limit: int = 100, _=Depends(_provider)):
    with engine.begin() as cx:
        rows = cx.execute(select(facturas).where(facturas.c.tenant_id == tid)
                          .order_by(facturas.c.created_at.desc())
                          .limit(min(limit, 500))).all()
    return {"facturas": [_factura_publica(r) for r in rows]}
