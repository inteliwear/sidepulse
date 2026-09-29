//! Platform startup management for an explicitly selected isolated bundle.
//! No production hooks, settings paths, or installed Python labels are used.
use super::{Platform, StageManifest, render_plist, render_systemd, xml_escape};
use serde::Serialize;
use std::{
    fs,
    io::{self, Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Job {
    Service,
    Tray,
    SdGuard,
}
impl Job {
    pub fn parse(value: &str) -> io::Result<Self> {
        match value {
            "service" => Ok(Self::Service),
            "tray" => Ok(Self::Tray),
            "sd-guard" => Ok(Self::SdGuard),
            _ => Err(io::Error::other("job must be service, tray, or sd-guard")),
        }
    }
    fn name(self) -> &'static str {
        match self {
            Self::Service => "service",
            Self::Tray => "tray",
            Self::SdGuard => "sd-guard",
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Operation {
    Install,
    Start,
    Stop,
    Uninstall,
    Status,
}
impl Operation {
    pub fn parse(value: &str) -> io::Result<Self> {
        match value {
            "install" => Ok(Self::Install),
            "start" => Ok(Self::Start),
            "stop" => Ok(Self::Stop),
            "uninstall" => Ok(Self::Uninstall),
            "status" => Ok(Self::Status),
            _ => Err(io::Error::other(
                "operation must be install, start, stop, uninstall, or status",
            )),
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct CommandSpec {
    pub program: String,
    pub args: Vec<String>,
}
#[derive(Clone, Debug, Serialize)]
pub struct StartupPlan {
    pub endpoint: String,
    pub platform: Platform,
    pub job: Job,
    pub operation: Operation,
    pub start: bool,
    pub system: bool,
    pub label: String,
    pub path: PathBuf,
    pub contents: String,
    pub probe: CommandSpec,
    pub commands: Vec<CommandSpec>,
    #[serde(skip)]
    expected: Option<Vec<u8>>,
}
#[derive(Debug, Serialize)]
pub struct StartupResult {
    pub path: PathBuf,
    pub installed: bool,
    pub manager_registered: bool,
    pub running: Option<bool>,
    pub start_requested: bool,
}
#[derive(Debug)]
pub struct CommandResult {
    pub success: bool,
    pub stdout: String,
    pub stderr: String,
}
pub fn current_user(platform: Platform) -> io::Result<String> {
    if platform == Platform::Linux {
        return Ok("current-user".into());
    }
    let output = if platform == Platform::Macos {
        Command::new("/usr/bin/id").arg("-u").output()?
    } else {
        Command::new("whoami.exe")
            .args(["/user", "/fo", "csv", "/nh"])
            .output()?
    };
    if !output.status.success() {
        return Err(io::Error::other("cannot determine startup user"));
    }
    let text = String::from_utf8_lossy(&output.stdout);
    if platform == Platform::Macos {
        Ok(text.trim().to_owned())
    } else {
        let user = text
            .trim()
            .rsplit(',')
            .next()
            .unwrap_or("")
            .trim_matches('"');
        if !user.starts_with("S-1-") {
            return Err(io::Error::other("cannot determine Windows user SID"));
        }
        Ok(user.to_owned())
    }
}

fn spec(program: &str, args: impl IntoIterator<Item = String>) -> CommandSpec {
    CommandSpec {
        program: program.into(),
        args: args.into_iter().collect(),
    }
}
fn strings(values: &[&str]) -> Vec<String> {
    values.iter().map(|v| v.to_string()).collect()
}
fn regular_bytes(path: &Path) -> io::Result<Option<Vec<u8>>> {
    match fs::symlink_metadata(path) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e),
        Ok(meta) if !meta.is_file() || meta.len() > 1024 * 1024 => Err(io::Error::other(
            "startup file is not a bounded regular file",
        )),
        Ok(_) => fs::read(path).map(Some),
    }
}
fn identity(stage: &Path) -> String {
    let mut hash = 0xcbf29ce484222325_u64;
    for byte in stage.to_string_lossy().as_bytes() {
        hash = (hash ^ u64::from(*byte)).wrapping_mul(0x100000001b3);
    }
    format!("{hash:016x}")
}
impl StartupPlan {
    pub fn new(
        manifest: &StageManifest,
        job: Job,
        mut operation: Operation,
        start: bool,
        directory: &Path,
        user: &str,
    ) -> io::Result<Self> {
        if manifest.binaries.len() < 8
            || manifest.service_command.is_empty()
            || manifest.tray_command.is_empty()
        {
            return Err(io::Error::other("bundle manifest is incomplete"));
        }
        if !directory.is_absolute()
            || directory.to_string_lossy().chars().any(char::is_control)
            || user.chars().any(char::is_control)
            || user.is_empty()
        {
            return Err(io::Error::other(
                "startup directory must be absolute and user must be valid",
            ));
        }
        if job == Job::SdGuard && manifest.platform != Platform::Macos {
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "SD eject protection requires macOS",
            ));
        }
        let command = match job {
            Job::Service => manifest.service_command.clone(),
            Job::Tray => manifest.tray_command.clone(),
            Job::SdGuard => vec![manifest.binaries[7].to_string_lossy().into_owned()],
        };
        let prefix = format!("sidepulse-next-{}", identity(&manifest.stage_dir));
        let name = format!("{prefix}-{}", job.name());
        let (label, path, contents, probe, commands) = match manifest.platform {
            Platform::Macos => {
                if user.parse::<u32>().is_err() {
                    return Err(io::Error::other("macOS startup requires a numeric user ID"));
                }
                let label = format!(
                    "io.sidepulse.next.{}.{}",
                    identity(&manifest.stage_dir),
                    job.name()
                );
                let path = directory.join(format!("{label}.plist"));
                let mut contents = render_plist(&label, &command, &manifest.stage_dir);
                let logging = format!(
                    "<key>ExitTimeOut</key><integer>30</integer>\n<key>StandardOutPath</key><string>{}</string>\n<key>StandardErrorPath</key><string>{}</string>\n",
                    xml_escape(
                        &manifest
                            .stage_dir
                            .join(format!("state/{}.out.log", job.name()))
                            .to_string_lossy()
                    ),
                    xml_escape(
                        &manifest
                            .stage_dir
                            .join(format!("state/{}.err.log", job.name()))
                            .to_string_lossy()
                    )
                );
                contents = contents.replace("</dict>", &format!("{logging}</dict>"));
                let domain = format!("gui/{user}");
                let target = format!("{domain}/{label}");
                let probe = spec("/bin/launchctl", strings(&["print", &target]));
                let commands = match operation {
                    Operation::Install | Operation::Start => {
                        let mut commands =
                            vec![spec("/bin/launchctl", strings(&["enable", &target]))];
                        if start {
                            commands.push(spec(
                                "/bin/launchctl",
                                vec![
                                    "bootstrap".into(),
                                    domain,
                                    path.to_string_lossy().into_owned(),
                                ],
                            ));
                        }
                        commands
                    }
                    Operation::Stop => vec![spec("/bin/launchctl", strings(&["bootout", &target]))],
                    Operation::Uninstall => vec![
                        spec("/bin/launchctl", strings(&["bootout", &target])),
                        spec("/bin/launchctl", strings(&["disable", &target])),
                    ],
                    Operation::Status => vec![],
                };
                (label, path, contents, probe, commands)
            }
            Platform::Linux => {
                let label = format!("{name}.service");
                let path = directory.join(&label);
                let contents = render_systemd(
                    &format!("SidePulse Rust preview {}", job.name()),
                    &command,
                    job == Job::Tray,
                )
                .replace(
                    "After=sidepulse-next.service\nRequires=sidepulse-next.service\n",
                    &format!("After={prefix}-service.service\nRequires={prefix}-service.service\n"),
                );
                let probe = spec(
                    "systemctl",
                    strings(&[
                        "--user",
                        "show",
                        &label,
                        "--property=LoadState",
                        "--property=ActiveState",
                    ]),
                );
                let commands = match operation {
                    Operation::Install | Operation::Start => {
                        let mut commands = vec![
                            spec("systemctl", strings(&["--user", "daemon-reload"])),
                            spec("systemctl", strings(&["--user", "enable", &label])),
                        ];
                        if start {
                            commands
                                .push(spec("systemctl", strings(&["--user", "restart", &label])));
                        }
                        commands
                    }
                    Operation::Stop => {
                        vec![spec("systemctl", strings(&["--user", "stop", &label]))]
                    }
                    Operation::Uninstall => vec![spec(
                        "systemctl",
                        strings(&["--user", "disable", "--now", &label]),
                    )],
                    Operation::Status => vec![],
                };
                (label, path, contents, probe, commands)
            }
            Platform::Windows => {
                let label = name;
                let path = directory.join(format!("{label}.xml"));
                let contents = render_task(&command, user, &manifest.stage_dir);
                let probe = spec("schtasks.exe", strings(&["/Query", "/TN", &label, "/XML"]));
                let commands = match operation {
                    Operation::Install | Operation::Start => {
                        let mut commands = vec![spec(
                            "schtasks.exe",
                            vec![
                                "/Create".into(),
                                "/TN".into(),
                                label.clone(),
                                "/XML".into(),
                                path.to_string_lossy().into_owned(),
                            ],
                        )];
                        if start {
                            commands.push(spec("schtasks.exe", strings(&["/Run", "/TN", &label])));
                        }
                        commands
                    }
                    Operation::Stop => {
                        vec![spec("schtasks.exe", strings(&["/End", "/TN", &label]))]
                    }
                    Operation::Uninstall => vec![spec(
                        "schtasks.exe",
                        strings(&["/Delete", "/TN", &label, "/F"]),
                    )],
                    Operation::Status => vec![],
                };
                (label, path, contents, probe, commands)
            }
        };
        let expected = regular_bytes(&path)?;
        if expected
            .as_ref()
            .is_some_and(|bytes| bytes != contents.as_bytes())
        {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "startup entry belongs to a different configuration; it was preserved",
            ));
        }
        if operation == Operation::Start && expected.is_none() {
            operation = Operation::Install;
        }
        Ok(Self {
            endpoint: manifest.endpoint.clone(),
            platform: manifest.platform,
            job,
            operation,
            start,
            system: false,
            label,
            path,
            contents,
            probe,
            commands,
            expected,
        })
    }
    pub fn system_sd_guard(operation: Operation, start: bool) -> io::Result<Self> {
        if Platform::current()? != Platform::Macos {
            return Err(io::Error::other("system SD guard requires macOS"));
        }
        let root = Path::new(crate::package::SYSTEM_PAYLOAD);
        if matches!(operation, Operation::Install | Operation::Start) {
            crate::package::verify(root)?;
            #[cfg(target_os = "macos")]
            verify_root_owned(root)?;
        }
        Self::system_guard_at(root, Path::new("/Library/LaunchDaemons"), operation, start)
    }
    fn system_guard_at(
        root: &Path,
        directory: &Path,
        mut operation: Operation,
        start: bool,
    ) -> io::Result<Self> {
        let label = "io.sidepulse.next.sd-guard.system".to_owned();
        let path = directory.join(format!("{label}.plist"));
        let command = vec![
            root.join("bin")
                .join("sidepulse-next-sd-guard")
                .to_string_lossy()
                .into_owned(),
        ];
        let mut contents = render_plist(&label, &command, root);
        contents = contents.replace("</dict>", "<key>UserName</key><string>root</string>\n<key>ExitTimeOut</key><integer>30</integer>\n<key>StandardOutPath</key><string>/var/log/sidepulse-next-sd-guard.log</string>\n<key>StandardErrorPath</key><string>/var/log/sidepulse-next-sd-guard.err.log</string>\n</dict>");
        let expected = regular_bytes(&path)?;
        if expected
            .as_ref()
            .is_some_and(|bytes| bytes != contents.as_bytes())
        {
            return Err(io::Error::other(
                "system guard entry belongs to a different command; preserved",
            ));
        }
        if operation == Operation::Start && expected.is_none() {
            operation = Operation::Install;
        }
        let target = format!("system/{label}");
        let mut commands = Vec::new();
        match operation {
            Operation::Install | Operation::Start => {
                commands.push(spec("/bin/launchctl", strings(&["enable", &target])));
                if start {
                    commands.push(spec(
                        "/bin/launchctl",
                        vec![
                            "bootstrap".into(),
                            "system".into(),
                            path.to_string_lossy().into_owned(),
                        ],
                    ));
                }
            }
            Operation::Stop => {
                commands.push(spec("/bin/launchctl", strings(&["bootout", &target])))
            }
            Operation::Uninstall => {
                commands.push(spec("/bin/launchctl", strings(&["bootout", &target])));
                commands.push(spec("/bin/launchctl", strings(&["disable", &target])));
            }
            Operation::Status => {}
        }
        Ok(Self {
            endpoint: String::new(),
            platform: Platform::Macos,
            job: Job::SdGuard,
            operation,
            start,
            system: true,
            label,
            path,
            contents,
            probe: spec("/bin/launchctl", strings(&["print", &target])),
            commands,
            expected,
        })
    }
    pub fn apply(&self) -> io::Result<StartupResult> {
        if self.system {
            #[cfg(target_os = "macos")]
            {
                if self.operation != Operation::Status && unsafe { libc::geteuid() } != 0 {
                    return Err(io::Error::new(
                        io::ErrorKind::PermissionDenied,
                        "system SD guard installation requires root",
                    ));
                }
                if matches!(self.operation, Operation::Install | Operation::Start) {
                    verify_root_owned(Path::new(crate::package::SYSTEM_PAYLOAD))?;
                    crate::package::verify(Path::new(crate::package::SYSTEM_PAYLOAD))?;
                }
            }
            #[cfg(not(target_os = "macos"))]
            return Err(io::Error::other("system SD guard requires macOS"));
        }
        if self.platform != Platform::current()? {
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "startup plan does not match this platform",
            ));
        }
        self.apply_with_stop(run_command, || {
            if self.platform == Platform::Windows
                && self.job == Job::Service
                && matches!(self.operation, Operation::Stop | Operation::Uninstall)
            {
                stop_service_gracefully(&self.endpoint)
            } else {
                Ok(false)
            }
        })
    }
    #[cfg(test)]
    fn apply_with(
        &self,
        runner: impl FnMut(&CommandSpec) -> io::Result<CommandResult>,
    ) -> io::Result<StartupResult> {
        self.apply_with_stop(runner, || Ok(false))
    }
    fn apply_with_stop(
        &self,
        mut runner: impl FnMut(&CommandSpec) -> io::Result<CommandResult>,
        before_stop: impl FnOnce() -> io::Result<bool>,
    ) -> io::Result<StartupResult> {
        if regular_bytes(&self.path)? != self.expected {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "startup entry changed after planning",
            ));
        }
        let probe = runner(&self.probe)?;
        let (registered, running) = probe_state(self.platform, &probe);
        if self.platform == Platform::Windows
            && registered
            && !task_matches(&probe.stdout, &self.contents)
        {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "scheduled task runs a different command; it was preserved",
            ));
        }
        if self.operation == Operation::Status {
            return Ok(StartupResult {
                path: self.path.clone(),
                installed: self.expected.is_some(),
                manager_registered: registered,
                running,
                start_requested: false,
            });
        }
        if self.expected.is_none() && !matches!(self.operation, Operation::Install) {
            if registered {
                return Err(io::Error::other(
                    "startup registration has no owned file; it was preserved",
                ));
            }
            return Ok(StartupResult {
                path: self.path.clone(),
                installed: false,
                manager_registered: false,
                running: Some(false),
                start_requested: false,
            });
        }
        if self.operation == Operation::Install && self.expected.is_none() {
            if registered {
                return Err(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    "startup registration already exists without an owned file",
                ));
            }
            fs::create_dir_all(self.path.parent().unwrap())?;
            let mut temporary = tempfile::NamedTempFile::new_in(self.path.parent().unwrap())?;
            temporary.write_all(self.contents.as_bytes())?;
            temporary.as_file().sync_all()?;
            temporary
                .persist_noclobber(&self.path)
                .map_err(|e| e.error)?;
        }
        let stopped = before_stop()?;
        for command in &self.commands {
            if stopped
                && self.platform == Platform::Windows
                && command.args.first().is_some_and(|a| a == "/End")
            {
                continue;
            }
            if self.platform == Platform::Macos {
                if command.args.first().is_some_and(|a| a == "bootstrap") && registered {
                    continue;
                }
                if command.args.first().is_some_and(|a| a == "bootout") && !registered {
                    continue;
                }
            }
            if self.platform == Platform::Windows
                && command.args.first().is_some_and(|a| a == "/Create")
                && registered
            {
                continue;
            }
            let result = runner(command)?;
            if !result.success {
                return Err(io::Error::other(format!(
                    "startup manager failed: {}",
                    result.stderr.trim()
                )));
            }
        }
        if self.operation == Operation::Uninstall {
            if regular_bytes(&self.path)?.as_deref() != Some(self.contents.as_bytes()) {
                return Err(io::Error::other("startup entry changed during removal"));
            }
            fs::remove_file(&self.path)?;
            if self.platform == Platform::Linux {
                let result = runner(&spec("systemctl", strings(&["--user", "daemon-reload"])))?;
                if !result.success {
                    return Err(io::Error::other(result.stderr));
                }
            }
        }
        let (registered, running) = if self.operation == Operation::Uninstall {
            (false, Some(false))
        } else {
            probe_state(self.platform, &runner(&self.probe)?)
        };
        Ok(StartupResult {
            path: self.path.clone(),
            installed: self.operation != Operation::Uninstall,
            manager_registered: registered,
            running,
            start_requested: self.start
                && matches!(self.operation, Operation::Install | Operation::Start),
        })
    }
}

