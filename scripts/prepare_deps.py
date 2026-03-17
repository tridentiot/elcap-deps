#!/usr/bin/env python3
"""Prepare elcap-deps from Segger JLink SDK archives.

Given the three SDK archives distributed by Segger by email, this script extracts the
files needed for elcap-deps into the correct directory layout and updates
__version__. The Firmwares are supplied separately through the JLink download portal
and must be downloaded manually https://www.segger.com/downloads/jlink/

Usage:
    uv run python scripts/prepare_deps.py \\
        --windows  <JLink_Windows_SDK_Vxxx.zip> \\
        --linux    <JLinkSDK_Linux_Vxxx.tgz> \\
        --macos    <JLinkSDK_MacOSX_Vxxx.tgz> \\
        --firmwares <path/to/Firmwares> \\
        [--output  <repo-root>]

Files extracted per platform
-----------------------------
Windows:
    x86_64/JLinkARM.dll              -> windows/jlink/x86_64/
    x86_64/JLink_x64.dll             -> windows/jlink/x86_64/
    x86_64/USBDriver/InstDrivers.exe -> windows/jlink/USBDriver/
    x86_64/USBDriver/x64/**          -> windows/jlink/USBDriver/x64/

Linux:
    {prefix}/x86_64/libjlinkarm.so   -> linux/jlink/x86_64/
    {prefix}/x86_64/99-jlink.rules   -> linux/jlink/x86_64/
    {prefix}/arm64/libjlinkarm.so    -> linux/jlink/arm64/
    {prefix}/arm64/99-jlink.rules    -> linux/jlink/arm64/

macOS:
    {prefix}/arm64/libjlinkarm.dylib -> macos/jlink/arm64/

Firmwares (platform-independent):
    *.bin -> {platform}/jlink/{arch}/Firmwares/  (all four platform/arch dirs)
"""

import argparse
import re
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path


def _extract_version(windows_zip: Path) -> str:
    """Read APP_VERSION_STRING from Version.h inside the Windows SDK zip."""
    with zipfile.ZipFile(windows_zip) as zip_file:
        content = zip_file.read("x86_64/Inc/Version.h").decode("utf-8", errors="replace")
    match = re.search(r'APP_VERSION_STRING\s+"(\d+\.\d+)', content)
    if not match:
        raise ValueError("Could not parse APP_VERSION_STRING from x86_64/Inc/Version.h")
    return match.group(1)


def _resolve_tar_symlink(
    members_by_name: dict[str, tarfile.TarInfo],
    member: tarfile.TarInfo,
) -> tarfile.TarInfo:
    """Follow symlink chains within a tarfile and return the real member."""
    seen: set[str] = set()
    while member.issym():
        if member.name in seen:
            raise RuntimeError(f"Circular symlink in archive: {member.name}")
        seen.add(member.name)
        parent = "/".join(member.name.split("/")[:-1])
        target_name = f"{parent}/{member.linkname}" if parent else member.linkname
        resolved = members_by_name.get(target_name)
        if resolved is None:
            raise RuntimeError(
                f"Broken symlink in archive: {member.name} -> {member.linkname}"
            )
        member = resolved
    return member


def _extract_windows(zip_path: Path, output: Path) -> None:
    dest_jlink = output / "windows" / "jlink"
    with zipfile.ZipFile(zip_path) as zip_file:
        for name in zip_file.namelist():
            if name in ("x86_64/JLinkARM.dll", "x86_64/JLink_x64.dll"):
                dest = dest_jlink / "x86_64" / Path(name).name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zip_file.read(name))
                print(f"  {dest.relative_to(output)}")
            elif name == "x86_64/USBDriver/InstDrivers.exe" or (
                name.startswith("x86_64/USBDriver/x64/") and not name.endswith("/")
            ):
                rel_path = Path(name).relative_to("x86_64/USBDriver")
                dest = dest_jlink / "USBDriver" / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zip_file.read(name))
                print(f"  {dest.relative_to(output)}")


