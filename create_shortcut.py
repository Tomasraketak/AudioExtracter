import os
import subprocess
from pathlib import Path


def create_desktop_shortcut():
    app_dir = os.path.dirname(os.path.abspath(__file__))
    pythonw_exe = os.path.join(app_dir, ".venv", "Scripts", "pythonw.exe")
    app_py = os.path.join(app_dir, "app.py")
    icon_path = os.path.join(app_dir, "app_icon.ico")

    # Vygenerovat ikonu pokud ještě neexistuje
    if not os.path.exists(icon_path):
        try:
            from generate_icon import create_app_icon
            create_app_icon(icon_path)
        except Exception:
            pass

    ps_script = f"""
$desktop = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktop 'Bionic Media & Audio Studio.lnk'
$wsh = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut($lnkPath)
$shortcut.TargetPath = '{pythonw_exe}'
$shortcut.Arguments = '"{app_py}"'
$shortcut.WorkingDirectory = '{app_dir}'
$shortcut.Description = 'Extrakce audia, konverze MP4/MOV/AAC/MP3 a AI Prepis (Bionic Studio)'
if (Test-Path '{icon_path}') {{
    $shortcut.IconLocation = '{icon_path}'
}}
$shortcut.Save()
Write-Output $lnkPath
"""
    res = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if res.returncode == 0:
        print(f"[OK] Zastupce na plose vytvoren: {res.stdout.strip()}")
    else:
        print(f"[WARN] Nepodarilo se vytvorit zastupce: {res.stderr}")


if __name__ == "__main__":
    create_desktop_shortcut()
