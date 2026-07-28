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

# 20) ID de acceso corto: se genera al crear el local
r = c.post("/api/provider/tenants", headers=PROV,
           json={"business_name":"Bar Activacion","plan":"Pro","pin":"5150"})
d = r.json()
AID = d.get("access_id")
ok("crear local devuelve access_id de 6 caracteres", bool(AID) and len(AID)==6)
ok("access_id sin caracteres ambiguos (I,O,0,1)", not set("IO01") & set(AID))

# 21) activación del dispositivo con ID + PIN (sin licencia)
r = c.post("/api/device/activate", json={"access_id":AID,"pin":"5150"})
ok("activar con ID+PIN correcto", r.status_code==200 and "token" in r.json())
ok("la activación devuelve la licencia", r.json().get("license_key","").startswith("VTX-"))
ACT = {"Authorization":"Bearer "+r.json()["token"]}

# 22) el token de activación sirve para sincronizar directamente
r = c.post("/api/sync", headers=ACT, json={})
ok("el token de activación permite sincronizar", r.status_code==200)

# 23) activación en minúsculas también funciona (se normaliza)
r = c.post("/api/device/activate", json={"access_id":AID.lower(),"pin":"5150"})
ok("ID de acceso admite minúsculas", r.status_code==200)

# 24) credenciales incorrectas y licencia suspendida
r = c.post("/api/device/activate", json={"access_id":AID,"pin":"0000"})
ok("activar con PIN incorrecto -> 401", r.status_code==401)
r = c.post("/api/device/activate", json={"access_id":"ZZZZZZ","pin":"5150"})
ok("activar con ID inexistente -> 401", r.status_code==401)

TID2 = d["id"]
c.patch(f"/api/provider/tenants/{TID2}", headers=PROV, json={"status":"Suspendido"})
r = c.post("/api/device/activate", json={"access_id":AID,"pin":"5150"})
ok("activar con licencia suspendida -> 403", r.status_code==403)
c.patch(f"/api/provider/tenants/{TID2}", headers=PROV, json={"status":"Activo"})

# 25) el panel del proveedor expone el access_id para poder entregarlo
r = c.get("/api/provider/tenants", headers=PROV)
ok("el listado del proveedor incluye access_id",
   all(t.get("access_id") for t in r.json()["tenants"]))

# 26) copia de seguridad: descarga completa
r = c.get("/api/provider/backup", headers=PROV)
ok("copia de seguridad requiere proveedor", c.get("/api/provider/backup").status_code==401)
ok("copia de seguridad responde", r.status_code==200)
DUMP = r.json()
ok("la copia lleva locales, documentos y registros",
   DUMP["counts"]["tenants"]>0 and DUMP["counts"]["documents"]>0 and DUMP["counts"]["records"]>0)
ok("la copia incluye el hash del PIN (imprescindible para restaurar)",
   all(t.get("pin_hash") for t in DUMP["tenants"]))

# 27) restaurar sobre la BD actual no duplica ni pierde nada
antes = c.get("/api/provider/tenants", headers=PROV).json()["tenants"]
r = c.post("/api/provider/restore", headers=PROV, json=DUMP)
ok("restaurar sobre datos existentes -> ok", r.status_code==200)
ok("restaurar no duplica nada", all(v==0 for v in r.json()["added"].values()))
despues = c.get("/api/provider/tenants", headers=PROV).json()["tenants"]
ok("los totales de ventas se mantienen tras restaurar",
   [t["sales_total"] for t in antes]==[t["sales_total"] for t in despues])

# 28) formato desconocido se rechaza en vez de corromper la base de datos
r = c.post("/api/provider/restore", headers=PROV, json={"vortexpos_backup":99,"tenants":[]})
ok("copia de formato desconocido -> 400", r.status_code==400)

# ---------------------------------------------------------------------------
# CENTRO DE MANDO DEL PROVEEDOR: leads, fallos y estadísticas
# ---------------------------------------------------------------------------

