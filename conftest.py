import ctypes.util
import os

# Windows 上没有系统 opus 库，从 pyogg 自带的 DLL 加载（仅用于跑测试）
try:
    import pyogg

    _dll_dir = os.path.dirname(pyogg.__file__)
    if os.path.exists(os.path.join(_dll_dir, "opus.dll")):
        os.add_dll_directory(_dll_dir)
        _orig = ctypes.util.find_library
        ctypes.util.find_library = (
            lambda name: os.path.join(_dll_dir, "opus.dll")
            if name == "opus"
            else _orig(name)
        )
except ImportError:
    pass
