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


if __name__ == "__main__":
    unittest.main()