# 29) leads: los de la app de captación se importan solos al crear la base
r = c.get("/api/provider/leads")
ok("los leads exigen token de proveedor -> 401", r.status_code==401)
r = c.get("/api/provider/leads", headers=PROV)
LEADS0 = r.json()["leads"]
ok("los leads de captación se importaron al arrancar", len(LEADS0)==14)
ok("un lead conocido llegó con su teléfono",
   any(l["business_name"].startswith("Cafe-Bar Ocana") and "640" in l["phone"] for l in LEADS0))
ok("los leads importados entran en la línea pos y estado nuevo",
   all(l["line"]=="pos" and l["status"]=="nuevo" for l in LEADS0))

# 30) alta, validación y edición de un lead
r = c.post("/api/provider/leads", headers=PROV, json={"business_name":""})
ok("lead sin nombre -> 400", r.status_code==400)
r = c.post("/api/provider/leads", headers=PROV,
           json={"business_name":"Bar Pruebas","line":"inventada"})
ok("lead con línea inexistente -> 400", r.status_code==400)
r = c.post("/api/provider/leads", headers=PROV,
           json={"business_name":"Bar Pruebas","next_date":"15/08/2026"})
ok("lead con fecha en formato erróneo -> 400", r.status_code==400)
r = c.post("/api/provider/leads", headers=PROV, json={
    "business_name":"Hotel Marina","line":"agencia","priority":"alta",
    "zone":"Torre del Mar","phone":"+34 600 11 22 33","next_action":"Llamar al dueño",
    "next_date":"2020-01-01"})
ok("crear lead -> ok", r.status_code==200)
LEAD = r.json()["lead"]
ok("el lead nuevo guarda línea, prioridad y próxima acción",
   LEAD["line"]=="agencia" and LEAD["priority"]=="alta" and LEAD["next_action"]=="Llamar al dueño")

r = c.get("/api/provider/leads?line=agencia", headers=PROV)
ok("filtro por línea", len(r.json()["leads"])==1 and r.json()["leads"][0]["id"]==LEAD["id"])
r = c.get("/api/provider/leads?status=nuevo", headers=PROV)
ok("filtro por estado", len(r.json()["leads"])==15)
r = c.get("/api/provider/leads?q=torre", headers=PROV)
ok("búsqueda por texto (zona)", len(r.json()["leads"])>=2)

r = c.patch("/api/provider/leads/"+LEAD["id"], headers=PROV,
            json={"status":"interesado","next_date":"2026-09-01"})
ok("editar lead: estado y próxima fecha",
   r.status_code==200 and r.json()["lead"]["status"]=="interesado"
   and r.json()["lead"]["next_date"]=="2026-09-01")
r = c.patch("/api/provider/leads/"+LEAD["id"], headers=PROV, json={"status":"zombi"})
ok("editar lead con estado inexistente -> 400", r.status_code==400)
r = c.patch("/api/provider/leads/no-existe", headers=PROV, json={"status":"cliente"})
ok("editar lead inexistente -> 404", r.status_code==404)

r = c.post("/api/provider/leads", headers=PROV, json={"business_name":"Borrar esto"})
BORRABLE = r.json()["lead"]["id"]
ok("borrar lead", c.delete("/api/provider/leads/"+BORRABLE, headers=PROV).status_code==200)
ok("borrar lead inexistente -> 404",
   c.delete("/api/provider/leads/"+BORRABLE, headers=PROV).status_code==404)

# 31) fallos: la app los reporta sola por un endpoint público y saneado
r = c.post("/api/errors", json={"type":"sync","message":"No se pudo sincronizar",
                                "app_version":"2.0.0","context":{"intentos":3}})
ok("reportar un fallo no requiere token", r.status_code==200 and r.json()["ok"])
r = c.post("/api/errors", json={"type":"sync","message":"No se pudo sincronizar",
                                "app_version":"2.0.0","context":{"intentos":4}})
ok("el mismo fallo repetido no crea otra fila", r.status_code==200)

