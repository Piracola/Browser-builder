import argparse
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Dict

import requests

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class BrowserBuilder:
    def __init__(self, args: argparse.Namespace):
        self.browser_name: str = args.browser.lower()
        self.version: Optional[str] = args.version
        self.url: Optional[str] = args.url
        self.libportable_path: Path = Path(args.libportable).resolve()
        self.seven_z_path_arg: Optional[str] = args.seven_z_path
        self.launcher_arg: Optional[str] = args.launcher
        
        if not self.libportable_path.exists():
            raise FileNotFoundError(f"Libportable path not found: {self.libportable_path}")

        self.workspace: Path = Path(args.workspace).resolve() if args.workspace else Path(os.getcwd())
        self.temp_dir: Path = self.workspace / "temp_build"
        self.output_dir: Path = self.workspace / "output"
        self.installer_name: str = f"{self.browser_name}_installer.exe"
        
        # Browser specific configurations
        self.browser_configs: Dict[str, Dict[str, str]] = {
            "firefox": {"exe_name": "firefox.exe", "folder_name": "Firefox"},
            "floorp": {"exe_name": "floorp.exe", "folder_name": "Floorp"},
            "zen": {"exe_name": "zen.exe", "folder_name": "Zen"}
        }
        
        if self.browser_name not in self.browser_configs:
            raise ValueError(f"Unsupported browser: {self.browser_name}")
            
        self.config = self.browser_configs[self.browser_name]

    def _get_seven_z(self) -> str:
        """Resolve 7z executable path."""
        if self.seven_z_path_arg and os.path.exists(self.seven_z_path_arg):
            return self.seven_z_path_arg
            
        seven_z = shutil.which("7z")
        if seven_z:
            return seven_z
            
        default_path = r"C:\Program Files\7-Zip\7z.exe"
        if os.path.exists(default_path):
            return default_path
            
        return "7z"

    def fetch_latest_version(self):
        """Validate version and URL."""
        if not self.version or not self.url:
            raise ValueError("Version and URL must be provided via arguments.")
        logger.info(f"Resolved version: {self.version}")
        logger.info(f"Resolved URL: {self.url}")

    def download(self) -> Path:
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        installer_path = self.temp_dir / self.installer_name
        
        if installer_path.exists():
            logger.info("Installer already exists, skipping download.")
            return installer_path

        logger.info(f"Downloading {self.url}...")
        try:
            with requests.get(self.url, stream=True) as r:
                r.raise_for_status()
                with open(installer_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
        except requests.RequestException as e:
            logger.error(f"Download failed: {e}")
            raise
            
        return installer_path

    def extract(self, installer_path: Path) -> Path:
        extract_dir = self.temp_dir / "extracted"
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir(parents=True)

        logger.info(f"Extracting {installer_path} to {extract_dir}...")
        seven_z = self._get_seven_z()
        
        try:
            subprocess.run([seven_z, "x", str(installer_path), f"-o{extract_dir}", "-y"], check=True, stdout=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            logger.error("Extraction failed. Ensure 7-Zip is installed and available.")
            raise

        # Clean up unnecessary files
        self._remove_file(extract_dir, "setup.exe")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Locate core directory
        source_core = self._find_core_dir(extract_dir)
        
        # Target directory setup
        target_name = self.config["folder_name"]
        target_core = self.output_dir / target_name
        
        if target_core.exists():
            shutil.rmtree(target_core)
            
        logger.info(f"Moving core files from {source_core} to {target_core}")
        shutil.move(str(source_core), str(target_core))
        
        return target_core

    def _remove_file(self, root_dir: Path, filename: str):
        for root, _, files in os.walk(root_dir):
            if filename in files:
                try:
                    os.remove(os.path.join(root, filename))
                except OSError:
                    pass

    def _find_core_dir(self, extract_dir: Path) -> Path:
        if (extract_dir / "core").exists():
            return extract_dir / "core"
        
        for root, _, files in os.walk(extract_dir):
            if self.config["exe_name"].lower() in [f.lower() for f in files]:
                return Path(root)
        
        return extract_dir

    def inject(self, core_dir: Path):
        logger.info("Injecting portable files...")
        
        # 1. Copy libportable files
        for item in self.libportable_path.glob("*"):
            if item.is_file():
                shutil.copy2(item, core_dir)
        
        # 2. Prepare upcheck.exe
        self._prepare_upcheck(core_dir)

        # 3. Run injection
        upcheck = core_dir / "upcheck.exe"
        if upcheck.exists():
            try:
                logger.info(f"Running injection: {upcheck} -dll")
                result = subprocess.run([str(upcheck), "-dll"], cwd=core_dir, capture_output=True, text=True)
                if result.returncode != 0:
                    logger.warning(f"Injection warning: {result.stderr}")
                else:
                    logger.info("Injection successful.")
            except Exception as e:
                logger.error(f"Injection failed: {e}")
                raise
        else:
            logger.warning("upcheck.exe not found, skipping injection execution.")

        # 4. Cleanup injection tools
        self._cleanup_injection_tools(core_dir)

        # 5. Handle portable.ini
        self._setup_portable_ini(core_dir)

    def _prepare_upcheck(self, core_dir: Path):
        upcheck = core_dir / "upcheck.exe"
        if not upcheck.exists():
            # Try 64 first, then 32
            for candidate in ["upcheck64.exe", "upcheck32.exe"]:
                src = core_dir / candidate
                if src.exists():
                    shutil.move(str(src), str(upcheck))
                    break

    def _cleanup_injection_tools(self, core_dir: Path):
        # Remove upcheck exes and setdll tools
        for pattern in ["upcheck*.exe", "setdll*.exe"]:
            for f in core_dir.glob(pattern):
                try: f.unlink()
                except: pass
        
        # Also remove portable32.dll if we are assuming 64bit, or let the user decide?
        # The original script deleted portable32.dll unconditionally in some paths or if bits=64.
        # Here we assume we are building 64-bit mostly.
        p32 = core_dir / "portable32.dll"
        if p32.exists():
            try: p32.unlink()
            except: pass

    def _setup_portable_ini(self, core_dir: Path):
        ini_example = core_dir / "portable(example).ini"
        ini_target = core_dir / "portable.ini"
        if ini_example.exists() and not ini_target.exists():
            shutil.copy2(ini_example, ini_target)

    def generate_launcher(self, custom_launcher_path: Optional[str] = None):
        """Generate or copy launcher script."""
        if custom_launcher_path and Path(custom_launcher_path).exists():
            logger.info(f"Using provided launcher: {custom_launcher_path}")
            shutil.copy2(custom_launcher_path, self.output_dir)
            return

        logger.info("Generating default launcher script...")
        launcher_name = f"开始.bat" 
        
        launcher_content = f"""@echo off
chcp 65001 >nul
setlocal

set "target=%~dp0{self.config['folder_name']}\{self.config['exe_name']}"
set "lnk=%~dp0{self.browser_name.capitalize()}.lnk"

if not exist "%target%" (
    echo [Error] Target not found: %target%
    pause & exit /b 1
)

powershell -NoP -EP Bypass -C "$w=New-Object -ComObject WScript.Shell;$s=$w.CreateShortcut('%lnk%');$s.TargetPath='%target%';$s.WorkingDirectory='%~dp0{self.config['folder_name']}';$s.Description='{self.browser_name.capitalize()} Portable';$s.Save()" 2>nul

if %errorlevel% neq 0 (
    echo [Error] Failed to create shortcut
    pause & exit /b 1
)
echo [Success] Shortcut created: %lnk%
"""
        launcher_path = self.output_dir / launcher_name
        with open(launcher_path, "w", encoding="utf-8") as f:
            f.write(launcher_content)
        logger.info(f"Generated launcher: {launcher_path}")

    def create_archive(self):
        logger.info("Creating archive...")
        archive_name = f"{self.browser_name.capitalize()}_{self.version}.7z"
        output_archive = self.workspace / archive_name
        
        if output_archive.exists():
            output_archive.unlink()

        seven_z = self._get_seven_z()
        try:
            subprocess.run([seven_z, "a", str(output_archive), "*", "-mx9"], cwd=self.output_dir, check=True, stdout=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
             logger.error("Archiving failed.")
             raise
        
        logger.info(f"Archive created: {output_archive}")
        
        if "GITHUB_OUTPUT" in os.environ:
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"artifact_path={output_archive}\n")
                f.write(f"artifact_name={archive_name}\n")
                f.write(f"version={self.version}\n")

    def cleanup(self):
        if self.temp_dir.exists():
            logger.info(f"Cleaning up temporary directory: {self.temp_dir}")
            try:
                shutil.rmtree(self.temp_dir)
            except Exception as e:
                logger.warning(f"Failed to cleanup temp dir: {e}")

    def run(self):
        self.fetch_latest_version()
        installer = self.download()
        core_dir = self.extract(installer)
        self.inject(core_dir)
        self.generate_launcher(self.launcher_arg)
        self.create_archive()
        self.cleanup()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build Portable Browser")
    parser.add_argument("--browser", required=True, help="Browser name (firefox, floorp, zen)")
    parser.add_argument("--version", help="Browser version")
    parser.add_argument("--url", help="Download URL")
    parser.add_argument("--libportable", required=True, help="Path to libportable directory")
    parser.add_argument("--launcher", help="Path to custom launcher script (optional)")
    parser.add_argument("--workspace", help="Workspace directory")
    parser.add_argument("--seven-z-path", help="Path to 7z executable")
    
    args = parser.parse_args()
    
    try:
        builder = BrowserBuilder(args)
        builder.run()
    except Exception as e:
        logger.error(f"Build failed: {e}")
        sys.exit(1)
