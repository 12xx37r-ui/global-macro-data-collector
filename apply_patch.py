from pathlib import Path
import subprocess, sys

patch_file = Path(__file__).with_name("global_cn_pbc_archive_final.patch")
if not patch_file.exists():
    raise SystemExit(f"missing {patch_file}")
subprocess.run(["git", "apply", "--check", str(patch_file)], check=True)
subprocess.run(["git", "apply", str(patch_file)], check=True)
print("Patch applied.")
print("Run: python -m pytest -q")
