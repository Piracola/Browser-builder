# Browser-builder

Firefox 系列浏览器便携版的通用构建脚本仓库。

子仓库的 GitHub Actions 会将本仓库检出到 `builder` 目录，安装 `builder/requirements.txt`，再调用 `python builder/build.py` 完成下载、解包、注入 libportable、生成启动脚本和打包发布。

## 子仓库

| 项目 | 用途 | 链接 |
|------|------|------|
| Firefox-Libportable | Firefox 便携版 | https://github.com/Piracola/Firefox-Libportable |
| Floorp_portable | Floorp 便携版 | https://github.com/Piracola/Floorp_portable |
| Zen-Libportable | Zen 便携版 | https://github.com/Piracola/Zen-Libportable |

## 用法

```powershell
python build.py --browser firefox --version <version> --url <installer-url> --libportable <libportable-dir> --launcher <launcher-bat>
```

支持的浏览器参数为 `firefox`、`floorp`、`zen`。
