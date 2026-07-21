use std::path::Path;

// Emit rerun-if-changed for EVERY file under ../ui recursively. A bare
// `rerun-if-changed=../ui` only watches the directory's own mtime, which changes
// when files are added/removed but NOT when an existing file's content is edited —
// so editing ui/js/*.js or ui/*.html/*.css would leave the compile-time-embedded
// assets stale (the running GUI never reflects the change). Walking the tree fixes it.
fn watch_dir(dir: &Path) {
    let entries = match std::fs::read_dir(dir) {
        Ok(e) => e,
        Err(_) => return,
    };
    for entry in entries.flatten() {
        let path = entry.path();
        println!("cargo:rerun-if-changed={}", path.display());
        if path.is_dir() {
            watch_dir(&path);
        }
    }
}

fn main() {
    // frontend assets are embedded at compile time; recompile when any of them change
    println!("cargo:rerun-if-changed=../ui");
    watch_dir(Path::new("../ui"));
    tauri_build::build()
}
