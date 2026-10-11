"""One-click source upload; local environments, videos and nested Git stay intact."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "github-upload.json"
DEFAULT_IGNORE = ROOT / "upload-ignore.txt"
LIMIT = 100 * 1024 * 1024
SKIP_DIRS = {".git", ".github-sync", "__pycache__", ".pytest_cache", ".mypy_cache",
             ".ruff_cache", ".venv", "venv", "node_modules", ".worktrees"}
SKIP_ROOT_DIRS = {".edge-preview-hd"}
VIDEO_SUFFIXES = {".mp4", ".webm", ".avi", ".mov", ".mkv", ".m4v", ".mpg",
                  ".mpeg", ".wmv"}


def find_git() -> str:
    installed = shutil.which("git")
    if installed:
        return installed
    for base in (os.environ.get("LOCALAPPDATA", "") + "/Programs",
                 os.environ.get("ProgramFiles", "C:/Program Files"),
                 os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")):
        candidate = Path(base) / "Git/cmd/git.exe"
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("尚未安装 Git。请先运行：\n"
                       "winget install --id Git.Git --exact --source winget --scope user\n"
                       "安装后重新双击 upload-to-github.cmd。")


def git(exe: str, cwd: Path, *args: str, input_data: bytes | None = None,
        capture: bool = False, allowed: tuple[int, ...] = (0,)) -> bytes:
    # Git does not automatically use Windows browser proxy settings.
    # Reuse an enabled system proxy without changing global Git configuration.
    environment = os.environ.copy()
    proxies = urllib.request.getproxies()
    for scheme in ("http", "https"):
        if scheme in proxies and not environment.get(scheme + "_proxy") and not environment.get(scheme.upper() + "_PROXY"):
            value = proxies[scheme]
            environment[scheme.upper() + "_PROXY"] = value if "://" in value else "http://" + value
    result = subprocess.run([exe, "-c", "core.quotepath=false", *args], cwd=cwd,
                            input=input_data, stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, env=environment)
    if result.returncode not in allowed:
        detail = result.stderr.decode("utf-8", errors="replace") if capture else ""
        raise RuntimeError(f"Git 操作失败（{args[0]}，退出码 {result.returncode}）。\n{detail}"
                           "\n请检查网络、GitHub 登录和仓库权限。脚本不会强制覆盖远程历史。")
    return result.stdout or b""


def source_files(root: Path, exe: str, git_dir: Path) -> list[str]:
    candidates = []
    for base, dirs, files in os.walk(root, followlinks=False):
        parent = Path(base)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS
                         and not (parent == root and d in SKIP_ROOT_DIRS)
                         and not (parent / d).is_symlink()
                         and not (parent / d / "pyvenv.cfg").is_file()
                         and not (ROOT != root and (parent / d).resolve() == ROOT))
        for name in sorted(files):
            path = parent / name
            if (path.is_symlink() or name in {".git", "github-upload.json"}
                    or path.suffix.lower() in VIDEO_SUFFIXES):
                continue
            candidates.append(path.relative_to(root).as_posix())
    if not candidates:
        raise RuntimeError("没有找到可以上传的文件。")
    ignore_option = ["-c", f"core.excludesFile={DEFAULT_IGNORE}"] if DEFAULT_IGNORE.is_file() else []
    ignored_data = git(exe, root, *ignore_option, f"--git-dir={git_dir}", f"--work-tree={root}",
                       "check-ignore", "--no-index", "-z", "--stdin",
                       input_data=("\0".join(candidates) + "\0").encode("utf-8"),
                       capture=True, allowed=(0, 1))
    ignored = set(ignored_data.decode("utf-8").rstrip("\0").split("\0"))
    # Nested projects may ignore their own .gitignore and entire output directories.
    # Explicitly include only ignore rules and directory documentation there.
    selected = [p for p in candidates if p not in ignored
                or Path(p).name in {".gitignore", "VIDEO_FILES.md", "MODEL_FILES.md"}]
    oversized = [p for p in selected if (root / p).stat().st_size >= LIMIT]
    if oversized:
        raise RuntimeError("以下文件达到或超过 100 MiB，请加入 .gitignore 或另行使用 Git LFS：\n"
                           + "\n".join(oversized))
    return sorted(selected)


def safe_target(mirror: Path, relative: str) -> Path:
    parts = relative.split("/")
    if not relative or any(p in {"", ".", "..", ".git"} for p in parts):
        raise RuntimeError(f"同步路径不合法：{relative}")
    candidate = mirror.joinpath(*parts)
    candidate.resolve().relative_to(mirror.resolve())
    return candidate


@contextmanager
def upload_lock(state: Path):
    import msvcrt
    with (state / "upload.lock").open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError("另一个上传窗口正在运行，请等它结束后再试。") from error
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def state_directory(root: Path, remote: str, exe: str) -> Path:
    base = root / ".github-sync"
    # Continue using an existing checkout created by the original project tool.
    legacy = base / "repository"
    if (legacy / ".git").is_dir():
        actual = git(exe, legacy, "remote", "get-url", "origin", capture=True).decode().strip()
        if actual == remote:
            return base
    target_id = hashlib.sha256(remote.encode("utf-8")).hexdigest()[:16]
    return base / "targets" / target_id


def upload(root: Path, remote: str, exe: str) -> None:
    state = state_directory(root, remote, exe)
    state.mkdir(parents=True, exist_ok=True)
    with upload_lock(state):
        sync(root, remote, exe, state)


def sync(root: Path, remote: str, exe: str, state: Path) -> None:
    mirror = state / "repository"
    manifest = state / "uploaded-files.json"
    if not mirror.exists():
        print("首次连接仓库；如出现登录页面，请使用有仓库写入权限的 GitHub 账号。", flush=True)
        git(exe, root, "clone", "--", remote, str(mirror))
    if not (mirror / ".git").is_dir():
        raise RuntimeError("同步副本不完整，请检查 .github-sync/repository；项目源文件未被修改。")
    actual_remote = git(exe, mirror, "remote", "get-url", "origin", capture=True).decode().strip()
    if actual_remote != remote:
        raise RuntimeError("同步副本的远程地址与目标仓库不同，已停止上传。")
    index = git(exe, mirror, "ls-files", "--stage", capture=True)
    if any(line.startswith(b"160000 ") for line in index.splitlines()):
        raise RuntimeError("目标仓库已有 Git 子模块，请先确认子模块布局再上传。")
    git(exe, mirror, "config", "core.longpaths", "true")
    git(exe, mirror, "fetch", "origin")
    head = git(exe, mirror, "rev-parse", "--verify", "HEAD", capture=True, allowed=(0, 128))
    if head:
        branch = git(exe, mirror, "symbolic-ref", "--short", "HEAD", capture=True).decode().strip()
        remote_ref = f"refs/remotes/origin/{branch}"
        remote_head = git(exe, mirror, "rev-parse", "--verify", remote_ref,
                          capture=True, allowed=(0, 128))
        if remote_head:
            git(exe, mirror, "merge", "--ff-only", remote_ref)
    else:
        branch = "main"
        git(exe, mirror, "symbolic-ref", "HEAD", "refs/heads/main")
    remote_parts = remote.replace("\\", "/").rstrip("/").split("/")
    default_name = remote_parts[-2] if len(remote_parts) > 1 else "GitHub User"
    for field, prompt in (("user.name", f"提交者名称（回车使用 {default_name}）："),
                          ("user.email", "提交邮箱（可填写 GitHub 设置中的隐私 noreply 邮箱）：")):
        current = git(exe, mirror, "config", "--get", field, capture=True, allowed=(0, 1)).strip()
        if not current:
            value = input(prompt).strip()
            if field == "user.name" and not value:
                value = default_name
            if not value:
                raise RuntimeError("首次上传需要填写提交邮箱；以后不再询问。")
            git(exe, mirror, "config", field, value)

    files = source_files(root, exe, mirror / ".git")
    previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else []
    if not isinstance(previous, list) or not all(isinstance(p, str) for p in previous):
        raise RuntimeError("同步清单格式不正确。")
    selected = set(files)
    for relative in previous:
        if relative not in selected:
            destination = safe_target(mirror, relative)
            if destination.is_file():
                destination.unlink()
    print(f"准备同步 {len(files)} 个文件；已应用视频排除规则和忽略规则。", flush=True)
    for relative in files:
        destination = safe_target(mirror, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, destination)

    # This staging checkout contains ordinary source directories, never nested .git metadata.
    git(exe, mirror, "add", "--all")
    mandatory = [p for p in files if Path(p).name in {".gitignore", "VIDEO_FILES.md", "MODEL_FILES.md"}]
    for offset in range(0, len(mandatory), 50):
        git(exe, mirror, "add", "--force", "--", *mandatory[offset:offset + 50])
    changed = git(exe, mirror, "diff", "--cached", "--name-only", "-z", capture=True)
    if changed:
        print(f"提交 {len(changed.rstrip(bytes([0])).split(bytes([0])))} 个文件变更。", flush=True)
        git(exe, mirror, "commit", "-m", f"Sync local project {datetime.now():%Y-%m-%d %H:%M:%S}")
    else:
        print("文件没有新变化，检查是否有上次尚未推送的提交。", flush=True)
    # Keep the manifest when a push fails so retries also handle deletions correctly.
    temporary = manifest.with_suffix(".tmp")
    temporary.write_text(json.dumps(files, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest)
    git(exe, mirror, "push", "--set-upstream", "origin", branch)
    print(f"上传成功：{remote.removesuffix('.git')}", flush=True)


def normalize_repository(value: str) -> str:
    value = value.strip().rstrip("/")
    match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", value)
    if not match:
        raise RuntimeError("仓库地址应为 https://github.com/用户名/仓库名；请先在 GitHub 创建仓库。")
    return value if value.endswith(".git") else value + ".git"


def load_configuration() -> dict[str, str]:
    if CONFIG.is_file():
        values = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
        if not isinstance(values, dict):
            raise RuntimeError("github-upload.json 必须是 JSON 对象。")
        for key in ("folder", "repository"):
            if not isinstance(values.get(key, ""), str):
                raise RuntimeError(f"github-upload.json 中 {key} 必须是字符串。")
        return {"folder": values.get("folder", ""), "repository": values.get("repository", "")}
    return {"folder": "", "repository": ""}


def save_configuration(folder: Path, repository: str) -> None:
    temporary = CONFIG.with_suffix(".tmp")
    temporary.write_text(json.dumps({"folder": str(folder), "repository": repository},
                                    ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(CONFIG)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="只查看上传清单，不连接 GitHub")
    mode.add_argument("--configure", action="store_true", help="重新选择文件夹和仓库，仅保存设置")
    parser.add_argument("--folder", help="要上传的文件夹路径")
    parser.add_argument("--repo", help="目标 GitHub 仓库地址")
    args = parser.parse_args()
    try:
        exe = find_git()
        config = load_configuration()
        folder_text = args.folder if args.folder is not None else ("" if args.configure else config["folder"])
        if not folder_text:
            folder_text = input("要上传的文件夹路径（可把文件夹拖入窗口）：").strip().strip('"')
        if not folder_text:
            raise RuntimeError("尚未填写要上传的文件夹。")
        root = Path(os.path.expandvars(folder_text)).expanduser()
        if not root.is_absolute():
            root = ROOT / root
        root = root.resolve()
        if not root.is_dir():
            raise RuntimeError(f"文件夹不存在：{root}")
        remote_text = args.repo if args.repo is not None else ("" if args.configure else config["repository"])
        if not args.preview and not remote_text:
            remote_text = input("目标 GitHub 仓库地址：https://github.com/用户名/仓库名\n> ").strip()
        remote = normalize_repository(remote_text) if remote_text else ""
        save_configuration(root, remote)
        print(f"源文件夹：{root}", flush=True)
        if args.configure:
            print(f"目标仓库：{remote}\n设置已保存。双击 upload-to-github.cmd 开始上传。", flush=True)
            return 0
        if args.preview:
            state = root / ".github-sync"
            state.mkdir(exist_ok=True)
            preview = state / "preview"
            if not (preview / ".git").is_dir():
                git(exe, root, "init", "--quiet", str(preview))
            files = source_files(root, exe, preview / ".git")
            total = sum((root / p).stat().st_size for p in files)
            for path in files:
                print(path)
            print(f"\n预计上传 {len(files)} 个文件，合计 {total / 1024 / 1024:.1f} MiB。未连接 GitHub。")
        else:
            print(f"目标仓库：{remote}", flush=True)
            upload(root, remote, exe)
        return 0
    except (RuntimeError, OSError, ValueError, EOFError, subprocess.SubprocessError) as error:
        print(f"\n上传未完成：{error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n上传已取消；可以重新运行脚本。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
