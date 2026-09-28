//! Desktop activation adapters. Session policy belongs to sidepulse-core.

use std::io;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use base64::Engine;
use sidepulse_core::SessionTarget;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DesktopPlatform {
    Macos,
    Windows,
    Linux,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LaunchPlan {
    pub executable: String,
    pub args: Vec<String>,
    pub cwd: Option<String>,
    pub command_file: Option<String>,
}

pub fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}
fn applescript_quote(value: &str) -> String {
    format!("\"{}\"", value.replace('\\', "\\\\").replace('"', "\\\""))
}

pub fn resume_command(executable: &str, args: &[String], cwd: &str) -> String {
    format!(
        "cd {} && {}",
        shell_quote(cwd),
        std::iter::once(executable)
            .chain(args.iter().map(String::as_str))
            .map(shell_quote)
            .collect::<Vec<_>>()
            .join(" ")
    )
}

pub fn launch_plan(
    platform: DesktopPlatform,
    target: &SessionTarget,
    terminal: &str,
    custom_path: &str,
) -> io::Result<LaunchPlan> {
    match target {
        SessionTarget::Url { url } => {
            if ![
                "codex://threads/",
                "claude://",
                "vscode://anthropic.claude-code/open?session=",
            ]
            .iter()
            .any(|prefix| url.starts_with(prefix))
                || url.contains('\0')
            {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "invalid session URL",
                ));
            }
            let (executable, args) = match platform {
                DesktopPlatform::Macos => ("/usr/bin/open", vec![url.clone()]),
                DesktopPlatform::Linux => ("xdg-open", vec![url.clone()]),
                DesktopPlatform::Windows => (
                    "rundll32.exe",
                    vec!["url.dll,FileProtocolHandler".into(), url.clone()],
                ),
            };
            Ok(LaunchPlan {
                executable: executable.into(),
                args,
                cwd: None,
                command_file: None,
            })
        }
        SessionTarget::Terminal {
            executable,
            args,
            cwd,
        } => {
            if !matches!(executable.as_str(), "codex" | "claude" | "grok" | "junie")
                || args
                    .iter()
                    .chain(std::iter::once(cwd))
                    .any(|arg| arg.contains('\0'))
            {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "invalid session command",
                ));
            }
            match platform {
                DesktopPlatform::Windows => {
                    if terminal != "terminal" {
                        return Err(io::Error::new(
                            io::ErrorKind::Unsupported,
                            "select the default terminal for session opening on Windows",
                        ));
                    }
                    // Encoding isolates every session value from both wt's semicolon
                    // commands and PowerShell parsing. Quoted literals cannot expand.
                    let quote = |value: &str| format!("'{}'", value.replace('\'', "''"));
                    let script = format!(
                        "Set-Location -LiteralPath {}; & {}",
                        quote(cwd),
                        std::iter::once(executable.as_str())
                            .chain(args.iter().map(String::as_str))
                            .map(quote)
                            .collect::<Vec<_>>()
                            .join(" ")
                    );
                    let bytes: Vec<_> = script.encode_utf16().flat_map(u16::to_le_bytes).collect();
                    let encoded = base64::engine::general_purpose::STANDARD.encode(bytes);
                    Ok(LaunchPlan {
                        executable: "wt.exe".into(),
                        args: vec![
                            "new-tab".into(),
                            "powershell.exe".into(),
                            "-NoExit".into(),
                            "-EncodedCommand".into(),
                            encoded,
                        ],
                        cwd: None,
                        command_file: None,
                    })
                }
                DesktopPlatform::Linux => {
                    let (program, prefix) = match terminal {
                        "terminal" => ("x-terminal-emulator", vec!["-e".into()]),
                        "ghostty" => (
                            "ghostty",
                            vec![format!("--working-directory={cwd}"), "-e".into()],
                        ),
                        "kitty" => ("kitty", vec!["--directory".into(), cwd.clone()]),
                        "wezterm" => (
                            "wezterm",
                            vec!["start".into(), "--cwd".into(), cwd.clone(), "--".into()],
                        ),
                        "alacritty" => (
                            "alacritty",
                            vec!["--working-directory".into(), cwd.clone(), "-e".into()],
                        ),
                        "custom" if !custom_path.trim().is_empty() => {
                            (custom_path, vec!["-e".into()])
                        }
                        _ => {
                            return Err(io::Error::new(
                                io::ErrorKind::Unsupported,
                                "this terminal is not available on Linux",
                            ));
                        }
                    };
                    Ok(LaunchPlan {
                        executable: program.into(),
                        args: prefix
                            .into_iter()
                            .chain(std::iter::once(executable.clone()))
                            .chain(args.iter().cloned())
                            .collect(),
                        cwd: Some(cwd.clone()),
                        command_file: None,
                    })
                }
                DesktopPlatform::Macos => {
                    let command = resume_command(executable, args, cwd);
                    if matches!(terminal, "warp" | "custom") {
                        let app = if terminal == "warp" {
                            "Warp"
                        } else if !custom_path.trim().is_empty() {
                            custom_path
                        } else {
                            return Err(io::Error::new(
                                io::ErrorKind::InvalidInput,
                                "custom terminal path is empty",
                            ));
                        };
                        return Ok(LaunchPlan {
                            executable: "/usr/bin/open".into(),
                            args: vec![
                                "-a".into(),
                                app.into(),
                                "__SIDEPULSE_COMMAND_FILE__".into(),
                            ],
                            cwd: None,
                            command_file: Some(format!("#!/bin/zsh\nrm -f -- \"$0\"\n{command}\n")),
                        });
                    }
                    let script = match terminal {
                        "terminal" => format!(
                            "tell application \"Terminal\"\nactivate\ndo script {}\nend tell",
                            applescript_quote(&command)
                        ),
                        "iterm" => format!(
                            "tell application \"iTerm\"\nactivate\nif (count of windows) = 0 then\ncreate window with default profile command {}\nelse\ntell current window to create tab with default profile command {}\nend if\nend tell",
                            applescript_quote(&command),
                            applescript_quote(&command)
                        ),
                        "ghostty" => format!(
                            "tell application \"Ghostty\"\nactivate\nset cfg to new surface configuration\nset initial working directory of cfg to {}\nset command of cfg to {}\nnew window with configuration cfg\nend tell",
                            applescript_quote(cwd),
                            applescript_quote(&command)
                        ),
                        _ => {
                            let app = match terminal {
                                "kitty" => "kitty",
                                "wezterm" => "WezTerm",
                                "alacritty" => "Alacritty",
                                _ => {
                                    return Err(io::Error::new(
                                        io::ErrorKind::InvalidInput,
                                        "invalid terminal preference",
                                    ));
                                }
                            };
                            let mut launch_args =
                                vec!["-n".into(), "-a".into(), app.into(), "--args".into()];
                            if terminal == "wezterm" {
                                launch_args.extend([
                                    "start".into(),
                                    "--new-tab".into(),
                                    "--".into(),
                                ]);
                            } else {
                                launch_args.push("-e".into());
                            }
                            launch_args.extend(["/bin/zsh".into(), "-lc".into(), command]);
                            return Ok(LaunchPlan {
                                executable: "/usr/bin/open".into(),
                                args: launch_args,
                                cwd: None,
                                command_file: None,
                            });
                        }
                    };
                    Ok(LaunchPlan {
                        executable: "/usr/bin/osascript".into(),
                        args: vec!["-e".into(), script],
                        cwd: None,
                        command_file: None,
                    })
                }
            }
        }
    }
}

