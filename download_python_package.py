import os
import re
import subprocess
import sys
import threading
import hashlib
import json
import tempfile
import time
import datetime
import atexit
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

PACKAGE_LIST = Path(r".\requirements.txt")
DOWNLOAD_DIR = Path(r"E:\python包下载\python311-packages")
PROGRESS_FILE = DOWNLOAD_DIR / ".download_progress.txt"
FAILURE_LOG = DOWNLOAD_DIR / "download_failures.txt"

# 阿里云为主源；清华源为备用源（解析和下载均注入双源，uv 内部自动轮换）。
INDEX_URLS = (
    "https://mirrors.aliyun.com/pypi/simple/",
    "https://pypi.tuna.tsinghua.edu.cn/simple/",
)

# Python 3.11 的 Windows x86 和 Linux ARM64 目标。
PLATFORMS = (
    "win_amd64",
    "manylinux2014_x86_64",
    "manylinux2014_aarch64",
)

# Phase 2: 平台间并发解析依赖树的线程数
PHASE2_MAX_WORKERS = min(4, len(PLATFORMS))

# uv 缓存与锁目录迁移，避免吃满 C 盘
UV_CACHE_DIR = DOWNLOAD_DIR / "uv_cache"
UV_LOCK_DIR = DOWNLOAD_DIR / "uv_lock"
# pip 下载缓存目录
PIP_CACHE_DIR = DOWNLOAD_DIR / "pip_cache"
# 临时文件目录（tempfile 重定向），避免写 C:\Users\...\AppData\Local\Temp
TEMP_DIR = DOWNLOAD_DIR / "temp"

# 每组包数量（15000 包 ÷ 100 = 150 组）
GROUP_SIZE = 100

# uv 子进程超时（秒），防止网络挂起时永久阻塞
UV_COMPILE_TIMEOUT = 600    # 10 分钟
UV_DOWNLOAD_TIMEOUT = 1800  # 30 分钟

# 核心映射：将标准平台标签映射为 uv 规范的目标平台参数
UV_PLATFORM_MAP = {
    "win_amd64": "windows",
    "manylinux2014_x86_64": "x86_64-manylinux2014",
    "manylinux2014_aarch64": "aarch64-manylinux2014",
}


class _TeeLogger:
    """将 stdout 同时输出到控制台和日志文件，两边每行都加时间戳。

    用法：sys.stdout = _TeeLogger(log_path, sys.stdout)，程序退出时由 atexit 自动恢复。
    """
    def __init__(self, log_path, original_stdout):
        self._original = original_stdout
        self._file = open(log_path, "a", encoding="utf-8")
        self._lock = threading.Lock()
        self._buffer = ""

    def write(self, msg):
        # 按行缓冲：每凑齐一行就同时往控制台和文件写（带时间戳）
        with self._lock:
            self._buffer += msg
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                stamped = f"[{ts}] {line}\n"
                self._original.write(stamped)
                self._file.write(stamped)
            self._file.flush()

    def flush(self):
        with self._lock:
            if self._buffer:
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                stamped = f"[{ts}] {self._buffer}\n"
                self._original.write(stamped)
                self._file.write(stamped)
                self._buffer = ""
            self._file.flush()
        self._original.flush()

    def close(self):
        self.flush()
        self._file.close()


def _normalize_name(name):
    """包名规范化（PEP 503：统一为小写连字符，并去除 uv 错误信息中的反引号包裹）。"""
    name = name.strip("`")
    return re.sub(r"[-_.]+", "-", name).lower()


def _check_uv_available():
    """检查 uv 是否在 PATH 中可用。"""
    try:
        result = subprocess.run(
            ["uv", "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0, (result.stdout or result.stderr).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False, None


def read_packages():
    """读取并去重 requirements.txt 中的包名。"""
    packages = []
    seen = set()
    with PACKAGE_LIST.open(encoding="utf-8") as file:
        for line in file:
            requirement = line.split("#", 1)[0].strip()
            if not requirement or requirement.startswith(("-", "--")):
                continue

            package_name = re.split(r"[<>=!~;\[\]\s]", requirement, maxsplit=1)[0]
            package_name = re.sub(r"[-_.]+", "-", package_name).lower()
            if package_name and package_name not in seen:
                packages.append(requirement)
                seen.add(package_name)
    return packages


def _parse_uv_compile_output(stdout):
    """从 uv pip compile 的输出中提取依赖清单。"""
    deps = []
    for line in stdout.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "#", "git+")):
            continue
        if "==" in line:
            deps.append(line)
    return deps


