#!/usr/bin/env python3
"""repomix.py — Python port of repomix: pack a repo into a single AI-friendly file."""

import argparse
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
import xml.dom.minidom
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

DEFAULT_IGNORES = [
    ".git", "node_modules", ".next", "__pycache__", "*.pyc", "*.pyo",
    ".DS_Store", "dist", "build", ".cache", "coverage", ".nyc_output",
    "*.log", "*.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    ".env", ".env.*", "venv", ".venv", "env", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "*.egg-info", "target", "out",
    ".idea", ".vscode", "*.swp", "*.swo", "*~",
]


def load_gitignore_patterns(root: Path) -> list[str]:
    patterns = []
    gitignore = root / ".gitignore"
    if gitignore.is_file():
        for line in gitignore.read_text(errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    return patterns


def should_ignore(path: Path, root: Path, ignore_patterns: list[str]) -> bool:
    rel = str(path.resolve().relative_to(root.resolve()))
    for pat in DEFAULT_IGNORES + ignore_patterns:
        p = pat.rstrip("/")
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, f"{p}/**") or fnmatch.fnmatch(path.name, p):
            return True
    return False


def matches_include(rel_path: str, includes: list[str]) -> bool:
    if not includes:
        return True
    return any(fnmatch.fnmatch(rel_path, inc) for inc in includes)


def count_tokens(text: str) -> int:
    # Rough approximation: ~4 chars per token for code
    return max(1, len(text) // 4)


def get_file_content(filepath: Path) -> str:
    try:
        return filepath.read_text(errors="replace")
    except (OSError, UnicodeDecodeError):
        return f"[Binary or unreadable: {filepath.name}]"


def get_git_logs(root: Path, count: int = 50) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "log", f"-{count}", "--pretty=format:%h %s", "--name-only"],
            capture_output=True, text=True, cwd=root, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def get_git_diffs(root: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "diff", "--stat"], capture_output=True, text=True, cwd=root, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def build_file_tree(paths: list[Path], root: Path) -> dict:
    tree: dict = {}
    for p in paths:
        try:
            parts = p.resolve().relative_to(root.resolve()).parts
        except ValueError:
            continue
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = None
    return tree


def format_tree(tree: dict, prefix: str = "") -> str:
    lines = []
    items = sorted(tree.items())
    for i, (name, subtree) in enumerate(items):
        is_last = i == len(items) - 1
        connector = "└── " if is_last else "├── "
        lines.append(f"{prefix}{connector}{name}")
        if subtree is not None:
            ext_prefix = "    " if is_last else "│   "
            lines.append(format_tree(subtree, prefix + ext_prefix))
    return "\n".join(lines)


def output_json(files: list[dict], meta: dict) -> str:
    return json.dumps({"metadata": meta, "files": files}, indent=2)


def output_xml(files: list[dict], meta: dict) -> str:
    root = ET.Element("repository")
    meta_el = ET.SubElement(root, "metadata")
    for k, v in meta.items():
        child = ET.SubElement(meta_el, k)
        child.text = str(v)
    files_el = ET.SubElement(root, "files")
    for f in files:
        file_el = ET.SubElement(files_el, "file", path=f["path"], tokens=str(f.get("tokens", 0)))
        file_el.text = f["content"]
    raw = ET.tostring(root, encoding="unicode")
    dom = xml.dom.minidom.parseString(raw)
    return dom.toprettyxml(indent="  ")


def output_markdown(files: list[dict], meta: dict) -> str:
    lines = [f"# Repository Pack", "", "## Metadata", ""]
    for k, v in meta.items():
        lines.append(f"- **{k}**: {v}")
    lines.append("")
    lines.append("## Files")
    lines.append("")
    for f in files:
        lines.append(f"### {f['path']}")
        lines.append(f"```{f.get('language', '')}")
        lines.append(f["content"])
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def output_plain(files: list[dict], meta: dict) -> str:
    lines = ["=" * 60, "REPOSITORY PACK", "=" * 60, ""]
    for k, v in meta.items():
        lines.append(f"{k}: {v}")
    lines.append("")
    for f in files:
        lines.append("-" * 40)
        lines.append(f"File: {f['path']}  (tokens: {f.get('tokens', 0)})")
        lines.append("-" * 40)
        lines.append(f["content"])
        lines.append("")
    return "\n".join(lines)


OUTPUTTERS = {
    "json": output_json,
    "xml": output_xml,
    "markdown": output_markdown,
    "plain": output_plain,
}


