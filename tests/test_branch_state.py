from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "openhop_repeater_main"
    / "rootfs"
    / "usr"
    / "local"
    / "lib"
    / "openhop-addon"
    / "branch_state.py"
)
PIP_WRAPPER = (
    ROOT
    / "openhop_repeater_main"
    / "rootfs"
    / "usr"
    / "local"
    / "bin"
    / "openhop-venv-pip"
)
SPEC = importlib.util.spec_from_file_location("branch_state", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
branch_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(branch_state)


class GitRefValidationTests(unittest.TestCase):
    def test_accepts_normal_branch_names(self) -> None:
        for ref in (
            "main",
            "dev",
            "fix/all-the-things",
            "release/v1.2.3",
            "user/topic_42",
        ):
            with self.subTest(ref=ref):
                self.assertTrue(branch_state.is_valid_git_ref(ref))

    def test_rejects_unsafe_or_invalid_refs(self) -> None:
        for ref in (
            "",
            "-main",
            ".hidden",
            "feature//topic",
            "feature..topic",
            "feature@{topic",
            "feature topic",
            "feature:topic",
            "feature+topic",
            "feature#topic",
            "topic.lock",
            "topic/",
            "topic.",
            "topic./child",
            "topic\\name",
        ):
            with self.subTest(ref=ref):
                self.assertFalse(branch_state.is_valid_git_ref(ref))


class SourceRefNormalizationTests(unittest.TestCase):
    def test_normalizes_pr_numbers_and_spellings(self) -> None:
        cases: list[tuple[object, str]] = [
            (42, "refs/pull/42/head"),
            ("42", "refs/pull/42/head"),
            ("  42  ", "refs/pull/42/head"),
            ("#42", "refs/pull/42/head"),
            ("pr 42", "refs/pull/42/head"),
            ("PR-42", "refs/pull/42/head"),
            ("PR #42", "refs/pull/42/head"),
            ("pull/42/head", "refs/pull/42/head"),
            ("refs/pull/42/head", "refs/pull/42/head"),
            ("refs/pull/42/merge", "refs/pull/42/merge"),
            (42.0, "refs/pull/42/head"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(branch_state.normalize_source_ref(raw), expected)

    def test_keeps_branch_names_but_rejects_invalid_prs(self) -> None:
        for raw, expected in (
            ("main", "main"),
            ("dev", "dev"),
            ("fix/all-the-things", "fix/all-the-things"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(branch_state.normalize_source_ref(raw), expected)
        for raw in ("", "0", "00", "007", "-1", 0, -3, 4.2, True, None, ["42"]):
            with self.subTest(raw=raw):
                self.assertEqual(branch_state.normalize_source_ref(raw), "")

    def test_validates_normalized_source_refs(self) -> None:
        self.assertTrue(branch_state.is_valid_source_ref("main"))
        self.assertTrue(branch_state.is_valid_source_ref("refs/pull/42/head"))
        self.assertTrue(branch_state.is_valid_source_ref("refs/pull/42/merge"))
        for ref in ("", "42", "#42", "pr 42", "pull/42/head", "feature..topic"):
            with self.subTest(ref=ref):
                self.assertFalse(branch_state.is_valid_source_ref(ref))


class DesiredRefTests(unittest.TestCase):
    def _write_options(self, directory: Path, payload: object) -> Path:
        options = directory / "options.json"
        options.write_text(json.dumps(payload), encoding="utf-8")
        return options

    def test_reads_branch_and_pr_from_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertEqual(
                branch_state.read_desired_ref(
                    self._write_options(root, {"branch_or_pr": "dev"})
                ),
                "dev",
            )
            options = root / "pr.json"
            options.write_text(json.dumps({"branch_or_pr": 42}), encoding="utf-8")
            self.assertEqual(
                branch_state.read_desired_ref(options), "refs/pull/42/head"
            )

    def test_missing_or_empty_options_select_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertEqual(
                branch_state.read_desired_ref(root / "missing.json"), "main"
            )
            self.assertEqual(
                branch_state.read_desired_ref(
                    self._write_options(root, {"branch_or_pr": "   "})
                ),
                "main",
            )
            self.assertEqual(
                branch_state.read_desired_ref(self._write_options(root, {})), "main"
            )

    def test_resolve_reports_invalid_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ref, error = branch_state.resolve_desired_ref(
                self._write_options(root, {"branch_or_pr": "bogus branch!!"})
            )
            self.assertEqual(ref, "main")
            self.assertIsNotNone(error)
            self.assertIn("branch_or_pr", error or "")

    def test_env_override_wins_over_options_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            options = self._write_options(root, {"branch_or_pr": "dev"})
            old = os.environ.get("OPENHOP_ADDON_SOURCE_REF")
            old_core = os.environ.get("OPENHOP_ADDON_CORE_REF")
            os.environ["OPENHOP_ADDON_SOURCE_REF"] = "43"
            os.environ.pop("OPENHOP_ADDON_CORE_REF", None)
            try:
                self.assertEqual(
                    branch_state.read_desired_ref(options), "refs/pull/43/head"
                )
                ref, error = branch_state.resolve_desired_ref(options)
                self.assertEqual(ref, "refs/pull/43/head")
                self.assertIsNone(error)
            finally:
                if old is None:
                    del os.environ["OPENHOP_ADDON_SOURCE_REF"]
                else:
                    os.environ["OPENHOP_ADDON_SOURCE_REF"] = old
                if old_core is not None:
                    os.environ["OPENHOP_ADDON_CORE_REF"] = old_core

    def test_desired_ref_cli_strict_fails_on_invalid_option(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            options = self._write_options(root, {"branch_or_pr": "bogus branch!!"})
            env = os.environ.copy()
            env.pop("OPENHOP_ADDON_SOURCE_REF", None)
            env.pop("OPENHOP_ADDON_CORE_REF", None)
            result = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "desired-ref",
                    "--strict",
                    "--options",
                    str(options),
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("branch_or_pr", result.stderr)


class CoreOptionTests(unittest.TestCase):
    def _write_options(self, directory: Path, payload: object) -> Path:
        options = directory / "options.json"
        options.write_text(json.dumps(payload), encoding="utf-8")
        return options

    def test_core_defaults_to_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertEqual(
                branch_state.read_desired_ref(root / "missing.json", "core"), ""
            )
            self.assertEqual(
                branch_state.read_desired_ref(
                    self._write_options(root, {"branch_or_pr": "dev"}), "core"
                ),
                "",
            )
            self.assertEqual(
                branch_state.read_desired_ref(
                    self._write_options(root, {"core_branch_or_pr": "  "}), "core"
                ),
                "",
            )

    def test_core_reads_branch_and_pr(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertEqual(
                branch_state.read_desired_ref(
                    self._write_options(root, {"core_branch_or_pr": "dev"}), "core"
                ),
                "dev",
            )
            self.assertEqual(
                branch_state.read_desired_ref(
                    self._write_options(root, {"core_branch_or_pr": 7}), "core"
                ),
                "refs/pull/7/head",
            )
            ref, error = branch_state.resolve_desired_ref(
                self._write_options(root, {"core_branch_or_pr": "bogus!!"}), "core"
            )
            self.assertEqual(ref, "")
            self.assertIsNotNone(error)
            self.assertIn("core_branch_or_pr", error or "")

    def test_core_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            options = self._write_options(root, {"core_branch_or_pr": "dev"})
            old_core = os.environ.get("OPENHOP_ADDON_CORE_REF")
            old_repeater = os.environ.get("OPENHOP_ADDON_SOURCE_REF")
            os.environ.pop("OPENHOP_ADDON_SOURCE_REF", None)
            os.environ["OPENHOP_ADDON_CORE_REF"] = "8"
            try:
                self.assertEqual(
                    branch_state.read_desired_ref(options, "core"),
                    "refs/pull/8/head",
                )
                # The repeater resolution is unaffected by the core override.
                self.assertEqual(branch_state.read_desired_ref(options), "main")
            finally:
                if old_core is None:
                    os.environ.pop("OPENHOP_ADDON_CORE_REF", None)
                else:
                    os.environ["OPENHOP_ADDON_CORE_REF"] = old_core
                if old_repeater is not None:
                    os.environ["OPENHOP_ADDON_SOURCE_REF"] = old_repeater

    def test_install_spec_supports_both_packages(self) -> None:
        self.assertEqual(
            subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "install-spec",
                    "--package",
                    "core",
                    "refs/pull/7/head",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            ).stdout.strip(),
            "openhop_core[hardware] @ "
            "git+https://github.com/openhop-dev/openhop_core.git@refs/pull/7/head",
        )

    def test_installed_ref_supports_core(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "openhop_core-1.0.0.dist-info" / "direct_url.json"
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps(
                    {
                        "url": "https://github.com/openhop-dev/openhop_core.git",
                        "vcs_info": {
                            "vcs": "git",
                            "requested_revision": "refs/pull/7/head",
                            "commit_id": "0" * 40,
                        },
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                branch_state.installed_ref(root, "core"), "refs/pull/7/head"
            )
            # The repeater lookup ignores core distributions.
            self.assertEqual(branch_state.installed_ref(root), "")


class PipArgumentValidationTests(unittest.TestCase):
    def test_accepts_expected_upstream_branch_installs(self) -> None:
        install_spec = (
            "openhop_repeater[hardware] @ "
            "git+https://github.com/openhop-dev/openhop_repeater.git@dev"
        )
        for options in (
            ["--upgrade", "--no-cache-dir"],
            ["--upgrade", "--force-reinstall", "--no-cache-dir"],
        ):
            with self.subTest(options=options):
                self.assertTrue(
                    branch_state.are_safe_pip_args(["install", *options, install_spec])
                )

    def test_accepts_pull_request_installs(self) -> None:
        install_spec = (
            "openhop_repeater[hardware] @ "
            "git+https://github.com/openhop-dev/openhop_repeater.git@refs/pull/42/head"
        )
        self.assertTrue(
            branch_state.are_safe_pip_args(
                [
                    "install",
                    "--upgrade",
                    "--force-reinstall",
                    "--no-cache-dir",
                    install_spec,
                ]
            )
        )

    def test_accepts_core_installs(self) -> None:
        self.assertTrue(
            branch_state.are_safe_pip_args(
                [
                    "install",
                    "--upgrade",
                    "--force-reinstall",
                    "--no-cache-dir",
                    "openhop_core[hardware] @ "
                    "git+https://github.com/openhop-dev/openhop_core.git"
                    "@refs/pull/7/head",
                ]
            )
        )
        self.assertTrue(
            branch_state.are_safe_pip_args(
                [
                    "install",
                    "--upgrade",
                    "--force-reinstall",
                    "--no-cache-dir",
                    "openhop_core[hardware] @ "
                    "git+https://github.com/openhop-dev/openhop_core.git@dev",
                ]
            )
        )

    def test_rejects_unnormalized_pr_spellings(self) -> None:
        for ref in ("42", "#42", "pr 42", "pull/42/head"):
            with self.subTest(ref=ref):
                self.assertFalse(
                    branch_state.are_safe_pip_args(
                        [
                            "install",
                            "--upgrade",
                            "--force-reinstall",
                            "--no-cache-dir",
                            "openhop_repeater[hardware] @ "
                            "git+https://github.com/openhop-dev/"
                            f"openhop_repeater.git@{ref}",
                        ]
                    )
                )

    def test_rejects_other_repositories_and_unsafe_refs(self) -> None:
        for install_spec in (
            (
                "openhop_repeater[hardware] @ "
                "git+https://example.invalid/openhop_repeater.git@dev"
            ),
            (
                "openhop_repeater[hardware] @ "
                "git+https://github.com/openhop-dev/"
                "openhop_repeater.git@dev#subdirectory=repeater"
            ),
            "openhop_repeater",
        ):
            with self.subTest(install_spec=install_spec):
                self.assertFalse(
                    branch_state.are_safe_pip_args(["install", install_spec])
                )

    def test_rejects_extra_packages_or_changed_options(self) -> None:
        install_spec = (
            "openhop_repeater[hardware] @ "
            "git+https://github.com/openhop-dev/openhop_repeater.git@dev"
        )
        for args in (
            ["install", "--upgrade", "--no-cache-dir", install_spec, "requests"],
            ["install", "--no-cache-dir", "--upgrade", install_spec],
            ["--disable-pip-version-check", "install", install_spec],
            ["install", "--upgrade", "pip", "setuptools", "wheel"],
        ):
            with self.subTest(args=args):
                self.assertFalse(branch_state.are_safe_pip_args(args))

    def test_allows_only_version_checks_outside_branch_installs(self) -> None:
        self.assertTrue(branch_state.are_safe_pip_args(["--version"]))
        self.assertTrue(branch_state.are_safe_pip_args(["-V"]))
        self.assertFalse(branch_state.are_safe_pip_args(["--help"]))
        self.assertFalse(
            branch_state.are_safe_pip_args(["uninstall", "-y", "openhop_repeater"])
        )

    def test_wrapper_rejects_an_unsafe_install_before_invoking_pip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bin_dir = Path(temp_dir) / "venv" / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "python").symlink_to(sys.executable)
            (bin_dir / "pip").symlink_to(PIP_WRAPPER)

            env = os.environ.copy()
            env["OPENHOP_ADDON_BRANCH_HELPER"] = str(MODULE_PATH)
            result = subprocess.run(
                [
                    str(bin_dir / "pip"),
                    "install",
                    "openhop_repeater @ git+https://example.invalid/repo.git@main",
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("Refusing an unsafe", result.stderr)

            version = subprocess.run(
                [str(bin_dir / "pip"), "--version"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertIn("pip", version.stdout.lower())


class InstalledRefTests(unittest.TestCase):
    def _write_direct_url(
        self,
        root: Path,
        name: str,
        revision: str,
        mtime: int,
        *,
        url: str = "https://github.com/openhop-dev/openhop_repeater.git",
        vcs: str = "git",
    ) -> None:
        target = root / f"openhop_repeater-{name}.dist-info" / "direct_url.json"
        target.parent.mkdir(parents=True)
        target.write_text(
            json.dumps(
                {
                    "url": url,
                    "vcs_info": {
                        "vcs": vcs,
                        "requested_revision": revision,
                        "commit_id": "0" * 40,
                    },
                }
            ),
            encoding="utf-8",
        )
        os.utime(target, ns=(mtime, mtime))

    def test_returns_newest_distribution_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_direct_url(root, "1.0.0", "main", 1)
            self._write_direct_url(root, "2.0.0", "dev", 2)
            self.assertEqual(branch_state.installed_ref(root), "dev")

    def test_ignores_other_repositories_and_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            invalid = root / "openhop_repeater-3.0.0.dist-info" / "direct_url.json"
            invalid.parent.mkdir(parents=True)
            invalid.write_text("not-json", encoding="utf-8")

            other = root / "openhop_repeater-2.0.0.dist-info" / "direct_url.json"
            other.parent.mkdir(parents=True)
            other.write_text(
                json.dumps(
                    {
                        "url": "https://github.com/example/other.git",
                        "vcs_info": {"requested_revision": "wrong"},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(branch_state.installed_ref(root), "")

    def test_returns_pr_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_direct_url(root, "1.0.0", "refs/pull/42/head", 1)
            self.assertEqual(branch_state.installed_ref(root), "refs/pull/42/head")

    def test_ignores_unnormalized_pr_spellings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_direct_url(root, "1.0.0", "42", 1)
            self.assertEqual(branch_state.installed_ref(root), "")

    def test_ignores_spoofed_urls_non_git_metadata_and_invalid_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_direct_url(
                root,
                "3.0.0",
                "main",
                3,
                url="https://example.invalid/openhop_repeater.git",
            )
            self._write_direct_url(root, "2.0.0", "dev", 2, vcs="hg")
            self._write_direct_url(root, "1.0.0", "unsafe ref", 1)
            self.assertEqual(branch_state.installed_ref(root), "")


if __name__ == "__main__":
    unittest.main()
