//! Side-by-side Rust preview bundle. It never registers startup jobs or changes
//! the installed Python application's hooks, settings, or device ownership.

use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use sidepulse_core::{ClientRequest, PROTOCOL_VERSION, RequestKind, ServerMessage, ServerPayload};
use tempfile::Builder;

const BINARIES: [&str; 5] = [
    "sidepulse-next",
    "sidepulse-next-hook",
    "sidepulse-next-service",
    "sidepulse-next-tray",
    "sidepulse-next-stage",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Platform {
    Macos,
    Linux,
    Windows,
}

impl Platform {
    pub fn current() -> io::Result<Self> {
        match std::env::consts::OS {
            "macos" => Ok(Self::Macos),
            "linux" => Ok(Self::Linux),
            "windows" => Ok(Self::Windows),
            other => Err(io::Error::new(
                io::ErrorKind::Unsupported,
                format!("preview staging is unavailable on {other}"),
            )),
        }
    }

    fn executable_suffix(self) -> &'static str {
        if self == Self::Windows { ".exe" } else { "" }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StageManifest {
    pub schema_version: u8,
    pub platform: Platform,
    pub stage_dir: PathBuf,
    pub endpoint: String,
    pub binaries: Vec<PathBuf>,
    pub service_command: Vec<String>,
    pub tray_command: Vec<String>,
    pub launch_files: Vec<PathBuf>,
    pub enabled: bool,
}

pub struct StagePlan {
    source_dir: PathBuf,
    manifest: StageManifest,
}

impl StagePlan {
    pub fn new(source_dir: &Path, stage_dir: &Path, platform: Platform) -> io::Result<Self> {
        let source_dir = fs::canonicalize(source_dir)?;
        let stage_dir = absolute_stage_path(stage_dir)?;
        if fs::symlink_metadata(&stage_dir).is_ok() {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "stage directory already exists",
            ));
        }
        let bin_dir = stage_dir.join("bin");
        let binaries = BINARIES
            .iter()
            .map(|name| {
                let filename = format!("{name}{}", platform.executable_suffix());
                let source = source_dir.join(&filename);
                let metadata = fs::symlink_metadata(&source)?;
                if !metadata.file_type().is_file() {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        format!("not a regular binary: {}", source.display()),
                    ));
                }
                Ok(bin_dir.join(filename))
            })
            .collect::<io::Result<Vec<_>>>()?;
        let endpoint = endpoint_for_stage(&stage_dir, platform);
        let service_command = service_command_for_stage(&stage_dir, &binaries[2], &endpoint);
        let tray_command = vec![binaries[3].to_string_lossy().into_owned(), endpoint.clone()];
        let launch_files = match platform {
            Platform::Macos => vec![
                stage_dir.join("launch/io.sidepulse.next.service.plist"),
                stage_dir.join("launch/io.sidepulse.next.tray.plist"),
            ],
            Platform::Linux => vec![
                stage_dir.join("launch/sidepulse-next.service"),
                stage_dir.join("launch/sidepulse-next-tray.service"),
            ],
            Platform::Windows => vec![
                stage_dir.join("launch/start-service.ps1"),
                stage_dir.join("launch/start-tray.ps1"),
            ],
        };
        Ok(Self {
            source_dir,
            manifest: StageManifest {
                schema_version: 1,
                platform,
                stage_dir,
                endpoint,
                binaries,
                service_command,
                tray_command,
                launch_files,
                enabled: false,
            },
        })
    }

    pub fn manifest(&self) -> &StageManifest {
        &self.manifest
    }

    pub fn stage(&self) -> io::Result<&StageManifest> {
        let final_dir = &self.manifest.stage_dir;
        if fs::symlink_metadata(final_dir).is_ok() {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "stage directory already exists",
            ));
        }
        let parent = final_dir.parent().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "stage directory needs a parent",
            )
        })?;
        let temporary = Builder::new()
            .prefix(".sidepulse-next-stage-")
            .tempdir_in(parent)?;
        fs::create_dir(temporary.path().join("bin"))?;
        fs::create_dir(temporary.path().join("state"))?;
        fs::create_dir(temporary.path().join("launch"))?;
        for binary in &self.manifest.binaries {
            let filename = binary.file_name().expect("binary has filename");
            let source = self.source_dir.join(filename);
            let metadata = fs::symlink_metadata(&source)?;
            if !metadata.file_type().is_file() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("not a regular binary: {}", source.display()),
                ));
            }
            fs::copy(&source, temporary.path().join("bin").join(filename))?;
        }
        fs::write(temporary.path().join("settings.json"), b"{}\n")?;
        let rendered = render_launch_files(&self.manifest);
        for (path, contents) in self.manifest.launch_files.iter().zip(rendered) {
            fs::write(
                temporary
                    .path()
                    .join("launch")
                    .join(path.file_name().unwrap()),
                contents,
            )?;
        }
        let mut manifest_file = fs::File::create(temporary.path().join("manifest.json"))?;
        serde_json::to_writer_pretty(&mut manifest_file, &self.manifest)?;
        manifest_file.write_all(b"\n")?;
        manifest_file.sync_all()?;
        drop(manifest_file);
        fs::rename(temporary.path(), final_dir)?;
        Ok(&self.manifest)
    }
}

