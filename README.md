# Python 包下载工具 使用说明

本目录下有两个脚本，配合使用：先用 `get_list_python_package.py` 生成包清单，再用 `download_python_package.py` 批量下载。

执行顺序：**先运行 1，再运行 2**。

---

## 1. get_list_python_package.py

### 作用
从 GitHub 上的 `top-pypi-packages` 数据源拉取近期最热门的 PyPI 包名列表，写入 `requirements.txt`，供下载脚本使用。

### 使用办法
```bash
pip install requests
python get_list_python_package.py
```
运行后会在当前目录生成（或覆盖）`requirements.txt`，并打印最终包数量。

### 需要更改的变量
| 变量 | 位置 | 默认值 | 说明 |
|------|------|--------|------|
| `OUTPUT` | 第 5 行 | `.\requirements.txt` | 输出清单文件路径 |
| `URL` | 第 9 行 | `https://hugovk.github.io/top-pypi-packages/top-pypi-packages-30-days.json` | 数据源 URL，可换成其它符合该 JSON 结构的源 |
| `packages[:20000]` | 第 32 行 | `20000` | 截取前 N 个热门包，按需调整数量 |

---

## 2. download_python_package.py

### 作用
读取 `requirements.txt`，针对多个目标平台批量下载 Python 3.11 的 wheel / sdist 文件。流程分两阶段：
- **Phase 1**：用 `pip download --no-deps` 并行下载包本体（平台间并发 + 分组并发 + 逐包回退）
- **Phase 2**：用 `uv pip compile` 做依赖树解析（带 Lock 缓存），再用 `pip download --no-deps` 下载解析出的依赖

特性：断点续传（进度文件）、双镜像源自动容错切换、分组并发、已下载包跳过、缓存目录全部重定向到下载目录（避免写满 C 盘）、所有控制台输出带时间戳同步写入日志文件。

### 使用办法
```bash
# 1. 先安装 uv 引擎（脚本强依赖）
# PowerShell:
irm https://astral.sh/uv/install.ps1 | iex

# 2. 确保 requirements.txt 已生成且路径正确
# 3. 运行下载脚本
python download_python_package.py
```
运行后：
- 包文件按平台分目录存放在 `DOWNLOAD_DIR\<platform>\` 下
- 断点进度写在 `DOWNLOAD_DIR\.download_progress.txt`（中断后再次运行会自动跳过已完成分组）
- 失败包记录在 `DOWNLOAD_DIR\download_failures.txt`
- 日志文件命名形如 `YYYY-MM-DD-HH：MM：SS-download_python_packages.log`

### 需要更改的变量
| 变量 | 位置 | 默认值 | 说明 |
|------|------|--------|------|
| `PACKAGE_LIST` | 第 15 行 | `.\requirements.txt` | 输入的包清单文件路径 |
| `DOWNLOAD_DIR` | 第 16 行 | `E:\python包下载\python311-packages` | **下载根目录**，所有平台子目录、缓存、日志、进度文件均在此目录下 |
| `INDEX_URLS` | 第 21-24 行 | 阿里云 + 清华 | 镜像源（主源 + 备源），下载失败会自动切换 |
| `PLATFORMS` | 第 27-31 行 | `win_amd64`、`manylinux2014_x86_64`、`manylinux2014_aarch64` | 目标平台列表，删减可只下部分平台 |
| `GROUP_SIZE` | 第 45 行 | `100` | 每组包数量，影响分组数与单次解析规模 |
| `UV_COMPILE_TIMEOUT` | 第 48 行 | `600` | uv 依赖解析超时（秒） |
| `UV_DOWNLOAD_TIMEOUT` | 第 49 行 | `1800` | uv 下载超时（秒） |
| Python 版本 | 多处 `--python-version` | `3.11` | 如需改为其它 Python 版本，需同步修改 `main`、`_run_uv_compile`、`_pip_download_cmd`、`_load_group_lock` 中的 `3.11` 字样 |

### 注意事项
- `DOWNLOAD_DIR` 务必改成你自己机器上的可用路径，默认 `E:\` 盘不一定存在
- 首次运行前需安装 `uv`，否则脚本会直接退出
- `requirements.txt` 中包数量较多时，首次下载耗时较长，可中断后重跑（自动断点续传）
- 如需调整并发度，可改 `download_via_cli` 的 `max_workers`（默认 6）和 `PHASE2_MAX_WORKERS`（默认 4）
