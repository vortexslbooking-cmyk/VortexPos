# Conectar vortexPOS (tablet) con vortexPOS Cloud

La app `vortexpos.html` funciona sola en local. Para sincronizar con el servidor
central se le añade este módulo. Es **aditivo**: si no hay internet o no está
configurado, la app sigue funcionando exactamente igual que ahora (offline-first).

## 1. Configurar la URL del servidor y la licencia

Desde el **panel técnico** del cliente se guardarían (en `ADMIN`, ya persistido):
`cloudUrl` (p. ej. `https://vortexpos-cloud.onrender.com`), `licenseKey` y el PIN
ya existente. La primera vez, la app llama a `/api/device/login`.

## 2. Módulo de sincronización (borrador para pegar en la app)

```js
/* ---- vortexPOS Cloud sync (offline-first) ---- */
const CLOUD = {
  url: () => (ADMIN.cloudUrl || "").replace(/\/$/, ""),
  token: null,
  async login() {
    if (!this.url() || !ADMIN.licenseKey) return false;
    try {
      const r = await fetch(this.url() + "/api/device/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ license_key: ADMIN.licenseKey, pin: ADMIN.pin })
      });
      if (!r.ok) return false;
      this.token = (await r.json()).token;
      return true;
    } catch (e) { return false; }   // sin internet: seguimos en local
  },
  async sync() {
    if (!this.token && !(await this.login())) return;
    // documentos reemplazables (carta, config) + registros nuevos (ventas/cierres)
    const body = {
      documents: {
        menu:   { json: S.menu,        updated_at: new Date().toISOString() },
        config: { json: { vat: ADMIN.business?.vat }, updated_at: new Date().toISOString() }
      },
      records: [
        ...S.sales.map(s => ({ kind: "sale",    record_id: s.id, json: s, created_at: new Date(s.paidAt).toISOString() })),
        ...(S.closures||[]).map(c => ({ kind: "closure", record_id: c.id, json: c, created_at: new Date(c.to).toISOString() }))
      ]
    };
    try {
      const r = await fetch(this.url() + "/api/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": "Bearer " + this.token },
        body: JSON.stringify(body)
      });
      if (r.status === 401) { this.token = null; return; }   // reintenta login luego
      if (r.status === 403) { toast("Licencia no activa — contacta con el proveedor", "bad"); return; }
      if (!r.ok) return;
      const data = await r.json();
      // Fusiona lo que devuelve el servidor (carta remota del proveedor, ventas de otras tablets)
      if (data.documents?.menu) S.menu = data.documents.menu.json;
      // (aquí se fusionan records por id para multi-dispositivo)
      persist();
    } catch (e) { /* sin conexión: se reintenta en el próximo ciclo */ }
  }
};

// sincroniza cada 30 s y al recuperar conexión
setInterval(() => CLOUD.sync(), 30000);
window.addEventListener("online", () => CLOUD.sync());
CLOUD.sync();
```

## 3. Endpoints que usa

| Acción | Método | Ruta |
|---|---|---|
| Login del dispositivo | `POST` | `/api/device/login` → `{ license_key, pin }` |
| Sincronizar | `POST` | `/api/sync` → `{ documents, records }` (con token) |

## Notas

- La **fusión de `records` por id** (para que una tablet vea las ventas de otra) es la
  parte que hay que rematar en el cliente; el servidor ya devuelve el conjunto
  autoritativo listo para fusionar.
- Nada de esto rompe el modo offline: si `cloudUrl` está vacío, el módulo no hace nada.
- Conviene añadir en el panel técnico los campos **URL del servidor** y **Licencia**.
