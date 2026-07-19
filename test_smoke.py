"""
Prueba de extremo a extremo del servidor vortexPOS Cloud.
Simula un dispositivo real de un local y verifica el ciclo completo:
proveedor crea licencia -> dispositivo inicia sesión -> sincroniza ventas ->
segundo dispositivo del mismo local recibe esas ventas -> proveedor ve el resumen ->
suspender la licencia bloquea la sincronización.

Ejecutar:  python -m pytest test_smoke.py -q      (o)   python test_smoke.py
"""
import os, tempfile

# BD temporal aislada para la prueba
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_URL"] = "sqlite:///" + _tmp.name
os.environ["PROVIDER_EMAIL"] = "admin@vortexpos.local"
os.environ["PROVIDER_PASSWORD"] = "vortex-admin"

from fastapi.testclient import TestClient
from app.main import app

c = TestClient(app)
checks = []
def ok(name, cond):
    checks.append((cond, name))
    print(("PASS" if cond else "FAIL"), "—", name)

# 1) salud
r = c.get("/health"); ok("health responde", r.status_code==200 and r.json()["ok"])

# 2) panel se sirve
r = c.get("/"); ok("panel de proveedor se sirve", r.status_code==200 and "vortexPOS Cloud" in r.text)

# 3) login proveedor incorrecto rechazado
r = c.post("/api/provider/login", json={"email":"x","password":"y"})
ok("login proveedor incorrecto -> 401", r.status_code==401)

# 4) login proveedor correcto
r = c.post("/api/provider/login", json={"email":"admin@vortexpos.local","password":"vortex-admin"})
ok("login proveedor correcto", r.status_code==200 and "token" in r.json())
PROV = {"Authorization":"Bearer "+r.json()["token"]}

# 5) crear local
r = c.post("/api/provider/tenants", headers=PROV,
           json={"business_name":"Bar El Rincón","plan":"Pro","pin":"4821"})
ok("crear licencia", r.status_code==200 and r.json()["license_key"].startswith("VTX-"))
LIC = r.json()["license_key"]

# 6) device login con PIN incorrecto
r = c.post("/api/device/login", json={"license_key":LIC,"pin":"0000"})
ok("device PIN incorrecto -> 401", r.status_code==401)

# 7) device login correcto
r = c.post("/api/device/login", json={"license_key":LIC,"pin":"4821"})
ok("device login correcto", r.status_code==200 and "token" in r.json())
DEV1 = {"Authorization":"Bearer "+r.json()["token"]}

# 8) tablet 1 sincroniza: carta + 2 ventas
sync1 = {
  "documents":{"menu":{"json":[{"cat":"Bebidas","items":[{"id":"b1","name":"Caña","price":2.5}]}],
                       "updated_at":"2026-07-15T10:00:00+00:00"}},
  "records":[
    {"kind":"sale","record_id":"s1","json":{"total":12.5,"method":"efectivo",
       "items":[{"name":"Caña","price":2.5,"qty":5}]},"created_at":"2026-07-15T11:00:00+00:00"},
    {"kind":"sale","record_id":"s2","json":{"total":7.6,"method":"tarjeta",
       "items":[{"name":"Copa de vino","price":3.8,"qty":2}]},"created_at":"2026-07-15T11:05:00+00:00"},
  ]}
sync1["pull_records"]=True
r = c.post("/api/sync", headers=DEV1, json=sync1)
ok("tablet 1 sincroniza", r.status_code==200 and len(r.json()["records"])==2)

# 9) reenvío idempotente (misma venta s1 otra vez) NO duplica
r = c.post("/api/sync", headers=DEV1, json={"records":[sync1["records"][0]],"pull_records":True})
ok("venta duplicada no se duplica (idempotencia)", len(r.json()["records"])==2)

# 10) tablet 2 del mismo local recibe las ventas de la tablet 1
r = c.post("/api/device/login", json={"license_key":LIC,"pin":"4821"})
DEV2 = {"Authorization":"Bearer "+r.json()["token"]}
r = c.post("/api/sync", headers=DEV2, json={"pull_records":True})  # pull puro
recs = r.json()["records"]
ok("tablet 2 recibe las 2 ventas (multi-dispositivo)", len(recs)==2)
ok("tablet 2 recibe la carta (documento)", "menu" in r.json()["documents"])

# 11) tablet 2 añade una venta y la tablet 1 la ve
c.post("/api/sync", headers=DEV2, json={"records":[
    {"kind":"sale","record_id":"s3","json":{"total":5.0,"method":"efectivo",
       "items":[{"name":"Café","price":2.5,"qty":2}]},"created_at":"2026-07-15T11:10:00+00:00"}]})
r = c.post("/api/sync", headers=DEV1, json={"pull_records":True})
ok("tablet 1 ve la venta de la tablet 2", len(r.json()["records"])==3)

# 12) proveedor ve el resumen agregado
r = c.get("/api/provider/tenants", headers=PROV)
t = r.json()["tenants"][0]
ok("resumen: 3 ventas", t["sales_count"]==3)
ok("resumen: total 25,10 €", abs(t["sales_total"]-25.10)<0.001)

