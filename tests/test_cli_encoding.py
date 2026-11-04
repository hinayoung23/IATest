import os
import subprocess
import sys


def test_cli_help_and_init_work_with_redirected_ansi_output(tmp_path):
    environment = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    for args in (["--help"], ["init", "--project", "encoding"]):
        completed = subprocess.run(
            [sys.executable, "-m", "iatest", *args],
            cwd=tmp_path, env=environment, capture_output=True, timeout=30,
        )
        assert completed.returncode == 0, completed.stderr.decode("ascii", "replace")
        assert completed.stdout
        completed.stdout.decode("cp1252")
    assert (tmp_path / "iatest.json").is_file()