def _run_uv_compile(req_path, platform, index_urls, cache_subdir=None):
    """调用 uv 进程进行纯元数据依赖树解析（不下载），双源 + 超时保护。"""
    uv_platform = UV_PLATFORM_MAP.get(platform)
    if not uv_platform:
        raise RuntimeError(f"未配置 uv 平台映射：{platform}")

    command = [
        "uv", "pip", "compile",
        "-q",
        "--python-version", "3.11",
        "--python-platform", uv_platform,
        "--index-url", index_urls[0],
        "--extra-index-url", index_urls[1],
        "--index-strategy", "unsafe-best-match",
        "--only-binary=:all:",
        str(req_path),
    ]
    cache_dir = UV_CACHE_DIR / cache_subdir if cache_subdir else UV_CACHE_DIR
    try:
        result = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=UV_COMPILE_TIMEOUT,
            env={**os.environ, "UV_CACHE_DIR": str(cache_dir), "RUST_LOG": "warn"},
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"uv pip compile 超时（{UV_COMPILE_TIMEOUT}秒）")
    if result.returncode != 0:
        raise RuntimeError(f"uv pip compile 失败 (exit {result.returncode}):\n{(result.stderr or '')}")
    return _parse_uv_compile_output(result.stdout), (result.stderr or "")


def _extract_problem_packages(stderr):
    """从 uv 报错信息中提取导致解析失败的包名（无 wheel / 平台不匹配 / 元数据损坏等）。"""
    # 规范化换行：uv 错误信息常跨行（如 "with a\n      matching"），将连续空白折叠为单个空格
    text = re.sub(r"\s+", " ", stderr)
    found = []
    direct_patterns = [
        r"all versions of (\S+) have no usable wheels",
        r"all versions of (\S+) have no wheels with a matching",
        r"only the following versions of (\S+) are available",
        # 元数据损坏（brave-search、wfuzz 等）
        r"(\S+) has invalid metadata",
        # 平台标签不匹配（eccodeslib 等）
        r"no wheels with a matching platform tag.*?for\s+(\S+)",
        # 找不到发行版
        r"because no distributions were found for\s+(\S+)",
        # 无匹配平台标签的变体：如 "all versions of X have no wheels..."
        r"all versions of (\S+) have no wheels",
    ]
    for pat in direct_patterns:
        found.extend(re.findall(pat, text))

    for m in re.finditer(r"you require\s+(.*?)\s*,\s+we can conclude", text, re.DOTALL):
        clause = m.group(1)
        for part in re.split(r"\s+and\s+", clause):
            name = re.split(r"[<>=!~;\s\[]", part.strip())[0]
            name = name.strip().rstrip(",")
            if name:
                found.append(name)

    seen = set()
    result = []
    for p in found:
        normalized = _normalize_name(p)
        if normalized and normalized not in seen and normalized not in ("your", "and"):
            seen.add(normalized)
            result.append(normalized)
    return result


def _extract_package_name(line):
    """从需求行中提取规范化包名（与 read_packages 一致的正则提取）。"""
    name = re.split(r"[<>=!~;\[\]\s]", line.strip(), maxsplit=1)[0]
    return _normalize_name(name)


def _run_group_with_retry(req_path, platform, index_urls, group_idx, max_rounds=60, log=None):
    """单组 uv 解析，失败时循环剔除问题包并重试。"""
    cache_subdir = f"group{group_idx}"
    original_lines = req_path.read_text(encoding="utf-8").splitlines()
    excluded = set()
    retry_path = req_path.parent / f"{req_path.stem}_retry{group_idx}{req_path.suffix}"

    try:
        for round_num in range(max_rounds):
            excluded_normalized = {_normalize_name(p) for p in excluded}
            current_lines = [
                l for l in original_lines
                if l.strip() and _extract_package_name(l) not in excluded_normalized
            ]

            # 所有包均被剔除时直接返回空依赖
            if not current_lines:
                if log:
                    log(f"  组{group_idx}: 所有包均已剔除，返回空依赖")
                return [], sorted(excluded)

            retry_path.write_text("\n".join(current_lines), encoding="utf-8")
            current_path = retry_path if excluded else req_path

            try:
                deps, _ = _run_uv_compile(current_path, platform, index_urls, cache_subdir)
                if excluded and log:
                    log(f"  组{group_idx}: 重试成功，已剔除 {len(excluded)} 个问题包: {sorted(excluded)}")
                return deps, sorted(excluded)
            except RuntimeError as e:
                new_problems = _extract_problem_packages(str(e))
                if not new_problems:
                    raise
                new_normalized = {_normalize_name(p) for p in new_problems} - excluded_normalized
                if not new_normalized:
                    raise
                excluded.update(new_problems)
                if log:
                    log(f"  组{group_idx}: 第 {round_num+1} 轮解析失败，剔除问题包: {sorted(new_problems)}")
        raise RuntimeError(f"uv 解析循环重试 {max_rounds} 轮仍失败，已剔除: {sorted(excluded)[:10]}")
    finally:
        retry_path.unlink(missing_ok=True)


