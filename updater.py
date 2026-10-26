"""Windows helper that swaps an onedir build after the widget has exited."""

import argparse
import ctypes
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from update_config import APP_FOLDER_NAME, UPDATER_EXECUTABLE_NAME

MAX_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20_000


def show_error(message):
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "우리 반 진도 업데이트", 0x10)
    else:
        print(message, file=sys.stderr)


def wait_for_process(pid, timeout_seconds=120):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        if ctypes.get_last_error() == 87:  # The app already exited.
            return
        raise OSError(ctypes.get_last_error(), "업데이트 전에 앱이 종료되지 않았어요.")
    try:
        result = kernel32.WaitForSingleObject(handle, timeout_seconds * 1000)
        if result != 0:
            raise TimeoutError("앱이 종료될 때까지 기다리는 시간이 초과됐어요.")
    finally:
        kernel32.CloseHandle(handle)


def safe_extract(archive_path, destination):
    destination = destination.resolve()
    total_size = 0
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise ValueError("업데이트 파일 안에 항목이 너무 많아요.")
        for info in entries:
            raw_name = info.filename.replace("\\", "/")
            parts = PurePosixPath(raw_name).parts
            if (not parts or raw_name.startswith("/") or
                    any(part in ("", ".", "..") for part in parts) or
                    any(":" in part for part in parts)):
                raise ValueError("업데이트 ZIP에 안전하지 않은 경로가 있어요.")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise ValueError("업데이트 ZIP에 바로가기가 포함돼 있어요.")
            total_size += info.file_size
            if total_size > MAX_UNPACKED_BYTES:
                raise ValueError("압축 해제 파일이 허용 크기를 넘었어요.")

            target = destination.joinpath(*parts).resolve()
            try:
                target.relative_to(destination)
            except ValueError as exc:
                raise ValueError("업데이트 ZIP 경로가 설치 폴더 밖을 가리켜요.") from exc
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)


def launch_app(executable, install_dir):
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    ready_file = Path(tempfile.gettempdir()) / f"UsagiJindo-ready-{uuid.uuid4().hex}.tmp"
    environment = os.environ.copy()
    environment["USAGIJINDO_UPDATE_READY_FILE"] = str(ready_file)
    process = subprocess.Popen([str(executable)], cwd=str(install_dir), close_fds=True,
                               creationflags=flags, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env=environment)
    deadline = time.monotonic() + 20
    try:
        while not ready_file.is_file():
            if process.poll() is not None:
                raise RuntimeError("새 앱이 시작 단계에서 종료됐어요.")
            if time.monotonic() >= deadline:
                try:
                    process.terminate()
                except OSError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                raise TimeoutError("새 앱이 준비됐다는 응답을 받지 못했어요.")
            time.sleep(0.2)
    finally:
        try:
            ready_file.unlink()
        except OSError:
            pass


def install_update(args):
    if os.name != "nt":
        raise OSError("이 업데이트 도우미는 Windows 전용이에요.")
    install_dir = Path(args.install_dir).resolve()
    archive_path = Path(args.archive).resolve()
    if not install_dir.is_dir() or not archive_path.is_file():
        raise FileNotFoundError("앱 폴더 또는 내려받은 업데이트 파일을 찾지 못했어요.")
    if install_dir.name.casefold() != APP_FOLDER_NAME.casefold():
        raise OSError(f"앱 폴더 이름을 '{APP_FOLDER_NAME}'으로 유지해야 업데이트할 수 있어요.")

    parent = install_dir.parent
    stage = Path(tempfile.mkdtemp(prefix=f".{install_dir.name}-update-", dir=parent))
    backup = parent / f"{install_dir.name}.backup-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    moved_old = False
    moved_new = False
    try:
        wait_for_process(args.pid)
        safe_extract(archive_path, stage)
        new_install_dir = stage / APP_FOLDER_NAME
        new_app = new_install_dir / args.exe_name
        if not new_app.is_file():
            raise FileNotFoundError("업데이트 ZIP에서 앱 실행 파일을 찾지 못했어요.")
        if not (new_install_dir / UPDATER_EXECUTABLE_NAME).is_file():
            raise FileNotFoundError("업데이트 ZIP에 업데이트 도우미가 없어요.")

        os.rename(install_dir, backup)
        moved_old = True
        try:
            os.rename(new_install_dir, install_dir)
            moved_new = True
            launch_app(install_dir / args.exe_name, install_dir)
        except Exception:
            if moved_new and install_dir.exists():
                failed_dir = parent / f"{install_dir.name}.failed-{uuid.uuid4().hex[:6]}"
                os.rename(install_dir, failed_dir)
            if moved_old and backup.exists():
                os.rename(backup, install_dir)
            raise
        try:
            archive_path.unlink()
        except OSError:
            pass
        try:
            archive_path.parent.rmdir()
        except OSError:
            pass
        # Keep the previous version beside the app so it can be restored if
        # the new build does not start correctly.
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--install-dir", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--exe-name", required=True)
    args = parser.parse_args()
    try:
        install_update(args)
    except Exception as exc:
        show_error(f"업데이트를 설치하지 못했어요. 기존 앱은 복구를 시도했습니다.\n\n{exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
