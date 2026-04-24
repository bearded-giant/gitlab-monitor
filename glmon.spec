# pyinstaller spec for gitlab-monitor (onedir mode)
#
# one-file mode extraction to /var/folders/.../_MEI* stalls on macOS 15+ /
# macOS 26 when the binary is adhoc-signed. onedir ships the bootloader +
# dependencies side-by-side in a directory, avoiding extraction entirely.
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# explicit target arch so CI cannot accidentally produce an x86_64 binary
# under Rosetta on an Apple Silicon runner. override via env for non-arm64 builds.
target_arch = os.environ.get('PYINSTALLER_TARGET_ARCH') or None

hiddenimports = collect_submodules('textual') + collect_submodules('rich') + [
    'gitlab',
    'gitlab.v4',
    'gitlab.v4.objects',
    'gitlab_monitor',
    'gitlab_monitor.config',
    'gitlab_monitor.tui',
]

datas = collect_data_files('textual') + collect_data_files('rich')

a = Analysis(
    ['entry.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='glmon',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=target_arch,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='glmon',
)
