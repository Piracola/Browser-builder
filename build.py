import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
import requests

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class BrowserBuilder:
    def __init__(self, args):
        self.browser_name = args.browser
        self.version = args.version
        self.url = args.url
        self.libportable_path = Path(args.libportable).resolve()
        self.workspace = Path(args.workspace).resolve() if args.workspace else Path(os.getcwd())
        self.temp_dir = self.workspace / "temp_build"
        self.output_dir = self.workspace / "output"
        self.installer_name = f"{self.browser_name.lower()}_installer.exe"
        
        # 针对不同浏览器的特定配置
        self.browser_configs = {
            "firefox": {
                "exe_name": "firefox.exe",
            },
            "floorp": {
                "exe_name": "floorp.exe",
            },
            "zen": {
                "exe_name": "zen.exe",
            }
        }
        
        if self.browser_name.lower() not in self.browser_configs:
            raise ValueError(f"Unsupported browser: {self.browser_name}")
            
        self.config = self.browser_configs[self.browser_name.lower()]

    def fetch_latest_version(self):
        """如果未提供版本和URL，尝试自动获取"""
        if self.version and self.url:
            return

        logger.info(f"Fetching latest version info for {self.browser_name}...")
        
        if self.browser_name.lower() == "firefox":
            # Mozilla API
            try:
                resp = requests.get("https://product-details.mozilla.org/1.0/firefox_versions.json")
                resp.raise_for_status()
                data = resp.json()
                self.version = data.get("LATEST_FIREFOX_VERSION")
                # 构造下载链接
                self.url = f"https://download-installer.cdn.mozilla.net/pub/firefox/releases/{self.version}/win64/en-US/Firefox%20Setup%20{self.version}.exe"
            except Exception as e:
                logger.error(f"Failed to fetch Firefox version: {e}")
                raise

        elif self.browser_name.lower() == "floorp":
            try:
                resp = requests.get("https://api.github.com/repos/Floorp-Projects/Floorp/releases/latest")
                resp.raise_for_status()
                data = resp.json()
                self.version = data["tag_name"]
                # 寻找 asset
                for asset in data["assets"]:
                    if "floorp-windows-x86_64.installer.exe" in asset["name"]:
                        self.url = asset["browser_download_url"]
                        break
                if not self.url:
                    raise ValueError("Could not find Floorp installer asset")
            except Exception as e:
                logger.error(f"Failed to fetch Floorp version: {e}")
                raise

        elif self.browser_name.lower() == "zen":
            try:
                resp = requests.get("https://api.github.com/repos/zen-browser/desktop/releases/latest")
                resp.raise_for_status()
                data = resp.json()
                self.version = data["tag_name"]
                for asset in data["assets"]:
                    if "zen.installer.exe" in asset["name"]:
                        self.url = asset["browser_download_url"]
                        break
                if not self.url:
                    # Fallback
                     self.url = "https://github.com/zen-browser/desktop/releases/latest/download/zen.installer.exe"
            except Exception as e:
                logger.error(f"Failed to fetch Zen version: {e}")
                raise
        
        logger.info(f"Resolved version: {self.version}")
        logger.info(f"Resolved URL: {self.url}")

    def download(self):
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        installer_path = self.temp_dir / self.installer_name
        
        if installer_path.exists():
            logger.info("Installer already exists, skipping download.")
            return installer_path

        logger.info(f"Downloading {self.url}...")
        with requests.get(self.url, stream=True) as r:
            r.raise_for_status()
            with open(installer_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        
        return installer_path

    def extract(self, installer_path):
        extract_dir = self.temp_dir / "extracted"
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir(parents=True)

        logger.info(f"Extracting {installer_path} to {extract_dir}...")
        
        # 使用 7z 解压
        try:
            subprocess.run(["7z", "x", str(installer_path), f"-o{extract_dir}", "-y"], check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            logger.warning("7z not found in PATH or failed, trying default location...")
            seven_z_path = r"C:\Program Files\7-Zip\7z.exe"
            if os.path.exists(seven_z_path):
                subprocess.run([seven_z_path, "x", str(installer_path), f"-o{extract_dir}", "-y"], check=True)
            else:
                # 尝试寻找 workspace 下可能的 7z (如果有的话)
                raise RuntimeError("7z executable not found. Please install 7-Zip.")

        # 清理 setup.exe 等不需要的文件
        for root, dirs, files in os.walk(extract_dir):
            for file in files:
                if file.lower() == "setup.exe":
                    try:
                        os.remove(os.path.join(root, file))
                    except OSError:
                        pass
        
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # 确定源目录
        # 逻辑：查找含有 exe_name 的目录，或者 core 目录
        source_core = None
        
        # 优先查找 'core' 目录
        if (extract_dir / "core").exists():
            source_core = extract_dir / "core"
        else:
            # 否则查找 exe 所在目录
            for root, dirs, files in os.walk(extract_dir):
                if self.config["exe_name"] in files:
                    source_core = Path(root)
                    break
            if not source_core:
                # 如果还没找到，假设解压根目录就是
                source_core = extract_dir

        target_core = self.output_dir / "core"
        if target_core.exists():
            shutil.rmtree(target_core)
            
        logger.info(f"Moving core files from {source_core} to {target_core}")
        # 移动文件
        shutil.move(str(source_core), str(target_core))
        
        return target_core

    def inject(self, core_dir):
        logger.info("Injecting portable files...")
        
        if not self.libportable_path.exists():
            raise FileNotFoundError(f"Libportable path not found: {self.libportable_path}")

        # 1. 复制 libportable 文件到 core 目录
        for item in self.libportable_path.glob("*"):
            if item.is_file():
                shutil.copy2(item, core_dir)
        
        # 2. 执行注入
        upcheck64 = core_dir / "upcheck64.exe"
        upcheck = core_dir / "upcheck.exe"
        
        if upcheck64.exists():
            shutil.move(str(upcheck64), str(upcheck))
        
        if not upcheck.exists():
            # 某些情况下可能只有 upcheck32? 假设 64 位
            logger.error("upcheck.exe not found in core dir!")
            # 尝试寻找 upcheck32 并重命名
            upcheck32 = core_dir / "upcheck32.exe"
            if upcheck32.exists():
                 shutil.move(str(upcheck32), str(upcheck))
            else:
                 return

        try:
            # 运行 upcheck.exe -dll
            logger.info(f"Running injection: {upcheck} -dll")
            result = subprocess.run([str(upcheck), "-dll"], cwd=core_dir, capture_output=True, text=True)
            logger.info(result.stdout)
            if result.returncode != 0:
                logger.error(result.stderr)
                # 不强制抛出异常，因为有时可能已经注入过或有其他警告
                logger.warning("Injection returned non-zero exit code.")
            else:
                logger.info("Injection successful.")
        except Exception as e:
            logger.error(f"Injection failed: {e}")
            raise

        # 3. 清理
        for f in core_dir.glob("upcheck*.exe"):
            try: f.unlink()
            except: pass
            
        for f in core_dir.glob("setdll*.exe"):
            try: f.unlink()
            except: pass
        
        p32 = core_dir / "portable32.dll"
        if p32.exists():
            try: p32.unlink()
            except: pass
            
        p32_up = core_dir / "upcheck32.exe"
        if p32_up.exists():
            try: p32_up.unlink()
            except: pass

        # 4. 处理 portable.ini
        ini_example = core_dir / "portable(example).ini"
        ini_target = core_dir / "portable.ini"
        if ini_example.exists() and not ini_target.exists():
            shutil.copy2(ini_example, ini_target)

    def add_launcher(self, launcher_path):
        if launcher_path and Path(launcher_path).exists():
            logger.info(f"Adding launcher script: {launcher_path}")
            shutil.copy2(launcher_path, self.output_dir)
        else:
            logger.warning("Launcher path not provided or does not exist.")

    def create_archive(self):
        logger.info("Creating archive...")
        archive_name = f"{self.browser_name}_Portable_{self.version}.7z"
        output_archive = self.workspace / archive_name
        
        if output_archive.exists():
            output_archive.unlink()

        # 使用 7z 打包
        try:
            subprocess.run(["7z", "a", str(output_archive), f"{self.output_dir}/*", "-mx9"], check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
             seven_z_path = r"C:\Program Files\7-Zip\7z.exe"
             subprocess.run([seven_z_path, "a", str(output_archive), f"{self.output_dir}/*", "-mx9"], check=True)
        
        logger.info(f"Archive created: {output_archive}")
        # 输出给 GitHub Actions
        if "GITHUB_OUTPUT" in os.environ:
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"artifact_path={output_archive}\n")
                f.write(f"artifact_name={archive_name}\n")
                f.write(f"version={self.version}\n")

    def check_remote_release(self):
        """Check if release already exists in the current repository"""
        repo = os.environ.get("GITHUB_REPOSITORY")
        token = os.environ.get("GITHUB_TOKEN")
        
        if not repo or not token:
            logger.warning("GITHUB_REPOSITORY or GITHUB_TOKEN not set, skipping release check.")
            return

        if not self.version:
             # 版本尚未获取，需要在 fetch_latest_version 后调用
             return

        logger.info(f"Checking if release {self.version} exists in {repo}...")
        
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        # 尝试完全匹配 tag
        url = f"https://api.github.com/repos/{repo}/releases/tags/{self.version}"
        
        try:
            resp = requests.get(url, headers=headers)
            if resp.status_code == 200:
                logger.info(f"Release {self.version} already exists. Skipping build.")
                sys.exit(0)
            elif resp.status_code == 404:
                logger.info(f"Release {self.version} does not exist. Proceeding with build.")
            else:
                logger.warning(f"Failed to check release status: {resp.status_code} {resp.text}")
        except Exception as e:
            logger.warning(f"Error checking release status: {e}")

    def run(self):
        self.fetch_latest_version()
        self.check_remote_release()
        installer = self.download()
        core_dir = self.extract(installer)
        self.inject(core_dir)
        return self.version

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build Portable Browser")
    parser.add_argument("--browser", required=True, help="Browser name (firefox, floorp, zen)")
    parser.add_argument("--version", help="Browser version")
    parser.add_argument("--url", help="Download URL")
    parser.add_argument("--libportable", required=True, help="Path to libportable directory")
    parser.add_argument("--launcher", help="Path to launcher script (e.g. start.bat)")
    parser.add_argument("--workspace", help="Workspace directory")
    
    args = parser.parse_args()
    
    try:
        builder = BrowserBuilder(args)
        builder.run()
        builder.add_launcher(args.launcher)
        builder.create_archive()
    except Exception as e:
        logger.error(f"Build failed: {e}")
        sys.exit(1)