r = c.get("/api/provider/errors")
ok("ver los fallos exige token de proveedor -> 401", r.status_code==401)
r = c.get("/api/provider/errors", headers=PROV)
ERRS = r.json()["errors"]
ok("el fallo llega al panel una sola vez", len(ERRS)==1)
ok("el fallo repetido cuenta las veces que ha pasado", ERRS[0]["hits"]==2)

# datos personales de clientes finales: nunca deben quedar guardados
r = c.post("/api/errors", json={"type":"pago","message":
    "Fallo al cobrar a ana.perez@gmail.com tel 611223344 tarjeta 4539112233445566"})
ok("reporte con datos personales -> ok", r.status_code==200)
sucio = [e for e in c.get("/api/provider/errors", headers=PROV).json()["errors"]
         if e["type"]=="pago"][0]
ok("el email del cliente no se guarda", "ana.perez@gmail.com" not in sucio["message"])
ok("el teléfono del cliente no se guarda", "611223344" not in sucio["message"])
ok("el número de tarjeta no se guarda", "4539112233445566" not in sucio["message"])

# atribución del local: solo se cree la que viene de un token de dispositivo
r = c.post("/api/errors", json={"type":"impresora","message":"Sin papel",
                                "tenant_id":"t_inventado"})
ok("un tenant_id inventado se ignora", r.status_code==200)
imp = [e for e in c.get("/api/provider/errors", headers=PROV).json()["errors"]
       if e["type"]=="impresora"][0]
ok("el fallo queda sin local si el tenant_id no existe", imp["tenant_id"] is None)
r = c.post("/api/errors", headers=DEV1, json={"type":"caja","message":"Cajón atascado"})
caja = [e for e in c.get("/api/provider/errors", headers=PROV).json()["errors"]
        if e["type"]=="caja"][0]
ok("con token del dispositivo, el fallo se atribuye a su local", caja["tenant_id"]==t["id"])
ok("el panel muestra el nombre del local que falló", caja["business_name"]=="Bar El Rincón")

# resolver un fallo y filtrar
r = c.patch("/api/provider/errors/"+caja["id"], headers=PROV, json={"resolved":True})
ok("marcar un fallo como resuelto", r.status_code==200)
ok("filtro sin resolver", all(not e["resolved"] for e in
   c.get("/api/provider/errors?resolved=false", headers=PROV).json()["errors"]))
ok("filtro resueltos",
   len(c.get("/api/provider/errors?resolved=true", headers=PROV).json()["errors"])==1)
ok("resolver un fallo inexistente -> 404",
   c.patch("/api/provider/errors/no-existe", headers=PROV, json={"resolved":True}).status_code==404)

# el endpoint público está limitado: nadie puede inundar la base de datos
for i in range(40):
    last = c.post("/api/errors", json={"type":"spam","message":f"ruido {i}"})
ok("el reporte público está limitado por IP -> 429", last.status_code==429)
from app import provider_api as _pa
_pa._ERR_HITS.clear()   # se libera el freno para el resto de la prueba

# 32) estadísticas globales del negocio
r = c.get("/api/provider/stats")
ok("las estadísticas exigen token de proveedor -> 401", r.status_code==401)
S = c.get("/api/provider/stats", headers=PROV).json()
tenants_all = c.get("/api/provider/tenants", headers=PROV).json()["tenants"]
ok("estadísticas: nº de locales", S["tenants"]["total"]==len(tenants_all))
ok("estadísticas: locales por estado",
   S["tenants"]["by_status"].get("Activo")==sum(1 for x in tenants_all if x["status"]=="Activo"))
ok("estadísticas: ventas totales de todo el negocio",
   abs(S["sales"]["total"]-sum(x["sales_total"] for x in tenants_all))<0.001)
ok("estadísticas: ticket medio",
   abs(S["sales"]["avg_ticket"]-round(S["sales"]["total"]/S["sales"]["count"],2))<0.011)
ok("estadísticas: leads por estado", sum(S["leads"]["by_status"].values())==15)
ok("estadísticas: leads por línea", S["leads"]["by_line"].get("agencia")==1)
ok("estadísticas: fallos sin resolver", S["errors"]["unresolved"]>=2)
ok("estadísticas: el resumen trae los últimos fallos", len(S["errors"]["recent"])>0)
ok("estadísticas: identifica el motor de base de datos", S["system"]["database"]=="sqlite")

