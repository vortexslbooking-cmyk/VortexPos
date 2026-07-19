// vortexPOS — envoltorio de escritorio (Windows) sobre la app web local.
// Sin ventana de consola en producción:
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error al arrancar vortexPOS");
}