fn stop_service_gracefully(endpoint: &str) -> io::Result<bool> {
    use sidepulse_core::{
        ClientRequest, PROTOCOL_VERSION, RequestKind, ServerMessage, ServerPayload,
    };
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 1,
        kind: RequestKind::Shutdown,
    };
    match sidepulse_ipc::request::<_, ServerMessage>(endpoint, &request, Duration::from_secs(2)) {
        Ok(message)
            if message.version == PROTOCOL_VERSION
                && message.request_id == Some(1)
                && message.payload == ServerPayload::Ack => {}
        Ok(_) => return Err(io::Error::other("service did not acknowledge shutdown")),
        Err(error)
            if matches!(
                error.kind(),
                io::ErrorKind::NotFound
                    | io::ErrorKind::ConnectionRefused
                    | io::ErrorKind::NotConnected
                    | io::ErrorKind::BrokenPipe
            ) =>
        {
            return Ok(false);
        }
        Err(error) => return Err(error),
    }
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        match sidepulse_ipc::request::<_, ServerMessage>(
            endpoint,
            &ClientRequest {
                kind: RequestKind::Snapshot,
                ..request.clone()
            },
            Duration::from_millis(200),
        ) {
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::NotFound
                        | io::ErrorKind::ConnectionRefused
                        | io::ErrorKind::NotConnected
                        | io::ErrorKind::BrokenPipe
                        | io::ErrorKind::UnexpectedEof
                ) =>
            {
                break;
            }
            Err(error)
                if !matches!(
                    error.kind(),
                    io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock
                ) =>
            {
                return Err(error);
            }
            _ => {}
        }
        if Instant::now() >= deadline {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "service has not finished shutting down",
            ));
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    Ok(true)
}
fn probe_state(platform: Platform, probe: &CommandResult) -> (bool, Option<bool>) {
    match platform {
        Platform::Macos => (
            probe.success,
            Some(
                probe.success
                    && probe.stdout.lines().any(|line| {
                        line.trim()
                            .strip_prefix("pid = ")
                            .is_some_and(|pid| pid.parse::<u32>().is_ok_and(|pid| pid > 0))
                    }),
            ),
        ),
        Platform::Linux => (
            probe.success && probe.stdout.lines().any(|line| line == "LoadState=loaded"),
            Some(
                probe.success
                    && probe
                        .stdout
                        .lines()
                        .any(|line| line == "ActiveState=active"),
            ),
        ),
        Platform::Windows => (probe.success, None),
    }
}

