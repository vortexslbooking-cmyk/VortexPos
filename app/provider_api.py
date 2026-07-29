"""
Centro de mando del proveedor: leads, fallos y estadísticas globales.

Va en un módulo aparte por la misma razón que backup_api.py: son datos del
PROVEEDOR (VORTEX S.L.), no de ningún local, y mezclarlos con la API de
sincronización de los bares solo serviría para confundir los dos mundos.
main.py lo enchufa con `app.include_router(provider_router)` al final.

Reglas de esta capa:
  · Todo /api/provider/* exige token de proveedor.
  · POST /api/errors es PÚBLICO (la app de un local reporta sus fallos sola),
    así que está limitado por IP, saneado y sin datos personales.
"""
import re
import json
import time
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException, Depends, Header, Request
from pydantic import BaseModel
from sqlalchemy import select, insert, update, delete, and_, or_, case, func as sa_func

from .db import (engine, tenants, records, leads, errors, tasks,
                 LEAD_LINES, LEAD_STATUSES, LEAD_PRIORITIES, TASK_WHO)

router = APIRouter()


# ---------------------------------------------------------------- Utilidades
def _provider(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """Import diferido para evitar el ciclo con main (igual que en backup_api)."""
    from .main import require_provider
    return require_provider(authorization)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt):
    from .main import iso
    return iso(dt)


def _txt(value: Any, limit: int) -> str:
    """Texto de entrada: nunca None, sin espacios sobrantes y con tope de longitud."""
    return str(value if value is not None else "").strip()[:limit]


def _one_of(value: Any, allowed, field: str) -> str:
    v = _txt(value, 30).lower()
    if v not in allowed:
        raise HTTPException(400, f"{field} no válido — usa uno de: {', '.join(allowed)}")
    return v


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _date_or_empty(value: Any) -> str:
    v = _txt(value, 10)
    if v and not _DATE_RE.match(v):
        raise HTTPException(400, "La fecha debe tener el formato AAAA-MM-DD")
    return v


def _today() -> str:
    return _now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------- Leads
class LeadIn(BaseModel):
    business_name: str = ""
    contact: str = ""
    phone: str = ""
    email: str = ""
    zone: str = ""
    business_type: str = ""
    line: str = "pos"
    status: str = "nuevo"
    priority: str = "media"
    source: str = ""
    notes: str = ""
    next_action: str = ""
    next_date: str = ""


class LeadPatch(BaseModel):
    business_name: Optional[str] = None
    contact: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    zone: Optional[str] = None
    business_type: Optional[str] = None
    line: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    source: Optional[str] = None
    notes: Optional[str] = None
    next_action: Optional[str] = None
    next_date: Optional[str] = None


# Longitud máxima por campo: coincide con la columna, así una entrada larga
# devuelve 400 en vez de reventar contra la base de datos en producción.
_LEAD_LIMITS = {
    "business_name": 200, "contact": 120, "phone": 40, "email": 160,
    "zone": 120, "business_type": 120, "source": 60, "notes": 4000,
    "next_action": 200,
}


def _lead_public(r) -> Dict[str, Any]:
    return {
        "id": r.id, "business_name": r.business_name, "contact": r.contact,
        "phone": r.phone, "email": r.email, "zone": r.zone,
        "business_type": r.business_type, "line": r.line, "status": r.status,
        "priority": r.priority, "source": r.source, "notes": r.notes,
        "next_action": r.next_action, "next_date": r.next_date,
        "created_at": _iso(r.created_at), "updated_at": _iso(r.updated_at),
    }


# Orden de trabajo comercial: primero lo que tiene fecha (y antes lo más urgente),
# después por prioridad. Es el orden en el que el fundador quiere atacar la lista.
_PRIORITY_ORDER = case((leads.c.priority == "alta", 0), (leads.c.priority == "media", 1), else_=2)
_DATE_FIRST = case((leads.c.next_date == "", 1), else_=0)

# Un lead "cerrado" ya no pide acción, esté como esté su próxima fecha.
_OPEN_STATUSES = tuple(s for s in LEAD_STATUSES if s not in ("cliente", "descartado"))


