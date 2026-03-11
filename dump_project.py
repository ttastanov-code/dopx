import os
from pathlib import Path

OUTPUT_FILE = "project_dump.txt"

IGNORE_DIRS = {
    ".git",
    "__pycache__",
    "venv",
    "app_venv",
    "node_modules",
    "staticfiles",
    "media",
    ".idea",
    ".vscode",
    "dist",
    "build",
}

IGNORE_FILES = {
    "project_dump.txt",
    ".DS_Store",
}

ALLOWED_EXTENSIONS = {
    ".py",
    ".html",
    ".txt",
    ".md",
    ".json",
    ".yml",
    ".yaml",
    ".env",
    ".ini",
    ".cfg",
    ".toml",
    ".js",
    ".css",
}


def should_ignore(path: Path):
    for part in path.parts:
        if part in IGNORE_DIRS:
            return True
    if path.name in IGNORE_FILES:
        return True
    return False


def write_tree(root, output):
    output.write("PROJECT STRUCTURE\n")
    output.write("=" * 80 + "\n\n")

    for path in sorted(root.rglob("*")):
        if should_ignore(path):
            continue

        try:
            rel = path.relative_to(root)
        except ValueError:
            continue

        indent = "  " * (len(rel.parts) - 1)
        output.write(f"{indent}{rel.name}\n")

    output.write("\n\n")


def dump_files(root, output):

    output.write("FILE CONTENTS\n")
    output.write("=" * 80 + "\n\n")

    for file_path in sorted(root.rglob("*")):

        if file_path.is_dir():
            continue

        if should_ignore(file_path):
            continue

        if file_path.suffix not in ALLOWED_EXTENSIONS:
            continue

        rel = file_path.relative_to(root)

        output.write("\n")
        output.write("=" * 80 + "\n")
        output.write(f"FILE: {rel}\n")
        output.write("=" * 80 + "\n\n")

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                output.write(f.read())
        except Exception as e:
            output.write(f"[ERROR READING FILE: {e}]")

        output.write("\n\n")


def main():

    root = Path(".").resolve()

    with open(OUTPUT_FILE, "w", encoding="utf-8") as output:

        write_tree(root, output)
        dump_files(root, output)

    print(f"\nProject dumped to: {OUTPUT_FILE}\n")


if __name__ == "__main__":
    main()