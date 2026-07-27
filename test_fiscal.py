"""
test_fiscal.py — Auditoría de cumplimiento fiscal (RD 1007/2023 / VeriFactu).

NO comprueba que la app "funcione": comprueba si los registros de venta son
FISCALMENTE VÁLIDOS según el Reglamento aprobado por el RD 1007/2023 y la
Orden HAC/1177/2024.

Cada comprobación se llama FALTA-xx y pasa (PASS) cuando el requisito ESTÁ
implementado. Un FAIL = requisito legal ausente en el producto.

Ejecutar:  python3 test_fiscal.py
"""
import os, re, tempfile, json, pathlib

RAIZ = pathlib.Path(__file__).resolve().parent.parent
APP_HTML = (RAIZ / "01-app-pos" / "vortexpos.html").read_text(encoding="utf-8")
SRV = "\n".join((RAIZ / "02-servidor-cloud" / "app" / f).read_text(encoding="utf-8")
                for f in os.listdir(RAIZ / "02-servidor-cloud" / "app") if f.endswith(".py"))
TODO = APP_HTML + "\n" + SRV

checks = []
def ok(name, cond, detalle=""):
    checks.append((bool(cond), name))
    print(("PASS" if cond else "FAIL"), "—", name, ("· " + detalle) if detalle and not cond else "")

print("== PARTE A · requisitos del registro de facturación (estático) ==")

# Art. 10.1.d) RD 1007/2023: número y, en su caso, serie de la factura.
# Art. 7.1.a) RD 1619/2012: numeración correlativa dentro de cada serie.
ok("FALTA-01 · numeración correlativa de facturas/tickets",
   bool(re.search(r"(invoiceNumber|numFactura|facturaNum|serie\s*[:=]|correlativ)", TODO)),
   "sale() usa id:uid() = Math.random(), no hay contador correlativo")

# Art. 12 RD 1007/2023 + anexo Orden HAC/1177/2024 (SHA-256): huella o hash.
# (se excluye security.py: ahí SHA-256 se usa para el PIN, no para facturación)
ok("FALTA-02 · huella/hash SHA-256 del registro de facturación",
   bool(re.search(r"(sha-?256|subtle\.digest)", APP_HTML, re.I))
   or bool(re.search(r"hashlib\.sha256\([^)]*(sale|record|factur)", SRV, re.I)),
   "no hay ninguna huella sobre los datos de venta (el único SHA-256 del "
   "repo está en security.py y sirve para el PIN del cliente)")

# Art. 8.2.b) + art. 10.1.ñ): encadenamiento con el registro anterior.
ok("FALTA-03 · encadenamiento con la huella del registro anterior",
   bool(re.search(r"(prevHash|huellaAnterior|previous_hash|chain)", TODO, re.I)),
   "cada venta es un objeto independiente, sin referencia a la anterior")

# Art. 12 RD 1007/2023: firma electrónica (salvo modalidad VERI*FACTU, art. 16.3).
ok("FALTA-04 · firma electrónica de los registros (o modo VERI*FACTU)",
   bool(re.search(r"(xades|firma electr|certificado digital|sign\(|signature)", TODO, re.I)),
   "no hay firma ni remisión a AEAT que la sustituya")

# Art. 20 y 21 Orden HAC/1177/2024: código QR en la factura, incluida la simplificada.
ok("FALTA-05 · código QR en el ticket",
   bool(re.search(r"(qrcode|qr_code|generarQR|\bQR\b)", TODO)),
   "ticketReceiptHTML() no imprime QR")

# Art. 20.1.b) Orden HAC/1177/2024: leyenda VERI*FACTU cuando se remite a AEAT.
ok("FALTA-06 · leyenda «VERI*FACTU» / «Factura verificable…»",
   "VERI*FACTU" in TODO or "verificable en la sede" in TODO.lower())

# Art. 10.1.a): NIF y razón social del expedidor como CAMPO del registro.
ok("FALTA-07 · NIF del emisor como campo estructurado",
   bool(re.search(r"\b(nif|cif)\s*[:=]", TODO, re.I)),
   "sólo existe ticketHeader, un texto libre opcional")

# Art. 11: registro de facturación de anulación.
ok("FALTA-08 · registro de anulación (no borrado) de una venta",
   bool(re.search(r"(anulacion|anulación|registro de anulaci)", TODO, re.I)),
   "no existe función de anulación ni de devolución")

# Art. 8.3: registro de eventos del sistema.
ok("FALTA-09 · registro de eventos del sistema informático",
   bool(re.search(r"(registro de eventos|eventLog|event_log|auditLog)", TODO, re.I)),
   "ERRLOG guarda 10 errores en memoria y se pierde al recargar")

# Art. 8.1 in fine + art. 16: capacidad de remitir a la AEAT.
ok("FALTA-10 · capacidad de remisión a la AEAT",
   bool(re.search(r"(agenciatributaria|aeat)", TODO, re.I)),
   "la única nube configurada es vortexpos-cloud.onrender.com")

# Art. 13: declaración responsable visible en el propio sistema.
ok("FALTA-11 · declaración responsable del productor visible en la app",
   bool(re.search(r"declaraci[oó]n responsable", TODO, re.I)))

# Art. 10.1.o): identificación del sistema informático y de su productor en el registro.
ok("FALTA-12 · identificación del sistema y del productor en cada registro",
   bool(re.search(r"(sistemaInformatico|IDSistemaInformatico|idSif)", TODO)))

