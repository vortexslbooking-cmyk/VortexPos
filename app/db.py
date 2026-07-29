"""
Capa de datos de vortexPOS Cloud.

Un único almacén multi-inquilino (multi-tenant): todos los locales comparten
las mismas tablas, pero cada fila lleva su tenant_id y las consultas SIEMPRE
filtran por él, de modo que un local nunca ve datos de otro.

- tenants   : un registro por local/licencia (negocio, plan, estado, PIN).
- documents : datos reemplazables por local (carta, config, mesas). Last-Write-Wins.
- records   : datos append-only e idempotentes (ventas, cierres, movimientos de caja).
              Nunca se pierden ni se duplican: clave (tenant_id, kind, record_id).

Y dos tablas que NO son de ningún local sino del PROVEEDOR (VORTEX S.L.), por eso
no llevan tenant_id como clave de aislamiento y solo se leen con token de proveedor:

- leads     : trabajo comercial (negocios a captar). Antes vivía en el localStorage
              del navegador del fundador: si limpiaba el navegador, lo perdía todo.
- errors    : fallos reportados por las apps de los locales, para verlos en un sitio.
              Llevan tenant_id solo como referencia de "quién lo reportó".
- meta      : marcas internas del servidor (p. ej. "los leads ya se importaron").

Portabilidad: se usa DATABASE_URL. Por defecto SQLite (desarrollo); en producción
se pone una URL de Postgres y el mismo código funciona sin cambios.
"""
import os
from sqlalchemy import (create_engine, MetaData, Table, Column, String, Text,
                        Float, Integer, Boolean, DateTime, UniqueConstraint, func)

def _normalize(url: str) -> str:
    """
    Render/Heroku entregan la URL como 'postgres://…', un esquema que SQLAlchemy 2.0
    ya no admite; además usamos el driver psycopg v3. Se normaliza para que el mismo
    código funcione en local (SQLite) y en producción (Postgres) sin tocar nada.
    """
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DATABASE_URL = _normalize(os.environ.get("DATABASE_URL", "sqlite:///./vortexpos.db"))

# SQLite necesita este flag con FastAPI (varios hilos); Postgres lo ignora.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True, future=True)

metadata = MetaData()

tenants = Table(
    "tenants", metadata,
    Column("id", String(40), primary_key=True),
    Column("license_key", String(40), unique=True, nullable=False),
    # ID de acceso corto y legible que se entrega al cliente junto al PIN.
    # Con ID + PIN la app se activa sola: el cliente nunca ve la licencia larga.
    Column("access_id", String(16), unique=True, nullable=True),
    Column("pin_hash", String(255), nullable=False),
    Column("business_name", String(200), nullable=False, default=""),
    Column("plan", String(20), nullable=False, default="Pro"),
    Column("status", String(20), nullable=False, default="Activo"),  # Activo|Pendiente|Suspendido|Baja
    Column("notes", Text, nullable=False, default=""),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("last_seen", DateTime(timezone=True), nullable=True),
)

