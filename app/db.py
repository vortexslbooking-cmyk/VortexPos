"""
Capa de datos de vortexPOS Cloud.

Un único almacén multi-inquilino (multi-tenant): todos los locales comparten
las mismas tablas, pero cada fila lleva su tenant_id y las consultas SIEMPRE
filtran por él, de modo que un local nunca ve datos de otro.

- tenants   : un registro por local/licencia (negocio, plan, estado, PIN).
- documents : datos reemplazables por local (carta, config, mesas). Last-Write-Wins.
- records   : datos append-only e idempotentes (ventas, cierres, movimientos de caja).
              Nunca se pierden ni se duplican: clave (tenant_id, kind, record_id).

Portabilidad: se usa DATABASE_URL. Por defecto SQLite (desarrollo); en producción
se pone una URL de Postgres y el mismo código funciona sin cambios.
"""
import os
from sqlalchemy import (create_engine, MetaData, Table, Column, String, Text,
                        Float, DateTime, UniqueConstraint, func)

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
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("tenant_id", "kind", "record_id", name="uq_rec"),
)


def init_db():
    metadata.create_all(engine)
