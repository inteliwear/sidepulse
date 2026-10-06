use chrono::{DateTime, Utc};

fn main() {
    println!("cargo:rerun-if-env-changed=SOURCE_DATE_EPOCH");
    let built_at = std::env::var("SOURCE_DATE_EPOCH")
        .ok()
        .and_then(|value| value.parse::<i64>().ok())
        .and_then(|seconds| DateTime::<Utc>::from_timestamp(seconds, 0))
        .unwrap_or_else(Utc::now);
    println!(
        "cargo:rustc-env=SIDEPULSE_TRAY_BUILD_TIME={}",
        built_at.format("%Y-%m-%d %H:%M UTC")
    );
}
