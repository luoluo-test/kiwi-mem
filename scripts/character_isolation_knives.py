"""Run destructive mutations only in disposable source copies, never live files."""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MUTATIONS = [
    ("role routing defaults every request", "character_gateway.py",
     'return candidates[0] if candidates else "default"', 'return "default"'),
    ("worker token validation removed", "character_boundary.py",
     'if self.token and not secrets.compare_digest(', 'if False and not secrets.compare_digest('),
    ("worker accepts another role header", "character_boundary.py",
     'if selected is not None and selected != (self.character or "default").encode():', 'if False:'),
]

for label, filename, old, new in MUTATIONS:
    with tempfile.TemporaryDirectory(prefix="kiwi-character-knife-") as folder:
        target = Path(folder)
        for name in ("character_gateway.py", "character_boundary.py"):
            shutil.copy2(ROOT / name, target / name)
        (target / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts/test_character_isolation.py", target / "scripts")
        path = target / filename
        source = path.read_text(encoding="utf-8")
        assert source.count(old) == 1, label
        path.write_text(source.replace(old, new), encoding="utf-8")
        result = subprocess.run([sys.executable, str(target / "scripts/test_character_isolation.py")],
                                capture_output=True, text=True, encoding="utf-8")
        if result.returncode == 0 or "FAIL" not in result.stderr:
            raise AssertionError(f"mutation survived or did not reach assertions: {label}\n{result.stderr}")
        print("KILLED:", label)
print("PASS: 3 character isolation mutations rejected; working source never modified")