# un lead con la fecha pasada aparece como pendiente de acción
r = c.post("/api/provider/leads", headers=PROV,
           json={"business_name":"Vence ayer","next_date":"2020-05-05",
                 "next_action":"Enviar propuesta"})
VENCIDO = r.json()["lead"]["id"]
S2 = c.get("/api/provider/stats", headers=PROV).json()
ok("un lead con fecha pasada requiere acción", S2["leads"]["due"]>=1 and
   any(l["id"]==VENCIDO for l in S2["leads"]["due_list"]))
c.patch("/api/provider/leads/"+VENCIDO, headers=PROV, json={"status":"cliente"})
S3 = c.get("/api/provider/stats", headers=PROV).json()
ok("un lead ya cerrado deja de pedir acción",
   not any(l["id"]==VENCIDO for l in S3["leads"]["due_list"]))

# 33) el panel es una sola página con todas las secciones
r = c.get("/")
ok("el panel trae la navegación por secciones",
   all(s in r.text for s in ("Resumen","Locales","Leads","Fallos","Ajustes")))

# 34) recuperación real: base de datos vacía restaurada desde cero
# (se vuelve a tomar la copia para que incluya los leads recién creados)
DUMP = c.get("/api/provider/backup", headers=PROV).json()
ok("la copia de seguridad incluye los leads", DUMP["counts"]["leads"]==16)
import tempfile as _tf, importlib, sys as _sys
_tmp2 = _tf.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_URL"] = "sqlite:///" + _tmp2.name
# Se recargan todos a la vez: si backup_api o provider_api se quedasen cargados
# seguirían apuntando al engine de la base anterior y la restauración (o las
# estadísticas) irían al sitio erróneo.
for _m in ("app.main", "app.backup_api", "app.provider_api", "app.db"):
    _sys.modules.pop(_m, None)
from app.main import app as app2
c2 = TestClient(app2)
tok2 = c2.post("/api/provider/login",
               json={"email":"admin@vortexpos.local","password":"vortex-admin"}).json()["token"]
PROV2 = {"Authorization":"Bearer "+tok2}
ok("la base de datos nueva empieza vacía",
   c2.get("/api/provider/tenants", headers=PROV2).json()["tenants"]==[])
r = c2.post("/api/provider/restore", headers=PROV2, json=DUMP)
ok("restaurar en base de datos vacía -> ok", r.status_code==200)
ok("se recuperan todos los locales",
   r.json()["added"]["tenants"]==DUMP["counts"]["tenants"])
ok("se recupera todo el histórico",
   r.json()["added"]["records"]==DUMP["counts"]["records"])
rec = c2.get("/api/provider/tenants", headers=PROV2).json()["tenants"]
ok("las ventas recuperadas cuadran con las originales",
   sorted(t["sales_total"] for t in rec)==sorted(t["sales_total"] for t in antes))
leads2 = c2.get("/api/provider/leads", headers=PROV2).json()["leads"]
ok("el trabajo comercial (leads) se recupera entero", len(leads2)==DUMP["counts"]["leads"])
ok("los leads se recuperan con su estado y su próxima acción",
   any(l["business_name"]=="Hotel Marina" and l["status"]=="interesado"
       and l["next_action"]=="Llamar al dueño" for l in leads2))
ok("las estadísticas de la base recuperada cuadran con las originales",
   abs(c2.get("/api/provider/stats", headers=PROV2).json()["sales"]["total"]
       - sum(t["sales_total"] for t in antes)) < 0.001)

# 30) tras restaurar, el cliente entra con su MISMO ID y PIN de siempre
r = c2.post("/api/device/activate", json={"access_id":AID,"pin":"5150"})
ok("el cliente entra con su ID y PIN tras la recuperación", r.status_code==200)

passed = sum(1 for c_,_ in checks if c_)
print(f"\n{passed}/{len(checks)} comprobaciones superadas")
if passed != len(checks):
    raise SystemExit(1)
