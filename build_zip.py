import os
import zipfile

PROJECT = "astrbot_plugin_repeater"
EXCLUDE_PREFIXES = (".",)
EXCLUDE_DIRS = {"data", "__pycache__"}
EXCLUDE_FILES = {"astrbot_plugin_repeater.zip"}


def build_zip():
    zip_path = f"{PROJECT}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk("."):
            rel = os.path.relpath(root, ".")
            parts = rel.split(os.sep)

            if any(
                p.startswith(EXCLUDE_PREFIXES) or p in EXCLUDE_DIRS
                for p in parts
                if p and p != "."
            ):
                dirs[:] = []
                continue

            for f in files:
                if f.startswith(EXCLUDE_PREFIXES) or f in EXCLUDE_FILES:
                    continue
                file_path = os.path.join(root, f)
                arcname = os.path.join(PROJECT, rel, f) if rel != "." else os.path.join(PROJECT, f)
                zf.write(file_path, arcname)

    print(f"Created {zip_path}")


if __name__ == "__main__":
    build_zip()