print()
print("== PARTE B · inalterabilidad efectiva (dinámico, art. 8.2.a) ==")

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_URL"] = "sqlite:///" + _tmp.name
os.environ["PROVIDER_EMAIL"] = "audit@vortexpos.local"
os.environ["PROVIDER_PASSWORD"] = "audit-pass"
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)

tok = c.post("/api/provider/login",
             json={"email": "audit@vortexpos.local", "password": "audit-pass"}).json()["token"]
PROV = {"Authorization": "Bearer " + tok}
t = c.post("/api/provider/tenants", headers=PROV,
           json={"business_name": "Bar Auditoría", "plan": "Pro", "pin": "1111"}).json()
DEV = {"Authorization": "Bearer " + c.post("/api/device/login",
       json={"license_key": t["license_key"], "pin": "1111"}).json()["token"]}

def push(records):
    return c.post("/api/sync", headers=DEV, json={"documents": {}, "records": records})

venta = {"kind": "sale", "record_id": "abc1234",
         "json": {"id": "abc1234", "tableId": "M1", "method": "efectivo",
                  "paidAt": 1753200000000, "total": 100.0,
                  "items": [{"name": "Menú", "qty": 1, "price": 100.0}]},
         "created_at": "2026-07-22T20:00:00+00:00"}
push([venta])

# B1 · intento de MODIFICAR una venta ya enviada (mismo record_id, otro importe)
venta_mod = json.loads(json.dumps(venta))
venta_mod["json"]["total"] = 5.0
r = push([venta_mod])
srv = c.get(f"/api/provider/tenants/{t['id']}/summary", headers=PROV).json()
ok("El servidor rechaza o AVISA de un intento de modificar una venta enviada",
   r.status_code != 200 or "conflict" in r.text.lower() or "alterad" in r.text.lower(),
   f"devuelve {r.status_code} OK sin avisar; el total del servidor sigue en "
   f"{srv.get('sales_total')} € mientras la caja del bar muestra 5,00 €")

# B2 · venta que nunca se envía (borrada del localStorage antes de sincronizar)
push([])  # el dispositivo simplemente no la incluye
srv2 = c.get(f"/api/provider/tenants/{t['id']}/summary", headers=PROV).json()
ok("El sistema detecta la OMISIÓN de una venta (hueco en la secuencia)",
   False,
   "sin numeración correlativa ni encadenamiento no hay forma de saber que falta un registro")

# B3 · el propio dato local: ¿hay verificación de integridad al cargar?
ok("La app verifica la integridad de los datos al cargarlos de localStorage",
   bool(re.search(r"(verifyHash|checkIntegrity|comprobarHuella)", APP_HTML)),
   "load() sólo valida que S.tables sea un array (línea ~565)")

# B4 · importar copia de seguridad sustituye el histórico completo
ok("Restaurar una copia no permite sustituir el histórico de ventas ya cerrado",
   not bool(re.search(r"function importBackup[\s\S]{0,600}S\s*=\s*data", APP_HTML)),
   "importBackup() hace S = data: reemplaza ventas y cierres Z sin dejar rastro")

print()
print("== PARTE C · IVA (art. 10.1.m: base, tipo y cuota por cada tipo) ==")

# C1 · ¿se guarda el tipo de IVA aplicado dentro de la venta o del cierre Z?
ok("El tipo de IVA queda congelado en la venta / en el cierre Z",
   bool(re.search(r"function sale\([\s\S]{0,400}vat", APP_HTML)),
   "sale() no guarda vat; salesSummary() devuelve {total,tickets,items,byMethod,"
   "byProduct,avg} sin IVA. Base y cuota se recalculan al vuelo con "
   "ADMIN.business.vat (líneas 1047, 1148, 1545): cambiar el tipo hoy reescribe "
   "el IVA de TODOS los cierres Z pasados")

# C2 · ¿admite varios tipos de IVA a la vez (10% consumo en local / 21% ciertos productos)?
ok("Soporta más de un tipo de IVA (p. ej. 10 % en sala y 21 % para llevar)",
   bool(re.search(r"(item|prod)\w*\.vat|vatRate|tipoIva", APP_HTML)),
   "ADMIN.business.vat es un único número global aplicado a toda la carta, "
   "aunque la app crea mesas de la zona 'Para llevar' (línea 586)")

# C3 · el ticket entregado al cliente, ¿lleva número de factura?
recibo = re.search(r"function ticketReceiptHTML\([\s\S]{0,1400}?\n\}", APP_HTML).group(0)
ok("El ticket entregado al cliente lleva número (art. 7.1.a RD 1619/2012)",
   bool(re.search(r"(n[ºu]m|invoice|factura\s*n)", recibo, re.I)),
   "ticketReceiptHTML() imprime nombre, fecha, líneas, base, IVA y total. "
   "Sin número, sin serie y sin NIF salvo que el dueño lo escriba a mano "
   "en el campo libre 'Cabecera del ticket'")

print()
buenas = sum(1 for c_, _ in checks if c_)
print(f"{buenas}/{len(checks)} requisitos fiscales cubiertos")
faltan = [n for c_, n in checks if not c_]
if faltan:
    print("\nREQUISITOS AUSENTES:")
    for n in faltan:
        print("  ·", n)
