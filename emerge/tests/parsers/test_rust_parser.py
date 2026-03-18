"""
Unit tests for the Rust parser's crate:: resolution in Cargo workspace layouts.
"""

import tempfile
import unittest
from pathlib import Path

from emerge.languages.rustparser import RustParser
from emerge.analysis import Analysis


class RustParserFindCrateSrcDirTestCase(unittest.TestCase):
    """Test _find_crate_src_dir for workspace and single-crate projects."""

    def setUp(self):
        self.parser = RustParser()
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make(self, *parts, content=""):
        """Create a file inside self.tmpdir, creating parent dirs as needed."""
        path = Path(self.tmpdir, *parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return str(path)

    def test_workspace_member_crate(self):
        """crate:: in a workspace member should resolve to that member's src/."""
        workspace_root = Path(self.tmpdir) / "project"
        crate_src = workspace_root / "services" / "accounts" / "src"

        self._make("project", "Cargo.toml", content="[workspace]")
        self._make("project", "services", "accounts", "Cargo.toml", content="[package]")
        self._make("project", "services", "accounts", "src", "main.rs")
        file_path = self._make("project", "services", "accounts", "src", "api", "commands.rs")

        result = self.parser._find_crate_src_dir(file_path, str(workspace_root))
        self.assertEqual(result, str(crate_src))

    def test_single_crate_with_src(self):
        """Single crate where source_directory is the project root (has Cargo.toml + src/)."""
        project = Path(self.tmpdir) / "myproject"
        self._make("myproject", "Cargo.toml", content="[package]")
        file_path = self._make("myproject", "src", "lib.rs")

        result = self.parser._find_crate_src_dir(file_path, str(project))
        self.assertEqual(result, str(project / "src"))

    def test_fallback_to_analysis_source_dir(self):
        """Falls back to analysis_source_dir when no Cargo.toml is found."""
        project = Path(self.tmpdir) / "nocargo"
        file_path = self._make("nocargo", "src", "main.rs")

        result = self.parser._find_crate_src_dir(file_path, str(project))
        # Should fall back to src/ under the resolved analysis_source_dir
        self.assertEqual(result, str(project.resolve() / "src"))

    def test_fallback_no_src_dir(self):
        """Falls back to analysis_source_dir itself when neither Cargo.toml nor src/ exists."""
        project = Path(self.tmpdir) / "flat"
        file_path = self._make("flat", "main.rs")

        result = self.parser._find_crate_src_dir(file_path, str(project))
        self.assertEqual(result, str(project.resolve()))

    def test_does_not_escape_analysis_root(self):
        """Should not walk above the analysis source_directory."""
        workspace = Path(self.tmpdir) / "workspace"
        # Cargo.toml only at workspace level, not inside the member
        self._make("workspace", "Cargo.toml", content="[workspace]")
        member_src = (workspace / "member" / "src").resolve()
        file_path = self._make("workspace", "member", "src", "lib.rs")

        # analysis root is the member directory — should NOT find workspace Cargo.toml
        result = self.parser._find_crate_src_dir(file_path, str(workspace / "member"))
        self.assertEqual(result, str(member_src))


class RustParserEndToEndTestCase(unittest.TestCase):
    """End-to-end test: generate_file_result_from_analysis with a workspace layout."""

    def setUp(self):
        self.parser = RustParser()
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make(self, *parts, content=""):
        path = Path(self.tmpdir, *parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return str(path)

    def test_workspace_crate_use_resolved(self):
        """use crate::domain::account in a workspace member resolves to the correct file."""
        ws = Path(self.tmpdir) / "project"

        self._make("project", "Cargo.toml", content="[workspace]")
        self._make("project", "services", "accounts", "Cargo.toml", content="[package]")
        self._make("project", "services", "accounts", "src", "main.rs")
        self._make("project", "services", "accounts", "src", "domain", "mod.rs", content="pub mod account;")
        self._make("project", "services", "accounts", "src", "domain", "account.rs", content="pub struct Account;")
        self._make(
            "project", "services", "accounts", "src", "api", "commands.rs",
            content="use crate::domain::account::Account;\n"
        )

        analysis = Analysis()
        analysis.analysis_name = "test"
        analysis.source_directory = str(ws)

        # In production, the analyzer passes full_file_path as a path relative
        # to Path(source_directory).parent — mirror that format here.
        relative_commands_path = "project/services/accounts/src/api/commands.rs"

        self.parser.generate_file_result_from_analysis(
            analysis,
            file_name="commands.rs",
            full_file_path=relative_commands_path,
            file_content="use crate::domain::account::Account;\n",
        )

        results = self.parser.results
        self.assertTrue(results)
        result = list(results.values())[0]

        # The dependency should resolve to the account.rs inside the member crate
        self.assertTrue(
            len(result.scanned_import_dependencies) > 0,
            "Expected at least one dependency from use crate::domain::account"
        )
        dep = result.scanned_import_dependencies[0]
        self.assertIn("domain", dep)
        self.assertIn("account.rs", dep)
        # Must NOT be at workspace root level
        self.assertIn("services", dep)


class RustParserDetectWorkspaceMembersTestCase(unittest.TestCase):
    """Test _detect_workspace_members for various workspace configurations."""

    def setUp(self):
        self.parser = RustParser()
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make(self, *parts, content=""):
        path = Path(self.tmpdir, *parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return str(path)

    def test_basic_workspace_members(self):
        """Detects explicitly listed workspace members."""
        ws = Path(self.tmpdir) / "ws"
        self._make("ws", "Cargo.toml", content='[workspace]\nmembers = ["shared", "services/api"]')
        self._make("ws", "shared", "Cargo.toml", content='[package]\nname = "shared"')
        self._make("ws", "shared", "src", "lib.rs")
        self._make("ws", "services", "api", "Cargo.toml", content='[package]\nname = "api-service"')
        self._make("ws", "services", "api", "src", "main.rs")

        members = self.parser._detect_workspace_members(str(ws))
        self.assertIn("shared", members)
        self.assertIn("api_service", members)  # hyphen -> underscore
        self.assertTrue(members["shared"].endswith("src"))
        self.assertTrue(members["api_service"].endswith("src"))

    def test_glob_workspace_members(self):
        """Expands glob patterns in workspace members list."""
        ws = Path(self.tmpdir) / "ws"
        self._make("ws", "Cargo.toml", content='[workspace]\nmembers = ["services/*"]')
        self._make("ws", "services", "orders", "Cargo.toml", content='[package]\nname = "orders"')
        self._make("ws", "services", "orders", "src", "lib.rs")
        self._make("ws", "services", "payments", "Cargo.toml", content='[package]\nname = "payments"')
        self._make("ws", "services", "payments", "src", "lib.rs")

        members = self.parser._detect_workspace_members(str(ws))
        self.assertIn("orders", members)
        self.assertIn("payments", members)

    def test_hyphen_normalization(self):
        """Crate names with hyphens are normalized to underscores."""
        ws = Path(self.tmpdir) / "ws"
        self._make("ws", "Cargo.toml", content='[workspace]\nmembers = ["my-shared-lib"]')
        self._make("ws", "my-shared-lib", "Cargo.toml", content='[package]\nname = "my-shared-lib"')
        self._make("ws", "my-shared-lib", "src", "lib.rs")

        members = self.parser._detect_workspace_members(str(ws))
        self.assertIn("my_shared_lib", members)
        self.assertNotIn("my-shared-lib", members)

    def test_no_workspace_returns_empty(self):
        """Non-workspace project returns empty dict."""
        proj = Path(self.tmpdir) / "proj"
        self._make("proj", "Cargo.toml", content='[package]\nname = "myapp"')
        self._make("proj", "src", "main.rs")

        members = self.parser._detect_workspace_members(str(proj))
        self.assertEqual(members, {})

    def test_no_cargo_toml_returns_empty(self):
        """Directory with no Cargo.toml returns empty dict."""
        proj = Path(self.tmpdir) / "empty"
        self._make("empty", "src", "main.rs")

        members = self.parser._detect_workspace_members(str(proj))
        self.assertEqual(members, {})


class RustParserWorkspaceImportsTestCase(unittest.TestCase):
    """End-to-end tests for cross-crate workspace member imports."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make(self, *parts, content=""):
        path = Path(self.tmpdir, *parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return str(path)

    def _setup_workspace(self):
        """Create a standard workspace with shared + api crates."""
        ws = Path(self.tmpdir) / "project"
        self._make("project", "Cargo.toml",
                    content='[workspace]\nmembers = ["services/api", "shared"]')
        self._make("project", "shared", "Cargo.toml",
                    content='[package]\nname = "shared"')
        self._make("project", "shared", "src", "lib.rs")
        self._make("project", "shared", "src", "events.rs",
                    content="pub struct OrderCreated;")
        self._make("project", "shared", "src", "commands.rs",
                    content="pub struct CreateOrder;")
        self._make("project", "services", "api", "Cargo.toml",
                    content='[package]\nname = "api-service"')
        self._make("project", "services", "api", "src", "main.rs")
        return ws

    def _parse_file(self, ws, file_content):
        """Parse a file in services/api/src/main.rs and return its result."""
        parser = RustParser()
        analysis = Analysis()
        analysis.analysis_name = "test"
        analysis.source_directory = str(ws)

        parser.generate_file_result_from_analysis(
            analysis,
            file_name="main.rs",
            full_file_path="project/services/api/src/main.rs",
            file_content=file_content,
        )
        return list(parser.results.values())[0]

    def test_simple_workspace_import(self):
        """use shared::events::OrderCreated resolves to shared/src/events.rs."""
        ws = self._setup_workspace()
        result = self._parse_file(ws, "use shared::events::OrderCreated;\n")
        self.assertTrue(len(result.scanned_import_dependencies) > 0,
                        "Expected dependency from use shared::events::OrderCreated")
        dep = result.scanned_import_dependencies[0]
        self.assertIn("shared", dep)
        self.assertIn("events.rs", dep)

    def test_brace_group_workspace_import(self):
        """use shared::{events::OrderCreated, commands::CreateOrder} creates two deps."""
        ws = self._setup_workspace()
        result = self._parse_file(
            ws, "use shared::{events::OrderCreated, commands::CreateOrder};\n")
        self.assertEqual(len(result.scanned_import_dependencies), 2,
                         f"Expected 2 deps, got: {result.scanned_import_dependencies}")
        deps_str = " ".join(result.scanned_import_dependencies)
        self.assertIn("events.rs", deps_str)
        self.assertIn("commands.rs", deps_str)

    def test_glob_workspace_import(self):
        """use shared::events::* resolves to shared/src/events.rs."""
        ws = self._setup_workspace()
        result = self._parse_file(ws, "use shared::events::*;\n")
        self.assertTrue(len(result.scanned_import_dependencies) > 0)
        self.assertIn("events.rs", result.scanned_import_dependencies[0])

    def test_aliased_workspace_import(self):
        """use shared::events::OrderCreated as OC resolves correctly."""
        ws = self._setup_workspace()
        result = self._parse_file(ws, "use shared::events::OrderCreated as OC;\n")
        self.assertTrue(len(result.scanned_import_dependencies) > 0)
        self.assertIn("events.rs", result.scanned_import_dependencies[0])

    def test_hyphenated_crate_name(self):
        """Workspace member with hyphens is found via underscore import."""
        ws = Path(self.tmpdir) / "project"
        self._make("project", "Cargo.toml",
                    content='[workspace]\nmembers = ["my-utils", "app"]')
        self._make("project", "my-utils", "Cargo.toml",
                    content='[package]\nname = "my-utils"')
        self._make("project", "my-utils", "src", "lib.rs")
        self._make("project", "my-utils", "src", "helpers.rs",
                    content="pub fn help() {}")
        self._make("project", "app", "Cargo.toml",
                    content='[package]\nname = "app"')
        self._make("project", "app", "src", "main.rs")

        parser = RustParser()
        analysis = Analysis()
        analysis.analysis_name = "test"
        analysis.source_directory = str(ws)

        parser.generate_file_result_from_analysis(
            analysis,
            file_name="main.rs",
            full_file_path="project/app/src/main.rs",
            file_content="use my_utils::helpers::help;\n",
        )
        result = list(parser.results.values())[0]
        self.assertTrue(len(result.scanned_import_dependencies) > 0,
                        "Expected dependency from use my_utils::helpers")
        self.assertIn("helpers.rs", result.scanned_import_dependencies[0])

    def test_external_crate_not_matched(self):
        """External crate imports (serde, actix_web) produce no edges."""
        ws = self._setup_workspace()
        result = self._parse_file(ws, (
            "use serde::Deserialize;\n"
            "use actix_web::web;\n"
            "use tokio::sync::mpsc;\n"
        ))
        self.assertEqual(len(result.scanned_import_dependencies), 0,
                         f"External imports should produce no deps, got: {result.scanned_import_dependencies}")

    def test_mixed_imports(self):
        """Mix of workspace, crate::, and external imports."""
        ws = self._setup_workspace()
        # crate:: won't resolve since main.rs is in api crate with no domain/ module,
        # but shared:: should resolve
        result = self._parse_file(ws, (
            "use shared::events::OrderCreated;\n"
            "use serde::Deserialize;\n"
        ))
        self.assertEqual(len(result.scanned_import_dependencies), 1)
        self.assertIn("events.rs", result.scanned_import_dependencies[0])


if __name__ == "__main__":
    unittest.main()
