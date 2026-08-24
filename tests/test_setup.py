"""End-to-end shell bootstrap checks for the one-line installer."""

from pathlib import Path
import subprocess


def test_setup_bootstrap_paths() -> None:
    project_root = Path(__file__).resolve().parents[1]
    subprocess.run(["bash", str(project_root / "tests" / "test_setup.sh")], cwd=project_root, check=True)