@router.get("/api/provider/leads")
def list_leads(line: Optional[str] = None, status: Optional[str] = None,
               q: Optional[str] = None, _=Depends(_provider)):
    """Listado filtrable por línea, estado y texto libre (nombre / zona / contacto)."""
    where = []
    if line:
        where.append(leads.c.line == _one_of(line, LEAD_LINES, "line"))
    if status:
        where.append(leads.c.status == _one_of(status, LEAD_STATUSES, "status"))
    if q:
        like = "%" + _txt(q, 80).lower() + "%"
        where.append(or_(sa_func.lower(leads.c.business_name).like(like),
                         sa_func.lower(leads.c.zone).like(like),
                         sa_func.lower(leads.c.contact).like(like)))
    stmt = select(leads).order_by(_DATE_FIRST, leads.c.next_date,
                                  _PRIORITY_ORDER, leads.c.business_name)
    if where:
        stmt = stmt.where(and_(*where))
    with engine.begin() as cx:
        rows = cx.execute(stmt).all()
    return {"leads": [_lead_public(r) for r in rows]}


@router.post("/api/provider/leads")
def create_lead(body: LeadIn, _=Depends(_provider)):
    name = _txt(body.business_name, _LEAD_LIMITS["business_name"])
    if not name:
        raise HTTPException(400, "El nombre del negocio es obligatorio")
    ts = _now()
    values = {
        "id": "ld_" + secrets.token_hex(6),
        "business_name": name,
        "line": _one_of(body.line or "pos", LEAD_LINES, "line"),
        "status": _one_of(body.status or "nuevo", LEAD_STATUSES, "status"),
        "priority": _one_of(body.priority or "media", LEAD_PRIORITIES, "priority"),
        "next_date": _date_or_empty(body.next_date),
        "created_at": ts, "updated_at": ts,
    }
    for field, limit in _LEAD_LIMITS.items():
        if field != "business_name":
            values[field] = _txt(getattr(body, field), limit)
    with engine.begin() as cx:
        cx.execute(insert(leads).values(**values))
        row = cx.execute(select(leads).where(leads.c.id == values["id"])).first()
    return {"lead": _lead_public(row)}


@router.patch("/api/provider/leads/{lead_id}")
def patch_lead(lead_id: str, body: LeadPatch, _=Depends(_provider)):
    vals: Dict[str, Any] = {}
    for field, limit in _LEAD_LIMITS.items():
        v = getattr(body, field)
        if v is not None:
            vals[field] = _txt(v, limit)
    if vals.get("business_name") == "":
        raise HTTPException(400, "El nombre del negocio no puede quedar vacío")
    if body.line is not None:
        vals["line"] = _one_of(body.line, LEAD_LINES, "line")
    if body.status is not None:
        vals["status"] = _one_of(body.status, LEAD_STATUSES, "status")
    if body.priority is not None:
        vals["priority"] = _one_of(body.priority, LEAD_PRIORITIES, "priority")
    if body.next_date is not None:
        vals["next_date"] = _date_or_empty(body.next_date)
    if not vals:
        raise HTTPException(400, "Nada que actualizar")
    vals["updated_at"] = _now()
    with engine.begin() as cx:
        res = cx.execute(update(leads).where(leads.c.id == lead_id).values(**vals))
        if res.rowcount == 0:
            raise HTTPException(404, "Lead no encontrado")
        row = cx.execute(select(leads).where(leads.c.id == lead_id)).first()
    return {"lead": _lead_public(row)}


@router.delete("/api/provider/leads/{lead_id}")
def delete_lead(lead_id: str, _=Depends(_provider)):
    with engine.begin() as cx:
        res = cx.execute(delete(leads).where(leads.c.id == lead_id))
    if res.rowcount == 0:
        raise HTTPException(404, "Lead no encontrado")
    return {"ok": True}


# ---------------------------------------------------------------- Fallos
# El endpoint de reporte es público (una app que falla puede no estar autenticada),
# así que se protege por tres vías: límite por IP, tamaños acotados y saneado.
_ERR_HITS: Dict[str, List[float]] = {}
_ERR_MAX = 30            # reportes permitidos por IP…
_ERR_WINDOW = 600.0      # …cada 10 minutos


