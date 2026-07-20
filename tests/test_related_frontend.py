import subprocess
from pathlib import Path


def test_related_frontend_state_machine() -> None:
    test_file = Path(__file__).with_name("related_frontend.test.mjs")
    result = subprocess.run(
        ["node", "--test", str(test_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
