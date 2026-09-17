"""Exercise the actual shell guard with a fake Docker CLI, never a real daemon."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GIT_BASH = Path(shutil.which('git') or '.').resolve().parents[1] / 'bin/bash.exe'
BASH = shutil.which('bash') or (str(GIT_BASH) if GIT_BASH.exists() else None)


@unittest.skipUnless(BASH, 'requires Bash; Docker is simulated')
class UpdateGuardTests(unittest.TestCase):
    def run_guard(self, **scenario):
        with tempfile.TemporaryDirectory(prefix='kiwi-update-guard-') as folder:
            docker = Path(folder, 'docker')
            docker.write_text('''#!/usr/bin/env bash
case "$*" in
  'compose config') printf 'services:\n  kiwi-mem:\n    environment:\n      KIWI_CHARACTER_ISOLATION: %s\n' "${MODE:-false}" ;;
  'compose ps -q kiwi-mem') echo mock-container ;;
  'inspect '*) echo "KIWI_CHARACTER_ISOLATION=${RUNNING:-false}" ;;
  'compose exec '*)
    [ "${BROKEN:-0}" = 0 ] || exit 1
    if [[ "$*" == *to_regclass* ]]; then echo "${REGISTRY:-f}"; else echo "${COUNT:-0}"; fi ;;
  *) exit 2 ;;
esac
''', encoding='utf-8', newline='\n')
            docker.chmod(0o755)
            env = dict(os.environ, PATH=folder + os.pathsep + os.environ['PATH'], **scenario)
            return subprocess.run([BASH, str(ROOT / 'scripts/character_update_guard.sh'), 'docker compose'],
                                  env=env, capture_output=True, text=True).returncode

    def test_effective_configuration_blocks(self):
        for value in ('true', '"true"', "'true'", 'TRUE'):
            with self.subTest(value=value):
                self.assertEqual(self.run_guard(MODE=value), 42)

    def test_running_service_blocks_after_config_disabled(self):
        self.assertEqual(self.run_guard(RUNNING='true'), 42)

    def test_retained_role_databases_block(self):
        self.assertEqual(self.run_guard(REGISTRY='t', COUNT='2'), 42)

    def test_plain_legacy_database_is_allowed(self):
        self.assertEqual(self.run_guard(), 0)
        self.assertEqual(self.run_guard(REGISTRY='t', COUNT='0'), 0)

    def test_unverifiable_database_is_rejected(self):
        self.assertEqual(self.run_guard(BROKEN='1'), 2)
        self.assertEqual(self.run_guard(REGISTRY='t', COUNT='invalid'), 2)


if __name__ == '__main__':
    unittest.main()