/// Windows command-line quoting, including quotes and trailing backslashes.
fn windows_quote(value: &str) -> String {
    let mut output = String::from("\"");
    let mut slashes = 0;
    for c in value.chars() {
        if c == '\\' {
            slashes += 1;
            continue;
        }
        output.push_str(&"\\".repeat(if c == '\"' { slashes * 2 + 1 } else { slashes }));
        slashes = 0;
        output.push(c);
    }
    output.push_str(&"\\".repeat(slashes * 2));
    output.push('"');
    output
}
fn render_task(command: &[String], user: &str, stage: &Path) -> String {
    let args = command[1..]
        .iter()
        .map(|v| windows_quote(v))
        .collect::<Vec<_>>()
        .join(" ");
    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?><Task version=\"1.2\" xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\"><RegistrationInfo><Description>SidePulse Rust preview</Description></RegistrationInfo><Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger></Triggers><Principals><Principal id=\"Author\"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals><Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Enabled>true</Enabled></Settings><Actions Context=\"Author\"><Exec><Command>{exe}</Command><Arguments>{args}</Arguments><WorkingDirectory>{stage}</WorkingDirectory></Exec></Actions></Task>",
        user = xml_escape(user),
        exe = xml_escape(&command[0]),
        args = xml_escape(&args),
        stage = xml_escape(&stage.to_string_lossy())
    )
}
fn task_matches(xml: &str, expected: &str) -> bool {
    fn fields(xml: &str) -> Option<Vec<String>> {
        let document = roxmltree::Document::parse(xml).ok()?;
        let actions: Vec<_> = document
            .descendants()
            .filter(|n| n.has_tag_name("Actions"))
            .collect();
        if actions.len() != 1 || actions[0].children().filter(|n| n.is_element()).count() != 1 {
            return None;
        }
        let triggers: Vec<_> = document
            .descendants()
            .filter(|n| n.has_tag_name("Triggers"))
            .collect();
        if triggers.len() != 1 || triggers[0].children().filter(|n| n.is_element()).count() != 1 {
            return None;
        }
        let mut result = Vec::new();
        for name in [
            "Exec",
            "LogonTrigger",
            "Principal",
            "Command",
            "Arguments",
            "WorkingDirectory",
            "RunLevel",
            "LogonType",
        ] {
            let nodes: Vec<_> = document
                .descendants()
                .filter(|n| n.has_tag_name(name))
                .collect();
            if nodes.len() != 1 {
                return None;
            }
            if !["Exec", "LogonTrigger", "Principal"].contains(&name) {
                result.push(nodes[0].text().unwrap_or("").to_owned());
            }
        }
        let users: Vec<_> = document
            .descendants()
            .filter(|n| n.has_tag_name("UserId"))
            .collect();
        if users.len() != 2 {
            return None;
        }
        result.extend(users.into_iter().map(|n| n.text().unwrap_or("").to_owned()));
        Some(result)
    }
    fields(xml).is_some_and(|actual| Some(actual) == fields(expected))
}

