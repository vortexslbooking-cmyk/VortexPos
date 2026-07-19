# vortexPOS Cloud — servidor central

Backend multi-inquilino para la **Fase 2** de vortexPOS: cuentas por local,
sincronización offline-first y **panel de proveedor** para controlar todos los
locales desde un solo sitio.

> **Estado:** núcleo de la Fase 2a **construido y probado** (20/20 pruebas de extremo a
> extremo). Lo único que no puede hacerse automáticamente es *ponerlo vivo en internet*:
> eso necesita **tu** cuenta de nube y **tu** método de pago. Abajo tienes el despliegue
> en unos minutos.

---

## Qué hace

- **Panel de proveedor** (`/`) — tablero web con todos tus locales: estado, plan,
  ventas acumuladas y última conexión. Crear licencias, ver ventas en vivo y
  **suspender/activar** por impago con un clic.
- **Carta remota por cliente** — desde el detalle de cada local, botón **“Editar carta”**:
  añadir, retirar, agotar o cambiar precios de productos y categorías. Las tablets del
  cliente reciben la carta nueva en su siguiente sincronización (menos de 1 minuto).
- **Historial de cierres Z** con arqueo de efectivo y descuadre por cierre, y KPI de
  descuadre acumulado por local.
- **API de sincronización** (`/api/sync`) — cada tablet guarda en local (sigue
  funcionando sin internet) y sincroniza cuando hay conexión.
- **Multi-dispositivo** — varias tablets del mismo local comparten la misma caja.
- **Aislamiento total** — un local nunca ve los datos de otro (multi-inquilino por `tenant_id`).
- **Ventas a prueba de duplicados** — cada venta/cierre es idempotente: nunca se pierde ni se duplica.

## La app instalable (PWA) — `/app/`

Este servidor también **sirve la propia app vortexPOS como aplicación instalable**:

1. El cliente abre `https://TU-SERVIDOR/app/` en Chrome (Android) o Edge/Chrome (Windows).
2. El navegador ofrece **“Instalar aplicación”** (o menú ⋮ → *Instalar vortexPOS*).
3. Queda con **icono propio, pantalla completa y funcionamiento 100% sin conexión**
   (service worker: probado apagando el servidor y recargando — la app carga igual).

Ventaja clave: **actualizar una vez, llega a todos**. Al sustituir
`app/static/vortexpos.html` y redesplegar, cada local recibe la versión nueva en su
siguiente arranque con internet — sin reinstalar nada.

> Mantenimiento: tras cambiar el `vortexpos.html` raíz del proyecto, cópialo a
> `server/app/static/vortexpos.html` y sube el cambio.

## Probar en tu ordenador (2 minutos)

```bash
cd server
./run-local.sh
```

Abre **http://localhost:8000/** y entra con el usuario/clave que muestra la consola
(`admin@vortexpos.local` / `vortex-admin` por defecto). Crea un local con
“+ Nuevo local” y ya tienes su licencia y PIN para dárselos a un cliente.

Ejecutar las pruebas automáticas:

```bash
cd server
python3 test_smoke.py
```

## Ponerlo en internet — Render (recomendado, capa gratuita)

1. Sube la carpeta `server/` a un repositorio de **GitHub**.
2. En **https://render.com** → *New* → *Blueprint* → elige el repo.
   Render lee `render.yaml` y crea **el servicio web + una base de datos Postgres**
   ya conectados, con copias de seguridad automáticas.
3. En *Environment*, define `PROVIDER_EMAIL` y `PROVIDER_PASSWORD` (tus credenciales).
   `JWT_SECRET` se genera solo.
4. Al terminar, tu panel estará en `https://vortexpos-cloud.onrender.com/`.

> Alternativas equivalentes: **Railway**, **Fly.io** o cualquier hosting que soporte
> Docker + Postgres. El `Dockerfile` incluido funciona en todos.

## Conectar la app vortexPOS (tablets)

La app actual (`vortexpos.html`) sigue funcionando 100% en local. Para engancharla a
la nube se le añade un pequeño módulo de sincronización que:

1. Al abrir, hace `POST /api/device/login` con la **licencia** y el **PIN** del local
   y guarda el token.
2. Cada X segundos (y al recuperar internet) hace `POST /api/sync` enviando las
   ventas/cierres nuevos y los cambios de carta, y aplica lo que devuelve el servidor.

Esa integración en el cliente es el siguiente incremento (Fase 2a, lado tablet).
El servidor ya expone todo lo necesario — ver `INTEGRACION.md`.

## Estructura

```
server/
  app/
    main.py       API + panel (rutas, auth, sync)
    db.py         tablas multi-inquilino (tenants, documents, records)
    security.py   hash de PIN/clave (PBKDF2) y tokens JWT
    panel.html    panel de proveedor (web)
  requirements.txt
  Dockerfile        imagen para desplegar
  render.yaml       despliegue de un clic (web + Postgres)
  run-local.sh      arranque local
  test_smoke.py     prueba de extremo a extremo (20 comprobaciones)
  .env.example      variables de entorno
```

## Seguridad y cumplimiento

- Contraseñas y PINs **nunca se guardan en claro** (PBKDF2-HMAC-SHA256 con sal).
- Tokens JWT firmados; caducan a las 12 h (configurable).
- Elige **región de datos en la UE** al desplegar (RGPD).
- vortexPOS **no almacena datos de tarjeta** → fuera del alcance duro de PCI-DSS.
- Cambia SIEMPRE `PROVIDER_PASSWORD` y usa un `JWT_SECRET` largo en producción.
