"""Encoding, revision and shell installation of the claude-logos wrappers the UI serves.

The Windows wrapper is downloaded with Invoke-WebRequest and run by Windows
PowerShell 5.1, which reads a BOM-less .ps1 in the system ANSI code page: the
UTF-8 bytes of an em dash come back as mojibake and the script's own messages
stop being readable. The served .ps1 must therefore stay pure ASCII, and the
two wrappers are one tool with two front ends, so their revision constants
must stay in step.
"""

import base64
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PUBLIC_DIR = Path(__file__).resolve().parents[3] / "logos-ui" / "public"
WINDOWS_WRAPPER = PUBLIC_DIR / "claude-logos.ps1"
POSIX_WRAPPER = PUBLIC_DIR / "claude-logos.sh"


def test_windows_wrapper_is_ascii_only():
    data = WINDOWS_WRAPPER.read_bytes()
    bad_lines = [
        (no, line.decode("ascii", "replace"))
        for no, line in enumerate(data.splitlines(), 1)
        if any(byte > 127 for byte in line)
    ]
    assert not bad_lines, (
        "claude-logos.ps1 must stay pure ASCII, Windows PowerShell 5.1 reads a "
        "BOM-less .ps1 in the system ANSI code page and mangles UTF-8 bytes:\n"
        + "\n".join(f"line {no}: {line}" for no, line in bad_lines[:10])
    )


def _revision(path: Path, pattern: str) -> int:
    match = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"no revision constant matching {pattern!r} in {path.name}"
    return int(match.group(1))


def test_wrapper_revisions_are_in_step():
    windows = _revision(WINDOWS_WRAPPER, r"^\$ClaudeLogosVersion\s*=\s*(\d+)")
    posix = _revision(POSIX_WRAPPER, r"^CLAUDE_LOGOS_VERSION=(\d+)")
    assert windows == posix, (
        f"claude-logos.ps1 declares revision {windows} but claude-logos.sh "
        f"declares {posix}; the two wrappers are one tool and the constants "
        "must be kept in step"
    )


class WrapperInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=PUBLIC_DIR.parents[1])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.install_path = self.root / "bin" / "claude-logos"
        self.config_dir = self.root / "config"
        self.env = {name: value for name, value in os.environ.items() if not name.startswith("LOGOS_")}
        self.env.update(
            SHELL="/usr/bin/fish",
            PATH=os.defpath,
            LOGOS_CONFIG_DIR=str(self.config_dir),
            LOGOS_INSTALL_PATH=str(self.install_path),
            XDG_CONFIG_HOME=str(self.root / "xdg-config"),
        )

    def install(self):
        result = subprocess.run(
            ["bash", str(POSIX_WRAPPER), "--install"],
            input="LOGOS_URL=https://example.test\nLOGOS_KEY=test-placeholder\n",
            text=True,
            capture_output=True,
            env=self.env,
            cwd=self.root,
            check=True,
        )
        return result.stdout

    def fish(self, command):
        return subprocess.run(
            [shutil.which("fish"), "-c", command],
            env=self.env,
            cwd=self.root,
            text=True,
            capture_output=True,
            check=True,
        ).stdout

    @unittest.skipUnless(shutil.which("fish"), "fish is not installed")
    def test_fish_path_setup_persists_without_duplicates(self):
        output = self.install()
        command = next(line.strip() for line in output.splitlines() if line.strip().startswith("fish_add_path "))
        self.assertEqual(self.fish(command + "; command -s claude-logos").strip(), str(self.install_path))
        self.assertEqual(self.fish("command -s claude-logos").strip(), str(self.install_path))
        paths = self.fish(command + "; printf '%s\\n' $PATH").splitlines()
        self.assertEqual(paths.count(str(self.install_path.parent)), 1)
        self.assertIn("Then run: claude-logos", output)

    @unittest.skipUnless(shutil.which("fish"), "fish is not installed")
    def test_fish_path_setup_quotes_custom_directories(self):
        self.install_path = self.root / "bin ' $ (touch INJECTED) `id` \\ tools" / "claude-logos"
        self.env["LOGOS_INSTALL_PATH"] = str(self.install_path)
        output = self.install()
        command = next(line.strip() for line in output.splitlines() if line.strip().startswith("fish_add_path "))
        self.assertEqual(self.fish(command + "; command -s claude-logos").strip(), str(self.install_path))
        self.assertFalse((self.root / "INJECTED").exists())

    def test_install_skips_path_setup_when_directory_is_on_path(self):
        self.env["PATH"] = str(self.install_path.parent) + os.pathsep + self.env["PATH"]
        output = self.install()
        self.assertIn("Run: claude-logos", output)
        self.assertNotIn("fish_add_path", output)
        self.assertNotIn("not on your PATH", output)

    def test_fish_install_prints_native_path_setup(self):
        output = self.install()
        self.assertIn("fish_add_path '" + str(self.install_path.parent) + "'", output)
        self.assertIn("Then run: claude-logos", output)
        self.assertNotIn("export PATH", output)

    def test_bash_and_zsh_keep_their_profile_instructions(self):
        for shell, profile in [("bash", "~/.bashrc"), ("zsh", "~/.zshrc")]:
            with self.subTest(shell=shell):
                self.env["SHELL"] = "/bin/" + shell
                output = self.install()
                self.assertIn('echo \'export PATH="$HOME/.local/bin:$PATH"\' >> ' + profile, output)
                self.assertNotIn("fish_add_path", output)

    @unittest.skipUnless(shutil.which("fish"), "fish is not installed")
    def test_fish_install_pipeline_preserves_configuration(self):
        config = "LOGOS_URL=https://example.test\nLOGOS_MODEL=a model ' $() `id` \\ name\nLOGOS_KEY=test-placeholder\n"
        encoded = base64.b64encode(config.encode()).decode()
        command = "\n".join(
            [
                f"printf '%s' '{encoded}' \\",
                "  | base64 -d \\",
                f"  | bash {shlex.quote(str(POSIX_WRAPPER))} --install",
            ]
        )
        self.fish(command)
        written = (self.config_dir / "config").read_text()
        self.assertIn("LOGOS_URL=https://example.test\n", written)
        self.assertIn("LOGOS_MODEL=a model ' $() `id` \\ name\n", written)
        self.assertEqual((self.config_dir / "key").read_text(), "test-placeholder\n")
        self.assertEqual(self.install_path.read_bytes(), POSIX_WRAPPER.read_bytes())
        installed_command = command.replace(
            "bash " + shlex.quote(str(POSIX_WRAPPER)), shlex.quote(str(self.install_path))
        )
        self.assertIn("Installed:", self.fish(installed_command))