def _rate_limit_errors(ip: str):
    t = time.monotonic()
    if len(_ERR_HITS) > 2000:   # limpieza: el diccionario no puede crecer sin fin
        for k in [k for k, v in _ERR_HITS.items() if not v or t - v[-1] > _ERR_WINDOW]:
            _ERR_HITS.pop(k, None)
    hits = [h for h in _ERR_HITS.get(ip, []) if t - h < _ERR_WINDOW]
    if len(hits) >= _ERR_MAX:
        _ERR_HITS[ip] = hits
        raise HTTPException(429, "Demasiados reportes de error — inténtalo más tarde")
    hits.append(t)
    _ERR_HITS[ip] = hits


# Datos personales que JAMÁS deben quedar guardados aquí: los clientes finales de
# un bar no tienen nada que hacer en el registro de fallos del proveedor. Se
# tapan en el servidor y no se confía en que el cliente lo haya hecho antes.
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")
_RE_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,28}\b")
_RE_PHONE_ES = re.compile(r"(?:\+\d{1,3}[\s.-]?)?[6789]\d{2}[\s.-]?\d{2}[\s.-]?\d{2}[\s.-]?\d{2}\b")
_RE_LONG_NUM = re.compile(r"\b\d{9,}\b")   # tarjetas, DNI, teléfonos sin formato


def _redact(text: str) -> str:
    text = _RE_EMAIL.sub("[email-oculto]", text)
    text = _RE_IBAN.sub("[iban-oculto]", text)
    text = _RE_PHONE_ES.sub("[tel-oculto]", text)
    text = _RE_LONG_NUM.sub("[num-oculto]", text)
    return text


def _clean_context(value: Any, depth: int = 0) -> Any:
    """
    Deja el contexto en algo pequeño, plano y sin datos personales:
    máximo 20 claves, 2 niveles de anidamiento y 200 caracteres por valor.
    """
    if isinstance(value, str):
        return _redact(value.strip()[:200])
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if depth >= 2:
        return _redact(str(value)[:200])
    if isinstance(value, dict):
        return {_txt(k, 40): _clean_context(v, depth + 1)
                for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple)):
        return [_clean_context(v, depth + 1) for v in list(value)[:20]]
    return _redact(str(value)[:200])


class ErrorIn(BaseModel):
    type: str = "app"
    message: str = ""
    context: Any = None
    app_version: str = ""
    tenant_id: str = ""      # solo se acepta si el local existe de verdad


def _error_public(r) -> Dict[str, Any]:
    try:
        ctx = json.loads(r.context)
    except Exception:
        ctx = {}
    return {
        "id": r.id, "tenant_id": r.tenant_id, "type": r.type, "message": r.message,
        "context": ctx, "app_version": r.app_version, "hits": r.hits,
        "resolved": bool(r.resolved),
        "created_at": _iso(r.created_at), "updated_at": _iso(r.updated_at),
    }


@router.post("/api/errors")
def report_error(body: ErrorIn, request: Request,
                 authorization: Optional[str] = Header(None)):
    """
    Reporte de fallo desde la app de un local. Público a propósito: un fallo de
    activación o de red ocurre justo cuando el dispositivo NO tiene sesión, que
    es cuando más falta hace enterarse.

    El local se identifica por su token si lo tiene (fuente fiable); si no, se
    acepta un tenant_id declarado solo cuando corresponde a un local real. Nunca
    se guardan datos de los clientes finales del bar (ver _redact).
    """
    from .main import client_ip
    from .security import read_token
    _rate_limit_errors(client_ip(request))

    tenant_id: Optional[str] = None
    if authorization and authorization.lower().startswith("bearer "):
        data = read_token(authorization.split(" ", 1)[1]) or {}
        if data.get("role") == "device":
            tenant_id = data.get("tenant_id")
    declared = _txt(body.tenant_id, 40)

    etype = _txt(body.type, 60) or "app"
    message = _redact(_txt(body.message, 500)) or "(sin mensaje)"
    context = json.dumps(_clean_context(body.context) if body.context is not None else {},
                         ensure_ascii=False)[:2000]
    version = _txt(body.app_version, 30)
    ts = _now()

    with engine.begin() as cx:
        if tenant_id is None and declared:
            exists = cx.execute(select(tenants.c.id).where(tenants.c.id == declared)).first()
            tenant_id = declared if exists else None

        # Mismo fallo, mismo local y aún sin resolver -> suma en 'hits'.
        dup = cx.execute(select(errors.c.id, errors.c.hits).where(and_(
            errors.c.resolved.is_(False),
            errors.c.type == etype,
            errors.c.message == message,
            errors.c.tenant_id.is_(None) if tenant_id is None else errors.c.tenant_id == tenant_id,
        )).limit(1)).first()
        if dup:
            cx.execute(update(errors).where(errors.c.id == dup.id).values(
                hits=(dup.hits or 1) + 1, updated_at=ts,
                context=context, app_version=version or ""))
            return {"ok": True, "id": dup.id}

        eid = "er_" + secrets.token_hex(6)
        cx.execute(insert(errors).values(
            id=eid, tenant_id=tenant_id, type=etype, message=message,
            context=context, app_version=version, hits=1, resolved=False,
            created_at=ts, updated_at=ts))
    return {"ok": True, "id": eid}


