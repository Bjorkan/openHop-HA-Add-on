from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

import yaml
from fake_github_api import FakeGitHubApi, commit_body, pull_body

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "openhop_repeater_main"
RUN_SCRIPT = ADDON / "run.sh"
BRANCH_HELPER = (
    ADDON / "rootfs" / "usr" / "local" / "lib" / "openhop-addon" / "branch_state.py"
)
PATH_UTILS_HELPER = (
    ADDON / "rootfs" / "usr" / "local" / "lib" / "openhop-addon" / "path_utils.py"
)
CONFIG_HELPER = (
    ADDON / "rootfs" / "usr" / "local" / "lib" / "openhop-addon" / "config_bootstrap.py"
)
RUNTIME_INFO_HELPER = (
    ADDON / "rootfs" / "usr" / "local" / "lib" / "openhop-addon" / "runtime_info.py"
)
PIP_WRAPPER = ADDON / "rootfs" / "usr" / "local" / "bin" / "openhop-venv-pip"


def assert_process_exited(test_case: unittest.TestCase, pid_file: Path) -> None:
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    test_case.fail(f"child process {child_pid} survived the app wrapper shutdown")


def create_packaged_runtime(root: Path) -> Path:
    base_runtime = root / "protected-base"
    repeater = base_runtime / "repeater"
    repeater.mkdir(parents=True)
    (repeater / "__init__.py").write_text(
        "__version__ = 'packaged-test'\n", encoding="utf-8"
    )
    (repeater / "main.py").write_text(
        textwrap.dedent(
            """
            import os
            import pathlib
            import time


            def main() -> None:
                root = pathlib.Path(os.environ["OPENHOP_TEST_ROOT"])
                (root / "child-pid").write_text(
                    str(os.getpid()), encoding="utf-8"
                )
                (root / "packaged-active").write_text("yes", encoding="utf-8")
                while True:
                    time.sleep(1)


            if __name__ == "__main__":
                main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    for name in ("radio-settings.json", "radio-presets.json"):
        (base_runtime / name).write_text("{}\n", encoding="utf-8")
    return base_runtime


def base_bootstrap_env(
    root: Path, base_runtime: Path, pip_wrapper: Path
) -> dict[str, str]:
    yaml_site_packages = Path(yaml.__file__).resolve().parent.parent
    base_image_id = root / "base-image-id"
    base_image_id.write_text("test-base\n", encoding="utf-8")
    options_file = root / "options.json"
    if not options_file.exists():
        options_file.write_text(
            json.dumps({"branch_or_pr": "main", "core_branch_or_pr": ""}),
            encoding="utf-8",
        )
    env = os.environ.copy()
    env.pop("OPENHOP_ADDON_SOURCE_REF", None)
    env.pop("OPENHOP_ADDON_CORE_REF", None)
    env.update(
        {
            "OPENHOP_TEST_ROOT": str(root),
            "OPENHOP_ADDON_CONFIG_DIR": str(root / "config"),
            "OPENHOP_ADDON_DATA_DIR": str(root / "data"),
            "OPENHOP_ADDON_RUNTIME_CONFIG_DIR": str(root / "etc" / "openhop_repeater"),
            "OPENHOP_ADDON_FIRMWARE_DATA_DIR": str(root / "var" / "openhop_repeater"),
            "OPENHOP_ADDON_UPDATE_VENV_LINK": str(root / "opt" / "venv"),
            "OPENHOP_ADDON_TEMPLATE_CONFIG": str(ADDON / "config.yaml.example"),
            "OPENHOP_ADDON_BRANCH_HELPER": str(BRANCH_HELPER),
            "OPENHOP_ADDON_PATH_UTILS_HELPER": str(PATH_UTILS_HELPER),
            "OPENHOP_ADDON_CONFIG_HELPER": str(CONFIG_HELPER),
            "OPENHOP_ADDON_RUNTIME_INFO_HELPER": str(RUNTIME_INFO_HELPER),
            "OPENHOP_ADDON_VENV_PIP_WRAPPER": str(pip_wrapper),
            "OPENHOP_ADDON_TEST_WITHOUT_PIP": "true",
            "OPENHOP_ADDON_SYSTEM_PYTHON": "/usr/bin/python3",
            "OPENHOP_ADDON_BASE_SITE_PACKAGES_GLOB": str(yaml_site_packages),
            "OPENHOP_ADDON_BASE_RUNTIME_DIR": str(base_runtime),
            "OPENHOP_ADDON_BASE_IMAGE_ID_FILE": str(base_image_id),
            "OPENHOP_ADDON_BUILD_VERSION": "3.2.0",
            "OPENHOP_ADDON_OPTIONS_FILE": str(options_file),
            "OPENHOP_ADDON_YQ": str(root / "missing-yq"),
            # Keep existing tests hermetic: a closed loopback port makes the
            # startup version check fail fast without touching the network.
            "OPENHOP_ADDON_GITHUB_API_BASE": "http://127.0.0.1:1",
        }
    )
    return env


def write_options(root: Path, value: object, core_value: object = "") -> Path:
    options_file = root / "options.json"
    options_file.write_text(
        json.dumps({"branch_or_pr": value, "core_branch_or_pr": core_value}),
        encoding="utf-8",
    )
    return options_file


def write_install_fake_pip(root: Path, active_marker: str) -> Path:
    """Write a pip stand-in that records calls and installs runnable sources."""
    fake_pip = root / "fake-pip"
    fake_pip.write_text(
        textwrap.dedent(
            r"""
            #!/usr/bin/python3
            import json
            import os
            import pathlib
            import sys

            root = pathlib.Path(os.environ["OPENHOP_TEST_ROOT"])
            with (root / "pip-calls.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(sys.argv[1:]) + "\n")
            site = next(
                (root / "data" / "venv" / "lib").glob("python*/site-packages")
            )
            spec = sys.argv[-1]
            revision = spec.rsplit("@", 1)[-1]
            if spec.startswith("openhop_repeater"):
                url = "https://github.com/openhop-dev/openhop_repeater.git"
                package = site / "repeater"
                package.mkdir(parents=True, exist_ok=True)
                (package / "__init__.py").write_text(
                    "__version__ = 'dev-test'\n", encoding="utf-8"
                )
                (package / "main.py").write_text(
                    "import os, pathlib, time\n"
                    "def main():\n"
                    "    root = pathlib.Path("
                    "os.environ['OPENHOP_TEST_ROOT'])\n"
                    "    (root / 'child-pid').write_text("
                    "str(os.getpid()), encoding='utf-8')\n"
                    "    (root / '__OPENHOP_ACTIVE_MARKER__').write_text("
                    "'yes', encoding='utf-8')\n"
                    "    while True: time.sleep(1)\n"
                    "if __name__ == '__main__': main()\n",
                    encoding="utf-8",
                )
                dist_name = "openhop_repeater-1.0.0.dist-info"
            else:
                url = "https://github.com/openhop-dev/openhop_core.git"
                dist_name = "openhop_core-1.0.0.dist-info"
            dist_info = site / dist_name
            dist_info.mkdir(parents=True, exist_ok=True)
            (dist_info / "METADATA").write_text(
                "Metadata-Version: 2.1\n", encoding="utf-8"
            )
            (dist_info / "direct_url.json").write_text(
                json.dumps(
                    {
                        "url": url,
                        "vcs_info": {
                            "vcs": "git",
                            "requested_revision": revision,
                            "commit_id": "a" * 40,
                        },
                    }
                ),
                encoding="utf-8",
            )
            """
        )
        .lstrip()
        .replace("__OPENHOP_ACTIVE_MARKER__", active_marker),
        encoding="utf-8",
    )
    fake_pip.chmod(0o755)
    return fake_pip


def read_pip_calls(root: Path) -> list[list[str]]:
    calls_file = root / "pip-calls.jsonl"
    if not calls_file.exists():
        return []
    return [
        json.loads(line) for line in calls_file.read_text(encoding="utf-8").splitlines()
    ]


def run_bootstrap_until_marker(
    test_case: unittest.TestCase, root: Path, env: dict[str, str], marker_name: str
) -> str:
    log_path = root / "run.log"
    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            [str(RUN_SCRIPT)],
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                output = log_path.read_text(encoding="utf-8")
                if (root / marker_name).is_file() and (root / "child-pid").is_file():
                    break
                return_code = process.poll()
                if return_code is not None:
                    test_case.fail(
                        f"bootstrap exited early with status {return_code}:\n{output}"
                    )
                time.sleep(0.1)
            else:
                test_case.fail(
                    f"bootstrap did not create {marker_name}:\n"
                    + log_path.read_text(encoding="utf-8")
                )

            process.send_signal(signal.SIGTERM)
            test_case.assertEqual(process.wait(timeout=15), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=15)

    assert_process_exited(test_case, root / "child-pid")
    return log_path.read_text(encoding="utf-8")


class BootstrapIntegrationTests(unittest.TestCase):
    def test_inherited_stop_request_exits_before_bootstrap(self) -> None:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("OPENHOP_ADDON_")
        }
        env.update(
            {
                "OPENHOP_ADDON_STOP_REQUESTED": "true",
                "OPENHOP_ADDON_TEMPLATE_CONFIG": "/missing/config.yaml.example",
                "OPENHOP_ADDON_BRANCH_HELPER": "/missing/branch_state.py",
                "OPENHOP_ADDON_PATH_UTILS_HELPER": "/missing/path_utils.py",
                "OPENHOP_ADDON_CONFIG_HELPER": "/missing/config_bootstrap.py",
                "OPENHOP_ADDON_RUNTIME_INFO_HELPER": "/missing/runtime_info.py",
            }
        )
        result = subprocess.run(
            [str(RUN_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_selected_branch_is_installed_and_verified_before_start(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openhop-addon-install-") as temp_dir:
            root = Path(temp_dir)
            for directory in ("config", "data", "etc", "var", "opt"):
                (root / directory).mkdir()
            (root / "config" / "config.yaml").write_text(
                "repeater:\n  node_name: install-test\nradio_type: null\n",
                encoding="utf-8",
            )
            write_options(root, "dev")
            base_runtime = create_packaged_runtime(root)

            fake_pip = root / "fake-pip"
            fake_pip.write_text(
                textwrap.dedent(
                    r"""
                    #!/usr/bin/python3
                    import json
                    import os
                    import pathlib
                    import sys

                    root = pathlib.Path(os.environ["OPENHOP_TEST_ROOT"])
                    (root / "pip-args.json").write_text(
                        json.dumps(sys.argv[1:]), encoding="utf-8"
                    )
                    site = next(
                        (root / "data" / "venv" / "lib").glob(
                            "python*/site-packages"
                        )
                    )
                    package = site / "repeater"
                    package.mkdir(parents=True, exist_ok=True)
                    (package / "__init__.py").write_text(
                        "__version__ = 'dev-test'\n", encoding="utf-8"
                    )
                    (package / "main.py").write_text(
                        "import os, pathlib, time\n"
                        "def main():\n"
                        "    root = pathlib.Path("
                        "os.environ['OPENHOP_TEST_ROOT'])\n"
                        "    (root / 'child-pid').write_text("
                        "str(os.getpid()), encoding='utf-8')\n"
                        "    (root / 'dev-active').write_text("
                        "'yes', encoding='utf-8')\n"
                        "    while True: time.sleep(1)\n"
                        "if __name__ == '__main__': main()\n",
                        encoding="utf-8",
                    )
                    dist_info = site / "openhop_repeater-1.0.0.dist-info"
                    dist_info.mkdir(parents=True, exist_ok=True)
                    (dist_info / "METADATA").write_text(
                        "Metadata-Version: 2.1\n"
                        "Name: openhop_repeater\n"
                        "Version: 1.0.0\n",
                        encoding="utf-8",
                    )
                    (dist_info / "direct_url.json").write_text(
                        json.dumps(
                            {
                                "url": (
                                    "https://github.com/openhop-dev/"
                                    "openhop_repeater.git"
                                ),
                                "vcs_info": {
                                    "vcs": "git",
                                    "requested_revision": "dev",
                                    "commit_id": "deadbeef",
                                },
                            }
                        ),
                        encoding="utf-8",
                    )
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            fake_pip.chmod(0o755)

            output = run_bootstrap_until_marker(
                self,
                root,
                base_bootstrap_env(root, base_runtime, fake_pip),
                "dev-active",
            )

            args = json.loads((root / "pip-args.json").read_text(encoding="utf-8"))
            self.assertEqual(
                args[:-1],
                ["install", "--upgrade", "--force-reinstall", "--no-cache-dir"],
            )
            self.assertEqual(
                args[-1],
                (
                    "openhop_repeater[hardware] @ "
                    "git+https://github.com/openhop-dev/openhop_repeater.git@dev"
                ),
            )
            self.assertFalse((root / "packaged-active").exists())
            self.assertEqual(
                (root / "data" / "venv" / ".openhop-ha-branch").read_text(
                    encoding="utf-8"
                ),
                "dev\n",
            )
            self.assertIn("installed and verified source 'dev'", output)

    def test_selected_pr_number_is_normalized_to_pull_ref(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openhop-addon-pr-") as temp_dir:
            root = Path(temp_dir)
            for directory in ("config", "data", "etc", "var", "opt"):
                (root / directory).mkdir()
            (root / "config" / "config.yaml").write_text(
                "repeater:\n  node_name: pr-test\nradio_type: null\n",
                encoding="utf-8",
            )
            write_options(root, 42)
            base_runtime = create_packaged_runtime(root)

            fake_pip = root / "fake-pip"
            fake_pip.write_text(
                textwrap.dedent(
                    r"""
                    #!/usr/bin/python3
                    import json
                    import os
                    import pathlib
                    import sys

                    root = pathlib.Path(os.environ["OPENHOP_TEST_ROOT"])
                    (root / "pip-args.json").write_text(
                        json.dumps(sys.argv[1:]), encoding="utf-8"
                    )
                    site = next(
                        (root / "data" / "venv" / "lib").glob(
                            "python*/site-packages"
                        )
                    )
                    package = site / "repeater"
                    package.mkdir(parents=True, exist_ok=True)
                    (package / "__init__.py").write_text(
                        "__version__ = 'pr-test'\n", encoding="utf-8"
                    )
                    (package / "main.py").write_text(
                        "import os, pathlib, time\n"
                        "def main():\n"
                        "    root = pathlib.Path("
                        "os.environ['OPENHOP_TEST_ROOT'])\n"
                        "    (root / 'child-pid').write_text("
                        "str(os.getpid()), encoding='utf-8')\n"
                        "    (root / 'pr-active').write_text("
                        "'yes', encoding='utf-8')\n"
                        "    while True: time.sleep(1)\n"
                        "if __name__ == '__main__': main()\n",
                        encoding="utf-8",
                    )
                    dist_info = site / "openhop_repeater-1.0.0.dist-info"
                    dist_info.mkdir(parents=True, exist_ok=True)
                    (dist_info / "METADATA").write_text(
                        "Metadata-Version: 2.1\n"
                        "Name: openhop_repeater\n"
                        "Version: 1.0.0\n",
                        encoding="utf-8",
                    )
                    (dist_info / "direct_url.json").write_text(
                        json.dumps(
                            {
                                "url": (
                                    "https://github.com/openhop-dev/"
                                    "openhop_repeater.git"
                                ),
                                "vcs_info": {
                                    "vcs": "git",
                                    "requested_revision": "refs/pull/42/head",
                                    "commit_id": "deadbeef",
                                },
                            }
                        ),
                        encoding="utf-8",
                    )
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            fake_pip.chmod(0o755)

            output = run_bootstrap_until_marker(
                self,
                root,
                base_bootstrap_env(root, base_runtime, fake_pip),
                "pr-active",
            )

            args = json.loads((root / "pip-args.json").read_text(encoding="utf-8"))
            self.assertEqual(
                args[-1],
                (
                    "openhop_repeater[hardware] @ "
                    "git+https://github.com/openhop-dev/openhop_repeater.git"
                    "@refs/pull/42/head"
                ),
            )
            self.assertEqual(
                (root / "data" / "venv" / ".openhop-ha-branch").read_text(
                    encoding="utf-8"
                ),
                "refs/pull/42/head\n",
            )
            self.assertIn(
                "selected source: refs/pull/42/head; active source: refs/pull/42/head",
                output,
            )

    def test_legacy_channel_file_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openhop-addon-legacy-") as temp_dir:
            root = Path(temp_dir)
            for directory in ("config", "data", "etc", "var", "opt"):
                (root / directory).mkdir()
            (root / "config" / "config.yaml").write_text(
                "repeater:\n  node_name: legacy-test\nradio_type: null\n",
                encoding="utf-8",
            )
            write_options(root, "dev")
            (root / "data" / ".update_channel").write_text("other\n", encoding="utf-8")
            base_runtime = create_packaged_runtime(root)

            fake_pip = root / "fake-pip"
            fake_pip.write_text(
                textwrap.dedent(
                    r"""
                    #!/usr/bin/python3
                    import json
                    import os
                    import pathlib
                    import sys

                    root = pathlib.Path(os.environ["OPENHOP_TEST_ROOT"])
                    (root / "pip-args.json").write_text(
                        json.dumps(sys.argv[1:]), encoding="utf-8"
                    )
                    site = next(
                        (root / "data" / "venv" / "lib").glob(
                            "python*/site-packages"
                        )
                    )
                    package = site / "repeater"
                    package.mkdir(parents=True, exist_ok=True)
                    (package / "__init__.py").write_text(
                        "__version__ = 'dev-test'\n", encoding="utf-8"
                    )
                    (package / "main.py").write_text(
                        "import os, pathlib, time\n"
                        "def main():\n"
                        "    root = pathlib.Path("
                        "os.environ['OPENHOP_TEST_ROOT'])\n"
                        "    (root / 'child-pid').write_text("
                        "str(os.getpid()), encoding='utf-8')\n"
                        "    (root / 'dev-active').write_text("
                        "'yes', encoding='utf-8')\n"
                        "    while True: time.sleep(1)\n"
                        "if __name__ == '__main__': main()\n",
                        encoding="utf-8",
                    )
                    dist_info = site / "openhop_repeater-1.0.0.dist-info"
                    dist_info.mkdir(parents=True, exist_ok=True)
                    (dist_info / "METADATA").write_text(
                        "Metadata-Version: 2.1\n"
                        "Name: openhop_repeater\n"
                        "Version: 1.0.0\n",
                        encoding="utf-8",
                    )
                    (dist_info / "direct_url.json").write_text(
                        json.dumps(
                            {
                                "url": (
                                    "https://github.com/openhop-dev/"
                                    "openhop_repeater.git"
                                ),
                                "vcs_info": {
                                    "vcs": "git",
                                    "requested_revision": "dev",
                                    "commit_id": "deadbeef",
                                },
                            }
                        ),
                        encoding="utf-8",
                    )
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            fake_pip.chmod(0o755)

            output = run_bootstrap_until_marker(
                self,
                root,
                base_bootstrap_env(root, base_runtime, fake_pip),
                "dev-active",
            )

            args = json.loads((root / "pip-args.json").read_text(encoding="utf-8"))
            self.assertTrue(args[-1].endswith("@dev"))
            self.assertIn("ignoring legacy branch selection 'other'", output)

    def test_failed_branch_install_stops_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openhop-addon-fallback-") as temp_dir:
            root = Path(temp_dir)
            for directory in ("config", "data", "etc", "var", "opt"):
                (root / directory).mkdir()
            (root / "config" / "config.yaml").write_text(
                "repeater:\n  node_name: fallback-test\nradio_type: null\n",
                encoding="utf-8",
            )
            write_options(root, "dev")
            base_runtime = create_packaged_runtime(root)

            fake_pip = root / "failing-pip"
            fake_pip.write_text(
                '#!/bin/sh\nprintf \'%s\\n\' "$*" > "$OPENHOP_TEST_ROOT/pip-args"\nexit 1\n',
                encoding="utf-8",
            )
            fake_pip.chmod(0o755)

            env = base_bootstrap_env(root, base_runtime, fake_pip)
            result = subprocess.run(
                [str(RUN_SCRIPT)],
                env=env,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            combined = result.stdout + result.stderr
            self.assertIn("could not install requested source 'dev'", combined)
            self.assertIn("refusing to start with other code", combined)
            self.assertFalse((root / "child-pid").exists())

    def test_invalid_option_value_stops_before_start(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openhop-addon-invalid-") as temp_dir:
            root = Path(temp_dir)
            for directory in ("config", "data", "etc", "var", "opt"):
                (root / directory).mkdir()
            (root / "config" / "config.yaml").write_text(
                "repeater:\n  node_name: invalid-test\nradio_type: null\n",
                encoding="utf-8",
            )
            write_options(root, "bogus branch!!")
            base_runtime = create_packaged_runtime(root)
            fake_pip = root / "fake-pip"
            fake_pip.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            fake_pip.chmod(0o755)

            env = base_bootstrap_env(root, base_runtime, fake_pip)
            result = subprocess.run(
                [str(RUN_SCRIPT)],
                env=env,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            combined = result.stdout + result.stderr
            self.assertIn("invalid 'branch_or_pr' app option", combined)
            self.assertFalse((root / "child-pid").exists())

    def test_invalid_core_option_value_stops_before_start(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openhop-addon-invalid-core-"
        ) as temp_dir:
            root = Path(temp_dir)
            for directory in ("config", "data", "etc", "var", "opt"):
                (root / directory).mkdir()
            (root / "config" / "config.yaml").write_text(
                "repeater:\n  node_name: invalid-core-test\nradio_type: null\n",
                encoding="utf-8",
            )
            write_options(root, "dev", "bogus core!!")
            base_runtime = create_packaged_runtime(root)
            fake_pip = root / "fake-pip"
            fake_pip.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            fake_pip.chmod(0o755)

            env = base_bootstrap_env(root, base_runtime, fake_pip)
            result = subprocess.run(
                [str(RUN_SCRIPT)],
                env=env,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            combined = result.stdout + result.stderr
            self.assertIn("invalid 'core_branch_or_pr' app option", combined)
            self.assertFalse((root / "child-pid").exists())

    def test_core_override_is_installed_after_repeater(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openhop-addon-core-") as temp_dir:
            root = Path(temp_dir)
            for directory in ("config", "data", "etc", "var", "opt"):
                (root / directory).mkdir()
            (root / "config" / "config.yaml").write_text(
                "repeater:\n  node_name: core-test\nradio_type: null\n",
                encoding="utf-8",
            )
            write_options(root, "dev", 7)
            base_runtime = create_packaged_runtime(root)

            fake_pip = root / "fake-pip"
            fake_pip.write_text(
                textwrap.dedent(
                    r"""
                    #!/usr/bin/python3
                    import json
                    import os
                    import pathlib
                    import sys

                    root = pathlib.Path(os.environ["OPENHOP_TEST_ROOT"])
                    calls_file = root / "pip-calls.jsonl"
                    with calls_file.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(sys.argv[1:]) + "\n")
                    site = next(
                        (root / "data" / "venv" / "lib").glob(
                            "python*/site-packages"
                        )
                    )
                    spec = sys.argv[-1]
                    if spec.startswith("openhop_repeater"):
                        revision = spec.rsplit("@", 1)[-1]
                        url = (
                            "https://github.com/openhop-dev/"
                            "openhop_repeater.git"
                        )
                        package = site / "repeater"
                        package.mkdir(parents=True, exist_ok=True)
                        (package / "__init__.py").write_text(
                            "__version__ = 'dev-test'\n", encoding="utf-8"
                        )
                        (package / "main.py").write_text(
                            "import os, pathlib, time\n"
                            "def main():\n"
                            "    root = pathlib.Path("
                            "os.environ['OPENHOP_TEST_ROOT'])\n"
                            "    (root / 'child-pid').write_text("
                            "str(os.getpid()), encoding='utf-8')\n"
                            "    (root / 'core-active').write_text("
                            "'yes', encoding='utf-8')\n"
                            "    while True: time.sleep(1)\n"
                            "if __name__ == '__main__': main()\n",
                            encoding="utf-8",
                        )
                        dist_name = "openhop_repeater-1.0.0.dist-info"
                    else:
                        revision = spec.rsplit("@", 1)[-1]
                        url = (
                            "https://github.com/openhop-dev/"
                            "openhop_core.git"
                        )
                        dist_name = "openhop_core-1.0.0.dist-info"
                    dist_info = site / dist_name
                    dist_info.mkdir(parents=True, exist_ok=True)
                    (dist_info / "METADATA").write_text(
                        "Metadata-Version: 2.1\n", encoding="utf-8"
                    )
                    (dist_info / "direct_url.json").write_text(
                        json.dumps(
                            {
                                "url": url,
                                "vcs_info": {
                                    "vcs": "git",
                                    "requested_revision": revision,
                                    "commit_id": "deadbeef",
                                },
                            }
                        ),
                        encoding="utf-8",
                    )
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            fake_pip.chmod(0o755)

            output = run_bootstrap_until_marker(
                self,
                root,
                base_bootstrap_env(root, base_runtime, fake_pip),
                "core-active",
            )

            calls = [
                json.loads(line)
                for line in (root / "pip-calls.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(len(calls), 2)
            self.assertTrue(calls[0][-1].endswith("@dev"))
            self.assertIn("openhop_repeater", calls[0][-1])
            self.assertEqual(
                calls[1][-1],
                "openhop_core[hardware] @ "
                "git+https://github.com/openhop-dev/openhop_core.git"
                "@refs/pull/7/head",
            )
            self.assertEqual(
                (root / "data" / "venv" / ".openhop-ha-core").read_text(
                    encoding="utf-8"
                ),
                "refs/pull/7/head\n",
            )
            self.assertIn(
                "installed and verified openhop_core source 'refs/pull/7/head'",
                output,
            )
            self.assertIn(
                "selected core: refs/pull/7/head; active core: refs/pull/7/head",
                output,
            )

    def test_clearing_core_override_clears_marker(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openhop-addon-core-clear-"
        ) as temp_dir:
            root = Path(temp_dir)
            for directory in ("config", "data", "etc", "var", "opt"):
                (root / directory).mkdir()
            (root / "config" / "config.yaml").write_text(
                "repeater:\n  node_name: core-clear-test\nradio_type: null\n",
                encoding="utf-8",
            )
            write_options(root, "main", "")
            (root / "data" / "venv" / "bin").mkdir(parents=True)
            (root / "data" / "venv" / ".openhop-ha-core").write_text(
                "refs/pull/7/head\n", encoding="utf-8"
            )
            base_runtime = create_packaged_runtime(root)

            fake_pip = root / "fake-pip"
            fake_pip.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            fake_pip.chmod(0o755)

            output = run_bootstrap_until_marker(
                self,
                root,
                base_bootstrap_env(root, base_runtime, fake_pip),
                "packaged-active",
            )

            self.assertFalse((root / "data" / "venv" / ".openhop-ha-core").exists())
            self.assertIn("active core: pinned by repeater install", output)

    def test_failed_core_install_stops_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openhop-addon-core-fail-") as temp_dir:
            root = Path(temp_dir)
            for directory in ("config", "data", "etc", "var", "opt"):
                (root / directory).mkdir()
            (root / "config" / "config.yaml").write_text(
                "repeater:\n  node_name: core-fail-test\nradio_type: null\n",
                encoding="utf-8",
            )
            write_options(root, "main", "dev")
            base_runtime = create_packaged_runtime(root)

            fake_pip = root / "fake-pip"
            fake_pip.write_text(
                textwrap.dedent(
                    r"""
                    #!/usr/bin/python3
                    import json
                    import os
                    import pathlib
                    import sys

                    root = pathlib.Path(os.environ["OPENHOP_TEST_ROOT"])
                    spec = sys.argv[-1]
                    if spec.startswith("openhop_core"):
                        raise SystemExit(1)
                    site = next(
                        (root / "data" / "venv" / "lib").glob(
                            "python*/site-packages"
                        )
                    )
                    package = site / "repeater"
                    package.mkdir(parents=True, exist_ok=True)
                    (package / "__init__.py").write_text(
                        "__version__ = 'dev-test'\n", encoding="utf-8"
                    )
                    (package / "main.py").write_text(
                        "import time\ndef main():\n    while True: time.sleep(1)\n",
                        encoding="utf-8",
                    )
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            fake_pip.chmod(0o755)

            env = base_bootstrap_env(root, base_runtime, fake_pip)
            result = subprocess.run(
                [str(RUN_SCRIPT)],
                env=env,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            combined = result.stdout + result.stderr
            self.assertIn(
                "could not install requested openhop_core source 'dev'", combined
            )
            self.assertIn("refusing to start with other code", combined)
            self.assertFalse((root / "child-pid").exists())

    def test_clean_exit_restarts_into_persisted_branch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openhop-addon-bootstrap-") as temp_dir:
            root = Path(temp_dir)
            base_site = root / "base-site"
            repeater = base_site / "repeater"
            repeater.mkdir(parents=True)
            for directory in ("config", "data", "etc", "var", "opt"):
                (root / directory).mkdir()
            base_image_id = root / "base-image-id"
            base_image_id.write_text("packaged-code-v2\n", encoding="utf-8")

            # Simulate an app update with an update environment created by an
            # older image. The bootstrap must discard it before executing any
            # code from that environment.
            stale_venv = root / "data" / "venv"
            (stale_venv / "bin").mkdir(parents=True)
            stale_python = stale_venv / "bin" / "python"
            stale_python.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            stale_python.chmod(0o755)
            (stale_venv / ".openhop-ha-python").write_text(
                "addon=2.9.0;python=stale\n", encoding="utf-8"
            )
            (stale_venv / "stale-image-sentinel").write_text(
                "must be removed\n", encoding="utf-8"
            )

            # An empty file can be left behind by a failed or interrupted
            # first start. It should be initialized rather than treated as a
            # valid user configuration.
            (root / "config" / "config.yaml").touch()

            (base_site / "yaml.py").write_text(
                "def safe_load(text):\n"
                "    return {'repeater': {'node_name': 'test'}, 'radio_type': None}\n",
                encoding="utf-8",
            )
            (repeater / "__init__.py").write_text(
                "BRANCH = 'packaged-main'\n", encoding="utf-8"
            )
            (repeater / "main.py").write_text(
                textwrap.dedent(
                    """
                    from __future__ import annotations

                    import json
                    import os
                    import pathlib
                    import time


                    def emulate_restart() -> None:
                        root = pathlib.Path(os.environ["OPENHOP_TEST_ROOT"])
                        (root / "child-pid").write_text(
                            str(os.getpid()), encoding="utf-8"
                        )
                        (root / "dev-active").write_text("yes", encoding="utf-8")
                        raise SystemExit(0)


                    if __name__ == "__main__":
                        emulate_restart()
                    """
                ).lstrip(),
                encoding="utf-8",
            )

            fake_pip = root / "fake-pip"
            fake_pip.write_text(
                textwrap.dedent(
                    r"""
                    #!/usr/bin/python3
                    import json
                    import os
                    import pathlib
                    import sys

                    root = pathlib.Path(os.environ["OPENHOP_TEST_ROOT"])
                    (root / "pip-args.json").write_text(
                        json.dumps(sys.argv[1:]), encoding="utf-8"
                    )
                    site = next(
                        (root / "data" / "venv" / "lib").glob(
                            "python*/site-packages"
                        )
                    )
                    package = site / "repeater"
                    package.mkdir(parents=True, exist_ok=True)
                    (package / "__init__.py").write_text(
                        "__version__ = 'dev-test'\n", encoding="utf-8"
                    )
                    (package / "main.py").write_text(
                        "import os, pathlib, sys, time\n"
                        "def main():\n"
                        "    root = pathlib.Path("
                        "os.environ['OPENHOP_TEST_ROOT'])\n"
                        "    (root / 'child-pid').write_text("
                        "str(os.getpid()), encoding='utf-8')\n"
                        "    if (root / 'first-run-done').is_file():\n"
                        "        (root / 'second-start').write_text("
                        "'yes', encoding='utf-8')\n"
                        "        while True: time.sleep(1)\n"
                        "    (root / 'first-run-done').write_text("
                        "'yes', encoding='utf-8')\n"
                        "    sys.exit(0)\n"
                        "if __name__ == '__main__': main()\n",
                        encoding="utf-8",
                    )
                    dist_info = site / "openhop_repeater-1.0.0.dist-info"
                    dist_info.mkdir(parents=True, exist_ok=True)
                    (dist_info / "METADATA").write_text(
                        "Metadata-Version: 2.1\\n"
                        "Name: openhop_repeater\\n"
                        "Version: 1.0.0\\n",
                        encoding="utf-8",
                    )
                    (dist_info / "direct_url.json").write_text(
                        json.dumps(
                            {
                                "url": (
                                    "https://github.com/openhop-dev/"
                                    "openhop_repeater.git"
                                ),
                                "vcs_info": {
                                    "vcs": "git",
                                    "requested_revision": "dev",
                                    "commit_id": "deadbeef",
                                },
                            }
                        ),
                        encoding="utf-8",
                    )
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            fake_pip.chmod(0o755)

            (root / "options.json").write_text(
                json.dumps({"branch_or_pr": "dev", "core_branch_or_pr": ""}),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.pop("OPENHOP_ADDON_SOURCE_REF", None)
            env.pop("OPENHOP_ADDON_CORE_REF", None)
            env.update(
                {
                    "OPENHOP_TEST_ROOT": str(root),
                    "OPENHOP_ADDON_CONFIG_DIR": str(root / "config"),
                    "OPENHOP_ADDON_DATA_DIR": str(root / "data"),
                    "OPENHOP_ADDON_RUNTIME_CONFIG_DIR": str(
                        root / "etc" / "openhop_repeater"
                    ),
                    "OPENHOP_ADDON_FIRMWARE_DATA_DIR": str(
                        root / "var" / "openhop_repeater"
                    ),
                    "OPENHOP_ADDON_UPDATE_VENV_LINK": str(root / "opt" / "venv"),
                    "OPENHOP_ADDON_TEMPLATE_CONFIG": str(ADDON / "config.yaml.example"),
                    "OPENHOP_ADDON_BRANCH_HELPER": str(BRANCH_HELPER),
                    "OPENHOP_ADDON_PATH_UTILS_HELPER": str(PATH_UTILS_HELPER),
                    "OPENHOP_ADDON_CONFIG_HELPER": str(CONFIG_HELPER),
                    "OPENHOP_ADDON_RUNTIME_INFO_HELPER": str(RUNTIME_INFO_HELPER),
                    "OPENHOP_ADDON_VENV_PIP_WRAPPER": str(fake_pip),
                    "OPENHOP_ADDON_TEST_WITHOUT_PIP": "true",
                    "OPENHOP_ADDON_SYSTEM_PYTHON": "/usr/bin/python3",
                    "OPENHOP_ADDON_BASE_SITE_PACKAGES_GLOB": str(base_site),
                    "OPENHOP_ADDON_DEFAULT_BRANCH": "main",
                    "OPENHOP_ADDON_BUILD_VERSION": "3.2.0",
                    "OPENHOP_ADDON_BASE_IMAGE_ID_FILE": str(base_image_id),
                    "OPENHOP_ADDON_OPTIONS_FILE": str(root / "options.json"),
                    "OPENHOP_ADDON_GITHUB_API_BASE": "http://127.0.0.1:1",
                    "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_OPENHOP_REPEATER": (
                        "1.1.2.dev1"
                    ),
                }
            )

            log_path = root / "run.log"
            with log_path.open("w", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    [str(RUN_SCRIPT)],
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                )

                try:
                    deadline = time.monotonic() + 90
                    while time.monotonic() < deadline:
                        output = log_path.read_text(encoding="utf-8")
                        if (
                            "repeater requested a restart; rerunning source bootstrap"
                            in output
                            and (root / "second-start").is_file()
                        ):
                            break
                        return_code = process.poll()
                        if return_code is not None:
                            self.fail(
                                f"bootstrap exited early with status {return_code}:\n"
                                f"{log_path.read_text(encoding='utf-8')}"
                            )
                        time.sleep(0.1)
                    else:
                        self.fail(
                            "second start with the installed branch did not happen:\n"
                            + log_path.read_text(encoding="utf-8")
                        )

                    process.send_signal(signal.SIGTERM)
                    self.assertEqual(process.wait(timeout=15), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=15)

            assert_process_exited(self, root / "child-pid")
            output = log_path.read_text(encoding="utf-8")
            self.assertIn(
                "repeater requested a restart; rerunning source bootstrap", output
            )
            self.assertIn("selected source: dev; active source: dev", output)
            self.assertIn(str(root / "data" / "venv"), output)
            self.assertFalse((stale_venv / "stale-image-sentinel").exists())
            self.assertTrue(
                (stale_venv / ".openhop-ha-python")
                .read_text(encoding="utf-8")
                .startswith("addon=3.2.0;base=packaged-code-v2;python=")
            )
            self.assertEqual(
                (stale_venv / ".openhop-ha-branch").read_text(encoding="utf-8"),
                "dev\n",
            )
            args = json.loads((root / "pip-args.json").read_text(encoding="utf-8"))
            self.assertEqual(
                args[-1],
                (
                    "openhop_repeater[hardware] @ "
                    "git+https://github.com/openhop-dev/openhop_repeater.git@dev"
                ),
            )

            generated_config = root / "config" / "config.yaml"
            generated_text = generated_config.read_text(encoding="utf-8")
            self.assertNotIn('admin_password: "admin123"', generated_text)
            self.assertNotIn('guest_password: "guest123"', generated_text)
            generated = yaml.safe_load(generated_text)
            self.assertEqual(len(generated["repeater"]["security"]["jwt_secret"]), 64)
            self.assertEqual(generated_config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list((root / "config").glob("config.yaml.tmp.*")), [])

    def test_existing_config_is_merged_and_protected_base_runtime_starts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openhop-addon-merge-") as temp_dir:
            root = Path(temp_dir)
            for directory in ("config", "data", "etc", "var", "opt"):
                (root / directory).mkdir()

            config_path = root / "config" / "config.yaml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    repeater:
                      node_name: "kept-name"
                      security:
                        admin_password: "kept-admin-password"
                    identities:
                      room_servers:
                        - name: "test-room"
                          settings:
                            admin_password: "room-admin-password"
                            guest_password: "room-guest-password"
                    radio_type: null
                    """
                ).lstrip(),
                encoding="utf-8",
            )

            base_runtime = root / "protected-base"
            repeater = base_runtime / "repeater"
            repeater.mkdir(parents=True)
            (repeater / "__init__.py").write_text(
                "__version__ = 'packaged-test'\n", encoding="utf-8"
            )
            (base_runtime / "radio-settings.json").write_text(
                '{"hardware": {"test": {"name": "Test"}}}\n', encoding="utf-8"
            )
            (base_runtime / "radio-presets.json").write_text(
                '{"config": {"suggested_radio_settings": {"entries": []}}}\n',
                encoding="utf-8",
            )
            (repeater / "main.py").write_text(
                textwrap.dedent(
                    """
                    from __future__ import annotations

                    import os
                    import pathlib
                    import time

                    if __name__ == "__main__":
                        root = pathlib.Path(os.environ["OPENHOP_TEST_ROOT"])
                        (root / "child-pid").write_text(
                            str(os.getpid()), encoding="utf-8"
                        )
                        while True:
                            time.sleep(1)
                    """
                ).lstrip(),
                encoding="utf-8",
            )

            fake_yq = root / "yq"
            fake_yq.write_text(
                textwrap.dedent(
                    f"""\
                    #!{sys.executable}
                    from __future__ import annotations

                    import os
                    import pathlib
                    import sys
                    import yaml


                    def merge(base, override):
                        if isinstance(base, dict) and isinstance(override, dict):
                            result = dict(base)
                            for key, value in override.items():
                                result[key] = merge(result.get(key), value)
                            return result
                        return override


                    args = sys.argv[1:]
                    if args == ["--version"]:
                        print("yq (https://github.com/mikefarah/yq/) version v4.40.5")
                    elif args[0] == "eval" and ('has("' in args[1] or "jwt_secret" in args[1]):
                        data = yaml.safe_load(pathlib.Path(args[-1]).read_text()) or {{}}
                        security = (data.get("repeater") or {{}}).get("security") or {{}}
                        if "admin_password" in args[1]:
                            present = "admin_password" in security
                        elif "guest_password" in args[1]:
                            present = "guest_password" in security
                        else:
                            present = bool(security.get("jwt_secret"))
                        print("true" if present else "false")
                    elif args[0] == "eval" and args[1] == "-i":
                        path = pathlib.Path(args[-1])
                        data = yaml.safe_load(path.read_text()) or {{}}
                        security = data.setdefault("repeater", {{}}).setdefault("security", {{}})
                        if "admin_password" in args[2]:
                            security["admin_password"] = os.environ["OPENHOP_GENERATED_PASSWORD"]
                        elif "guest_password" in args[2]:
                            security["guest_password"] = os.environ["OPENHOP_GENERATED_PASSWORD"]
                        else:
                            security["jwt_secret"] = os.environ["OPENHOP_GENERATED_SECRET"]
                        path.write_text(yaml.safe_dump(data, sort_keys=False))
                    elif args[0] == "eval-all":
                        template = yaml.safe_load(pathlib.Path(args[-2]).read_text()) or {{}}
                        user = yaml.safe_load(pathlib.Path(args[-1]).read_text()) or {{}}
                        sys.stdout.write(yaml.safe_dump(merge(template, user), sort_keys=False))
                    elif args[0] == "eval" and args[1] == '... comments=""':
                        sys.stdout.write(pathlib.Path(args[-1]).read_text())
                    elif args[0] == "eval" and args[1] == ".":
                        yaml.safe_load(pathlib.Path(args[-1]).read_text())
                    else:
                        raise SystemExit(f"unexpected fake yq arguments: {{args!r}}")
                    """
                ),
                encoding="utf-8",
            )
            fake_yq.chmod(0o755)

            base_image_id = root / "base-image-id"
            base_image_id.write_text("protected-base-v1\n", encoding="utf-8")
            yaml_site_packages = Path(yaml.__file__).resolve().parent.parent
            (root / "options.json").write_text(
                '{"branch_or_pr": "main"}', encoding="utf-8"
            )
            env = os.environ.copy()
            env.pop("OPENHOP_ADDON_SOURCE_REF", None)
            env.pop("OPENHOP_ADDON_CORE_REF", None)
            env.update(
                {
                    "OPENHOP_TEST_ROOT": str(root),
                    "OPENHOP_ADDON_CONFIG_DIR": str(root / "config"),
                    "OPENHOP_ADDON_DATA_DIR": str(root / "data"),
                    "OPENHOP_ADDON_RUNTIME_CONFIG_DIR": str(
                        root / "etc" / "openhop_repeater"
                    ),
                    "OPENHOP_ADDON_FIRMWARE_DATA_DIR": str(
                        root / "var" / "openhop_repeater"
                    ),
                    "OPENHOP_ADDON_UPDATE_VENV_LINK": str(root / "opt" / "venv"),
                    "OPENHOP_ADDON_TEMPLATE_CONFIG": str(ADDON / "config.yaml.example"),
                    "OPENHOP_ADDON_BRANCH_HELPER": str(BRANCH_HELPER),
                    "OPENHOP_ADDON_PATH_UTILS_HELPER": str(PATH_UTILS_HELPER),
                    "OPENHOP_ADDON_CONFIG_HELPER": str(CONFIG_HELPER),
                    "OPENHOP_ADDON_RUNTIME_INFO_HELPER": str(RUNTIME_INFO_HELPER),
                    "OPENHOP_ADDON_VENV_PIP_WRAPPER": str(PIP_WRAPPER),
                    "OPENHOP_ADDON_TEST_WITHOUT_PIP": "true",
                    "OPENHOP_ADDON_SYSTEM_PYTHON": "/usr/bin/python3",
                    "OPENHOP_ADDON_BASE_SITE_PACKAGES_GLOB": str(yaml_site_packages),
                    "OPENHOP_ADDON_BASE_RUNTIME_DIR": str(base_runtime),
                    "OPENHOP_ADDON_BASE_IMAGE_ID_FILE": str(base_image_id),
                    "OPENHOP_ADDON_BUILD_VERSION": "3.2.0",
                    "OPENHOP_ADDON_YQ": str(fake_yq),
                    "OPENHOP_ADDON_OPTIONS_FILE": str(root / "options.json"),
                }
            )

            log_path = root / "run.log"
            with log_path.open("w", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    [str(RUN_SCRIPT)],
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                try:
                    deadline = time.monotonic() + 90
                    while time.monotonic() < deadline:
                        output = log_path.read_text(encoding="utf-8")
                        if (
                            "repeater process started" in output
                            and (root / "child-pid").is_file()
                        ):
                            break
                        return_code = process.poll()
                        if return_code is not None:
                            self.fail(
                                f"bootstrap exited early with status {return_code}:\n{output}"
                            )
                        time.sleep(0.1)
                    else:
                        self.fail(
                            "bootstrap did not start the protected packaged runtime:\n"
                            + log_path.read_text(encoding="utf-8")
                        )

                    process.send_signal(signal.SIGTERM)
                    self.assertEqual(process.wait(timeout=15), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=15)

            assert_process_exited(self, root / "child-pid")
            merged = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            self.assertEqual(merged["repeater"]["node_name"], "kept-name")
            security = merged["repeater"]["security"]
            self.assertEqual(security["admin_password"], "kept-admin-password")
            self.assertTrue(security["guest_password"])
            self.assertNotEqual(security["guest_password"], "guest123")
            self.assertEqual(len(security["jwt_secret"]), 64)
            self.assertIn("cache_ttl", merged["repeater"])
            self.assertIn("gps", merged)
            room_settings = merged["identities"]["room_servers"][0]["settings"]
            self.assertEqual(room_settings["admin_password"], "room-admin-password")
            self.assertEqual(room_settings["guest_password"], "room-guest-password")
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)

            output = log_path.read_text(encoding="utf-8")
            self.assertIn("merged missing settings", output)
            self.assertIn("generated unique credentials", output)
            self.assertIn(str(base_runtime / "repeater"), output)
            self.assertIn("active source: main (packaged image)", output)

            venv_python = root / "data" / "venv" / "bin" / "python"
            purelib = Path(
                subprocess.check_output(
                    [
                        str(venv_python),
                        "-c",
                        "import sysconfig; print(sysconfig.get_paths()['purelib'])",
                    ],
                    text=True,
                ).strip()
            )
            self.assertEqual(
                (purelib / "radio-settings.json").read_text(encoding="utf-8"),
                (base_runtime / "radio-settings.json").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (purelib / "radio-presets.json").read_text(encoding="utf-8"),
                (base_runtime / "radio-presets.json").read_text(encoding="utf-8"),
            )

    def test_existing_default_password_is_not_silently_changed(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openhop-addon-existing-password-"
        ) as temp_dir:
            root = Path(temp_dir)
            for directory in ("config", "data", "etc", "var", "opt"):
                (root / directory).mkdir()

            config_path = root / "config" / "config.yaml"
            config_path.write_text(
                "repeater:\n"
                "  node_name: kept-name\n"
                "  security:\n"
                "    admin_password: admin123\n"
                "    guest_password: guest123\n"
                "radio_type: null\n",
                encoding="utf-8",
            )

            base_runtime = root / "protected-base"
            repeater = base_runtime / "repeater"
            repeater.mkdir(parents=True)
            (repeater / "__init__.py").write_text(
                "__version__ = 'test'\n", encoding="utf-8"
            )
            (repeater / "main.py").write_text(
                "import os, pathlib, time\n"
                "if __name__ == '__main__':\n"
                "    root = pathlib.Path(os.environ['OPENHOP_TEST_ROOT'])\n"
                "    (root / 'child-pid').write_text(str(os.getpid()), encoding='utf-8')\n"
                "    time.sleep(30)\n",
                encoding="utf-8",
            )
            for name in ("radio-settings.json", "radio-presets.json"):
                (base_runtime / name).write_text("{}\n", encoding="utf-8")

            fake_yq = root / "yq"
            fake_yq.write_text(
                textwrap.dedent(
                    f"""\
                    #!{sys.executable}
                    import os, pathlib, sys, yaml

                    def merge(base, override):
                        if isinstance(base, dict) and isinstance(override, dict):
                            result = dict(base)
                            for key, value in override.items():
                                result[key] = merge(result.get(key), value)
                            return result
                        return override

                    args = sys.argv[1:]
                    if args == ["--version"]:
                        print("yq (https://github.com/mikefarah/yq/) version v4.40.5")
                    elif args[0] == "eval" and ('has("' in args[1] or "jwt_secret" in args[1]):
                        data = yaml.safe_load(pathlib.Path(args[-1]).read_text()) or {{}}
                        security = (data.get("repeater") or {{}}).get("security") or {{}}
                        if "admin_password" in args[1]:
                            present = "admin_password" in security
                        elif "guest_password" in args[1]:
                            present = "guest_password" in security
                        else:
                            present = bool(security.get("jwt_secret"))
                        print("true" if present else "false")
                    elif args[0] == "eval" and args[1] == "-i":
                        path = pathlib.Path(args[-1])
                        data = yaml.safe_load(path.read_text()) or {{}}
                        security = data.setdefault("repeater", {{}}).setdefault("security", {{}})
                        if "admin_password" in args[2]:
                            security["admin_password"] = os.environ["OPENHOP_GENERATED_PASSWORD"]
                        elif "guest_password" in args[2]:
                            security["guest_password"] = os.environ["OPENHOP_GENERATED_PASSWORD"]
                        else:
                            security["jwt_secret"] = os.environ["OPENHOP_GENERATED_SECRET"]
                        path.write_text(yaml.safe_dump(data, sort_keys=False))
                    elif args[0] == "eval-all":
                        template = yaml.safe_load(pathlib.Path(args[-2]).read_text()) or {{}}
                        user = yaml.safe_load(pathlib.Path(args[-1]).read_text()) or {{}}
                        sys.stdout.write(yaml.safe_dump(merge(template, user), sort_keys=False))
                    elif args[0] == "eval" and args[1] == '... comments=""':
                        sys.stdout.write(pathlib.Path(args[-1]).read_text())
                    elif args[0] == "eval" and args[1] == ".":
                        yaml.safe_load(pathlib.Path(args[-1]).read_text())
                    else:
                        raise SystemExit(2)
                    """
                ),
                encoding="utf-8",
            )
            fake_yq.chmod(0o755)

            base_image_id = root / "base-image-id"
            base_image_id.write_text("test-base\n", encoding="utf-8")
            yaml_site_packages = Path(yaml.__file__).resolve().parent.parent
            (root / "options.json").write_text(
                '{"branch_or_pr": "main"}', encoding="utf-8"
            )
            env = os.environ.copy()
            env.pop("OPENHOP_ADDON_SOURCE_REF", None)
            env.pop("OPENHOP_ADDON_CORE_REF", None)
            env.update(
                {
                    "OPENHOP_TEST_ROOT": str(root),
                    "OPENHOP_ADDON_CONFIG_DIR": str(root / "config"),
                    "OPENHOP_ADDON_DATA_DIR": str(root / "data"),
                    "OPENHOP_ADDON_RUNTIME_CONFIG_DIR": str(
                        root / "etc" / "openhop_repeater"
                    ),
                    "OPENHOP_ADDON_FIRMWARE_DATA_DIR": str(
                        root / "var" / "openhop_repeater"
                    ),
                    "OPENHOP_ADDON_UPDATE_VENV_LINK": str(root / "opt" / "venv"),
                    "OPENHOP_ADDON_TEMPLATE_CONFIG": str(ADDON / "config.yaml.example"),
                    "OPENHOP_ADDON_BRANCH_HELPER": str(BRANCH_HELPER),
                    "OPENHOP_ADDON_PATH_UTILS_HELPER": str(PATH_UTILS_HELPER),
                    "OPENHOP_ADDON_CONFIG_HELPER": str(CONFIG_HELPER),
                    "OPENHOP_ADDON_RUNTIME_INFO_HELPER": str(RUNTIME_INFO_HELPER),
                    "OPENHOP_ADDON_VENV_PIP_WRAPPER": str(PIP_WRAPPER),
                    "OPENHOP_ADDON_TEST_WITHOUT_PIP": "true",
                    "OPENHOP_ADDON_SYSTEM_PYTHON": "/usr/bin/python3",
                    "OPENHOP_ADDON_BASE_SITE_PACKAGES_GLOB": str(yaml_site_packages),
                    "OPENHOP_ADDON_BASE_RUNTIME_DIR": str(base_runtime),
                    "OPENHOP_ADDON_BASE_IMAGE_ID_FILE": str(base_image_id),
                    "OPENHOP_ADDON_BUILD_VERSION": "3.2.0",
                    "OPENHOP_ADDON_YQ": str(fake_yq),
                    "OPENHOP_ADDON_OPTIONS_FILE": str(root / "options.json"),
                }
            )

            log_path = root / "run.log"
            with log_path.open("w", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    [str(RUN_SCRIPT)],
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                try:
                    deadline = time.monotonic() + 90
                    while time.monotonic() < deadline:
                        output = log_path.read_text(encoding="utf-8")
                        if (
                            "repeater process started" in output
                            and (root / "child-pid").is_file()
                        ):
                            break
                        return_code = process.poll()
                        if return_code is not None:
                            self.fail(
                                "bootstrap exited early with status "
                                f"{return_code}:\n{output}"
                            )
                        time.sleep(0.1)
                    else:
                        self.fail(
                            "bootstrap did not start the packaged runtime:\n"
                            + log_path.read_text(encoding="utf-8")
                        )

                    process.send_signal(signal.SIGTERM)
                    self.assertEqual(process.wait(timeout=15), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=15)

            assert_process_exited(self, root / "child-pid")
            merged = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                merged["repeater"]["security"]["admin_password"], "admin123"
            )
            self.assertEqual(
                merged["repeater"]["security"]["guest_password"], "guest123"
            )

    def test_invalid_template_does_not_leave_partial_configuration(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openhop-addon-config-") as temp_dir:
            root = Path(temp_dir)
            for directory in ("config", "data", "etc", "var", "opt"):
                (root / directory).mkdir()

            invalid_template = root / "invalid-template.yaml"
            invalid_template.write_text(
                "repeater: {}\nradio_type: null\n", encoding="utf-8"
            )
            env = os.environ.copy()
            env.pop("OPENHOP_ADDON_SOURCE_REF", None)
            env.pop("OPENHOP_ADDON_CORE_REF", None)
            env.update(
                {
                    "OPENHOP_ADDON_CONFIG_DIR": str(root / "config"),
                    "OPENHOP_ADDON_DATA_DIR": str(root / "data"),
                    "OPENHOP_ADDON_RUNTIME_CONFIG_DIR": str(
                        root / "etc" / "openhop_repeater"
                    ),
                    "OPENHOP_ADDON_FIRMWARE_DATA_DIR": str(
                        root / "var" / "openhop_repeater"
                    ),
                    "OPENHOP_ADDON_UPDATE_VENV_LINK": str(root / "opt" / "venv"),
                    "OPENHOP_ADDON_TEMPLATE_CONFIG": str(invalid_template),
                    "OPENHOP_ADDON_BRANCH_HELPER": str(BRANCH_HELPER),
                    "OPENHOP_ADDON_PATH_UTILS_HELPER": str(PATH_UTILS_HELPER),
                    "OPENHOP_ADDON_CONFIG_HELPER": str(CONFIG_HELPER),
                    "OPENHOP_ADDON_RUNTIME_INFO_HELPER": str(RUNTIME_INFO_HELPER),
                    "OPENHOP_ADDON_VENV_PIP_WRAPPER": str(PIP_WRAPPER),
                    "OPENHOP_ADDON_TEST_WITHOUT_PIP": "true",
                    "OPENHOP_ADDON_SYSTEM_PYTHON": "/usr/bin/python3",
                    "OPENHOP_ADDON_BUILD_VERSION": "3.2.0",
                }
            )

            result = subprocess.run(
                [str(RUN_SCRIPT)],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("could not generate unique credentials", result.stderr)
            self.assertFalse((root / "config" / "config.yaml").exists())
            self.assertEqual(list((root / "config").glob("config.yaml.tmp.*")), [])


class StartupVersionCheckTests(unittest.TestCase):
    """Startup must always run the newest version of the configured refs."""

    COMMIT_A = "a" * 40
    COMMIT_B = "b" * 40

    def _prepare_root(self, root: Path) -> None:
        for directory in ("config", "data", "etc", "var", "opt"):
            (root / directory).mkdir()
        (root / "config" / "config.yaml").write_text(
            "repeater:\n  node_name: update-check-test\nradio_type: null\n",
            encoding="utf-8",
        )

    def _reset_child_markers(self, root: Path, marker_name: str) -> None:
        for name in (marker_name, "child-pid"):
            marker = root / name
            if marker.exists():
                marker.unlink()

    def test_startup_updates_branch_when_upstream_moves(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openhop-addon-fresh-") as temp_dir:
            root = Path(temp_dir)
            self._prepare_root(root)
            write_options(root, "dev")
            base_runtime = create_packaged_runtime(root)
            fake_pip = write_install_fake_pip(root, "dev-active")

            repeater_dev = "/repos/openhop-dev/openhop_repeater/commits/dev"
            api = FakeGitHubApi({repeater_dev: (200, commit_body(self.COMMIT_A))})
            try:
                env = base_bootstrap_env(root, base_runtime, fake_pip)
                env["OPENHOP_ADDON_GITHUB_API_BASE"] = api.base_url
                run_bootstrap_until_marker(self, root, env, "dev-active")

                self.assertEqual(len(read_pip_calls(root)), 1)
                self.assertEqual(
                    (root / "data" / "venv" / ".openhop-ha-source-commit").read_text(
                        encoding="utf-8"
                    ),
                    self.COMMIT_A + "\n",
                )

                # Upstream dev gains a new commit. The next startup must
                # detect it and reinstall the branch before starting.
                api.api_responses[repeater_dev] = (
                    200,
                    commit_body(self.COMMIT_B),
                )
                self._reset_child_markers(root, "dev-active")
                output = run_bootstrap_until_marker(self, root, env, "dev-active")

                calls = read_pip_calls(root)
                self.assertEqual(len(calls), 2)
                self.assertTrue(calls[1][-1].endswith("@dev"))
                self.assertIn("newer version of 'dev' is available upstream", output)
                self.assertIn(f"({self.COMMIT_B}); updating before start", output)
                self.assertEqual(
                    (root / "data" / "venv" / ".openhop-ha-source-commit").read_text(
                        encoding="utf-8"
                    ),
                    self.COMMIT_B + "\n",
                )
                self.assertIn(f"verified source commit: {self.COMMIT_B}", output)
            finally:
                api.close()

    def test_startup_check_failure_continues_with_installed_source(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openhop-addon-fresh-offline-"
        ) as temp_dir:
            root = Path(temp_dir)
            self._prepare_root(root)
            write_options(root, "dev")
            base_runtime = create_packaged_runtime(root)
            fake_pip = write_install_fake_pip(root, "dev-active")

            repeater_dev = "/repos/openhop-dev/openhop_repeater/commits/dev"
            api = FakeGitHubApi({repeater_dev: (200, commit_body(self.COMMIT_A))})
            try:
                env = base_bootstrap_env(root, base_runtime, fake_pip)
                env["OPENHOP_ADDON_GITHUB_API_BASE"] = api.base_url
                run_bootstrap_until_marker(self, root, env, "dev-active")
            finally:
                api.close()

            # The version check cannot reach upstream (for example while the
            # network is down). Startup must continue with the verified
            # installed source instead of reinstalling or starting other code.
            env["OPENHOP_ADDON_GITHUB_API_BASE"] = "http://127.0.0.1:1"
            self._reset_child_markers(root, "dev-active")
            output = run_bootstrap_until_marker(self, root, env, "dev-active")

            self.assertEqual(len(read_pip_calls(root)), 1)
            self.assertIn(
                "could not check upstream for a newer version of 'dev'",
                output,
            )
            self.assertIn("continuing with the verified installed source", output)
            self.assertIn("selected source: dev; active source: dev", output)

    def test_startup_updates_core_override_when_upstream_moves(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openhop-addon-core-fresh-"
        ) as temp_dir:
            root = Path(temp_dir)
            self._prepare_root(root)
            write_options(root, "dev", 7)
            base_runtime = create_packaged_runtime(root)
            fake_pip = write_install_fake_pip(root, "core-active")

            repeater_dev = "/repos/openhop-dev/openhop_repeater/commits/dev"
            core_pr = "/repos/openhop-dev/openhop_core/pulls/7"
            api = FakeGitHubApi(
                {
                    repeater_dev: (200, commit_body(self.COMMIT_A)),
                    core_pr: (200, pull_body(self.COMMIT_A)),
                }
            )
            try:
                env = base_bootstrap_env(root, base_runtime, fake_pip)
                env["OPENHOP_ADDON_GITHUB_API_BASE"] = api.base_url
                run_bootstrap_until_marker(self, root, env, "core-active")

                calls = read_pip_calls(root)
                self.assertEqual(len(calls), 2)
                self.assertIn("openhop_repeater", calls[0][-1])
                self.assertTrue(calls[1][-1].endswith("@refs/pull/7/head"))
                self.assertEqual(
                    (root / "data" / "venv" / ".openhop-ha-core-commit").read_text(
                        encoding="utf-8"
                    ),
                    self.COMMIT_A + "\n",
                )

                # The pull request gains a new commit. The next startup keeps
                # the fresh repeater install and only reinstalls core.
                api.api_responses[core_pr] = (200, pull_body(self.COMMIT_B))
                self._reset_child_markers(root, "core-active")
                output = run_bootstrap_until_marker(self, root, env, "core-active")

                calls = read_pip_calls(root)
                self.assertEqual(len(calls), 3)
                self.assertIn("openhop_core", calls[2][-1])
                self.assertTrue(calls[2][-1].endswith("@refs/pull/7/head"))
                self.assertIn(
                    "newer version of openhop_core 'refs/pull/7/head' "
                    "is available upstream",
                    output,
                )
                self.assertIn(f"verified core commit: {self.COMMIT_B}", output)
                self.assertEqual(
                    (root / "data" / "venv" / ".openhop-ha-core-commit").read_text(
                        encoding="utf-8"
                    ),
                    self.COMMIT_B + "\n",
                )
            finally:
                api.close()


if __name__ == "__main__":
    unittest.main()
