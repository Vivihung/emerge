"""
Contains the implementation of the Rust language parser and a relevant keyword enum.
"""

# License: MIT

from typing import Dict, Optional
from enum import Enum, unique
import glob as globmod
import logging
from pathlib import Path
import os
import re
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

import coloredlogs

from emerge.languages.abstractparser import AbstractParser, ParsingMixin, Parser, LanguageType
from emerge.results import FileResult
from emerge.abstractresult import AbstractResult, AbstractEntityResult
from emerge.log import Logger
from emerge.stats import Statistics

LOGGER = Logger(logging.getLogger('parser'))
coloredlogs.install(level='E', logger=LOGGER.logger(), fmt=Logger.log_format)


@unique
class RustParsingKeyword(Enum):
    USE = "use"
    MOD = "mod"
    PUB = "pub"
    CRATE = "crate"
    SUPER = "super"
    SELF = "self"
    OPEN_SCOPE = "{"
    CLOSE_SCOPE = "}"
    SEMICOLON = ";"
    INLINE_COMMENT = "//"
    START_BLOCK_COMMENT = "/*"
    STOP_BLOCK_COMMENT = "*/"
    NEWLINE = "\n"
    RS_FILE_EXTENSION = ".rs"


class RustParser(AbstractParser, ParsingMixin):

    def __init__(self):
        self._results: Dict[str, AbstractResult] = {}
        self._workspace_members: Optional[Dict[str, str]] = None
        self._workspace_members_source_dir: Optional[str] = None
        self._token_mappings: Dict[str, str] = {
            ':': ' : ',
            ';': ' ; ',
            '{': ' { ',
            '}': ' } ',
            '(': ' ( ',
            ')': ' ) ',
            '[': ' [ ',
            ']': ' ] ',
            '?': ' ? ',
            '!': ' ! ',
            ',': ' , ',
            '<': ' < ',
            '>': ' > ',
            '"': ' " '
        }

    @classmethod
    def parser_name(cls) -> str:
        return Parser.RUST_PARSER.name

    @classmethod
    def language_type(cls) -> str:
        return LanguageType.RUST.name

    @property
    def results(self) -> Dict[str, AbstractResult]:
        return self._results

    @results.setter
    def results(self, value):
        self._results = value

    def generate_file_result_from_analysis(self, analysis, *, file_name: str, full_file_path: str, file_content: str) -> None:
        LOGGER.debug(f'generating file results...')
        scanned_tokens = self.preprocess_file_content_and_generate_token_list_by_mapping(file_content, self._token_mappings)

        parent_analysis_source_path = f"{Path(analysis.source_directory).parent}/"
        relative_file_path_to_analysis = full_file_path.replace(parent_analysis_source_path, "")

        file_result = FileResult.create_file_result(
            analysis=analysis,
            scanned_file_name=file_name,
            relative_file_path_to_analysis=relative_file_path_to_analysis,
            absolute_name=full_file_path,
            display_name=file_name,
            module_name="",
            scanned_by=self.parser_name(),
            scanned_language=LanguageType.RUST,
            scanned_tokens=scanned_tokens,
            source=file_content,
            preprocessed_source=""
        )

        self._add_package_name_to_result(file_result)
        self._add_imports_to_result(file_result, analysis)
        self._results[file_result.unique_name] = file_result

    def after_generated_file_results(self, analysis) -> None:
        pass

    def generate_entity_results_from_analysis(self, analysis):
        raise NotImplementedError(f'currently not implemented in {self.parser_name()}')

    def create_unique_entity_name(self, entity: AbstractEntityResult) -> None:
        raise NotImplementedError(f'currently not implemented in {self.parser_name()}')

    def _add_imports_to_result(self, result: AbstractResult, analysis):
        LOGGER.debug(f'extracting imports from file result {result.scanned_file_name}...')
        # Prefer the in-memory source to avoid redundant I/O
        file_content = getattr(result, "source", None)
        if not file_content:
            file_content = self.read_input_from_file(result.absolute_name)

        # First pass: strip comments and buffer multi-line statements
        in_block_comment = False
        statement_buffer = ""
        statements = []

        for raw_line in file_content.splitlines():
            # Strip block comments while tracking state across lines
            clean_chars = []
            i = 0
            while i < len(raw_line):
                if in_block_comment:
                    if raw_line[i:i+2] == '*/':
                        in_block_comment = False
                        i += 2
                    else:
                        i += 1
                else:
                    if raw_line[i:i+2] == '//':
                        break
                    if raw_line[i:i+2] == '/*':
                        in_block_comment = True
                        i += 2
                    else:
                        clean_chars.append(raw_line[i])
                        i += 1

            line = ''.join(clean_chars).strip()
            if not line:
                continue

            # Buffer lines until we hit a semicolon to handle multi-line use/mod statements
            statement_buffer = (statement_buffer + " " + line).strip() if statement_buffer else line
            if ';' in statement_buffer:
                # Split on semicolons in case multiple statements were buffered
                parts = statement_buffer.split(';')
                for part in parts[:-1]:
                    stmt = part.strip()
                    if stmt:
                        statements.append(stmt + ';')
                # Keep remainder after last semicolon as new buffer
                statement_buffer = parts[-1].strip()

        # Lazily compute crate src dir on first use crate:: statement
        crate_src = ""

        # Lazily detect workspace members, invalidating cache when source_directory changes
        if self._workspace_members is None or self._workspace_members_source_dir != analysis.source_directory:
            self._workspace_members = self._detect_workspace_members(analysis.source_directory)
            self._workspace_members_source_dir = analysis.source_directory

        # Process all complete statements
        for stmt in statements:
            self._try_parse_mod_declaration(stmt, result, analysis)
            if not crate_src and stmt.lstrip().startswith('use ') and 'crate::' in stmt:
                abs_file_path = str(Path(result.absolute_dir_path) / result.scanned_file_name)
                crate_src = self._find_crate_src_dir(abs_file_path, analysis.source_directory)
            self._try_parse_use_statement(stmt, result, analysis, crate_src=crate_src)

    def _get_module_dir(self, result: AbstractResult) -> str:
        """Derive the Rust module directory for a file.

        For mod.rs/lib.rs/main.rs, submodules live in the file's directory.
        For foo/bar.rs, submodules live under foo/bar/.
        """
        result_dir = str(result.absolute_dir_path)
        file_name = Path(result.absolute_name).name
        if file_name in ('mod.rs', 'lib.rs', 'main.rs'):
            return result_dir
        stem = Path(result.absolute_name).stem
        return os.path.join(result_dir, stem)

    def _to_dependency_name(self, resolved_path: str, analysis) -> str:
        """Convert a resolved file path to a dependency name matching emerge's unique_name format.

        unique_name = full_file_path.replace(parent_analysis_source_path, "")
        We replicate that exact format here so dependency names match graph node names.
        """
        source_dir = analysis.source_directory
        try:
            rel = os.path.relpath(resolved_path, source_dir)
        except ValueError:
            rel = resolved_path
        full_path = os.path.join(source_dir, rel)
        parent_analysis_source_path = f"{Path(source_dir).parent}/"
        dependency = full_path.replace(parent_analysis_source_path, "")
        return dependency

    def _try_parse_mod_declaration(self, line: str, result: AbstractResult, analysis):
        """Parse mod declarations including pub(crate)/pub(super)/pub(in path) and attribute-prefixed forms."""
        mod_match = re.match(
            r'^(?:#\[[^\]]*\]\s*)*'          # optional inline attributes like #[cfg(...)]
            r'(?:pub(?:\s*\([^)]*\))?\s+)?'  # optional pub with or without (visibility)
            r'mod\s+([A-Za-z_]\w*)\s*;',     # module name and trailing semicolon
            line,
        )
        if not mod_match:
            return

        mod_name = mod_match.group(1)
        analysis.statistics.increment(Statistics.Key.PARSING_HITS)

        module_dir = self._get_module_dir(result)

        # A 'mod foo;' resolves to either: module_dir/foo.rs or module_dir/foo/mod.rs
        candidate_file = os.path.join(module_dir, f"{mod_name}.rs")
        candidate_mod = os.path.join(module_dir, mod_name, "mod.rs")

        resolved = None
        if os.path.exists(candidate_file):
            resolved = candidate_file
        elif os.path.exists(candidate_mod):
            resolved = candidate_mod

        if resolved:
            dependency = self._to_dependency_name(resolved, analysis)
            if self._is_dependency_in_ignore_list(dependency, analysis):
                LOGGER.debug(f'ignoring dependency from {result.unique_name} to {dependency}')
            else:
                result.scanned_import_dependencies.append(dependency)
                LOGGER.debug(f'adding mod dependency: {dependency}')

    def _try_parse_use_statement(self, line: str, result: AbstractResult, analysis, *, crate_src: str = ""):
        """Parse use statements including brace groups, globs, aliases, and visibility qualifiers."""
        use_match = re.match(
            r'^(?:#\[[^\]]*\]\s*)*'          # optional inline attributes
            r'(?:pub(?:\s*\([^)]*\))?\s+)?'  # optional pub with or without (visibility)
            r'use\s+([^;]+);',
            line,
        )
        if not use_match:
            return

        use_body = use_match.group(1).strip()
        if not use_body:
            return

        # Determine the first path segment to decide if this is an internal import
        first_segment = use_body.split('::')[0].split('{')[0].strip()
        workspace_members = self._workspace_members or {}
        is_internal = first_segment in ('crate', 'super', 'self')
        is_workspace_member = first_segment in workspace_members

        if not is_internal and not is_workspace_member:
            return  # truly external crate, skip

        # Collect fully qualified import paths
        import_paths = []

        if '{' in use_body and '}' in use_body:
            brace_start = use_body.find('{')
            brace_end = use_body.rfind('}')
            if brace_end <= brace_start:
                return
            prefix = use_body[:brace_start].rstrip().rstrip(':')
            for item in use_body[brace_start + 1:brace_end].split(','):
                item = re.split(r'\s+as\s+', item.strip(), maxsplit=1)[0].strip()
                if item.endswith('::*'):
                    item = item[:-3].rstrip(':')
                if item:
                    # In brace groups, `self` and `super` refer to the prefix module itself
                    if item in ('self', 'super') and prefix:
                        import_paths.append(prefix)
                    else:
                        import_paths.append(f"{prefix}::{item}" if prefix else item)
        else:
            body = re.split(r'\s+as\s+', use_body, maxsplit=1)[0].strip()
            if body.endswith('::*'):
                body = body[:-3].rstrip(':')
            if body:
                import_paths.append(body)

        module_dir = self._get_module_dir(result)

        for import_path in import_paths:
            parts = import_path.split('::')
            if len(parts) < 2:
                continue

            first = parts[0]

            if first in ('crate', 'super', 'self'):
                if not re.match(r'^(crate|super|self)(?:::\w+)*$', import_path):
                    continue

                analysis.statistics.increment(Statistics.Key.PARSING_HITS)
                resolved = None

                if first == 'crate':
                    resolved = self._resolve_module_path(crate_src, parts[1:])
                elif first == 'super':
                    resolved = self._resolve_module_path(str(Path(module_dir).parent), parts[1:])
                elif first == 'self':
                    resolved = self._resolve_module_path(module_dir, parts[1:])

                if resolved:
                    dependency = self._to_dependency_name(resolved, analysis)
                    if self._is_dependency_in_ignore_list(dependency, analysis):
                        LOGGER.debug(f'ignoring dependency from {result.unique_name} to {dependency}')
                    else:
                        result.scanned_import_dependencies.append(dependency)
                        LOGGER.debug(f'adding use dependency: {dependency}')

            elif first in workspace_members:
                analysis.statistics.increment(Statistics.Key.PARSING_HITS)
                member_src = workspace_members[first]
                resolved = self._resolve_module_path(member_src, parts[1:])
                if resolved:
                    dependency = self._to_dependency_name(resolved, analysis)
                    if self._is_dependency_in_ignore_list(dependency, analysis):
                        LOGGER.debug(f'ignoring dependency from {result.unique_name} to {dependency}')
                    else:
                        result.scanned_import_dependencies.append(dependency)
                        LOGGER.debug(f'adding workspace member dependency: {dependency}')

    def _find_crate_src_dir(self, file_path: str, analysis_source_dir: str) -> str:
        """Find the base directory for resolving crate:: imports.

        Walks up from file_path to the nearest Cargo.toml and returns its src/
        subdirectory if it exists, otherwise the Cargo.toml's parent directory.
        Falls back to analysis_source_dir (or its src/ subdirectory) if no
        Cargo.toml is found within the analysis root.

        file_path must be an absolute filesystem path (not the relative
        analysis name stored in result.absolute_name).
        """
        current = Path(file_path).resolve().parent
        analysis_root = Path(analysis_source_dir).resolve()
        while current != current.parent:
            if (current / "Cargo.toml").is_file():
                src_dir = current / "src"
                if src_dir.is_dir():
                    return str(src_dir)
                return str(current)
            if current == analysis_root or analysis_root not in current.parents:
                break
            current = current.parent
        analysis_root_src = analysis_root / "src"
        if analysis_root_src.is_dir():
            return str(analysis_root_src)
        return str(analysis_root)

    def _resolve_module_path(self, base_dir: str, module_parts: list) -> Optional[str]:
        """Try to resolve a Rust module path to a .rs file.

        For 'crate::api::routes', with base_dir as source_dir, module_parts = ['api', 'routes']:
          - Try base_dir/api/routes.rs
          - Try base_dir/api/routes/mod.rs
          - If neither exists, try progressively shorter paths (the last parts might be items, not modules)
        """
        if not module_parts:
            return None

        for depth in range(len(module_parts), 0, -1):
            path_parts = module_parts[:depth]
            candidate = os.path.join(base_dir, *path_parts[:-1], f"{path_parts[-1]}.rs") if path_parts else None
            if candidate and os.path.exists(candidate):
                return candidate.replace('\\', '/')

            candidate_mod = os.path.join(base_dir, *path_parts, "mod.rs")
            if os.path.exists(candidate_mod):
                return candidate_mod.replace('\\', '/')

        return None

    def _detect_workspace_members(self, source_directory: str) -> Dict[str, str]:
        """Detect Cargo workspace member crates and return a mapping of crate name to src/ directory.

        Checks source_directory for a Cargo.toml with a [workspace] section,
        then reads each member's Cargo.toml to get the package name. Hyphens in crate names
        are converted to underscores (Rust convention). Glob patterns in members are expanded.

        Only inspects source_directory itself (not parent directories) to stay consistent
        with the analysis boundary and avoid pulling in members outside the analysis tree.
        """
        members: Dict[str, str] = {}
        workspace_root = Path(source_directory).resolve()
        workspace_toml = None

        cargo_path = workspace_root / "Cargo.toml"
        if cargo_path.is_file():
            try:
                with open(cargo_path, "rb") as f:
                    data = tomllib.load(f)
                if "workspace" in data:
                    workspace_toml = data
            except (OSError, tomllib.TOMLDecodeError) as e:
                LOGGER.debug(f'failed to parse workspace Cargo.toml at {cargo_path}: {e}')

        if not workspace_toml:
            return members

        # Get member patterns from workspace config
        member_patterns = workspace_toml.get("workspace", {}).get("members", [])
        if not member_patterns:
            return members

        # Expand glob patterns and collect member directories, filtering to those within workspace_root
        member_dirs = []
        for pattern in member_patterns:
            expanded = globmod.glob(str(workspace_root / pattern))
            if expanded:
                member_dirs.extend(expanded)
            else:
                # Non-glob literal path
                literal = workspace_root / pattern
                if literal.is_dir():
                    member_dirs.append(str(literal))

        # Filter out any member dirs that resolve outside the workspace root
        resolved_root = str(workspace_root) + os.sep
        member_dirs = [
            d for d in member_dirs
            if os.path.realpath(d).startswith(resolved_root) or os.path.realpath(d) == str(workspace_root)
        ]

        # Read each member's Cargo.toml to get the package name
        for member_dir in member_dirs:
            member_cargo = Path(member_dir) / "Cargo.toml"
            if not member_cargo.is_file():
                continue
            try:
                with open(member_cargo, "rb") as f:
                    member_data = tomllib.load(f)
                pkg_name = member_data.get("package", {}).get("name", "")
                if not pkg_name:
                    continue
                # Rust convention: hyphens become underscores in use statements
                crate_name = pkg_name.replace("-", "_")
                src_dir = Path(member_dir) / "src"
                if src_dir.is_dir():
                    members[crate_name] = str(src_dir)
                else:
                    members[crate_name] = str(member_dir)
            except (OSError, tomllib.TOMLDecodeError) as e:
                LOGGER.debug(f'skipping workspace member at {member_dir}: {e}')
                continue

        LOGGER.debug(f'detected workspace members: {list(members.keys())}')
        return members

    def _add_package_name_to_result(self, result: AbstractResult) -> None:
        result.module_name = ""


if __name__ == "__main__":
    LEXER = RustParser()
    print(f'{LEXER.results=}')
