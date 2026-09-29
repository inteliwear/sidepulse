fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&["settings_poll", "settings_action"]),
    ))
    .expect("could not build Settings assets and permissions");
}
