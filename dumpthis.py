import fnmatch
import json
import os
from pathlib import Path

# The file where the output will be saved
OUTPUT_FILE = "project_context.json"
IGNORE_FILE = ".dumpignore"

# Default patterns to ignore (common Python/Django/Docker overhead)
DEFAULT_IGNORES = [
    ".git",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    ".venv",
    "venv",
    "env",
    ".env",
    "node_modules",
    "static",
    "media",
    "*.sqlite3",
    "*.log",
    ".idea",
    ".vscode",
    OUTPUT_FILE,
    IGNORE_FILE,
]


def load_ignore_patterns(root_path):
    """Load patterns from .dumpignore and combine with defaults."""
    patterns = list(DEFAULT_IGNORES)
    ignore_path = root_path / IGNORE_FILE

    if ignore_path.exists():
        with open(ignore_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    patterns.append(stripped)
    return patterns


def should_ignore(rel_path, patterns):
    """Check if a file or directory should be ignored based on patterns."""
    path_obj = Path(rel_path)

    for pattern in patterns:
        # Match against the whole relative path
        if fnmatch.fnmatch(str(rel_path), pattern):
            return True
        # Match against individual parts (e.g., ignoring a directory name anywhere)
        if any(fnmatch.fnmatch(part, pattern) for part in path_obj.parts):
            return True

    return False


def read_file_content(file_path):
    """Attempt to read file as UTF-8. Returns None if it's binary."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        # It's likely a binary file, skip reading
        return None


def generate_context():
    root_path = Path.cwd()
    patterns = load_ignore_patterns(root_path)

    context = {"project_root": root_path.name, "tree": [], "files": {}}

    print(f"Scanning project: {root_path.name}...")

    for dirpath, dirnames, filenames in os.walk(root_path):
        current_dir = Path(dirpath)
        rel_dir = current_dir.relative_to(root_path)

        # Modify dirnames in-place to prevent os.walk from entering ignored directories
        dirnames[:] = [d for d in dirnames if not should_ignore(rel_dir / d, patterns)]

        for filename in filenames:
            file_path = current_dir / filename
            rel_file_path = file_path.relative_to(root_path)

            if should_ignore(rel_file_path, patterns):
                continue

            # Add to tree
            context["tree"].append(str(rel_file_path))

            # Read content
            content = read_file_content(file_path)
            if content is not None:
                context["files"][str(rel_file_path)] = content
            else:
                context["tree"].remove(str(rel_file_path))
                context["tree"].append(f"{rel_file_path} (Binary/Skipped)")

    # Sort the tree for better readability
    context["tree"].sort()

    print(f"Compiled {len(context['files'])} files.")

    with open(root_path / OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(context, f, indent=2)

    print(f"Context saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    generate_context()
