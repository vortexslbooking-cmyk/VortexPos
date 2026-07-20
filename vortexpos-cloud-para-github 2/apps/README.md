# vortexPOS como apps nativas — Android (APK) y Windows (EXE)

Misma app, tres formatos: la **PWA** en `/app/` del servidor, un **APK** de Android
y un **instalador de Windows**. El HTML es único; esto son solo los envoltorios.

> **No necesitas instalar Android Studio ni Rust.** Las apps se compilan solas en
> **GitHub Actions** (gratis) cuando subes el repo. Tú solo descargas el resultado.

---

## Cómo fabricar el APK y el EXE (sin instalar nada)

1. Sube este repositorio a GitHub (con la carpeta **`.github`** incluida — en Mac es
   una carpeta oculta: pulsa **Cmd + Shift + .** en Finder para verla y arrastrarla).
2. En GitHub → pestaña **Actions** → elige el flujo:
   - **“Android APK”** → *Run workflow* → espera ~5 min → descarga el artefacto
     **vortexPOS-android-apk** (contiene `app-debug.apk`).
   - **“Windows installer”** → *Run workflow* → espera ~10 min → descarga
     **vortexPOS-windows-installer** (contiene el `setup.exe` NSIS y un `.msi`).
3. Además, ambos se **reconstruyen solos** cada vez que actualizas la app
   (`app/static/vortexpos.html`) en el repo.

## Instalarlas

**Android (APK):** envíalo por WhatsApp/USB. Al abrirlo, Android pedirá permitir
la instalación de orígenes desconocidos (una vez). Icono de vortexPOS, pantalla
completa, funciona sin internet. *Nota: es firma de depuración — perfecta para
distribución directa; para Play Store hará falta firma de release y cuenta de
desarrollador (25 USD una vez).*

**Windows (.exe):** ejecuta el `setup.exe`. SmartScreen puede avisar por ser un
editor sin certificado: **“Más información → Ejecutar de todas formas”**. (Un
certificado de firma de código elimina ese aviso; se puede añadir más adelante.)

## Actualizar la app dentro de las apps

```bash
./sync-app.sh        # copia vortexpos.html a nube + Android + Windows
```
…y sube los cambios. GitHub Actions reconstruye los instaladores automáticamente,
y la PWA se actualiza al redesplegar el servidor.

## Estructura

```
apps/
  android/            Capacitor (genera el proyecto nativo en CI con `cap add`)
    capacitor.config.json   id: es.vortex.pos
    assets/logo.png         icono maestro 1024px (iconos + splash derivados)
    www/index.html          la app
  windows/            Tauri v2 (instalador NSIS + MSI)
    src-tauri/              config nativa, ~5 MB de instalador resultante
    assets/logo.png         icono maestro (CI genera .ico y tamaños)
    www/index.html          la app
```