@router.get("/api/provider/errors")
def list_errors(resolved: Optional[bool] = None, limit: int = 200,
                _=Depends(_provider)):
    limit = max(1, min(int(limit or 200), 500))
    stmt = select(errors).order_by(errors.c.updated_at.desc()).limit(limit)
    if resolved is not None:
        stmt = stmt.where(errors.c.resolved.is_(bool(resolved)))
    with engine.begin() as cx:
        rows = cx.execute(stmt).all()
        names = dict(cx.execute(select(tenants.c.id, tenants.c.business_name)).all())
    out = []
    for r in rows:
        e = _error_public(r)
        e["business_name"] = names.get(r.tenant_id or "", "")
        out.append(e)
    return {"errors": out}


class ErrorPatch(BaseModel):
    resolved: bool


@router.patch("/api/provider/errors/{error_id}")
def patch_error(error_id: str, body: ErrorPatch, _=Depends(_provider)):
    with engine.begin() as cx:
        res = cx.execute(update(errors).where(errors.c.id == error_id).values(
            resolved=bool(body.resolved), updated_at=_now()))
    if res.rowcount == 0:
        raise HTTPException(404, "Fallo no encontrado")
    return {"ok": True, "resolved": bool(body.resolved)}


@router.delete("/api/provider/errors/{error_id}")
def delete_error(error_id: str, _=Depends(_provider)):
    with engine.begin() as cx:
        res = cx.execute(delete(errors).where(errors.c.id == error_id))
    if res.rowcount == 0:
        raise HTTPException(404, "Fallo no encontrado")
    return {"ok": True}


# ---------------------------------------------------------------- Estadísticas
def _money(v) -> float:
    return round(float(v or 0), 2)


def _server_version() -> str:
    from .main import app as _app
    return _app.version


