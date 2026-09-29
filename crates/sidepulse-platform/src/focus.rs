//! Native terminal reuse adapters; never executes during tests.
use crate::{DesktopPlatform, LaunchPlan, applescript_quote};
use sidepulse_core::{SessionTarget, TerminalSessionHints};
use std::{
    io::{self, Read, Seek, SeekFrom},
    process::{Command, Stdio},
    time::{Duration, Instant},
};
fn contains(variables: &[&str], terms: &[String]) -> String {
    let clauses: Vec<_> = variables
        .iter()
        .flat_map(|variable| {
            terms
                .iter()
                .map(move |term| format!("{variable} contains {}", applescript_quote(term)))
        })
        .collect();
    if clauses.is_empty() {
        "false".into()
    } else {
        clauses.join(" or ")
    }
}
fn selected_terminal<'a>(terminal: &'a str, custom: &str) -> &'a str {
    if terminal != "custom" {
        return terminal;
    }
    let name = custom
        .trim_end_matches('/')
        .rsplit('/')
        .next()
        .unwrap_or("")
        .to_lowercase();
    if name == "terminal.app" {
        "terminal"
    } else if name.contains("iterm") {
        "iterm"
    } else if name.contains("ghost") {
        "ghostty"
    } else {
        "custom"
    }
}
pub fn plan(
    platform: DesktopPlatform,
    target: &SessionTarget,
    terminal: &str,
    custom: &str,
) -> Option<LaunchPlan> {
    if platform != DesktopPlatform::Macos {
        return None;
    }
    let SessionTarget::Terminal {
        hints: Some(hints), ..
    } = target
    else {
        return None;
    };
    if [
        &hints.session_id,
        &hints.title,
        &hints.cwd,
        &hints.match_title,
    ]
    .iter()
    .any(|value| value.len() > 4096 || value.chars().any(char::is_control))
    {
        return None;
    }
    let terminal = selected_terminal(terminal, custom);
    let terms = hints.match_terms(terminal == "ghostty");
    if terms.is_empty() {
        return None;
    }
    let script = match terminal {
        "terminal" => {
            let condition = contains(&["tabTitle", "tabText"], &terms);
            format!(
                "if application \"Terminal\" is not running then return \"0\"\ntell application \"Terminal\"\nrepeat with windowRef in windows\nrepeat with tabRef in tabs of windowRef\nset tabTitle to \"\"\nset tabText to \"\"\ntry\nset tabTitle to custom title of tabRef\nend try\ntry\nset tabText to contents of tabRef\nend try\nif {condition} then\nset selected tab of windowRef to tabRef\nset index of windowRef to 1\nactivate\nreturn \"1\"\nend if\nend repeat\nend repeat\nend tell\nreturn \"0\""
            )
        }
        "iterm" => {
            let condition = contains(&["sessionName", "sessionText"], &terms);
            format!(
                "if application \"iTerm\" is not running then return \"0\"\ntell application \"iTerm\"\nrepeat with windowRef in windows\nrepeat with tabRef in tabs of windowRef\nrepeat with sessionRef in sessions of tabRef\nset sessionName to \"\"\nset sessionText to \"\"\ntry\nset sessionName to name of sessionRef\nend try\ntry\nset sessionText to contents of sessionRef\nend try\nif {condition} then\ntell windowRef to select tabRef\nselect sessionRef\nset index of windowRef to 1\nactivate\nreturn \"1\"\nend if\nend repeat\nend repeat\nend repeat\nend tell\nreturn \"0\""
            )
        }
        "ghostty" => ghostty_script(hints, &terms),
        _ => return None,
    };
    Some(LaunchPlan {
        executable: "/usr/bin/osascript".into(),
        args: vec!["-e".into(), script],
        cwd: None,
        command_file: None,
    })
}
fn ghostty_script(hints: &TerminalSessionHints, terms: &[String]) -> String {
    let condition = contains(&["windowName", "tabName", "terminalName"], terms);
    let weak = if hints.match_title.trim().is_empty() {
        "false".into()
    } else {
        contains(
            &["windowName", "tabName", "terminalName"],
            std::slice::from_ref(&hints.match_title),
        )
    };
    // A working directory alone must never select a different Ghostty session.
    // Bare prompt titles are accepted only when exactly one surface matches.
    format!(
        "if application \"Ghostty\" is not running then return \"0\"\ntell application \"Ghostty\"\nset matches to {{}}\nrepeat with windowRef in windows\nset windowName to name of windowRef\nrepeat with tabRef in tabs of windowRef\nset tabName to name of tabRef\nrepeat with terminalRef in terminals of tabRef\nset terminalName to name of terminalRef\nif {condition} then\nselect tab tabRef\nfocus terminalRef\nactivate window windowRef\nactivate\nreturn \"1\"\nend if\nif {weak} then set end of matches to {{windowRef, tabRef, terminalRef}}\nend repeat\nend repeat\nend repeat\nif (count of matches) is 1 then\nset matchRefs to item 1 of matches\nselect tab (item 2 of matchRefs)\nfocus (item 3 of matchRefs)\nactivate window (item 1 of matchRefs)\nactivate\nreturn \"1\"\nend if\nend tell\nreturn \"0\""
    )
}
pub(crate) fn run(plan: &LaunchPlan) -> io::Result<bool> {
    let mut output = tempfile::tempfile()?;
    let mut child = Command::new(&plan.executable)
        .args(&plan.args)
        .stdin(Stdio::null())
        .stdout(output.try_clone()?)
        .stderr(Stdio::null())
        .spawn()?;
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        if let Some(status) = child.try_wait()? {
            if !status.success() {
                return Ok(false);
            }
            output.seek(SeekFrom::Start(0))?;
            let mut text = String::new();
            output.take(1024).read_to_string(&mut text)?;
            return Ok(text.trim() == "1");
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Ok(false);
        }
        std::thread::sleep(Duration::from_millis(25));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn target() -> SessionTarget {
        SessionTarget::Terminal {
            executable: "grok".into(),
            args: vec!["--resume".into(), "grok-session".into()],
            cwd: "/tmp/shared project".into(),
            hints: Some(TerminalSessionHints {
                session_id: "grok-session".into(),
                cwd: "/tmp/shared project".into(),
                title: "Grok all good shared project".into(),
                match_title: "all good".into(),
            }),
        }
    }
    #[test]
    fn captured_terminal_iterm_and_ghostty_reuse_rules_are_preserved() {
        for (terminal, token) in [
            ("terminal", "selected tab of windowRef"),
            ("iterm", "select sessionRef"),
            ("ghostty", "focus terminalRef"),
        ] {
            let plan = plan(DesktopPlatform::Macos, &target(), terminal, "").unwrap();
            let script = &plan.args[1];
            assert!(script.contains("grok-session"));
            assert!(script.contains(token));
            assert!(!script.contains("do script"));
            if terminal == "ghostty" {
                assert!(!script.contains("/tmp/shared project"));
                assert!(script.contains("(count of matches) is 1"));
            }
        }
        assert!(plan(DesktopPlatform::Windows, &target(), "terminal", "").is_none());
        assert!(
            plan(
                DesktopPlatform::Macos,
                &target(),
                "custom",
                "/Applications/Ghostty.app"
            )
            .is_some()
        );
    }
    #[test]
    fn arbitrary_titles_remain_quoted_data_and_control_characters_disable_reuse() {
        let mut target = target();
        let SessionTarget::Terminal {
            hints: Some(hints), ..
        } = &mut target
        else {
            panic!()
        };
        hints.title = "title\" & do shell script \"unexpected".into();
        assert!(
            plan(DesktopPlatform::Macos, &target, "terminal", "")
                .unwrap()
                .args[1]
                .contains("title\\\" & do shell script \\\"unexpected")
        );
        let SessionTarget::Terminal {
            hints: Some(hints), ..
        } = &mut target
        else {
            panic!()
        };
        hints.title.push('\u{1b}');
        assert!(plan(DesktopPlatform::Macos, &target, "terminal", "").is_none());
    }
}