documents = Table(
    "documents", metadata,
    Column("tenant_id", String(40), nullable=False),
    Column("doc_key", String(60), nullable=False),   # menu | config | tables | reservations
    Column("json", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("tenant_id", "doc_key", name="uq_doc"),
)

records = Table(
    "records", metadata,
    Column("tenant_id", String(40), nullable=False),
    Column("kind", String(30), nullable=False),       # sale | closure | cashmove
    Column("record_id", String(40), nullable=False),  # uid del cliente (idempotencia)
    Column("json", Text, nullable=False),
    # Importe de la venta extraído del JSON al guardar. Es una COPIA denormalizada:
    # el JSON sigue siendo la verdad. Existe para que los totales del panel se
    # calculen con un SUM() en la base de datos en vez de parsear en Python el
    # histórico entero de todos los locales (eso no escala a miles de negocios).
    Column("amount", Float, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("tenant_id", "kind", "record_id", name="uq_rec"),
)

# ---------------------------------------------------------------- Datos del proveedor
LEAD_LINES = ("agencia", "pos", "ambas", "descarte")
LEAD_STATUSES = ("nuevo", "contactado", "interesado", "piloto", "cliente", "descartado")
LEAD_PRIORITIES = ("alta", "media", "baja")

leads = Table(
    "leads", metadata,
    Column("id", String(60), primary_key=True),
    Column("business_name", String(200), nullable=False, default=""),
    Column("contact", String(120), nullable=False, default=""),      # persona de contacto
    Column("phone", String(40), nullable=False, default=""),
    Column("email", String(160), nullable=False, default=""),
    Column("zone", String(120), nullable=False, default=""),
    Column("business_type", String(120), nullable=False, default=""),  # "chiringuito", "cafetería"…
    Column("line", String(20), nullable=False, default="pos"),         # LEAD_LINES
    Column("status", String(20), nullable=False, default="nuevo"),     # LEAD_STATUSES
    Column("priority", String(10), nullable=False, default="media"),   # LEAD_PRIORITIES
    Column("source", String(60), nullable=False, default=""),
    Column("notes", Text, nullable=False, default=""),
    Column("next_action", String(200), nullable=False, default=""),    # qué hay que hacer
    # Cuándo hay que hacerlo, en formato "AAAA-MM-DD" (vacío = sin fecha).
    # Se guarda como texto a propósito: ordena igual que una fecha, no arrastra
    # zonas horarias y es exactamente lo que envía un <input type="date">.
    Column("next_date", String(10), nullable=False, default=""),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

errors = Table(
    "errors", metadata,
    Column("id", String(40), primary_key=True),
    Column("tenant_id", String(40), nullable=True),   # local que lo reportó (si se pudo verificar)
    Column("type", String(60), nullable=False, default="app"),
    Column("message", Text, nullable=False, default=""),
    Column("context", Text, nullable=False, default="{}"),   # JSON técnico, ya saneado
    Column("app_version", String(30), nullable=False, default=""),
    # Un fallo repetido no crea filas nuevas: suma en 'hits'. Así una app en bucle
    # de error no inunda la base de datos ni el panel.
    Column("hits", Integer, nullable=False, default=1),
    Column("resolved", Boolean, nullable=False, default=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

meta = Table(
    "meta", metadata,
    Column("key", String(60), primary_key=True),
    Column("value", Text, nullable=False, default=""),
)

# ---------------------------------------------------------------- Agenda del equipo
# Tareas de trabajo de los dos fundadores. NO es de ningún local: es del proveedor,
# se guarda en el servidor (no en el navegador) para que los dos socios vean lo
# mismo, y alimenta el correo automático de cada mañana.
# A quién se asigna una tarea: nombre libre (los dos fundadores, un agente del
# equipo, o "Todos" para la tarea común del día). Se guarda el nombre tal cual en
# vez de un código cerrado para no tener que tocar el servidor cada vez que entre
# alguien nuevo al equipo.
TASK_WHO_DEFAULT = "Todos"
TASK_WHO_MAX = 20

tasks = Table(
    "tasks", metadata,
    Column("id", String(40), primary_key=True),
    Column("title", String(200), nullable=False, default=""),
    Column("detail", Text, nullable=False, default=""),
    # Día de la tarea en "AAAA-MM-DD" (texto: ordena bien y es lo que da <input type=date>).
    Column("date", String(10), nullable=False, default=""),
    Column("who", String(20), nullable=False, default="Todos"),   # nombre: Todos/Said/Alejandro/agente…
    Column("done", Boolean, nullable=False, default=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


def _column_exists(cx, table: str, column: str) -> bool:
    """Comprobación portable (SQLite y Postgres) de si una columna ya existe."""
    from sqlalchemy import inspect
    try:
        cols = inspect(cx).get_columns(table)
        return any(c["name"] == column for c in cols)
    except Exception:
        return False


def sale_amount(payload) -> float:
    """
    Importe de una venta a partir de su JSON. Un único sitio con esta regla:
    lo usan tanto la sincronización como la migración, así que nunca pueden
    discrepar. Si el dato no es un número válido, cuenta como 0 (jamás revienta:
    perder la sincronización de un local por una venta rara sería peor).
    """
    try:
        return round(float((payload or {}).get("total", 0) or 0), 2)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _migrate_sale_amount(cx):
    """
    Rellena records.amount en las ventas antiguas (las guardadas antes de que
    existiera la columna). Va por lotes para no cargar el histórico entero en
    memoria; en el segundo arranque ya no encuentra filas y no cuesta nada.
    """
    import json as _json
    from sqlalchemy import text
    while True:
        rows = cx.execute(text(
            "SELECT tenant_id, kind, record_id, json FROM records "
            "WHERE kind = 'sale' AND amount IS NULL LIMIT 500")).all()
        if not rows:
            return
        for tid, kind, rid, raw in rows:
            try:
                amount = sale_amount(_json.loads(raw))
            except Exception:
                amount = 0.0
            cx.execute(text(
                "UPDATE records SET amount = :a WHERE tenant_id = :t "
                "AND kind = :k AND record_id = :r"),
                {"a": amount, "t": tid, "k": kind, "r": rid})


def init_db():
    metadata.create_all(engine)
    from sqlalchemy import text
    with engine.begin() as cx:
        # Migración: instalaciones anteriores no tienen access_id. Se añade la columna
        # y se genera un ID para cada local existente, sin perder ningún dato.
        if not _column_exists(cx, "tenants", "access_id"):
            cx.execute(text("ALTER TABLE tenants ADD COLUMN access_id VARCHAR(16)"))
        rows = cx.execute(text("SELECT id FROM tenants WHERE access_id IS NULL OR access_id = ''")).all()
        for (tid,) in rows:
            cx.execute(text("UPDATE tenants SET access_id = :a WHERE id = :i"),
                       {"a": new_access_id(), "i": tid})

        # Migración: importe de la venta en columna propia (ver records.amount).
        if not _column_exists(cx, "records", "amount"):
            cx.execute(text("ALTER TABLE records ADD COLUMN amount FLOAT"))
        _migrate_sale_amount(cx)

        _seed_leads(cx)


def _seed_leads(cx):
    """
    Importa una sola vez los leads que el fundador tenía en la app de captación
    (04-captacion-clientes, guardados en el localStorage del navegador).

    La marca en 'meta' es lo que garantiza el "una sola vez": si más adelante él
    borra un lead, no reaparece en el siguiente arranque del servidor.
    """
    from sqlalchemy import select as _select, insert as _insert
    from .seed_leads import SEED_LEADS
    already = cx.execute(_select(meta.c.value).where(meta.c.key == "leads_seeded")).first()
    if already:
        return
    ts = _utcnow()
    existing = {r[0] for r in cx.execute(_select(leads.c.id)).all()}
    for lead in SEED_LEADS:
        if lead["id"] in existing:
            continue
        cx.execute(_insert(leads).values(created_at=ts, updated_at=ts, **lead))
    cx.execute(_insert(meta).values(key="leads_seeded", value=_iso_now(ts)))


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _iso_now(ts) -> str:
    return ts.isoformat()


_ACCESS_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sin I, O, 0, 1 (se confunden)


def new_access_id() -> str:
    """ID corto de 6 caracteres, fácil de dictar por teléfono."""
    import secrets as _s
    return "".join(_s.choice(_ACCESS_ALPHABET) for _ in range(6))
