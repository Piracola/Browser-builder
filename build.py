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
        if not self.libportable_path.exists():
            raise FileNotFoundError(f"Libportable path not found: {self.libportable_path}")

        self.workspace = Path(args.workspace).resolve() if args.workspace else Path(os.getcwd())
        self.temp_dir = self.workspace / "temp_build"
        self.output_dir = self.workspace / "output"
        self.installer_name = f"{self.browser_name.lower()}_installer.exe"
        self.seven_z_path = args.seven_z_path
        
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
        if not self.version or not self.url:
            raise ValueError("Version and URL must be provided via arguments.")

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
        
        # 确定 7z 路径
        seven_z = self.seven_z_path or shutil.which("7z") or r"C:\Program Files\7-Zip\7z.exe"
        
        if not seven_z or not os.path.exists(seven_z):
             # 尝试直接调用命令，如果 PATH 中有
             seven_z = "7z"
        
        # 使用 7z 解压
        try:
            subprocess.run([seven_z, "x", str(installer_path), f"-o{extract_dir}", "-y"], check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
             # 如果上面失败了，且是默认路径失败，尝试查找
             if seven_z == "7z":
                 logger.warning("7z command failed, trying default path...")
                 default_path = r"C:\Program Files\7-Zip\7z.exe"
                 if os.path.exists(default_path):
                     subprocess.run([default_path, "x", str(installer_path), f"-o{extract_dir}", "-y"], check=True)
                 else:
                     raise RuntimeError("7z executable not found. Please install 7-Zip or provide path via --seven-z-path.")
             else:
                 raise

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

        # 确定 7z 路径 (复用逻辑或再次查找)
        seven_z = self.seven_z_path or shutil.which("7z") or r"C:\Program Files\7-Zip\7z.exe"
        if not seven_z or not os.path.exists(seven_z): seven_z = "7z"

        # 使用 7z 打包
        try:
            subprocess.run([seven_z, "a", str(output_archive), f"{self.output_dir}/*", "-mx9"], check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
             # Fallback logic
             if seven_z == "7z":
                 seven_z_path = r"C:\Program Files\7-Zip\7z.exe"
                 if os.path.exists(seven_z_path):
                     subprocess.run([seven_z_path, "a", str(output_archive), f"{self.output_dir}/*", "-mx9"], check=True)
                 else:
                     raise
             else:
                 raise
        
        logger.info(f"Archive created: {output_archive}")
        # 输出给 GitHub Actions
        if "GITHUB_OUTPUT" in os.environ:
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"artifact_path={output_archive}\n")
                f.write(f"artifact_name={archive_name}\n")
                f.write(f"version={self.version}\n")

    def cleanup(self):
        """Clean up temporary build files"""
        if self.temp_dir.exists():
            logger.info(f"Cleaning up temporary directory: {self.temp_dir}")
            try:
                shutil.rmtree(self.temp_dir)
            except Exception as e:
                logger.warning(f"Failed to cleanup temp dir: {e}")

    def check_remote_release(self) -> bool:
        """Check if release already exists in the current repository. Returns True if exists."""
        repo = os.environ.get("GITHUB_REPOSITORY")
        token = os.environ.get("GITHUB_TOKEN")
        
        if not repo or not token:
            logger.warning("GITHUB_REPOSITORY or GITHUB_TOKEN not set, skipping release check.")
            return False

        if not self.version:
             return False

        logger.info(f"Checking if release {self.version} exists in {repo}...")
        
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        url = f"https://api.github.com/repos/{repo}/releases/tags/{self.version}"
        
        try:
            resp = requests.get(url, headers=headers)
            if resp.status_code == 200:
                logger.info(f"Release {self.version} already exists.")
                return True
            elif resp.status_code == 404:
                logger.info(f"Release {self.version} does not exist. Proceeding with build.")
                return False
            else:
                logger.warning(f"Failed to check release status: {resp.status_code} {resp.text}")
                return False
        except Exception as e:
            logger.warning(f"Error checking release status: {e}")
            return False

    def run(self):
        self.fetch_latest_version()
        if self.check_remote_release():
            logger.info("Release already exists. Exiting...")
            return 
            
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
    parser.add_argument("--seven-z-path", help="Path to 7z executable")
    
    args = parser.parse_args()
    
    try:
        builder = BrowserBuilder(args)
        if builder.run():
            builder.add_launcher(args.launcher)
            builder.create_archive()
            builder.cleanup()
    except Exception as e:
        logger.error(f"Build failed: {e}")
        sys.exit(1)
