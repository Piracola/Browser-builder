# Browser-builder

[![Validate Builder](https://img.shields.io/github/actions/workflow/status/Piracola/Browser-builder/validate.yml?style=flat-square&label=构建器自检)](https://github.com/Piracola/Browser-builder/actions/workflows/validate.yml)
[![libportable](https://img.shields.io/badge/libportable-v9.0.9-blue?style=flat-square)](https://github.com/adonais/libportable/releases/tag/v9.0.9)
[![License](https://img.shields.io/github/license/Piracola/Browser-builder?style=flat-square&label=许可证)](LICENSE)

**简体中文** | [English](README.en.md)

Firefox 系列便携浏览器的通用构建器。

这个仓库不面向普通用户，它是三个浏览器仓库共用的构建引擎：把上游官方安装包，自动加工成开箱即用的便携版压缩包。普通用户请直接到下面的浏览器仓库下载成品。

| 浏览器仓库 | 说明 |
|------|------|
| [Firefox-Libportable](https://github.com/Piracola/Firefox-Libportable) | Firefox 便携版 |
| [Floorp_portable](https://github.com/Piracola/Floorp_portable) | Floorp 便携版 |
| [Zen-Libportable](https://github.com/Piracola/Zen-Libportable) | Zen 便携版 |

## 仓库分工

整个项目分成两层，职责边界很明确：

| 层级 | 内容 |
|------|------|
| **Browser-builder**（本仓库） | 构建脚本 `build.py`、注入二进制 `bin/`、可复用工作流、上游同步工具 |
| **浏览器仓库** | 只有三样东西：`libportable/portable.ini`、`开始.bat`、十几行的工作流调用 |

也就是说：**所有可执行文件和构建逻辑都集中在这里**，浏览器仓库里只剩配置。改构建流程只需要改本仓库一处，三个浏览器同时生效。

## 构建流程

`build.py` 的完整流程，其中带 ✅ 的是会让构建直接失败的校验点：

| 步骤 | 说明 |
|------|------|
| 1. 解析版本 | Firefox 查 Mozilla `firefox_versions.json`，Floorp / Zen 查 GitHub Releases API |
| 2. ✅ 校验注入二进制 | 对照 `bin/manifest.json` 逐个核对 SHA-256，不符就拒绝构建 |
| 3. 下载安装包 | 带重试，写入临时文件后再改名，避免半截文件被当成完整下载 |
| 4. ✅ 校验安装包 | Firefox 比对官方 `SHA512SUMS`，Floorp / Zen 比对 GitHub 发布的 asset digest |
| 5. 解包 | 用 7-Zip 解开安装包，定位浏览器主目录 |
| 6. 注入便携化 | 复制 `portable.ini` 和 `portable64.dll`，运行 `upcheck.exe -dll` |
| 7. ✅ 校验注入结果 | 读取 `mozglue.dll` 的 PE 导入表，确认 `portable64.dll` 真的被写了进去 |
| 8. 复制启动脚本 | 使用浏览器仓库自带的 `开始.bat` |
| 9. ✅ 便携性实测 | 无头启动一次浏览器，确认用户数据写入了便携目录而不是 `%APPDATA%` |
| 10. 打包 | 生成 `<Browser>_<版本>.7z` 和同名 `.sha256` 校验文件 |

第 9 步是整套流程里最重要的一环。注入工具返回 0 只代表"导入表改写成功"，不代表浏览器真的便携。这一步会把成品复制一份、实际启动，然后检查配置文件里 `PortableDataPath` 指定的目录是否真的产生了用户数据。**未注入的浏览器不会创建这个目录**，所以这个检查能真正拦住"注入静默失效"这类最伤用户的问题——例如浏览器大版本更新导致 hook 不再生效。

## 校验与溯源

成品的可信度，取决于每一个环节都能说清楚"这些字节从哪来"：

| 环节 | 保障方式 |
|------|----------|
| 官方安装包 | 与上游公布的哈希逐字节比对，不符直接终止 |
| 注入二进制 | `bin/manifest.json` 记录上游仓库、release 标签、归档 SHA-256 和逐文件 SHA-256，每次构建都重新核对 |
| 构建器版本 | 每次构建都把实际检出的构建器提交号写进发行说明，事后可以精确复现 |
| 成品 | 发行说明里附 SHA-256，并随包上传 `.sha256` 文件 |

### bin/ 目录

`bin/` 存放注入用的二进制，全部来自 [adonais/libportable](https://github.com/adonais/libportable) 的正式发布：

```text
bin/
├── manifest.json              来源与哈希清单
├── upcheck64.exe              注入器
├── portable64.dll             便携化运行时（会随成品一起分发）
├── portable(example).ini      上游配置模板（浏览器仓库没提供配置时兜底）
├── README                     上游说明文档（随成品分发）
└── LICENSE-libportable.txt    上游许可证（随成品分发）
```

### 同步上游

仓库里有每周运行的 [Check libportable Upstream](.github/workflows/check-libportable.yml) 工作流，发现上游有新版本就自动开 issue 提醒。**不会自动替换二进制**——注入组件直接决定成品是否便携，必须人工确认。

```powershell
# 查看是否有新版本
python tools/sync_libportable.py --check

# 同步到指定版本（会重新下载、校验、更新 bin/ 和 manifest.json）
python tools/sync_libportable.py --tag v9.0.9
```

同步后请手动触发一次任意浏览器仓库的构建，确认便携性校验依然通过，再让定时构建照常运行。

## 本地使用

### 前置依赖

| 依赖 | 说明 |
|------|------|
| Python 3.10+ | 与 GitHub Actions 保持一致 |
| requests | `pip install -r requirements.txt` |
| 7-Zip | 需要能调用 `7z`，或用 `--seven-z-path` 指定 |

### 常用命令

```powershell
# 只解析最新版本和下载地址，不下载、不打包（最快的冒烟检查）
python build.py --browser firefox --auto-version --check-only

# 完整构建
python build.py --browser firefox --auto-version `
  --libportable ..\Firefox-Libportable\libportable `
  --launcher ..\Firefox-Libportable\开始.bat

# 构建中文版 Firefox
python build.py --browser firefox --auto-version --lang zh-CN `
  --libportable ..\Firefox-Libportable\libportable
```

### 参数说明

| 参数 | 作用 |
|------|------|
| `--browser` | 浏览器：`firefox`、`floorp`、`zen` |
| `--auto-version` | 自动解析最新版本和安装包地址 |
| `--check-only` | 只解析版本，不下载不打包 |
| `--version` / `--url` | 手动指定版本号和安装包地址 |
| `--lang` | Firefox 安装包语言，默认 `en-US`，可用 `zh-CN` 等 |
| `--libportable` | 浏览器仓库里存放 `portable.ini` 的目录（`--check-only` 时可省略） |
| `--launcher` | 要打进成品的启动脚本 |
| `--bin-dir` | 注入二进制目录，默认是本仓库的 `bin/` |
| `--workspace` | 构建工作目录，默认当前目录 |
| `--seven-z-path` | `7z.exe` 路径 |
| `--keep-temp` | 保留 `temp_build/`，方便排查 |
| `--smoke-test-timeout` | 便携性实测的等待秒数，默认 120 |
| `--skip-smoke-test` | 跳过便携性实测（仅调试用） |
| `--allow-injection-warning` | 注入返回非 0 时继续（仅调试用） |

最后两个参数会跳过安全网，正式构建不要使用。

## 可复用工作流

三个浏览器仓库共用 [`.github/workflows/build-portable.yml`](.github/workflows/build-portable.yml)，各自的工作流只需要这么几行：

```yaml
jobs:
  build:
    permissions:
      contents: write
      actions: write
    uses: Piracola/Browser-builder/.github/workflows/build-portable.yml@main
    with:
      browser: firefox
      lang: en-US
      release-notes-url: 'https://www.mozilla.org/en-US/firefox/{version}/releasenotes/'
```

| 输入参数 | 默认值 | 说明 |
|----------|--------|------|
| `browser` | 必填 | `firefox` / `floorp` / `zen` |
| `lang` | `en-US` | Firefox 安装包语言 |
| `release-notes-url` | 空 | 上游发行说明地址，`{version}` 会被替换成版本号 |
| `builder-ref` | `main` | 检出构建器的分支或标签 |
| `python-version` | `3.10` | 构建用的 Python 版本 |
| `keep-workflow-runs` | `10` | 保留的历史运行记录条数 |

想要完全可复现的构建，把 `uses:` 的引用和 `builder-ref` 同时指向同一个 tag：

```yaml
    uses: Piracola/Browser-builder/.github/workflows/build-portable.yml@v1
    with:
      browser: firefox
      builder-ref: v1
```

（可复用工作流内部拿不到自身所在的提交，`github.job_workflow_sha` 在这个场景下是空的，所以需要显式传一次。）

工作流会解析版本、对比 `last_version.txt`（只有定时任务在版本未变时跳过）、构建、发布 Release、回写版本记录。

## 构建产物

```text
Firefox_153.0.7z          成品压缩包
Firefox_153.0.7z.sha256   校验文件
```

在 GitHub Actions 中还会写出这些输出变量：

| 输出变量 | 含义 |
|----------|------|
| `resolved_version` / `resolved_url` | 解析到的版本和安装包地址 |
| `browser_display` | 浏览器显示名，用于 Release 标题 |
| `artifact_name` / `artifact_path` | 成品压缩包文件名 |
| `artifact_sha256` | 成品的 SHA-256 |
| `checksum_path` | `.sha256` 校验文件名 |
| `libportable_version` | 本次使用的 libportable 版本 |

## 新增浏览器支持

Firefox 系的浏览器基本都能直接接入。在 `build.py` 里补两处：

1. `BROWSERS` 字典：加上可执行文件名、成品目录名、显示名。
2. `resolve_version_and_url`：补上版本和安装包地址的解析方式。

然后新建一个浏览器仓库，放进 `libportable/portable.ini`、`开始.bat` 和调用可复用工作流的配置即可。

**不要把浏览器专属的配置塞进本仓库**——这是这套分层能持续省事的前提。

## 常见问题

### 找不到 7-Zip

确认能直接运行 `7z`，或手动指定：`--seven-z-path "C:\Program Files\7-Zip\7z.exe"`。

### 注入二进制哈希不符

说明 `bin/` 里的文件和 `manifest.json` 对不上。如果是你主动更新了上游版本，请用 `tools/sync_libportable.py --tag <版本>` 重新生成清单，而不是手工改哈希。

### 便携性实测失败

成品启动后没有在便携目录产生用户数据，说明这个包不便携，不应该发布。常见原因是浏览器大版本更新后 libportable 的 hook 失效，需要等上游更新。排查时可以加 `--keep-temp` 保留中间文件。

### GitHub API 限流

匿名调用 GitHub API 每小时只有 60 次配额。CI 里工作流已经自动传入 `GITHUB_TOKEN`；本地调试如果撞到限流，设置一下环境变量即可：

```powershell
$env:GITHUB_TOKEN = (gh auth token)
```

## 许可证

本仓库采用 MIT 许可证，详见 [LICENSE](LICENSE)。

`bin/` 中的二进制来自 [libportable](https://github.com/adonais/libportable)，遵循其自身的许可证，见 [bin/LICENSE-libportable.txt](bin/LICENSE-libportable.txt)。
