"""
Copia de seguridad y restauración de vortexPOS Cloud.

Va en un módulo aparte a propósito: es la red de seguridad del negocio y no
debe mezclarse con el resto de la API. main.py solo lo enchufa con
`app.include_router(backup_router)` al final del archivo.
"""
import json
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel
from sqlalchemy import select, insert, update, and_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .db import engine, tenants, documents, records, new_access_id

router = APIRouter()


def _provider(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """
    Reutiliza la comprobación de proveedor de main.py. La importación es diferida
    para evitar el import circular: cuando llega una petición, main ya está cargado.
    """
    from .main import require_provider
    return require_provider(authorization)


def _now():
    from .main import now
    return now()


def _iso(dt):
    from .main import iso
    return iso(dt)


def _parse_iso(s):
    from .main import parse_iso
    return parse_iso(s)


BACKUP_FORMAT = 1


@router.get("/api/provider/backup")
def provider_backup(_=Depends(_provider)):
    """
    Vuelca TODA la base de datos en un único JSON: locales, documentos e histórico.

    Es la red de seguridad del negocio: si el proveedor de hosting borra la base
    de datos, este fichero permite levantarla otra vez tal cual estaba. Incluye
    los hash de los PIN (necesarios para que los clientes sigan entrando con el
    mismo PIN tras restaurar), así que el fichero se guarda como si fuese una
    contraseña: cifrado o en un disco que solo controle el proveedor.
    """
    with engine.begin() as cx:
        t_rows = cx.execute(select(tenants).order_by(tenants.c.created_at)).all()
        d_rows = cx.execute(select(documents)).all()
        r_rows = cx.execute(select(records).order_by(records.c.created_at)).all()

    out_tenants = [{
        "id": r.id, "license_key": r.license_key,
        "access_id": getattr(r, "access_id", None),
        "pin_hash": r.pin_hash, "business_name": r.business_name,
        "plan": r.plan, "status": r.status, "notes": r.notes,
        "created_at": _iso(r.created_at), "last_seen": _iso(r.last_seen),
    } for r in t_rows]
    out_docs = [{
        "tenant_id": r.tenant_id, "doc_key": r.doc_key,
        "json": r.json, "updated_at": _iso(r.updated_at),
    } for r in d_rows]
    out_recs = [{
        "tenant_id": r.tenant_id, "kind": r.kind, "record_id": r.record_id,
        "json": r.json, "created_at": _iso(r.created_at),
    } for r in r_rows]

    return {
        "vortexpos_backup": BACKUP_FORMAT,
        "created_at": _iso(_now()),
        "counts": {"tenants": len(out_tenants), "documents": len(out_docs),
                   "records": len(out_recs)},
        "tenants": out_tenants, "documents": out_docs, "records": out_recs,
    }


class RestoreIn(BaseModel):
    vortexpos_backup: int = 0
    tenants: List[Dict[str, Any]] = []
    documents: List[Dict[str, Any]] = []
    records: List[Dict[str, Any]] = []


@router.post("/api/provider/restore")
def provider_restore(body: RestoreIn, _=Depends(_provider)):
    """
    Restaura una copia de seguridad. NUNCA borra nada: los locales y documentos
    se actualizan y el histórico se añade ignorando lo que ya existiese. Por eso
    es seguro lanzarla sobre una base de datos vacía (recuperación) o sobre una
    con datos (rellenar un hueco) sin miedo a perder ventas.
    """
    if body.vortexpos_backup != BACKUP_FORMAT:
        raise HTTPException(400, "Formato de copia desconocido — usa un fichero "
                                 f"generado por esta versión (formato {BACKUP_FORMAT})")
    is_sqlite = engine.dialect.name == "sqlite"
    added = {"tenants": 0, "documents": 0, "records": 0}
    with engine.begin() as cx:
        for t in body.tenants:
            tid = t.get("id")
            if not tid:
                continue
            values = dict(
                id=tid, license_key=t.get("license_key"),
                access_id=t.get("access_id") or new_access_id(),
                pin_hash=t.get("pin_hash"), business_name=t.get("business_name") or "",
                plan=t.get("plan") or "Pro", status=t.get("status") or "Activo",
                notes=t.get("notes") or "",
                created_at=_parse_iso(t.get("created_at")),
                last_seen=_parse_iso(t.get("last_seen")) if t.get("last_seen") else None,
            )
            exists = cx.execute(select(tenants.c.id).where(tenants.c.id == tid)).first()
            if exists:
                cx.execute(update(tenants).where(tenants.c.id == tid)
                           .values(**{k: v for k, v in values.items() if k != "id"}))
            else:
                cx.execute(insert(tenants).values(**values))
                added["tenants"] += 1

        for d in body.documents:
            tid, key = d.get("tenant_id"), d.get("doc_key")
            if not tid or not key:
                continue
            ts = _parse_iso(d.get("updated_at"))
            payload = d.get("json")
            if not isinstance(payload, str):
                payload = json.dumps(payload, ensure_ascii=False)
            exists = cx.execute(select(documents.c.tenant_id).where(and_(
                documents.c.tenant_id == tid, documents.c.doc_key == key))).first()
            if exists:
                cx.execute(update(documents).where(and_(
                    documents.c.tenant_id == tid, documents.c.doc_key == key
                )).values(json=payload, updated_at=ts))
            else:
                cx.execute(insert(documents).values(
                    tenant_id=tid, doc_key=key, json=payload, updated_at=ts))
                added["documents"] += 1

        for r in body.records:
            tid, kind, rid = r.get("tenant_id"), r.get("kind"), r.get("record_id")
            if not tid or not kind or not rid:
                continue
            payload = r.get("json")
            if not isinstance(payload, str):
                payload = json.dumps(payload, ensure_ascii=False)
            ins = sqlite_insert(records) if is_sqlite else pg_insert(records)
            res = cx.execute(ins.values(
                tenant_id=tid, kind=kind, record_id=rid, json=payload,
                created_at=_parse_iso(r.get("created_at"))
            ).on_conflict_do_nothing(index_elements=["tenant_id", "kind", "record_id"]))
            added["records"] += (res.rowcount or 0)

    return {"ok": True, "added": added}