@router.get("/api/provider/stats")
def provider_stats(_=Depends(_provider)):
    """
    Visión global del negocio en UNA sola llamada: el panel abre la pantalla de
    Resumen con una petición, que en un móvil con mala cobertura importa.

    Todos los agregados los calcula la base de datos (SUM/COUNT/GROUP BY); aquí
    no se recorre el histórico de ventas, por eso el coste no crece con los años.
    """
    ahora = _now()
    inicio_dia = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    inicio_mes = inicio_dia.replace(day=1)
    hace_24h = ahora - timedelta(hours=24)
    hoy = _today()

    with engine.begin() as cx:
        # --- Locales
        por_estado = dict(cx.execute(
            select(tenants.c.status, sa_func.count()).group_by(tenants.c.status)).all())
        total_locales = sum(por_estado.values())
        activos_hoy = cx.execute(select(sa_func.count()).select_from(tenants)
                                 .where(tenants.c.last_seen >= inicio_dia)).scalar() or 0

        # --- Ventas (histórico, mes y día)
        def ventas(desde=None):
            stmt = select(sa_func.count(), sa_func.coalesce(sa_func.sum(records.c.amount), 0)) \
                .where(records.c.kind == "sale")
            if desde is not None:
                stmt = stmt.where(records.c.created_at >= desde)
            n, total = cx.execute(stmt).first()
            return int(n or 0), _money(total)

        n_total, v_total = ventas()
        n_mes, v_mes = ventas(inicio_mes)
        n_hoy, v_hoy = ventas(inicio_dia)
        cierres = cx.execute(select(sa_func.count()).select_from(records)
                             .where(records.c.kind == "closure")).scalar() or 0

        # --- Leads
        leads_estado = dict(cx.execute(
            select(leads.c.status, sa_func.count()).group_by(leads.c.status)).all())
        leads_linea = dict(cx.execute(
            select(leads.c.line, sa_func.count()).group_by(leads.c.line)).all())
        pendientes_stmt = select(leads).where(and_(
            leads.c.next_date != "", leads.c.next_date <= hoy,
            leads.c.status.in_(_OPEN_STATUSES))).order_by(leads.c.next_date)
        pendientes = cx.execute(pendientes_stmt).all()

        # --- Fallos
        sin_resolver = cx.execute(select(sa_func.count()).select_from(errors)
                                  .where(errors.c.resolved.is_(False))).scalar() or 0
        total_fallos = cx.execute(select(sa_func.count()).select_from(errors)).scalar() or 0
        ultimas_24h = cx.execute(select(sa_func.count()).select_from(errors)
                                 .where(errors.c.updated_at >= hace_24h)).scalar() or 0
        ultimos = cx.execute(select(errors).where(errors.c.resolved.is_(False))
                             .order_by(errors.c.updated_at.desc()).limit(5)).all()
        nombres = dict(cx.execute(select(tenants.c.id, tenants.c.business_name)).all())

    fallos_recientes = []
    for r in ultimos:
        e = _error_public(r)
        e["business_name"] = nombres.get(r.tenant_id or "", "")
        fallos_recientes.append(e)

    return {
        "tenants": {
            "total": total_locales,
            "by_status": por_estado,
            "active_today": int(activos_hoy),
        },
        "sales": {
            "total": v_total, "count": n_total,
            "month_total": v_mes, "month_count": n_mes,
            "today_total": v_hoy, "today_count": n_hoy,
            "avg_ticket": _money(v_total / n_total) if n_total else 0.0,
            "closures": int(cierres),
        },
        "leads": {
            "total": sum(leads_estado.values()),
            "by_status": leads_estado,
            "by_line": leads_linea,
            "due": len(pendientes),
            "due_list": [_lead_public(r) for r in pendientes[:5]],
        },
        "errors": {
            "unresolved": int(sin_resolver), "total": int(total_fallos),
            "last_24h": int(ultimas_24h), "recent": fallos_recientes,
        },
        "system": {
            "server_time": _iso(ahora),
            "database": engine.dialect.name,
            "version": _server_version(),
        },
    }


# ---------------------------------------------------------------- Agenda del equipo
class TaskIn(BaseModel):
    title: str = ""
    detail: str = ""
    date: str = ""
    who: str = "ambos"


class TaskPatch(BaseModel):
    title: Optional[str] = None
    detail: Optional[str] = None
    date: Optional[str] = None
    who: Optional[str] = None
    done: Optional[bool] = None


_TASK_LIMITS = {"title": 200, "detail": 4000}


def _task_public(r) -> Dict[str, Any]:
    return {
        "id": r.id, "title": r.title, "detail": r.detail, "date": r.date,
        "who": r.who, "done": bool(r.done),
        "created_at": _iso(r.created_at), "updated_at": _iso(r.updated_at),
    }


@router.get("/api/provider/tasks")
def list_tasks(desde: Optional[str] = None, hasta: Optional[str] = None,
               _=Depends(_provider)):
    """
    Tareas del equipo, opcionalmente acotadas a un rango de fechas (el calendario
    del panel pide solo el mes que se está viendo). Orden: por día y, dentro del
    día, lo no hecho primero.
    """
    where = []
    if desde:
        where.append(tasks.c.date >= _date_or_empty(desde))
    if hasta:
        where.append(tasks.c.date <= _date_or_empty(hasta))
    stmt = select(tasks).order_by(tasks.c.date, tasks.c.done, tasks.c.created_at)
    if where:
        stmt = stmt.where(and_(*where))
    with engine.begin() as cx:
        rows = cx.execute(stmt).all()
    return {"tasks": [_task_public(r) for r in rows]}


@router.post("/api/provider/tasks")
def create_task(body: TaskIn, _=Depends(_provider)):
    title = _txt(body.title, _TASK_LIMITS["title"])
    if not title:
        raise HTTPException(400, "La tarea necesita un título")
    ts = _now()
    values = {
        "id": "tk_" + secrets.token_hex(6),
        "title": title,
        "detail": _txt(body.detail, _TASK_LIMITS["detail"]),
        "date": _date_or_empty(body.date),
        "who": _one_of(body.who or "ambos", TASK_WHO, "who"),
        "done": False, "created_at": ts, "updated_at": ts,
    }
    with engine.begin() as cx:
        cx.execute(insert(tasks).values(**values))
        row = cx.execute(select(tasks).where(tasks.c.id == values["id"])).first()
    return {"task": _task_public(row)}