/// Start the isolated service from a staged bundle and verify IPC, then stop it.
/// This never installs hooks, registers startup jobs, or configures a device.
pub fn smoke_stage(stage_dir: &Path) -> io::Result<()> {
    let stage_dir = fs::canonicalize(stage_dir)?;
    let manifest: StageManifest =
        serde_json::from_slice(&fs::read(stage_dir.join("manifest.json"))?)?;
    let platform = Platform::current()?;
    let expected_binaries = BINARIES
        .iter()
        .map(|name| {
            stage_dir
                .join("bin")
                .join(format!("{name}{}", platform.executable_suffix()))
        })
        .collect::<Vec<_>>();
    let expected_endpoint = endpoint_for_stage(&stage_dir, platform);
    let expected_command =
        service_command_for_stage(&stage_dir, &expected_binaries[2], &expected_endpoint);
    if manifest.stage_dir != stage_dir
        || manifest.platform != platform
        || manifest.binaries != expected_binaries
        || manifest.endpoint != expected_endpoint
        || manifest.service_command != expected_command
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "bundle manifest does not match its location or isolated service command",
        ));
    }
    for binary in &manifest.binaries {
        if !fs::symlink_metadata(binary).is_ok_and(|metadata| metadata.file_type().is_file()) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "bundle binary is missing or is not a regular file",
            ));
        }
    }
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 1,
        kind: RequestKind::Snapshot,
    };
    if sidepulse_ipc::request::<_, ServerMessage>(
        &manifest.endpoint,
        &request,
        Duration::from_millis(200),
    )
    .is_ok()
    {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "a service already owns the preview endpoint",
        ));
    }
    let mut child = SmokeChild(
        Command::new(&manifest.service_command[0])
            .args(&manifest.service_command[1..])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()?,
    );
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Ok(response) = sidepulse_ipc::request::<_, ServerMessage>(
            &manifest.endpoint,
            &request,
            Duration::from_millis(300),
        ) && matches!(response.payload, ServerPayload::Snapshot { .. })
        {
            break;
        }
        if let Some(status) = child.0.try_wait()? {
            return Err(io::Error::other(format!(
                "preview service exited: {status}"
            )));
        }
        if Instant::now() >= deadline {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "preview service did not become ready",
            ));
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    let settings = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 2,
        kind: RequestKind::Settings,
    };
    let response: ServerMessage =
        sidepulse_ipc::request(&manifest.endpoint, &settings, Duration::from_secs(2))?;
    if !matches!(
        response.payload,
        ServerPayload::Settings {
            active_device: None,
            ..
        }
    ) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "preview service settings or device isolation failed",
        ));
    }
    Ok(())
}

struct SmokeChild(Child);

impl Drop for SmokeChild {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn absolute_stage_path(path: &Path) -> io::Result<PathBuf> {
    let name = path.file_name().ok_or_else(|| {
        io::Error::new(io::ErrorKind::InvalidInput, "stage directory needs a name")
    })?;
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    let absolute = fs::canonicalize(parent)?.join(name);
    if absolute.to_string_lossy().chars().any(char::is_control) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "stage path contains a control character",
        ));
    }
    Ok(absolute)
}