fn decode(bytes: Vec<u8>) -> String {
    if bytes.starts_with(&[0xff, 0xfe])
        || bytes
            .iter()
            .skip(1)
            .step_by(2)
            .take(20)
            .filter(|&&b| b == 0)
            .count()
            > 10
    {
        let bytes = bytes.strip_prefix(&[0xff, 0xfe]).unwrap_or(&bytes);
        String::from_utf16_lossy(
            &bytes
                .as_chunks::<2>()
                .0
                .iter()
                .map(|c| u16::from_le_bytes([c[0], c[1]]))
                .collect::<Vec<_>>(),
        )
    } else {
        String::from_utf8_lossy(&bytes).into_owned()
    }
}
pub(crate) fn run_command(spec: &CommandSpec) -> io::Result<CommandResult> {
    let mut stdout = tempfile::tempfile()?;
    let mut stderr = tempfile::tempfile()?;
    let mut child = Command::new(&spec.program)
        .args(&spec.args)
        .stdin(Stdio::null())
        .stdout(stdout.try_clone()?)
        .stderr(stderr.try_clone()?)
        .spawn()?;
    let deadline = Instant::now() + Duration::from_secs(30);
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(25)),
            result => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(result.err().unwrap_or_else(|| {
                    io::Error::new(io::ErrorKind::TimedOut, "startup manager timed out")
                }));
            }
        }
    };
    use std::io::{Seek, SeekFrom};
    stdout.seek(SeekFrom::Start(0))?;
    stderr.seek(SeekFrom::Start(0))?;
    let mut out = Vec::new();
    let mut err = Vec::new();
    stdout.take(1024 * 1024 + 1).read_to_end(&mut out)?;
    stderr.take(65536).read_to_end(&mut err)?;
    if out.len() > 1024 * 1024 {
        return Err(io::Error::other("startup manager output exceeds limit"));
    }
    Ok(CommandResult {
        success: status.success(),
        stdout: decode(out),
        stderr: decode(err),
    })
}