@router.patch("/api/provider/tasks/{task_id}")
def patch_task(task_id: str, body: TaskPatch, _=Depends(_provider)):
    vals: Dict[str, Any] = {}
    if body.title is not None:
        t = _txt(body.title, _TASK_LIMITS["title"])
        if not t:
            raise HTTPException(400, "El título no puede quedar vacío")
        vals["title"] = t
    if body.detail is not None:
        vals["detail"] = _txt(body.detail, _TASK_LIMITS["detail"])
    if body.date is not None:
        vals["date"] = _date_or_empty(body.date)
    if body.who is not None:
        vals["who"] = _one_of(body.who, TASK_WHO, "who")
    if body.done is not None:
        vals["done"] = bool(body.done)
    if not vals:
        raise HTTPException(400, "Nada que actualizar")
    vals["updated_at"] = _now()
    with engine.begin() as cx:
        res = cx.execute(update(tasks).where(tasks.c.id == task_id).values(**vals))
        if res.rowcount == 0:
            raise HTTPException(404, "Tarea no encontrada")
        row = cx.execute(select(tasks).where(tasks.c.id == task_id)).first()
    return {"task": _task_public(row)}


@router.delete("/api/provider/tasks/{task_id}")
def delete_task(task_id: str, _=Depends(_provider)):
    with engine.begin() as cx:
        res = cx.execute(delete(tasks).where(tasks.c.id == task_id))
    if res.rowcount == 0:
        raise HTTPException(404, "Tarea no encontrada")
    return {"ok": True}


_WHO_LABEL = {"socio": "Tu socio", "fundador": "Tú", "ambos": "Los dos"}


@router.get("/api/provider/agenda-correo")
def agenda_correo(_=Depends(_provider)):
    """
    Devuelve el correo de la mañana ya redactado: atrasadas + hoy + un adelanto de
    mañana. Lo consume la tarea automática de GitHub, que solo tiene que enviarlo.
    Se redacta aquí (y no en el YAML) para poder probarlo de verdad con un test.
    """
    hoy = _today()
    manana = (_now() + timedelta(days=1)).strftime("%Y-%m-%d")
    with engine.begin() as cx:
        atrasadas = cx.execute(select(tasks).where(and_(
            tasks.c.done.is_(False), tasks.c.date != "", tasks.c.date < hoy))
            .order_by(tasks.c.date)).all()
        de_hoy = cx.execute(select(tasks).where(and_(
            tasks.c.done.is_(False), tasks.c.date == hoy))
            .order_by(tasks.c.created_at)).all()
        de_manana = cx.execute(select(tasks).where(and_(
            tasks.c.done.is_(False), tasks.c.date == manana))
            .order_by(tasks.c.created_at)).all()

    def linea(r):
        quien = _WHO_LABEL.get(r.who, "Los dos")
        extra = f" — {r.detail}" if r.detail else ""
        return f"  • {r.title}  ({quien}){extra}"

    partes: List[str] = [f"Agenda de VORTEX · {hoy}", ""]
    if atrasadas:
        partes.append(f"⚠️  ATRASADAS ({len(atrasadas)}):")
        partes += [linea(r) + f"  [era {r.date}]" for r in atrasadas]
        partes.append("")
    partes.append(f"📌 HOY ({len(de_hoy)}):")
    partes += [linea(r) for r in de_hoy] if de_hoy else ["  (nada apuntado para hoy 🎉)"]
    if de_manana:
        partes += ["", f"👀 Mañana ({len(de_manana)}):"]
        partes += [linea(r) for r in de_manana]
    partes += ["", "— Centro de mando: https://vortexpos-cloud.onrender.com/"]

    texto = "\n".join(partes)
    n_hoy = len(de_hoy) + len(atrasadas)
    asunto = f"Vortex · Hoy tienes {n_hoy} tarea{'s' if n_hoy != 1 else ''} ({hoy})" \
        if n_hoy else f"Vortex · Sin tareas para hoy ({hoy})"
    return {"subject": asunto, "text": texto, "count_today": len(de_hoy),
            "count_overdue": len(atrasadas)}
