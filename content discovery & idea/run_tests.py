"""Every offline test, one command. Exit code is non-zero if any fails (CI uses this).

    python run_tests.py
"""

import glob
import os
import subprocess
import sys

here = os.path.dirname(os.path.abspath(__file__))
mods = sorted(f"tests.{os.path.basename(p)[:-3]}" for p in glob.glob(os.path.join(here, "tests", "test_*.py")))
env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
failed = [m for m in mods if subprocess.run([sys.executable, "-m", m], cwd=here, env=env).returncode != 0]
print(f"\n{len(mods) - len(failed)}/{len(mods)} test files passed" + (f"; FAILED: {', '.join(failed)}" if failed else ""))
sys.exit(1 if failed else 0)
