@echo off
set "ZIG_GLOBAL_CACHE_DIR=%~dp0..\..\.cache\zig"
"%~dp0..\..\.venv\Lib\site-packages\ziglang\zig.exe" cc %*
