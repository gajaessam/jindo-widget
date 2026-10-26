"""Find and download verified releases from the configured public GitHub repo."""

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from update_config import (
    APP_VERSION,
    GITHUB_REPOSITORY,
    MAX_UPDATE_BYTES,
    RELEASE_ASSET_TEMPLATE,
)

API_ROOT = "https://api.github.com/repos"
USER_AGENT = "UsagiJindo-UpdateClient"
ALLOWED_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


class UpdateError(RuntimeError):
    pass


def _version_tuple(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(value).strip())
    if not match:
        raise UpdateError(f"버전 표기가 올바르지 않아요: {value}")
    return tuple(int(part) for part in match.groups())


def repository_is_configured():
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", GITHUB_REPOSITORY))


def check_for_update():
    """Return the latest compatible release when it is newer, otherwise None."""
    if not repository_is_configured():
        return None

    owner, repo = GITHUB_REPOSITORY.split("/", 1)
    request = urllib.request.Request(
        f"{API_ROOT}/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError("GitHub에서 업데이트 정보를 가져오지 못했어요.") from exc
    if len(raw) > 2 * 1024 * 1024:
        raise UpdateError("GitHub 업데이트 응답이 너무 커요.")
    try:
        release = json.loads(raw.decode("utf-8"))
        latest_tag = release["tag_name"]
        latest_version = _version_tuple(latest_tag)
        installed_version = _version_tuple(APP_VERSION)
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise UpdateError("GitHub 업데이트 정보 형식이 올바르지 않아요.") from exc

    if latest_version <= installed_version:
        return None

    asset_name = RELEASE_ASSET_TEMPLATE.format(version=".".join(map(str, latest_version)))
    assets = release.get("assets", [])
    asset = next((item for item in assets if item.get("name") == asset_name), None)
    if not asset:
        executable_assets = [item for item in assets
                             if str(item.get("name", "")).lower().endswith(".exe")]
        if len(executable_assets) == 1:
            asset = executable_assets[0]
    if not asset:
        raise UpdateError(f"최신 릴리스에 배포 파일 {asset_name}이 없어요.")
    asset_name = str(asset.get("name") or asset_name)
    download_url = asset.get("browser_download_url", "")
    parsed_url = urllib.parse.urlparse(download_url)
    if parsed_url.scheme != "https" or parsed_url.hostname != "github.com":
        raise UpdateError("배포 파일 주소가 GitHub 주소가 아니에요.")
    size = asset.get("size")
    if not isinstance(size, int) or not 0 < size <= MAX_UPDATE_BYTES:
        raise UpdateError("배포 파일 크기가 올바르지 않거나 너무 커요.")

    return {
        "tag": latest_tag,
        "version": ".".join(map(str, latest_version)),
        "notes": str(release.get("body") or "").strip(),
        "asset_name": asset_name,
        "download_url": download_url,
        "digest": asset.get("digest"),
        "size": size,
        "html_url": release.get("html_url", ""),
    }


def download_release_asset(update, destination):
    """Download an asset and verify GitHub's SHA-256 digest before using it."""
    digest = update.get("digest") or ""
    if not digest.startswith("sha256:"):
        raise UpdateError("릴리스 파일에 SHA-256 정보가 없어 설치를 진행할 수 없어요.")
    expected = digest.partition(":")[2].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise UpdateError("릴리스의 SHA-256 표기가 올바르지 않아요.")

    destination = Path(destination)
    partial = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(
        update["download_url"], headers={"User-Agent": USER_AGENT}
    )
    hasher = hashlib.sha256()
    downloaded = 0
    try:
        with urllib.request.urlopen(request, timeout=20) as response, partial.open("wb") as stream:
            final_url = urllib.parse.urlparse(response.geturl())
            if (final_url.scheme != "https" or
                    final_url.hostname not in ALLOWED_DOWNLOAD_HOSTS):
                raise UpdateError("GitHub의 안전한 다운로드 주소가 아니에요.")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > MAX_UPDATE_BYTES:
                    raise UpdateError("다운로드 파일이 허용 크기를 넘었어요.")
                hasher.update(chunk)
                stream.write(chunk)
        if downloaded != update["size"]:
            raise UpdateError("다운로드 파일 크기가 GitHub 정보와 달라요.")
        if hasher.hexdigest() != expected:
            raise UpdateError("다운로드 파일의 SHA-256 확인에 실패했어요.")
        partial.replace(destination)
        return destination
    except UpdateError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError("업데이트 파일을 내려받지 못했어요.") from exc
    finally:
        if partial.exists():
            partial.unlink()