def main():
    parser = argparse.ArgumentParser(description="Pack a repository into a single AI-friendly file")
    parser.add_argument("directory", nargs="?", default=".", help="Target directory (default: .)")
    parser.add_argument("-o", "--output", default=None, help="Output file path")
    parser.add_argument("--style", default="json", choices=["json", "xml", "markdown", "plain"],
                        help="Output format (default: json)")
    parser.add_argument("--include", default=None, help="Glob patterns to include (comma-separated)")
    parser.add_argument("--ignore", default=None, dest="ignore_patterns",
                        help="Extra patterns to ignore (comma-separated)")
    parser.add_argument("--no-gitignore", action="store_true", help="Don't use .gitignore rules")
    parser.add_argument("--no-default-patterns", action="store_true", help="Don't apply built-in ignore patterns")
    parser.add_argument("--stdout", action="store_true", help="Write to stdout instead of file")
    parser.add_argument("--include-diffs", action="store_true", help="Include git diff summary")
    parser.add_argument("--include-logs", action="store_true", help="Include git commit log")
    parser.add_argument("--include-logs-count", type=int, default=50, help="Number of commits to include")
    parser.add_argument("--no-file-summary", action="store_true", help="Omit file summary")
    parser.add_argument("--no-directory-structure", action="store_true", help="Omit directory tree")
    parser.add_argument("--no-security-check", action="store_true", help="Skip security scanning")
    args = parser.parse_args()

    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        sys.exit(1)

    ignore_pats = []
    if not args.no_default_patterns:
        ignore_pats = list(DEFAULT_IGNORES)
    if not args.no_gitignore:
        ignore_pats += load_gitignore_patterns(root)
    if args.ignore_patterns:
        ignore_pats += [p.strip() for p in args.ignore_patterns.split(",") if p.strip()]

    include_pats = [p.strip() for p in args.include.split(",") if p.strip()] if args.include else []

    # Collect files
    all_files: list[Path] = []
    for filepath in sorted(root.rglob("*")):
        if filepath.is_file():
            if not should_ignore(filepath, root, ignore_pats):
                try:
                    rel = str(filepath.resolve().relative_to(root.resolve()))
                except ValueError:
                    continue
                if matches_include(rel, include_pats):
                    all_files.append(filepath)

    # Process files
    files_data: list[dict] = []
    total_tokens = 0
    total_chars = 0
    for fp in all_files:
        content = get_file_content(fp)
        tokens = count_tokens(content)
        total_tokens += tokens
        total_chars += len(content)
        try:
            rel = str(fp.resolve().relative_to(root.resolve()))
        except ValueError:
            rel = str(fp)
        suffix = fp.suffix.lstrip(".") if fp.suffix else ""
        files_data.append({
            "path": rel,
            "tokens": tokens,
            "chars": len(content),
            "language": suffix,
            "content": content,
        })

    # Security scan (basic)
    security_issues = []
    if not args.no_security_check:
        sensitive_patterns = [
            (r"-----BEGIN.*PRIVATE KEY-----", "Private key detected"),
            (r"ghp_[A-Za-z0-9]{36}", "GitHub personal access token"),
            (r"gho_[A-Za-z0-9]{36}", "GitHub OAuth token"),
            (r"sk-[A-Za-z0-9]{32,}", "OpenAI API key"),
        ]
        import re
        for fp, content in [(f["path"], f["content"]) for f in files_data]:
            for pat, desc in sensitive_patterns:
                if re.search(pat, content):
                    security_issues.append(f"{fp}: {desc}")

    # Directory structure
    tree_str = build_file_tree(all_files, root)

    # Build output
    meta = {
        "total_files": len(all_files),
        "total_tokens": total_tokens,
        "total_chars": total_chars,
        "root": str(root),
    }

    if not args.no_directory_structure:
        meta["directory_structure"] = format_tree(tree_str)

    if not args.no_file_summary:
        sorted_files = sorted(files_data, key=lambda f: f["tokens"], reverse=True)
        meta["top_files"] = [
            {"path": f["path"], "tokens": f["tokens"]} for f in sorted_files[:10]
        ]

    if args.include_diffs:
        diffs = get_git_diffs(root)
        if diffs:
            meta["git_diffs"] = diffs

    if args.include_logs:
        logs = get_git_logs(root, args.include_logs_count)
        if logs:
            meta["git_logs"] = logs

    if security_issues:
        meta["security_issues"] = security_issues

    output_fn = OUTPUTTERS.get(args.style, output_json)
    result = output_fn(files_data, meta)

    if args.stdout:
        print(result)
    elif args.output:
        outpath = Path(args.output)
        outpath.write_text(result)
        print(f"✔ Packed {len(all_files)} files ({total_tokens} tokens) → {outpath}", file=sys.stderr)
    else:
        default_name = f"repomix-output.{args.style if args.style != 'plain' else 'txt'}"
        outpath = root / default_name
        outpath.write_text(result)
        print(f"✔ Packed {len(all_files)} files ({total_tokens} tokens) → {outpath}", file=sys.stderr)


if __name__ == "__main__":
    main()