fn endpoint_for_stage(stage: &Path, platform: Platform) -> String {
    let mut hash = 0xcbf29ce484222325_u64;
    for byte in stage.to_string_lossy().as_bytes() {
        hash = (hash ^ u64::from(*byte)).wrapping_mul(0x100000001b3);
    }
    if platform == Platform::Windows {
        format!("sidepulse-next-preview-{hash:016x}")
    } else {
        let endpoint = stage
            .join("state/events.sock")
            .to_string_lossy()
            .into_owned();
        if endpoint.len() <= 95 {
            endpoint
        } else {
            format!("/tmp/sidepulse-next-{hash:016x}.sock")
        }
    }
}

fn service_command_for_stage(stage: &Path, executable: &Path, endpoint: &str) -> Vec<String> {
    let mut command = vec![
        executable.to_string_lossy().into_owned(),
        endpoint.to_owned(),
        "--state".into(),
        stage
            .join("state/latest.json")
            .to_string_lossy()
            .into_owned(),
        "--settings".into(),
        stage.join("settings.json").to_string_lossy().into_owned(),
    ];
    for provider in ["codex", "claude", "grok", "cursor", "junie"] {
        command.push("--log".into());
        command.push(provider.into());
        command.push(
            stage
                .join(format!("state/{provider}.jsonl"))
                .to_string_lossy()
                .into_owned(),
        );
    }
    command
}

fn render_launch_files(manifest: &StageManifest) -> [String; 2] {
    match manifest.platform {
        Platform::Macos => [
            render_plist(
                "io.sidepulse.next.service",
                &manifest.service_command,
                &manifest.stage_dir,
            ),
            render_plist(
                "io.sidepulse.next.tray",
                &manifest.tray_command,
                &manifest.stage_dir,
            ),
        ],
        Platform::Linux => [
            render_systemd(
                "SidePulse Rust preview service",
                &manifest.service_command,
                false,
            ),
            render_systemd("SidePulse Rust preview tray", &manifest.tray_command, true),
        ],
        Platform::Windows => [
            render_powershell(&manifest.service_command),
            render_powershell(&manifest.tray_command),
        ],
    }
}

fn render_plist(label: &str, command: &[String], working_directory: &Path) -> String {
    let arguments = command
        .iter()
        .map(|part| format!("    <string>{}</string>\n", xml_escape(part)))
        .collect::<String>();
    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n<plist version=\"1.0\">\n<dict>\n  <key>Label</key><string>{label}</string>\n  <key>ProgramArguments</key>\n  <array>\n{arguments}  </array>\n  <key>WorkingDirectory</key><string>{}</string>\n  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n</dict>\n</plist>\n",
        xml_escape(&working_directory.to_string_lossy())
    )
}

fn xml_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

fn render_systemd(description: &str, command: &[String], tray: bool) -> String {
    let command = command
        .iter()
        .map(|part| systemd_quote(part))
        .collect::<Vec<_>>()
        .join(" ");
    let dependency = if tray {
        "After=sidepulse-next.service\nRequires=sidepulse-next.service\n"
    } else {
        "After=network-online.target\n"
    };
    format!(
        "[Unit]\nDescription={description}\n{dependency}\n[Service]\nExecStart={command}\nRestart=on-failure\nRestartSec=2\n\n[Install]\nWantedBy=default.target\n"
    )
}

fn systemd_quote(value: &str) -> String {
    format!(
        "\"{}\"",
        value
            .replace('\\', "\\\\")
            .replace('"', "\\\"")
            .replace('%', "%%")
    )
}