def _group_sha256(packages):
    """计算一组包内容的哈希值（用于 Lock 校验）。"""
    h = hashlib.sha256()
    for pkg in packages:
        h.update(pkg.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _load_group_lock(platform, group_idx, group_hash, uv_version):
    """读取分组的 Lock 文件，校验哈希 + uv 版本 + Python 版本 + 分组大小。"""
    lock_path = UV_LOCK_DIR / f"deps_lock_{platform}_group{group_idx}.json"
    if not lock_path.is_file():
        return None
    try:
        with lock_path.open(encoding="utf-8") as f:
            data = json.load(f)
        if (data.get("hash") == group_hash
                and data.get("uv_version") == uv_version
                and data.get("python_version") == "3.11"
                and data.get("group_size") == GROUP_SIZE):
            return data.get("deps", [])
        return None
    except (json.JSONDecodeError, OSError):
        return None


def _save_group_lock(platform, group_idx, group_hash, uv_version, deps):
    """保存分组的 Lock 文件。"""
    UV_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = UV_LOCK_DIR / f"deps_lock_{platform}_group{group_idx}.json"
    try:
        with lock_path.open("w", encoding="utf-8") as f:
            json.dump({
                "hash": group_hash,
                "uv_version": uv_version,
                "python_version": "3.11",
                "group_size": GROUP_SIZE,
                "deps": deps,
            }, f, ensure_ascii=False)
    except OSError:
        pass


def _split_packages_by_size(packages, group_size):
    """按固定大小切分包列表，负载天然均衡。"""
    groups = []
    for i in range(0, len(packages), group_size):
        groups.append(packages[i:i + group_size])
    return groups


def _write_group_file(packages, group_idx):
    """将一组包写入临时文件。"""
    fd, path = tempfile.mkstemp(suffix=f"_group{group_idx}.txt", prefix="uv_split_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(packages) + "\n")
    return Path(path)


def _pip_download_cmd(platform, platform_dir, index_urls, no_deps=True):
    """构建 pip download 基础命令（不含包参数）。"""
    urls = index_urls or INDEX_URLS
    cmd = [
        sys.executable, "-m", "pip", "download",
        "--python-version", "3.11",
        "--platform", platform,
        "--only-binary=:all:",
        "--index-url", urls[0],
        "--extra-index-url", urls[1],
        "--cache-dir", str(PIP_CACHE_DIR),
        "-d", str(platform_dir),
    ]
    if no_deps:
        cmd.append("--no-deps")
    return cmd


# 模块级缓存：避免每次 download_via_cli 调用都重新扫描目录（15000 个文件扫描成本高）
_downloaded_cache = {}  # str(platform_dir) -> set of normalized package names
_downloaded_cache_lock = threading.Lock()


def _scan_platform_dir(platform_dir):
    """扫描目录，提取已下载包名的规范化集合（内部不使用缓存）。"""
    downloaded = set()
    if not platform_dir.is_dir():
        return downloaded
    for item in platform_dir.iterdir():
        if not item.is_file():
            continue
        fname = item.name
        # 去除扩展名
        for ext in (".whl", ".tar.gz", ".zip", ".tgz", ".tar.bz2"):
            if fname.endswith(ext):
                fname = fname[: -len(ext)]
                break
        parts = fname.split("-")
        # 版本号是第一个以数字开头且包含 . 的部分
        for i, part in enumerate(parts):
            if part and part[0].isdigit() and "." in part:
                pkg_name = "-".join(parts[:i])
                if pkg_name:
                    downloaded.add(_normalize_name(pkg_name))
                break
    return downloaded


def _get_downloaded_package_names(platform_dir, refresh=False):
    """返回已下载包名的规范化集合（带缓存，避免重复扫描）。

    refresh=True 时强制重新扫描目录。
    """
    key = str(platform_dir)
    with _downloaded_cache_lock:
        if not refresh and key in _downloaded_cache:
            return _downloaded_cache[key]
    downloaded = _scan_platform_dir(platform_dir)
    with _downloaded_cache_lock:
        _downloaded_cache[key] = downloaded
    return downloaded


def _add_to_downloaded_cache(platform_dir, pkg_spec):
    """下载成功后，将包名加入缓存（避免下次重复扫描或误判）。"""
    key = str(platform_dir)
    name = _extract_pkg_name(pkg_spec)
    with _downloaded_cache_lock:
        if key in _downloaded_cache:
            _downloaded_cache[key].add(name)
        else:
            _downloaded_cache[key] = {name}


def _extract_pkg_name(spec):
    """从需求规格中提取规范化包名（如 'numpy==1.24.0' -> 'numpy'）。"""
    name = re.split(r"[<>=!~;\[\]\s]", spec.strip(), maxsplit=1)[0]
    return _normalize_name(name)


def _is_network_error(stderr_text):
    """判断 pip 错误输出是否为可重试的网络类错误。
    覆盖：连接中断、不完整读取、连接重置、SSL、超时、空文件(hash 校验失败)等。
    """
    if not stderr_text:
        return False
    keywords = (
        "ChunkedEncodingError",
        "Connection broken",
        "IncompleteRead",
        "ConnectionError",
        "Connection reset",
        "Connection aborted",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "ReadTimeoutError",
        "Read timed out",
        "SSLError",
        "MaxRetryError",
        "ProtocolError",
        "TimeoutExpired",
        # pip hash 校验失败：常因镜像源返回 0 字节空文件导致
        "do not match the hashes",
        # 空文件的 SHA256 固定值：精准识别"下到空文件"场景
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )
    text = stderr_text.lower() if isinstance(stderr_text, str) else ""
    return any(kw.lower() in text for kw in keywords)


def _get_dir_size_bytes(path):
    """获取目录总大小（字节）。仅扫描文件 stat，不递归子目录。"""
    total = 0
    if not path.is_dir():
        return total
    try:
        for item in path.iterdir():
            if item.is_file():
                try:
                    total += item.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _format_speed(bytes_per_sec):
    """格式化下载速度：>=1MB/s 用 MB/s，否则用 KB/s。"""
    if bytes_per_sec <= 0:
        return "0.00KB/s"
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / (1024 * 1024):.2f}MB/s"
    return f"{bytes_per_sec / 1024:.2f}KB/s"


def download_via_cli(package_specs, platform, platform_dir, index_urls=None,
                     no_deps=True, log=None, label="", max_workers=6):
    """
    通过 pip download 并行逐包下载。自动跳过已下载的包。
    pip download 的批量模式是全有全无（一个包失败则整批不保存），因此直接并行逐包下载。
    package_specs: list of requirement strings (如 ["requests", "numpy==1.24.0"])
    返回: (all_success, failed_list, output_text)
    """
    if not package_specs:
        return True, [], ""

    # 跳过已下载的包（带缓存，首次扫描后后续调用直接命中缓存）
    downloaded = _get_downloaded_package_names(platform_dir)
    to_download = [p for p in package_specs if _extract_pkg_name(p) not in downloaded]
    skipped = len(package_specs) - len(to_download)
    if log:
        log(f"  {label}: 共 {len(package_specs)} 包，跳过已下载 {skipped} 包，需下载 {len(to_download)} 包")
    if not to_download:
        return True, [], ""

    base_cmd = _pip_download_cmd(platform, platform_dir, index_urls, no_deps=no_deps)
    failed = []
    failed_lock = threading.Lock()
    done_count = [0]
    total = len(to_download)

    # 记录本组下载起始时间和目录起始字节数，用于计算下载速度
    dl_start_time = time.monotonic()
    dl_start_bytes = _get_dir_size_bytes(platform_dir)

    def _download_one(pkg):
        def _attempt(cmd):
            """执行一次 pip download，返回 (success, error_text)。"""
            try:
                r = subprocess.run(
                    cmd + [pkg],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=600,
                )
                if r.returncode == 0:
                    return True, ""
                return False, (r.stderr or "")
            except subprocess.TimeoutExpired:
                return False, "TimeoutExpired"

        try:
            # 第一次尝试（原源）
            ok, err = _attempt(base_cmd)
            if ok:
                _add_to_downloaded_cache(platform_dir, pkg)
                return True

            # 仅网络类错误才重试（超时也属于网络错误）
            if _is_network_error(err):
                # 第二次：原源重试一次
                ok2, err2 = _attempt(base_cmd)
                if ok2:
                    _add_to_downloaded_cache(platform_dir, pkg)
                    return True
                # 第三次：换源（主备互换）重试一次
                swapped_urls = list(index_urls or INDEX_URLS)[::-1]
                swapped_cmd = _pip_download_cmd(platform, platform_dir, swapped_urls, no_deps=no_deps)
                ok3, err3 = _attempt(swapped_cmd)
                if ok3:
                    _add_to_downloaded_cache(platform_dir, pkg)
                    return True
                last_err = err3 or err2 or err
                tag = "重试失败"
            else:
                last_err = err
                tag = "失败"

            with failed_lock:
                failed.append(pkg)
            if log:
                err_line = (last_err or "").strip().split("\n")[-1][:200] if (last_err or "").strip() else "unknown"
                log(f"  {label}: {tag} - {pkg}: {err_line}")
            return False
        finally:
            with failed_lock:
                done_count[0] += 1
                if log and (done_count[0] % 20 == 0 or done_count[0] == total):
                    # 计算本组实时下载速度
                    elapsed = time.monotonic() - dl_start_time
                    current_bytes = _get_dir_size_bytes(platform_dir)
                    downloaded_bytes = current_bytes - dl_start_bytes
                    speed = downloaded_bytes / elapsed if elapsed > 0 else 0
                    speed_str = _format_speed(speed)
                    log(f"  {label}: 该组进度 {done_count[0]}/{total}（失败 {len(failed)}），速度 {speed_str}")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(_download_one, to_download))

    if failed:
        msg = f"{len(failed)}/{total} 包下载失败: {failed[:20]}"
        if len(failed) > 20:
            msg += f" ...（共 {len(failed)} 个）"
        return False, failed, msg
    return True, [], ""