def _extract_linux(tgz_path: Path, output: Path) -> None:
    dest_jlink = output / "linux" / "jlink"
    with tarfile.open(tgz_path) as tar:
        members = tar.getmembers()
        by_name = {member.name: member for member in members}
        prefix = members[0].name.split("/")[0] + "/"

        for member in members:
            if not member.name.startswith(prefix):
                continue
            rel_path = member.name[len(prefix):]
            parts = rel_path.split("/")
            if len(parts) != 2:
                continue
            arch, fname = parts
            if arch not in ("x86_64", "arm64") or fname not in (
                "libjlinkarm.so",
                "99-jlink.rules",
            ):
                continue

            actual = _resolve_tar_symlink(by_name, member) if member.issym() else member
            file_obj = tar.extractfile(actual)
            if file_obj is None:
                raise RuntimeError(f"Could not extract file content for {member.name}")
            dest = dest_jlink / arch / fname
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(file_obj.read())
            print(f"  {dest.relative_to(output)}")


def _extract_macos(tgz_path: Path, output: Path) -> None:
    dest_jlink = output / "macos" / "jlink"
    with tarfile.open(tgz_path) as tar:
        members = tar.getmembers()
        by_name = {member.name: member for member in members}
        prefix = members[0].name.split("/")[0] + "/"

        for member in members:
            if not member.name.startswith(prefix):
                continue
            rel_path = member.name[len(prefix):]
            if rel_path != "arm64/libjlinkarm.dylib":
                continue

            actual = _resolve_tar_symlink(by_name, member) if member.issym() else member
            file_obj = tar.extractfile(actual)
            if file_obj is None:
                raise RuntimeError(f"Could not extract file content for {member.name}")
            dest = dest_jlink / "arm64" / "libjlinkarm.dylib"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(file_obj.read())
            print(f"  {dest.relative_to(output)}")


def _copy_firmwares(firmwares_dir: Path, output: Path) -> None:
    targets = [
        output / "windows" / "jlink" / "x86_64" / "Firmwares",
        output / "linux"   / "jlink" / "x86_64" / "Firmwares",
        output / "linux"   / "jlink" / "arm64"   / "Firmwares",
        output / "macos"   / "jlink" / "arm64"   / "Firmwares",
    ]
    bins = sorted(firmwares_dir.glob("*.bin"))
    if not bins:
        raise FileNotFoundError(f"No .bin files found in {firmwares_dir}")
    print(f"  {len(bins)} firmware files -> .Firmwares in each platform/arch")
    for target in targets:
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        for bin_file in bins:
            shutil.copy2(bin_file, target / bin_file.name)
        print(f"  {target.relative_to(output)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare elcap-deps from Segger JLink SDK archives.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--windows", required=True, type=Path, metavar="ZIP",
                        help="Windows SDK zip (JLink_Windows_SDK_Vxxx.zip)")
    parser.add_argument("--linux", required=True, type=Path, metavar="TGZ",
                        help="Linux SDK tgz (JLinkSDK_Linux_Vxxx.tgz)")
    parser.add_argument("--macos", required=True, type=Path, metavar="TGZ",
                        help="macOS SDK tgz (JLinkSDK_MacOSX_Vxxx.tgz)")
    parser.add_argument("--firmwares", required=True, type=Path, metavar="DIR",
                        help="Directory containing JLink *.bin firmware files")
    parser.add_argument("--output", type=Path, metavar="DIR",
                        default=Path(__file__).resolve().parent.parent,
                        help="Repo root to write into (default: parent of scripts/)")
    args = parser.parse_args()

    errors: list[str] = []
    for flag, path in [
        ("--windows", args.windows),
        ("--linux", args.linux),
        ("--macos", args.macos),
        ("--firmwares", args.firmwares),
    ]:
        if not path.exists():
            errors.append(f"{flag}: path not found: {path}")
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        sys.exit(1)

    print("Extracting version from Windows SDK...")
    version = _extract_version(args.windows)
    print(f"  {version}")

    print("\nWindows SDK:")
    _extract_windows(args.windows, args.output)

    print("\nLinux SDK:")
    _extract_linux(args.linux, args.output)

    print("\nmacOS SDK:")
    _extract_macos(args.macos, args.output)

    print("\nFirmwares:")
    _copy_firmwares(args.firmwares, args.output)

    print("\nUpdating __version__...")
    (args.output / "__version__").write_text(version + "\n")
    print(f"  {version}")

    print("\nDone. Review changes, then open a PR.")


if __name__ == "__main__":
    main()
