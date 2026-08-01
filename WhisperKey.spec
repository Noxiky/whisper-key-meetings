from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs, collect_submodules


root = Path(SPECPATH).resolve()
datas = [
    (str(root / "src" / "whisper_key" / "config.defaults.yaml"), "whisper_key"),
    (str(root / "src" / "whisper_key" / "commands.defaults.yaml"), "whisper_key"),
    (str(root / "src" / "whisper_key" / "assets"), "whisper_key/assets"),
    (str(root / "src" / "whisper_key" / "platform" / "windows" / "assets"), "whisper_key/platform/windows/assets"),
    (str(root / "src" / "whisper_key" / "ui" / "whisperkey.tokens.json"), "whisper_key/ui"),
]
datas += collect_data_files("faster_whisper", includes=["assets/*.onnx"])
binaries = []
hiddenimports = collect_submodules("sherpa_onnx") + [
    "soundcard",
    "sounddevice",
    "global_hotkeys",
    "pystray._win32",
    "PIL._tkinter_finder",
]
for package in ("ctranslate2", "onnxruntime", "sherpa_onnx", "ten_vad"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden
for package in ("nvidia.cublas", "nvidia.cuda_runtime", "nvidia.cuda_nvrtc", "nvidia.cudnn"):
    binaries += collect_dynamic_libs(package)

a = Analysis(
    [str(root / "scripts" / "whisperkey_gui.py")],
    pathex=[str(root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "torch", "tensorflow"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WhisperKey",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(root / "src" / "whisper_key" / "platform" / "windows" / "assets" / "whisperkey-icon.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="WhisperKey",
)