def load_progress():
    if not PROGRESS_FILE.is_file():
        return set()
    with PROGRESS_FILE.open(encoding="utf-8") as file:
        return {line.strip() for line in file if line.strip()}


def mark_progress(progress, key, progress_lock):
    with progress_lock:
        if key in progress:
            return
        progress.add(key)
        with PROGRESS_FILE.open("a", encoding="utf-8") as file:
            file.write(f"{key}\n")
            file.flush()


def process_group(packages, group_idx, total_groups, platform, platform_dir,
                  index_urls, uv_version, progress, progress_lock, log):
    """处理单个分组：uv 依赖解析（含 Lock 缓存）+ uv 批量下载。"""
    progress_key = f"__phase2_group{group_idx}__{platform}"
    if progress_key in progress:
        log(f"[{platform}] 组 {group_idx}/{total_groups}: 已完成，跳过")
        return True, None, []

    group_hash = _group_sha256(packages)
    excluded = []

    # 1. 尝试 Lock 缓存 —— 命中则跳过 uv 解析
    deps = _load_group_lock(platform, group_idx, group_hash, uv_version)
    if deps is not None:
        log(f"[{platform}] 组 {group_idx}/{total_groups} ({len(packages)}包): Lock 命中，跳过解析")
    else:
        log(f"[{platform}] 组 {group_idx}/{total_groups} ({len(packages)}包): 开始 uv 依赖解析...")
        group_path = _write_group_file(packages, group_idx)
        try:
            deps, excluded = _run_group_with_retry(
                group_path, platform, index_urls, group_idx, log=log
            )
        finally:
            group_path.unlink(missing_ok=True)
        log(f"[{platform}] 组 {group_idx}/{total_groups}: 解析完成，{len(deps)} 个依赖"
            + (f"，剔除 {len(excluded)} 个问题包" if excluded else ""))
        _save_group_lock(platform, group_idx, group_hash, uv_version, deps)

    # 无依赖时直接标记完成
    if not deps:
        log(f"[{platform}] 组 {group_idx}/{total_groups}: 无依赖需下载，标记完成")
        mark_progress(progress, progress_key, progress_lock)
        return True, None, excluded

    # 2. pip 批量下载（deps 已由 uv compile 解析完毕，用 --no-deps 直接下载）
    label = f"[{platform}] 组 {group_idx}/{total_groups}"
    log(f"{label}: 开始下载 ({len(deps)} 个依赖)...")
    success, failed, output = download_via_cli(
        deps, platform, platform_dir, index_urls=index_urls,
        no_deps=True, log=log, label=label
    )

    if success:
        log(f"{label}: 下载完成")
        mark_progress(progress, progress_key, progress_lock)
    else:
        # 即使部分包失败也标记完成（避免无限重试），失败包记录到日志
        mark_progress(progress, progress_key, progress_lock)
        try:
            with FAILURE_LOG.open("a", encoding="utf-8") as file:
                file.write(f"[{platform}] __phase2_group{group_idx}__\n{output}\n\n")
        except OSError:
            pass
        log(f"{label}: 下载部分失败，已标记完成（失败包: {len(failed)} 个）")

    return success, output, excluded