r = c.get(f"/api/provider/tenants/{t['id']}/summary", headers=PROV)
sm = r.json()
ok("desglose por método de pago", sm["by_method"].get("efectivo")==17.5 and sm["by_method"].get("tarjeta")==7.6)
ok("producto más vendido = Caña", sm["top_products"][0]["name"]=="Caña")

# 13) suspender licencia bloquea la sincronización
c.patch(f"/api/provider/tenants/{t['id']}", headers=PROV, json={"status":"Suspendido"})
r = c.post("/api/sync", headers=DEV1, json={})
ok("licencia suspendida bloquea sync -> 403", r.status_code==403)
r = c.post("/api/device/login", json={"license_key":LIC,"pin":"4821"})
ok("licencia suspendida bloquea login -> 403", r.status_code==403)

# 14) reactivar restaura el servicio
c.patch(f"/api/provider/tenants/{t['id']}", headers=PROV, json={"status":"Activo"})
r = c.post("/api/device/login", json={"license_key":LIC,"pin":"4821"})
ok("reactivar restaura el acceso", r.status_code==200)

# 15) aislamiento entre locales: un segundo local no ve datos del primero
r = c.post("/api/provider/tenants", headers=PROV,
           json={"business_name":"Chiringuito Ola","plan":"Básico","pin":"1111"})
LIC2 = r.json()["license_key"]
r = c.post("/api/device/login", json={"license_key":LIC2,"pin":"1111"})
DEV_B = {"Authorization":"Bearer "+r.json()["token"]}
r = c.post("/api/sync", headers=DEV_B, json={"pull_records":True})
ok("aislamiento multi-inquilino: local 2 no ve ventas del local 1", len(r.json()["records"])==0)

# 16) el proveedor edita la carta en remoto (añadir/retirar) y el dispositivo la recibe
r = c.put(f"/api/provider/tenants/{t['id']}/menu", headers=PROV,
          json={"json":[{"cat":"Cócteles","station":"barra",
                         "items":[{"name":"Mojito Cloud","price":9.5},{"name":"Retirado","price":1,"off":True}]}]})
ok("proveedor guarda carta remota", r.status_code==200 and r.json()["ok"] and r.json()["categorias"]==1)

r = c.get(f"/api/provider/tenants/{t['id']}/menu", headers=PROV)
m = r.json()["menu"]
ok("GET carta devuelve lo guardado (con id asignado)",
   m[0]["items"][0]["name"]=="Mojito Cloud" and "id" in m[0]["items"][0] and m[0]["items"][1].get("off") is True)

r = c.post("/api/device/login", json={"license_key":LIC,"pin":"4821"})
DEV3 = {"Authorization":"Bearer "+r.json()["token"]}
r = c.post("/api/sync", headers=DEV3, json={})
docs = r.json()["documents"]
ok("dispositivo recibe la carta del proveedor",
   any(cat.get("cat")=="Cócteles" for cat in docs.get("menu",{}).get("json",[])))

# 17) LWW: un push del dispositivo con carta ANTIGUA no pisa la del proveedor
r = c.post("/api/sync", headers=DEV3, json={"documents":{"menu":{
    "json":[{"cat":"Vieja","station":"cocina","items":[]}],
    "updated_at":"2020-01-01T00:00:00+00:00"}}})
ok("carta antigua del dispositivo no pisa la del proveedor (LWW)",
   any(cat.get("cat")=="Cócteles" for cat in r.json()["documents"]["menu"]["json"]))


# 17b) PWA: la app se sirve instalable desde /app/
r = c.get("/app/")
ok("PWA: /app/ sirve la app vortexPOS", r.status_code==200 and "vortexPOS" in r.text and "serviceWorker" in r.text)
r = c.get("/app/manifest.webmanifest")
ok("PWA: manifest válido", r.status_code==200 and r.json()["short_name"]=="vortexPOS" and len(r.json()["icons"])==3)
r = c.get("/app/sw.js")
ok("PWA: service worker servido como JS", r.status_code==200 and "javascript" in r.headers["content-type"] and "/api/" in r.text)
r = c.get("/app/icon-512.png")
ok("PWA: icono 512 disponible", r.status_code==200 and r.headers["content-type"]=="image/png")
r = c.get("/app/../etc/passwd")
ok("PWA: rutas fuera de la lista blanca -> 404", r.status_code==404)

# 18) por defecto la respuesta del sync es ligera (sin histórico)
r = c.post("/api/sync", headers=DEV3, json={})
ok("sync por defecto no arrastra el histórico", r.status_code==200 and r.json()["records"]==[])

# 19) la API rechaza PINs no tecleables en el pad (defensa en profundidad)
r = c.post("/api/provider/tenants", headers=PROV,
           json={"business_name":"Mal PIN","plan":"Pro","pin":"abc123"})
ok("crear licencia con PIN no numérico -> 400", r.status_code==400)
r = c.patch(f"/api/provider/tenants/{t['id']}", headers=PROV, json={"pin":"12"})
ok("cambiar a PIN demasiado corto -> 400", r.status_code==400)

passed = sum(1 for c_,_ in checks if c_)
print(f"\n{passed}/{len(checks)} comprobaciones superadas")
if passed != len(checks):
    raise SystemExit(1)