pub fn open_session(target: &SessionTarget, terminal: &str, custom_path: &str) -> io::Result<()> {
    let platform = if cfg!(target_os = "macos") {
        DesktopPlatform::Macos
    } else if cfg!(windows) {
        DesktopPlatform::Windows
    } else {
        DesktopPlatform::Linux
    };
    let plan = launch_plan(platform, target, terminal, custom_path)?;
    let command_file = if let Some(contents) = &plan.command_file {
        use std::io::Write;
        let mut file = tempfile::Builder::new()
            .prefix("sidepulse-resume-")
            .suffix(".command")
            .tempfile()?;
        file.write_all(contents.as_bytes())?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            file.as_file()
                .set_permissions(std::fs::Permissions::from_mode(0o700))?;
        }
        Some(file.into_temp_path().keep().map_err(|error| error.error)?)
    } else {
        None
    };
    let args: Vec<_> = plan
        .args
        .iter()
        .map(|argument| {
            if argument == "__SIDEPULSE_COMMAND_FILE__" {
                command_file
                    .as_ref()
                    .unwrap()
                    .to_string_lossy()
                    .into_owned()
            } else {
                argument.clone()
            }
        })
        .collect();
    let mut command = Command::new(&plan.executable);
    command
        .args(&args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    if let Some(cwd) = &plan.cwd {
        command.current_dir(cwd);
    }
    let mut child = match command.spawn() {
        Ok(child) => child,
        #[cfg(windows)]
        Err(error) if error.kind() == io::ErrorKind::NotFound && plan.executable == "wt.exe" => {
            use std::os::windows::process::CommandExt;
            Command::new("powershell.exe")
                .args(&plan.args[2..])
                .creation_flags(0x0000_0010)
                .spawn()?
        }
        Err(error) => {
            if let Some(path) = &command_file {
                let _ = std::fs::remove_file(path);
            }
            return Err(error);
        }
    };
    let deadline = Instant::now() + Duration::from_millis(300);
    loop {
        if let Some(status) = child.try_wait()? {
            return if status.success() {
                Ok(())
            } else {
                if let Some(path) = &command_file {
                    let _ = std::fs::remove_file(path);
                }
                Err(io::Error::other(format!(
                    "{} could not open the session",
                    plan.executable
                )))
            };
        }
        if Instant::now() >= deadline {
            std::thread::spawn(move || {
                let _ = child.wait();
            });
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn target() -> SessionTarget {
        SessionTarget::Terminal {
            executable: "codex".into(),
            args: vec!["resume".into(), "a'; Write-Host oops; &'雪".into()],
            cwd: "/tmp/project's folder".into(),
        }
    }

    #[test]
    fn terminal_adapters_preserve_hostile_values_as_data() {
        let target = target();
        let mac = launch_plan(DesktopPlatform::Macos, &target, "terminal", "").unwrap();
        assert_eq!(mac.executable, "/usr/bin/osascript");
        assert!(mac.args[1].contains("'a'\\\"'\\\"'; Write-Host oops; &"));
        let linux = launch_plan(DesktopPlatform::Linux, &target, "terminal", "").unwrap();
        assert_eq!(
            linux.args,
            vec!["-e", "codex", "resume", "a'; Write-Host oops; &'雪"]
        );
        let windows = launch_plan(DesktopPlatform::Windows, &target, "terminal", "").unwrap();
        let encoded = windows.args.last().unwrap();
        assert!(!encoded.contains(';'));
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(encoded)
            .unwrap();
        let units: Vec<_> = bytes
            .as_chunks::<2>()
            .0
            .iter()
            .map(|value| u16::from_le_bytes(*value))
            .collect();
        let decoded = String::from_utf16(&units).unwrap();
        assert_eq!(
            decoded,
            "Set-Location -LiteralPath '/tmp/project''s folder'; & 'codex' 'resume' 'a''; Write-Host oops; &''雪'"
        );
    }

    #[test]
    fn unsupported_terminals_and_unrecognized_urls_return_errors() {
        assert!(launch_plan(DesktopPlatform::Linux, &target(), "iterm", "").is_err());
        assert!(
            launch_plan(
                DesktopPlatform::Macos,
                &SessionTarget::Url {
                    url: "https://example.com".into()
                },
                "terminal",
                ""
            )
            .is_err()
        );
    }
}
