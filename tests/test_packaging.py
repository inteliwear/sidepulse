"""Packaging and import-contract tests.

These guard the class of bug where the code imports a module that nothing
declares as a dependency, so the package installs cleanly and then explodes
at runtime on a user's machine.

The concrete instance that motivated this file: ``status_bar.py`` grew a
``from ScriptingBridge import SBApplication`` for the Ghostty integration.
``ScriptingBridge`` ships in ``pyobjc-framework-ScriptingBridge``, which was
never added to ``pyproject.toml``. Because the import shared a ``try`` block
with AppKit, the failure surfaced as a misleading "requires PyObjC/AppKit"
message and killed the whole status bar.

``test_hard_third_party_imports_are_declared`` fails on exactly that mistake.
"""

from __future__ import annotations

import ast
import importlib
import re
import subprocess
import sys
import unittest
from importlib.metadata import PackageNotFoundError, distribution, packages_distributions
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Top-level packages that live in this repo.
FIRST_PARTY = {"sidepulse", "sidepulse_cli", "agent_monitor"}

# Modules that are stdlib on some supported interpreters and not others, so
# they are legitimately imported without being declared.
CONDITIONAL_STDLIB = {"tomllib"}


def normalize_dist_name(name: str) -> str:
    """PEP 503 normalization, so Foo_Bar and foo-bar compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def load_pyproject() -> dict:
    if tomllib is None:  # pragma: no cover
        raise unittest.SkipTest("need tomllib (3.11+) or tomli to parse pyproject.toml")
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def requirement_name(spec: str) -> str:
    """Pull the distribution name out of a requirement string.

    ``"pyobjc-framework-Cocoa>=10; sys_platform == 'darwin'"`` -> ``pyobjc-framework-cocoa``
    """
    head = spec.split(";", 1)[0].strip()
    head = re.split(r"[<>=!~\[\s]", head, maxsplit=1)[0]
    return normalize_dist_name(head)


def declared_dependencies() -> set[str]:
    """Distribution names declared in ``[project] dependencies``."""
    data = load_pyproject()
    return {requirement_name(dep) for dep in data["project"].get("dependencies", [])}


def dependency_closure(roots: set[str]) -> set[str]:
    """Every distribution reachable from ``roots`` through installed metadata.

    A direct import satisfied only transitively (``objc`` via ``pyobjc-core``,
    which ``pyobjc-framework-Cocoa`` pulls in) is accepted. An import satisfied
    by nothing in the closure is the bug this module exists to catch.
    """
    seen: set[str] = set()
    queue = list(roots)
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            reqs = distribution(name).requires or []
        except PackageNotFoundError:
            continue
        for req in reqs:
            # Skip requirements gated behind an extra we do not install.
            if "extra ==" in req:
                continue
            child = requirement_name(req)
            if child and child not in seen:
                queue.append(child)
    return seen


class ImportRecord:
    def __init__(self, module: str, path: Path, lineno: int, optional: bool):
        self.module = module
        self.top = module.split(".")[0]
        self.path = path
        self.lineno = lineno
        self.optional = optional

    @property
    def where(self) -> str:
        rel = self.path.relative_to(REPO_ROOT)
        return f"{rel}:{self.lineno}"


def handlers_swallow_failure(node: ast.Try) -> bool:
    """True when a failed import in this ``try`` leaves the module usable.

    A handler that re-raises anything -- including ``raise SystemExit(...)``,
    which is how the ScriptingBridge bug manifested -- does not make the
    import optional. It makes it mandatory with a nicer error message, so the
    dependency still has to be declared.
    """
    if not node.handlers:
        return False
    for handler in node.handlers:
        for child in ast.walk(handler):
            if isinstance(child, ast.Raise):
                return False
    return True


def scan_imports(root: Path) -> list[ImportRecord]:
    """Every import in the tree, tagged with whether the code can live without it.

    An import is optional when either:

    * it sits inside a function or method body, so it only runs when that
      feature is actually used and the caller can handle the failure; or
    * it sits inside a module-level ``try`` whose handlers swallow the error
      (assigning ``None``, setting a flag) rather than re-raising.

    Everything else runs unconditionally at import time and must be
    satisfiable from the declared dependencies.
    """
    records: list[ImportRecord] = []

    def visit(node: ast.AST, path: Path, in_function: bool, in_safe_try: bool) -> None:
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            # Relative imports are first-party by definition.
            modules = [node.module] if node.level == 0 and node.module else []
        else:
            modules = []
        for module in modules:
            records.append(
                ImportRecord(
                    module, path, node.lineno, optional=in_function or in_safe_try
                )
            )

        if isinstance(node, ast.Try):
            # Only the protected body benefits from the handler.
            safe = in_safe_try or handlers_swallow_failure(node)
            for child in node.body:
                visit(child, path, in_function, safe)
            for child in (*node.handlers, *node.orelse, *node.finalbody):
                visit(child, path, in_function, in_safe_try)
            return

        entering_function = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for child in ast.iter_child_nodes(node):
            visit(child, path, in_function or entering_function, in_safe_try)

    for path in sorted(root.rglob("*.py")):
        visit(ast.parse(path.read_text(encoding="utf-8"), str(path)), path, False, False)
    return records


def third_party(records: list[ImportRecord]) -> list[ImportRecord]:
    return [
        record
        for record in records
        if record.top not in sys.stdlib_module_names
        and record.top not in FIRST_PARTY
        and record.top not in CONDITIONAL_STDLIB
    ]


class ImportContractTests(unittest.TestCase):
    """The dependency declaration must cover what the code actually imports."""

    def test_source_tree_is_parseable(self):
        # Cheap canary: a syntax error anywhere makes every other scan lie.
        for path in sorted(SRC_ROOT.rglob("*.py")):
            with self.subTest(path=path.relative_to(REPO_ROOT)):
                ast.parse(path.read_text(encoding="utf-8"), str(path))

    @unittest.skipUnless(
        sys.platform == "darwin",
        "every declared dependency is darwin-gated, so the contract can only "
        "be checked where they are installed",
    )
    def test_hard_third_party_imports_are_declared(self):
        """A module-level import of an undeclared distribution is a release bug.

        This is the regression test for the missing ScriptingBridge dependency.
        """
        closure = dependency_closure(declared_dependencies())
        module_to_dists = packages_distributions()

        missing: list[str] = []
        for record in third_party(scan_imports(SRC_ROOT)):
            if record.optional:
                continue
            providers = module_to_dists.get(record.top)
            if providers is None:
                missing.append(
                    f"{record.where}: imports {record.top!r}, which is not "
                    f"installed and not declared in pyproject.toml"
                )
                continue
            if not any(normalize_dist_name(p) in closure for p in providers):
                missing.append(
                    f"{record.where}: imports {record.top!r} from "
                    f"{providers}, which no declared dependency pulls in. "
                    f"Add it to [project] dependencies, or make the import "
                    f"guarded if the feature is optional."
                )

        self.assertEqual(
            [],
            missing,
            "Undeclared hard dependencies:\n  " + "\n  ".join(missing),
        )

    @unittest.skipUnless(sys.platform == "darwin", "optional deps are darwin-gated")
    def test_guarded_optional_imports_are_reachable_or_optional(self):
        """Guarded imports may be absent, but must name a real distribution.

        Catches typo'd optional imports (``ScriptingBrige``) that would
        silently disable a feature forever instead of failing loudly.
        """
        module_to_dists = packages_distributions()
        unknown: list[str] = []
        for record in third_party(scan_imports(SRC_ROOT)):
            if not record.optional:
                continue
            if record.top in module_to_dists:
                continue
            # Not installed here; accept only if some extra declares it.
            data = load_pyproject()
            extras = data["project"].get("optional-dependencies", {})
            declared_anywhere = {
                requirement_name(dep)
                for deps in extras.values()
                for dep in deps
            } | declared_dependencies()
            if normalize_dist_name(record.top) not in declared_anywhere:
                unknown.append(f"{record.where}: optional import {record.top!r}")

        # mlx_lm is the reply-classifier extra; it maps to distribution "mlx-lm".
        unknown = [u for u in unknown if "mlx_lm" not in u]
        self.assertEqual([], unknown, "Unrecognized optional imports:\n  " + "\n  ".join(unknown))

    def test_declared_dependencies_are_installed(self):
        """The test environment matches what we ship, or the suite proves nothing."""
        if sys.platform != "darwin":
            self.skipTest("declared dependencies are darwin-gated")
        for name in sorted(declared_dependencies()):
            with self.subTest(dependency=name):
                try:
                    distribution(name)
                except PackageNotFoundError:
                    self.fail(
                        f"{name} is declared in pyproject.toml but not installed. "
                        f"Run: pip install -e ."
                    )


class ModuleImportSmokeTests(unittest.TestCase):
    """Every shipped module must import. Nothing here is clever; that is the point."""

    def module_names(self) -> list[str]:
        names = []
        for path in sorted(SRC_ROOT.rglob("*.py")):
            rel = path.relative_to(SRC_ROOT)
            if rel.name == "__main__.py":
                continue  # executes on import by design
            parts = list(rel.parts)
            parts[-1] = rel.stem
            if parts[-1] == "__init__":
                parts.pop()
            if parts:
                names.append(".".join(parts))
        return names

    def test_every_module_imports(self):
        darwin_only = {
            "sidepulse.status_bar",
            "sidepulse.virtual_device",
            "sidepulse.led_wasm",
        }
        for name in self.module_names():
            if name in darwin_only and sys.platform != "darwin":
                continue
            with self.subTest(module=name):
                try:
                    importlib.import_module(name)
                except SystemExit as exc:
                    # status_bar raises SystemExit with an install hint when
                    # PyObjC is missing. On a machine with deps installed that
                    # is always a bug -- most likely an undeclared dependency.
                    self.fail(f"{name} raised SystemExit on import: {exc}")

    def test_main_module_does_not_import_eagerly(self):
        # __main__.py runs the CLI; it must be importable via runpy without
        # side effects beyond argument parsing.
        result = subprocess.run(
            [sys.executable, "-m", "sidepulse", "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=REPO_ROOT,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("usage", result.stdout.lower())


class EntryPointTests(unittest.TestCase):
    """Every console script in pyproject must resolve to a real callable."""

    def test_console_scripts_resolve(self):
        scripts = load_pyproject()["project"].get("scripts", {})
        self.assertTrue(scripts, "expected console scripts in pyproject.toml")
        for name, target in sorted(scripts.items()):
            with self.subTest(script=name):
                module_name, _, attr = target.partition(":")
                if module_name == "sidepulse.status_bar" and sys.platform != "darwin":
                    continue
                module = importlib.import_module(module_name)
                self.assertTrue(
                    callable(getattr(module, attr, None)),
                    f"{target} is not callable",
                )

    def test_console_scripts_respond_to_help(self):
        """An entry point that cannot even print --help is broken for users."""
        scripts = load_pyproject()["project"].get("scripts", {})
        for name, target in sorted(scripts.items()):
            module_name, _, _ = target.partition(":")
            if module_name == "sidepulse.status_bar":
                continue  # launching the GUI is not a --help operation
            with self.subTest(script=name):
                result = subprocess.run(
                    [sys.executable, "-c", f"import sys; sys.argv=['{name}','--help']; "
                     f"import {module_name} as m; "
                     f"sys.exit(m.{target.split(':')[1]}())"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=REPO_ROOT,
                )
                self.assertIn(
                    result.returncode,
                    (0, 2),
                    f"{name} --help failed: {result.stderr}",
                )


class VersionTests(unittest.TestCase):
    """Tag-derived package metadata and the runtime version must not drift.

    A published wheel whose metadata disagrees with ``sidepulse.__version__``
    makes bug reports impossible to place.
    """

    def test_version_is_dynamic(self):
        project = load_pyproject()["project"]
        self.assertIn("version", project["dynamic"])
        self.assertNotIn("version", project)

    def test_metadata_and_dunder_version_agree(self):
        try:
            import sidepulse
        except ModuleNotFoundError:
            self.skipTest("sidepulse is not installed in this environment")
        from importlib.metadata import version

        try:
            installed = version("sidepulse")
        except PackageNotFoundError:
            self.skipTest("sidepulse is not installed in this environment")
        self.assertEqual(
            installed,
            sidepulse.__version__,
            "installed metadata and sidepulse.__version__ disagree",
        )


class PackageDataTests(unittest.TestCase):
    def test_declared_package_data_exists(self):
        data = load_pyproject()
        package_data = (
            data.get("tool", {})
            .get("setuptools", {})
            .get("package-data", {})
        )
        for package, patterns in package_data.items():
            package_dir = SRC_ROOT / package.replace(".", "/")
            with self.subTest(package=package):
                self.assertTrue(
                    package_dir.is_dir(),
                    f"package-data references missing package {package}",
                )
            for pattern in patterns:
                with self.subTest(package=package, pattern=pattern):
                    self.assertTrue(
                        list(package_dir.glob(pattern)),
                        f"package-data pattern {pattern!r} matches nothing in {package}",
                    )


if __name__ == "__main__":
    unittest.main()