fn render_powershell(command: &[String]) -> String {
    let parts = command
        .iter()
        .map(|part| format!("'{}'", part.replace('\'', "''")))
        .collect::<Vec<_>>()
        .join(" ");
    format!("& {parts}\r\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dummy_binaries(source: &Path, platform: Platform) {
        fs::create_dir(source).unwrap();
        for name in BINARIES {
            fs::write(
                source.join(format!("{name}{}", platform.executable_suffix())),
                b"preview-binary",
            )
            .unwrap();
        }
    }

    #[test]
    fn stages_isolated_bundles_for_all_platforms() {
        for platform in [Platform::Macos, Platform::Linux, Platform::Windows] {
            let directory = tempfile::tempdir().unwrap();
            let source = directory.path().join("source");
            dummy_binaries(&source, platform);
            let destination = directory.path().join("preview & 'test'");
            let plan = StagePlan::new(&source, &destination, platform).unwrap();
            assert!(!destination.exists());
            assert!(!plan.manifest().enabled);
            let manifest = plan.stage().unwrap();
            assert_eq!(manifest.binaries.len(), 5);
            assert_eq!(manifest.launch_files.len(), 2);
            assert_eq!(manifest.service_command.len(), 21);
            assert!(manifest.service_command.contains(&"cursor".to_owned()));
            assert!(
                manifest
                    .service_command
                    .iter()
                    .all(|part| !part.contains("--auto-device"))
            );
            assert!(destination.join("state").is_dir());
            assert_eq!(
                fs::read(destination.join("settings.json")).unwrap(),
                b"{}\n"
            );
            for binary in &manifest.binaries {
                assert_eq!(fs::read(binary).unwrap(), b"preview-binary");
            }
            for launch_file in &manifest.launch_files {
                assert!(launch_file.is_file());
            }
            let saved: StageManifest =
                serde_json::from_slice(&fs::read(destination.join("manifest.json")).unwrap())
                    .unwrap();
            assert_eq!(&saved, manifest);
            assert_eq!(
                StagePlan::new(&source, &destination, platform)
                    .err()
                    .unwrap()
                    .kind(),
                io::ErrorKind::AlreadyExists
            );
            let rendered = fs::read_to_string(&manifest.launch_files[0]).unwrap();
            match platform {
                Platform::Macos => {
                    assert!(rendered.contains("preview &amp; &apos;test&apos;"));
                    assert!(rendered.contains("<key>ProgramArguments</key>"));
                }
                Platform::Linux => {
                    assert!(rendered.contains("ExecStart="));
                    assert!(rendered.contains("preview & 'test'"));
                }
                Platform::Windows => {
                    assert!(rendered.contains("preview & ''test''"));
                    assert!(rendered.contains("sidepulse-next-service.exe"));
                }
            }
        }
    }

    #[test]
    fn rejects_symlink_source_and_existing_destination() {
        let directory = tempfile::tempdir().unwrap();
        let source = directory.path().join("source");
        dummy_binaries(&source, Platform::Linux);
        let destination = directory.path().join("preview");
        fs::create_dir(&destination).unwrap();
        assert_eq!(
            StagePlan::new(&source, &destination, Platform::Linux)
                .err()
                .unwrap()
                .kind(),
            io::ErrorKind::AlreadyExists
        );
        fs::remove_dir(&destination).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::symlink;
            let service = source.join("sidepulse-next-service");
            fs::remove_file(&service).unwrap();
            symlink(source.join("sidepulse-next"), &service).unwrap();
            assert_eq!(
                StagePlan::new(&source, &destination, Platform::Linux)
                    .err()
                    .unwrap()
                    .kind(),
                io::ErrorKind::InvalidInput
            );
        }
    }

    #[test]
    fn uses_short_unix_socket_name_when_stage_path_is_long() {
        let long = PathBuf::from("/tmp").join("x".repeat(180));
        for platform in [Platform::Macos, Platform::Linux] {
            let endpoint = endpoint_for_stage(&long, platform);
            assert!(endpoint.starts_with("/tmp/sidepulse-next-"));
            assert!(endpoint.len() <= 95);
        }
    }

    #[cfg(unix)]
    #[test]
    fn rejects_stage_path_that_could_break_launch_files() {
        let directory = tempfile::tempdir().unwrap();
        let source = directory.path().join("source");
        dummy_binaries(&source, Platform::Linux);
        let path = directory.path().join("bad\nunit");
        assert_eq!(
            StagePlan::new(&source, &path, Platform::Linux)
                .err()
                .unwrap()
                .kind(),
            io::ErrorKind::InvalidInput
        );
    }

    #[test]
    fn smoke_check_rejects_a_modified_service_command_before_launch() {
        let directory = tempfile::tempdir().unwrap();
        let source = directory.path().join("source");
        let platform = Platform::current().unwrap();
        dummy_binaries(&source, platform);
        let destination = directory.path().join("preview");
        let plan = StagePlan::new(&source, &destination, platform).unwrap();
        plan.stage().unwrap();
        let manifest_path = destination.join("manifest.json");
        let mut manifest: StageManifest =
            serde_json::from_slice(&fs::read(&manifest_path).unwrap()).unwrap();
        manifest.service_command.push("--auto-device".into());
        fs::write(&manifest_path, serde_json::to_vec(&manifest).unwrap()).unwrap();
        assert_eq!(
            smoke_stage(&destination).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }
}