def main():
    if not PACKAGE_LIST.is_file():
        raise FileNotFoundError(f"找不到包列表：{PACKAGE_LIST}")

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    UV_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    UV_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    PIP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    # 将 tempfile 模块的全局临时目录重定向到下载目录下，避免写 C 盘
    tempfile.tempdir = str(TEMP_DIR)

    platform_dirs = {platform: DOWNLOAD_DIR / platform for platform in PLATFORMS}
    for platform_dir in platform_dirs.values():
        platform_dir.mkdir(parents=True, exist_ok=True)

    # 设置日志文件：按执行日期+时分秒命名，所有控制台输出同时写入文件（每行带时间戳）
    # 注意：Windows 文件名不允许半角冒号":"，故时分秒之间用全角冒号"："分隔
    now_dt = datetime.datetime.now()
    log_filename = f"{now_dt.strftime('%Y-%m-%d')}-{now_dt.strftime('%H：%M：%S')}-download_python_packages.log"
    log_path = DOWNLOAD_DIR / log_filename
    _tee = _TeeLogger(log_path, sys.stdout)
    _original_stdout = sys.stdout
    sys.stdout = _tee

    def _restore_stdout():
        sys.stdout = _original_stdout
        _tee.close()
    atexit.register(_restore_stdout)

    print(f"===== 开始执行（日志文件: {log_path}）=====")

    # 1. 检测 uv 可用性（脚本全局强依赖 uv 引擎）
    uv_ok, uv_version = _check_uv_available()
    if not uv_ok:
        print("【错误】未检测到 uv 引擎。本脚本已全线升级为 uv 加速架构，请先安装 uv。")
        print(" 安装命令 (PowerShell): irm https://astral.sh/uv/install.ps1 | iex")
        sys.exit(1)

    packages = read_packages()
    progress = load_progress()
    failures = []

    progress_lock = threading.Lock()
    output_lock = threading.Lock()

    def log(message):
        with output_lock:
            print(message, flush=True)

    groups = _split_packages_by_size(packages, GROUP_SIZE)
    total_groups = len(groups)

    print(f"已成功加载 uv 引擎: {uv_version}")
    print(f"包总数：{len(packages)}，目标平台数：{len(PLATFORMS)}")
    print(f"分组策略：每组 {GROUP_SIZE} 包，共 {total_groups} 组")

    # ===== Phase 1: 下载所有包本体（平台间并行，--no-deps，分组批量+逐包回退） =====
    print("\n===== Phase 1: 开始下载包本体（pip download，--no-deps）=====")

    phase1_platforms = [
        platform for platform in PLATFORMS
        if f"__phase1_batch__{platform}" not in progress
    ]

    if phase1_platforms:
        with ThreadPoolExecutor(max_workers=len(phase1_platforms)) as executor:
            def _do_phase1(platform):
                p_dir = platform_dirs[platform]
                total_p1 = len(groups)
                p1_failed_all = []
                log(f"  [{platform}] Phase 1: 分 {total_p1} 组下载包本体（每组 {GROUP_SIZE} 包）...")
                for gi, group_pkgs in enumerate(groups, 1):
                    p1_key = f"__phase1_group{gi}__{platform}"
                    if p1_key in progress:
                        continue
                    label = f"  [{platform}] P1 组 {gi}/{total_p1}"
                    success, failed, output = download_via_cli(
                        group_pkgs, platform, p_dir, index_urls=INDEX_URLS,
                        no_deps=True, log=log, label=label
                    )
                    if not success and failed:
                        p1_failed_all.extend(failed)
                        try:
                            with FAILURE_LOG.open("a", encoding="utf-8") as file:
                                file.write(f"[{platform}] __phase1_group{gi}__\n{output}\n\n")
                        except OSError:
                            pass
                    mark_progress(progress, p1_key, progress_lock)
                    log(f"  [{platform}] Phase 1: 组 {gi}/{total_p1} 完成"
                        + (f"（失败 {len(failed)} 包）" if failed else ""))
                mark_progress(progress, f"__phase1_batch__{platform}", progress_lock)
                log(f"  [{platform}] Phase 1 全部完成！"
                    + (f"（共 {len(p1_failed_all)} 个包下载失败）" if p1_failed_all else ""))
                if p1_failed_all:
                    return ("__phase1_batch__", platform, f"Phase 1 失败包: {p1_failed_all[:50]}")
                return None

            futures = {executor.submit(_do_phase1, p): p for p in phase1_platforms}
            for future in as_completed(futures):
                platform = futures[future]
                try:
                    result = future.result()
                    if result:
                        failures.append(result)
                except Exception as e:
                    log(f"  [{platform}] Phase 1 线程异常: {e}")
                    failures.append(("__phase1_batch__", platform, str(e)))
    else:
        print("所有平台 Phase 1 已完成，跳过。")

    # ===== Phase 2: 分组进行 uv 依赖解析 + uv 下载（平台间并行，组内顺序） =====
    print(f"\n===== Phase 2: 分组依赖解析 + 下载（每组 {GROUP_SIZE} 包，顺序处理）=====")

    pending_platforms = [
        platform for platform in PLATFORMS
        if f"__phase2_complete__{platform}" not in progress
    ]

    if pending_platforms:
        with ThreadPoolExecutor(max_workers=PHASE2_MAX_WORKERS) as executor:
            def _wrap_phase2(platform):
                p_dir = platform_dirs[platform]
                log(f"[{platform}] 开始分组处理（共 {total_groups} 组，每组 {GROUP_SIZE} 包）...")

                platform_failures = []
                for idx, group_pkgs in enumerate(groups, 1):
                    try:
                        success, output, _ = process_group(
                            group_pkgs, idx, total_groups, platform, p_dir,
                            INDEX_URLS, uv_version, progress, progress_lock, log
                        )
                        if not success:
                            platform_failures.append((f"__phase2_group{idx}__", platform, output))
                    except Exception as e:
                        log(f"[{platform}] 组 {idx}/{total_groups}: 异常 - {e}")
                        platform_failures.append((f"__phase2_group{idx}__", platform, str(e)))

                if not platform_failures:
                    mark_progress(progress, f"__phase2_complete__{platform}", progress_lock)
                    log(f"[{platform}] 所有 {total_groups} 组处理完成！")
                else:
                    log(f"[{platform}] {len(platform_failures)}/{total_groups} 组下载失败")

                return platform_failures

            futures = {executor.submit(_wrap_phase2, p): p for p in pending_platforms}
            for future in as_completed(futures):
                platform = futures[future]
                try:
                    platform_failures = future.result()
                    failures.extend(platform_failures)
                except Exception as e:
                    log(f"[{platform}] Phase 2 线程异常: {e}")
                    failures.append(("__phase2__", platform, str(e)))
    else:
        print("所有平台 Phase 2 已完成，跳过。")

    # 3. 写出错误及失败日志（不截断，保留完整信息）
    if failures:
        with FAILURE_LOG.open("w", encoding="utf-8") as file:
            for requirement, platform, output in failures:
                file.write(f"[{platform}] {requirement}\n{output}\n\n")

    # 4. 最终战果清点
    file_count = sum(
        item.is_file()
        for platform_dir in platform_dirs.values()
        for item in platform_dir.iterdir()
    )
    print("\n================ 下载报告 ================")
    print(f" 状态: 运行完毕")
    print(f" 失败/警告任务块数：{len(failures)}" + (f"（详情参见: {FAILURE_LOG.name}）" if failures else ""))
    print(f" 最终本地已就绪包文件总量：{file_count} 个 whl/sdist 文件")
    print(f" 增量断点进度文件已回写：{PROGRESS_FILE}")
    print(f" 日志文件已保存：{log_path}")


if __name__ == "__main__":
    main()
