fn main() {
    // frontend assets are embedded at compile time; recompile when they change
    println!("cargo:rerun-if-changed=../ui");
    tauri_build::build()
}
