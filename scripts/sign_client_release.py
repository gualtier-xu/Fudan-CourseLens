"""Authenticode signing and verification for the stable launcher/installer.

Skeleton for the client release track. Signing fails closed until a real
certificate source is provided. Certificate material is only ever read from
environment variables or an existing certificate store, never from process
arguments, per docs/client-update-operations.md.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# The packaged launcher is the only file in the update runtime allowlist that
# carries an embedded Authenticode signature; the installer-only scripts are
# signed at controlled-installer build time.
DEFAULT_LAUNCHER_ARTIFACTS = (
    "start_fudan_courselens.ps1",
    "scripts/start_managed_courselens.ps1",
    "scripts/install_managed_client.ps1",
    "scripts/setup_fudan_courselens_runtime.ps1",
)
PE_SUFFIXES = {".exe", ".dll", ".msi", ".sys", ".cab"}
THUMBPRINT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
TIMESTAMP_SCHEMA = "courselens.authenticode-inventory.v1"
DEFAULT_TIMESTAMP_URL = "http://timestamp.digicert.com"
PFX_B64_ENV = "WINDOWS_AUTHENTICODE_CERTIFICATE_PFX"
PFX_PASSWORD_ENV = "WINDOWS_AUTHENTICODE_CERTIFICATE_PASSWORD"


def _escape_ps(value: str) -> str:
    return value.replace("'", "''")


def _windows_system_directory() -> str:
    """Return System32 from Windows itself, never from an inherited environment."""
    if os.name != "nt":
        return r"C:\Windows\System32"
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise SystemExit("Windows System32 directory is unavailable")
    return buffer.value


def _powershell(script: str) -> str:
    # A parent PSModulePath that points at PowerShell 7 or tool-runtime module
    # directories makes Windows PowerShell load an incompatible
    # Microsoft.PowerShell.Security, so Get-AuthenticodeSignature fails before
    # any signing logic runs. Use the trusted Windows PowerShell module path
    # for the child only, rather than inheriting a PowerShell 7/tool runtime.
    child_env = os.environ.copy()
    for key in list(child_env):
        if key.casefold() == "psmodulepath":
            del child_env[key]
    child_env["PSMODULEPATH"] = os.path.join(
        _windows_system_directory(), "WindowsPowerShell", "v1.0", "Modules"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=child_env,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise SystemExit(f"authenticode powershell step failed: {detail[:400]}")
    return result.stdout.decode("utf-8", errors="replace").strip()


def signature_status(path: Path) -> dict:
    status = _powershell(
        "$ErrorActionPreference = 'Stop';"
        "$s = Get-AuthenticodeSignature -LiteralPath '" + _escape_ps(str(path)) + "';"
        "[pscustomobject]@{ status = [string]$s.Status;"
        " message = [string]$s.StatusMessage;"
        " timestamped = [bool]($null -ne $s.TimeStamperCertificate) }"
        " | ConvertTo-Json -Compress"
    )
    try:
        payload = json.loads(status)
    except json.JSONDecodeError as exc:
        raise SystemExit("authenticode status output is unreadable") from exc
    return {
        "status": str(payload.get("status", "")),
        "message": str(payload.get("message", "")),
        "timestamped": bool(payload.get("timestamped")),
        "signed": str(payload.get("status", "")) == "Valid",
    }


def find_signtool() -> Path:
    located = shutil.which("signtool.exe")
    if located:
        return Path(located)
    candidates = sorted(
        Path(r"C:\Program Files (x86)\Windows Kits\10\bin").glob("*/x64/signtool.exe"),
        reverse=True,
    )
    if candidates:
        return candidates[0]
    raise SystemExit("signtool.exe was not found; install the Windows SDK or pass --signtool")


def import_pfx(pfx_path: Path, machine_store: bool = False) -> str:
    password = os.environ.get(PFX_PASSWORD_ENV)
    if password is None:
        raise SystemExit(f"{PFX_PASSWORD_ENV} must be set in the environment for --pfx")
    store = "LocalMachine" if machine_store else "CurrentUser"
    thumbprint = _powershell(
        "$ErrorActionPreference = 'Stop';"
        "$p = ConvertTo-SecureString $env:" + PFX_PASSWORD_ENV + " -AsPlainText -Force;"
        "$c = @(Import-PfxCertificate -FilePath '" + _escape_ps(str(pfx_path)) + "'"
        " -CertStoreLocation ('Cert:\\{0}\\My' -f '" + store + "') -Password $p"
        " | Where-Object { $_.HasPrivateKey });"
        "if ($c.Count -lt 1) { throw 'no certificate with a private key' };"
        "$c[0].Thumbprint"
    )
    if not THUMBPRINT_RE.fullmatch(thumbprint):
        raise SystemExit("imported certificate thumbprint is invalid")
    return thumbprint


def import_pfx_from_env() -> str:
    encoded = os.environ.get(PFX_B64_ENV)
    if not encoded:
        raise SystemExit(f"{PFX_B64_ENV} must be set in the environment")
    try:
        raw = base64.b64decode(encoded.strip(), validate=True)
    except Exception as exc:  # noqa: BLE001 - fail closed on any decode error
        raise SystemExit(f"{PFX_B64_ENV} is not valid base64") from exc
    handle = tempfile.NamedTemporaryFile(suffix=".pfx", delete=False)
    try:
        handle.write(raw)
        handle.close()
        return import_pfx(Path(handle.name))
    finally:
        Path(handle.name).unlink(missing_ok=True)


def select_certificate(args) -> str:
    if args.thumbprint:
        if not THUMBPRINT_RE.fullmatch(args.thumbprint):
            raise SystemExit("--thumbprint must be a 40-hex-character SHA-1 thumbprint")
        return args.thumbprint
    if args.pfx_from_env:
        return import_pfx_from_env()
    if args.pfx:
        return import_pfx(Path(args.pfx).resolve())
    raise SystemExit(
        "signing requires a certificate source: --pfx, --pfx-from-env-b64, or --thumbprint"
    )


def sign_ps1(thumbprint: str, path: Path, timestamp_url: str, machine_store: bool) -> None:
    store = "LocalMachine" if machine_store else "CurrentUser"
    _powershell(
        "$ErrorActionPreference = 'Stop';"
        "$cert = Get-Item ('Cert:\\{0}\\My\\{1}' -f '" + store + "', '" + thumbprint + "');"
        "$null = Set-AuthenticodeSignature -LiteralPath '" + _escape_ps(str(path)) + "'"
        " -Certificate $cert -TimestampServer '" + _escape_ps(timestamp_url) + "'"
        " -HashAlgorithm SHA256"
    )


def sign_pe(signtool: Path, thumbprint: str, path: Path, timestamp_url: str, machine_store: bool) -> None:
    command = [
        str(signtool), "sign", "/sha1", thumbprint,
        *(["/sm"] if machine_store else []),
        "/fd", "sha256", "/tr", timestamp_url, "/td", "sha256", str(path),
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    if result.returncode != 0:
        detail = result.stdout.decode("utf-8", errors="replace").strip()
        raise SystemExit(f"signtool failed for {path.name}: {detail[:400]}")


def resolve_artifacts(root: Path, requested: list[str]) -> list[Path]:
    names = list(requested) if requested else [
        name for name in DEFAULT_LAUNCHER_ARTIFACTS if (root / name).is_file()
    ]
    if not names:
        raise SystemExit("no launcher artifacts are available to sign")
    paths = []
    for name in names:
        path = (root / name)
        if not path.is_file():
            raise SystemExit(f"artifact is missing: {name}")
        paths.append(path.resolve())
    return paths


def inventory(root: Path, requested: list[str]) -> dict:
    artifacts = []
    for path in resolve_artifacts(root, requested):
        relative = path.relative_to(root).as_posix() if root in path.parents else path.name
        kind = "ps1" if path.suffix.casefold() == ".ps1" else (
            "pe" if path.suffix.casefold() in PE_SUFFIXES else "unsupported"
        )
        artifacts.append({"file": relative, "kind": kind, **signature_status(path)})
    return {
        "schema": TIMESTAMP_SCHEMA,
        "all_signed": bool(artifacts) and all(item["signed"] for item in artifacts),
        "all_timestamped": bool(artifacts) and all(item["timestamped"] for item in artifacts),
        "artifacts": artifacts,
    }


def main(argv=None) -> int:
    if sys.platform != "win32":
        raise SystemExit("Authenticode signing requires Windows")
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("inventory", "sign", "verify"))
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--file", action="append", default=[],
        help="artifact to process, relative to --root; defaults to the launcher inventory",
    )
    parser.add_argument("--pfx", help="path to a PFX file; password comes from the environment")
    parser.add_argument(
        "--pfx-from-env-b64", action="store_true", dest="pfx_from_env",
        help=f"import base64 PFX bytes from {PFX_B64_ENV}",
    )
    parser.add_argument("--thumbprint", help="use an existing certificate-store certificate")
    parser.add_argument("--machine-store", action="store_true", help="use the LocalMachine store")
    parser.add_argument("--signtool", type=Path, help="explicit signtool.exe path for PE artifacts")
    parser.add_argument("--timestamp-url", default=DEFAULT_TIMESTAMP_URL)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    report = inventory(root, args.file)
    if args.mode == "inventory":
        print(json.dumps(report, sort_keys=True))
        return 0
    if args.mode == "verify":
        print(json.dumps(report, sort_keys=True))
        if not report["all_signed"] or not report["all_timestamped"]:
            return 2
        return 0
    thumbprint = select_certificate(args)
    signtool = args.signtool.resolve() if args.signtool else None
    for artifact in report["artifacts"]:
        if artifact["kind"] == "ps1":
            sign_ps1(thumbprint, root / artifact["file"], args.timestamp_url, args.machine_store)
        elif artifact["kind"] == "pe":
            signtool = signtool or find_signtool()
            sign_pe(signtool, thumbprint, root / artifact["file"], args.timestamp_url, args.machine_store)
        else:
            raise SystemExit(f"unsupported artifact type: {artifact['file']}")
    verified = inventory(root, args.file)
    print(json.dumps(verified, sort_keys=True))
    if not verified["all_signed"] or not verified["all_timestamped"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