#[cfg(target_os = "macos")]
fn verify_root_owned(root: &Path) -> io::Result<()> {
    use std::os::unix::fs::MetadataExt;
    for path in root.ancestors() {
        let meta = fs::symlink_metadata(path)?;
        if !meta.is_dir() || meta.uid() != 0 || meta.mode() & 0o022 != 0 {
            return Err(io::Error::other(
                "system payload and every parent must be root-owned directories without group or other write access",
            ));
        }
    }
    let mut paths = Vec::new();
    crate::upgrade::files(root, Path::new(""), &mut paths)?;
    for relative in paths {
        let meta = fs::symlink_metadata(root.join(relative))?;
        if meta.uid() != 0 || meta.mode() & 0o022 != 0 {
            return Err(io::Error::other(
                "system payload file is writable or not root-owned",
            ));
        }
    }
    fn directories(path: &Path) -> io::Result<()> {
        let meta = fs::symlink_metadata(path)?;
        if !meta.is_dir() {
            return Ok(());
        }
        if meta.uid() != 0 || meta.mode() & 0o022 != 0 {
            return Err(io::Error::other(
                "system payload directory is writable or not root-owned",
            ));
        }
        for entry in fs::read_dir(path)? {
            directories(&entry?.path())?;
        }
        Ok(())
    }
    directories(root)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn bundle(root: &Path, platform: Platform) -> StageManifest {
        let source = root.join("source");
        fs::create_dir(&source).unwrap();
        for name in crate::BINARIES {
            fs::write(
                source.join(format!("{name}{}", platform.executable_suffix())),
                b"binary",
            )
            .unwrap();
        }
        crate::StagePlan::new(&source, &root.join("preview & 'quoted'"), platform)
            .unwrap()
            .manifest()
            .clone()
    }
    fn success(stdout: String) -> CommandResult {
        CommandResult {
            success: true,
            stdout,
            stderr: String::new(),
        }
    }
    #[test]
    fn system_sd_guard_uses_immutable_payload_and_separate_system_domain() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path().join("immutable payload");
        let launchd = directory.path().join("LaunchDaemons");
        let plan =
            StartupPlan::system_guard_at(&root, &launchd, Operation::Install, false).unwrap();
        assert!(plan.system);
        assert!(plan.contents.contains("<string>root</string>"));
        let executable = root.join("bin").join("sidepulse-next-sd-guard");
        assert!(
            plan.contents
                .contains(&xml_escape(&executable.to_string_lossy()))
        );
        assert!(plan.probe.args[1].starts_with("system/"));
        assert!(
            !plan
                .commands
                .iter()
                .any(|command| command.args.contains(&"bootstrap".into()))
        );
        let result = plan
            .apply_with(|_| {
                Ok(CommandResult {
                    success: false,
                    stdout: String::new(),
                    stderr: String::new(),
                })
            })
            .unwrap_err();
        assert!(result.to_string().contains("startup manager"));
        // Owned file remains after manager failure and can be explicitly removed.
        assert!(plan.path.is_file());
        let remove =
            StartupPlan::system_guard_at(&root, &launchd, Operation::Uninstall, false).unwrap();
        remove
            .apply_with(|command| {
                Ok(CommandResult {
                    success: command.args[0] != "print",
                    stdout: String::new(),
                    stderr: String::new(),
                })
            })
            .unwrap();
        assert!(!plan.path.exists());
        fs::write(&plan.path, b"external job").unwrap();
        assert!(StartupPlan::system_guard_at(&root, &launchd, Operation::Install, false).is_err());
    }
    #[cfg(target_os = "macos")]
    #[test]
    fn system_guard_refuses_a_user_owned_payload() {
        let directory = tempfile::tempdir().unwrap();
        assert!(verify_root_owned(directory.path()).is_err());
    }
    #[test]
    fn platform_plans_install_without_starting_then_remove_only_owned_entries() {
        for platform in [Platform::Macos, Platform::Linux, Platform::Windows] {
            let root = tempfile::tempdir().unwrap();
            let manifest = bundle(root.path(), platform);
            let startup = root.path().join("startup");
            let user = if platform == Platform::Macos {
                "501"
            } else {
                "S-1-5-21-123"
            };
            let plan = StartupPlan::new(
                &manifest,
                Job::Service,
                Operation::Install,
                false,
                &startup,
                user,
            )
            .unwrap();
            assert!(!startup.exists());
            assert!(!plan.commands.iter().any(|c| {
                c.args
                    .iter()
                    .any(|a| ["bootstrap", "restart", "/Run"].contains(&a.as_str()))
            }));
            let mut calls = Vec::new();
            let result = plan
                .apply_with(|command| {
                    calls.push(command.clone());
                    Ok(if command == &plan.probe {
                        CommandResult {
                            success: false,
                            stdout: String::new(),
                            stderr: String::new(),
                        }
                    } else {
                        success(String::new())
                    })
                })
                .unwrap();
            assert!(result.installed);
            assert!(!result.start_requested);
            assert_eq!(fs::read_to_string(&plan.path).unwrap(), plan.contents);
            let remove = StartupPlan::new(
                &manifest,
                Job::Service,
                Operation::Uninstall,
                false,
                &startup,
                user,
            )
            .unwrap();
            remove
                .apply_with(|command| {
                    Ok(success(
                        if command == &remove.probe && platform == Platform::Windows {
                            plan.contents.clone()
                        } else {
                            String::new()
                        },
                    ))
                })
                .unwrap();
            assert!(!plan.path.exists());
        }
    }
    #[test]
    fn edited_entries_and_changed_plans_are_preserved() {
        let root = tempfile::tempdir().unwrap();
        let manifest = bundle(root.path(), Platform::Macos);
        let startup = root.path().join("startup");
        fs::create_dir(&startup).unwrap();
        let plan = StartupPlan::new(
            &manifest,
            Job::Tray,
            Operation::Install,
            true,
            &startup,
            "501",
        )
        .unwrap();
        fs::write(&plan.path, "unrelated").unwrap();
        assert!(
            plan.apply_with(|_| panic!("no startup command may execute"))
                .is_err()
        );
        assert!(
            StartupPlan::new(
                &manifest,
                Job::Tray,
                Operation::Uninstall,
                true,
                &startup,
                "501"
            )
            .is_err()
        );
        assert_eq!(fs::read_to_string(&plan.path).unwrap(), "unrelated");
    }
    #[test]
    fn windows_task_validation_rejects_changed_actions_users_and_privileges() {
        let root = tempfile::tempdir().unwrap();
        let manifest = bundle(root.path(), Platform::Windows);
        let startup = root.path().join("startup");
        fs::create_dir(&startup).unwrap();
        let plan = StartupPlan::new(
            &manifest,
            Job::Service,
            Operation::Install,
            true,
            &startup,
            "S-1-5-21-123",
        )
        .unwrap();
        assert!(task_matches(&plan.contents, &plan.contents));
        for changed in [
            plan.contents.replace("LeastPrivilege", "HighestAvailable"),
            plan.contents.replace("S-1-5-21-123", "S-1-5-18"),
            plan.contents.replace("<Command>", "<Command>other"),
            plan.contents.replace(
                "</Actions>",
                "<Exec><Command>other</Command></Exec></Actions>",
            ),
        ] {
            assert!(!task_matches(&changed, &plan.contents));
            assert!(
                plan.apply_with(|command| {
                    assert_eq!(command, &plan.probe);
                    Ok(success(changed.clone()))
                })
                .is_err()
            );
            assert!(!plan.path.exists());
        }
        assert_eq!(windows_quote("a\\"), "\"a\\\\\"");
        assert_eq!(windows_quote("a\"b"), "\"a\\\"b\"");
        assert_eq!(decode(vec![0xff, 0xfe, b'a', 0, b'b', 0]), "ab");
    }
    #[test]
    fn a_failed_manager_keeps_the_owned_file_for_an_explicit_retry() {
        let root = tempfile::tempdir().unwrap();
        let manifest = bundle(root.path(), Platform::Linux);
        let startup = root.path().join("startup");
        let plan = StartupPlan::new(
            &manifest,
            Job::Service,
            Operation::Install,
            true,
            &startup,
            "user",
        )
        .unwrap();
        assert!(
            plan.apply_with(|command| Ok(CommandResult {
                success: false,
                stdout: String::new(),
                stderr: if command == &plan.probe {
                    String::new()
                } else {
                    "manager unavailable".into()
                }
            }))
            .is_err()
        );
        assert_eq!(fs::read_to_string(&plan.path).unwrap(), plan.contents);
    }
    #[cfg(unix)]
    #[test]
    fn startup_symlinks_are_preserved() {
        let root = tempfile::tempdir().unwrap();
        let manifest = bundle(root.path(), Platform::Macos);
        let startup = root.path().join("startup");
        fs::create_dir(&startup).unwrap();
        let plan = StartupPlan::new(
            &manifest,
            Job::Service,
            Operation::Install,
            false,
            &startup,
            "501",
        )
        .unwrap();
        let other = root.path().join("other");
        fs::write(&other, "keep").unwrap();
        std::os::unix::fs::symlink(&other, &plan.path).unwrap();
        assert!(
            StartupPlan::new(
                &manifest,
                Job::Service,
                Operation::Install,
                false,
                &startup,
                "501"
            )
            .is_err()
        );
        assert_eq!(fs::read_to_string(other).unwrap(), "keep");
    }
}
