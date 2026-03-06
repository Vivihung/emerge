"""
Contains the implementation of the Rust language parser and a relevant keyword enum.
"""

# License: MIT

from typing import Dict
from enum import Enum, unique
import logging
from pathlib import Path
import os
import re

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

        for line in file_content.splitlines():
            line = line.strip()
            if not line or line.startswith('//'):
                continue

            # Handle 'mod <name>;' declarations (submodule declarations)
            self._try_parse_mod_declaration(line, result, analysis)

            # Handle 'use crate::...' and 'use super::...' and 'use self::...' imports
            self._try_parse_use_statement(line, result, analysis)

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
        """Parse 'mod foo;' or 'pub mod foo;' declarations and resolve to file paths."""
        mod_match = re.match(r'^(?:pub\s+)?mod\s+(\w+)\s*;', line)
        if not mod_match:
            return

        mod_name = mod_match.group(1)
        analysis.statistics.increment(Statistics.Key.PARSING_HITS)

        result_dir = str(result.absolute_dir_path)

        # A 'mod foo;' resolves to either: dir/foo.rs or dir/foo/mod.rs
        candidate_file = os.path.join(result_dir, f"{mod_name}.rs")
        candidate_mod = os.path.join(result_dir, mod_name, "mod.rs")

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

    def _try_parse_use_statement(self, line: str, result: AbstractResult, analysis):
        """Parse 'use crate::...', 'use super::...', 'use self::...' statements."""
        use_match = re.match(r'^(?:pub\s+)?use\s+((?:crate|super|self)(?:::\w+)+)(?:::\{[^}]*\})?\s*;', line)
        if not use_match:
            use_match = re.match(r'^(?:pub\s+)?use\s+((?:crate|super|self)(?:::\w+)*)::\{[^}]*\}\s*;', line)
        if not use_match:
            return

        import_path = use_match.group(1)

        parts = import_path.split('::')
        if not parts:
            return

        analysis.statistics.increment(Statistics.Key.PARSING_HITS)

        source_dir = analysis.source_directory
        result_dir = str(result.absolute_dir_path)

        resolved = None

        if parts[0] == 'crate':
            # crate:: means from the project root (source_directory)
            module_parts = parts[1:]
            resolved = self._resolve_module_path(source_dir, module_parts)

        elif parts[0] == 'super':
            # super:: means parent module
            parent_dir = str(Path(result_dir).parent)
            module_parts = parts[1:]
            resolved = self._resolve_module_path(parent_dir, module_parts)

        elif parts[0] == 'self':
            # self:: means current module
            module_parts = parts[1:]
            resolved = self._resolve_module_path(result_dir, module_parts)

        if resolved:
            dependency = self._to_dependency_name(resolved, analysis)
            if self._is_dependency_in_ignore_list(dependency, analysis):
                LOGGER.debug(f'ignoring dependency from {result.unique_name} to {dependency}')
            else:
                result.scanned_import_dependencies.append(dependency)
                LOGGER.debug(f'adding use dependency: {dependency}')

    def _resolve_module_path(self, base_dir: str, module_parts: list) -> str:
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

    def _add_package_name_to_result(self, result: AbstractResult) -> str:
        result.module_name = ""


if __name__ == "__main__":
    LEXER = RustParser()
    print(f'{LEXER.results=}')
