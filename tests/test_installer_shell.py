"""PKG-MIGRATION-1 static pins for the bundled-runtime and installer shell.

The 3.12 bundled runtime is a portable python312 tree with the client
dependencies installed in place (卡⑦B / R-OP7); the installer shell packages
it per-user. These pins keep the packaging contracts from drifting:
the provisioning script stays venv-free, the bundled client lock stays
test-free, the two client locks stay in lockstep, and the iss shell keeps its
pinned roots, probe, language, and the empty [UninstallDelete] red line.
"""

import re
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]

PROVISION = ROOT / "scripts" / "install_bundled_runtime.ps1"
LOCK_310 = ROOT / "requirements-client-py310.lock.txt"
LOCK_312 = ROOT / "requirements-client-py312.lock.txt"
ISS = ROOT / "installer" / "courselens.iss"
BUILD = ROOT / "installer" / "build_installer.ps1"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_provision_script_ships_a_venv_free_bundled_python312():
    content = _text(PROVISION)
    assert 'Join-Path $ProjectRoot "tools\\python312"' in content
    assert '"3.12.10"' in content
    # both integrity anchors from runtime-assets-style pinning stay present
    assert re.search(r'\$NupkgSha256 = "[0-9A-F]{64}"', content)
    assert re.search(r'\$PythonExeSha256 = "[0-9A-F]{64}"', content)
    assert "requirements-client-py312.lock.txt" in content
    # no venv artifacts in executable lines (comments may contrast with the
    # development recipe); no relocation rewriting anywhere
    code = "\n".join(
        line for line in content.splitlines() if not line.lstrip().startswith("#")
    )
    assert ".venv-client" not in code
    assert "pyvenv.cfg" not in content
    assert "activate" not in content.lower()


def test_bundled_client_lock_never_carries_test_dependencies():
    lines = [
        line.strip().lower()
        for line in _text(LOCK_312).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines, "client lock must not be empty"
    for forbidden in ("pytest", "pluggy", "iniconfig", "_pytest"):
        assert not any(line.startswith(forbidden) for line in lines)


def test_py310_and_py312_client_locks_stay_in_lockstep():
    def pins(path: Path) -> list[str]:
        return sorted(
            line.strip().lower()
            for line in _text(path).splitlines()
            if line.strip() and not line.strip().startswith("#")
        )

    assert pins(LOCK_312) == pins(LOCK_310)


def test_iss_shell_keeps_pinned_roots_and_the_data_red_line():
    raw = ISS.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "iss must stay UTF-8 with BOM"
    content = raw.decode("utf-8-sig")
    assert "AppId={{A2A00F7A-AD6C-4822-B23F-5155DDC320E0}" in content
    assert "DefaultDirName={localappdata}\\Programs\\CourseLens" in content
    assert "PrivilegesRequired=lowest" in content
    assert 'MessagesFile: "ChineseSimplified.isl"' in content
    assert "-AllowExistingEmptyRoot" in content
    assert "{localappdata}\\CourseLens" in content
    assert "WinHttp.WinHttpRequest.5.1" in content
    assert "fudan-courselens" in content
    section = content[content.index("[UninstallDelete]"):]
    assert "DestDir" not in section and "DelTree" not in section


def test_iss_uninstall_prompt_is_silent_safe_and_double_confirmed():
    """UNINST-FIX-1: the /VERYSILENT uninstall must never block on the [Code]
    data-preservation prompt (a plain MsgBox is officially non-suppressible —
    not even /SUPPRESSMSGBOXES hides it), so silent runs log the decision and
    keep the data by default; the interactive "No" branch must pass a second
    confirmation before DelTree, and DelTree must run in directory form
    (True, True, True) — the shipped (False, False, True) call matched
    nothing and deleted nothing (silent no-op behind a "delete" promise)."""
    content = _text(ISS)
    code = content[content.index("procedure CurUninstallStepChanged"):]
    assert "if UninstallSilent() then" in code
    assert "Log(" in code
    assert code.count("MsgBox(") == code.count("SuppressibleMsgBox(")
    # both prompts default to IDYES = keep the data (safe answer when silent)
    assert code.count("MB_YESNO, IDYES)") == 2
    assert "再次确认" in code
    assert "dataRoot := ExpandConstant('{localappdata}\\CourseLens');" in code
    assert "DelTree(dataRoot, True, True, True);" in code
    assert "False, False, True" not in content


def test_installer_build_script_validates_without_signing():
    content = _text(BUILD)
    # INSTALLER-ICON: the build-time static contract pins the uninstall icon
    # directive, so an accidental removal fails the build before release.
    assert "UninstallDisplayIcon={app}\\courselens-icon.ico" in content
    assert "[switch]$ValidateOnly" in content
    assert "[switch]$AllowExistingEmptyRoot" not in content  # that switch lives in the managed installer
    assert "校验单" in content
    assert "Get-FileHash" in content
    # N9-G U1: dev bytecode caches are purged from the staged payload and the
    # purge is verified in-script (pyc = pure download weight for the managed
    # launcher, which runs with PYTHONDONTWRITEBYTECODE=1).
    assert '"__pycache__"' in content
    assert "bytecode purge" in content
    assert 'throw "Bytecode purge left' in content
    # signing belongs to runbook R2 in a separately authorized batch: no
    # signing tool may be invoked or referenced by the build script
    assert "sign_client_release" not in content
    assert "signtool" not in content
    assert "Authenticode" not in content


def test_iss_shell_carries_explorer_identity_and_icon_asset():
    """N9-G U4: the installer must present a real product identity in the
    Explorer properties page (icon on setup.exe/unins000.exe, publisher,
    version resources pinned to the canonical AppVersion channel)."""
    content = _text(ISS)
    assert "SetupIconFile=courselens-icon.ico" in content
    # INSTALLER-ICON: the uninstall entry carries the product icon too —
    # without UninstallDisplayIcon Inno writes no DisplayIcon value and
    # 系统 设置→应用 shows the generic icon (0.1.0 defect). The directive
    # must live inside the [Setup] section to function.
    assert "UninstallDisplayIcon={app}\\courselens-icon.ico" in content
    setup_section = content[content.index("[Setup]") : content.index("[Languages]")]
    assert "UninstallDisplayIcon={app}\\courselens-icon.ico" in setup_section
    assert "AppPublisher=CourseLens" in content
    assert "VersionInfoVersion={#AppVersion}" in content
    assert "VersionInfoProductVersion={#AppVersion}" in content
    assert "VersionInfoProductName={#AppName}" in content
    icon = ROOT / "installer" / "courselens-icon.ico"
    assert icon.exists(), "the rasterized product icon must travel with the iss"
    raw = icon.read_bytes()
    # ICO container magic: reserved 0x0000 + type 0x0001.
    assert raw[:4] == b"\x00\x00\x01\x00", "courselens-icon.ico is not an ICO container"

    expected_sizes = {(size, size) for size in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)}
    frame_count = int.from_bytes(raw[4:6], "little")
    assert frame_count == len(expected_sizes)
    for index in range(frame_count):
        entry = 6 + index * 16
        width = raw[entry] or 256
        height = raw[entry + 1] or 256
        bits = int.from_bytes(raw[entry + 6 : entry + 8], "little")
        payload_size = int.from_bytes(raw[entry + 8 : entry + 12], "little")
        offset = int.from_bytes(raw[entry + 12 : entry + 16], "little")
        assert (width, height) in expected_sizes
        assert bits == 32
        assert raw[offset : offset + 4] == b"\x28\x00\x00\x00", f"{width}px frame lacks a DIB alpha/AND mask"
        assert int.from_bytes(raw[offset + 8 : offset + 12], "little", signed=True) == height * 2
        mask_row_bytes = ((width + 31) // 32) * 4
        assert payload_size >= 40 + width * height * 4 + mask_row_bytes * height

    image = Image.open(icon)
    assert image.ico.sizes() == expected_sizes
    # The ICO uses size-specific optical bounds: taskbar frames gain height
    # without getting wider, while desktop frames gain only a small amount of
    # lateral breathing room. These are minima rather than exact pixels so a
    # future redraw can keep the same optical intent without becoming brittle.
    min_bbox = {
        16: (14, 14),
        20: (18, 18),
        24: (22, 22),
        32: (30, 30),
        40: (38, 38),
        48: (46, 46),
        64: (61, 62),
        96: (92, 94),
        128: (122, 126),
        256: (246, 254),
    }
    for width, height in expected_sizes:
        frame = image.ico.getimage((width, height)).convert("RGBA")
        alpha = frame.getchannel("A")
        assert all(alpha.getpixel((x, y)) == 0 for x, y in (
            *((x, 0) for x in range(width)),
            *((x, height - 1) for x in range(width)),
            *((0, y) for y in range(height)),
            *((width - 1, y) for y in range(height)),
        )), f"{width}px ICO frame has a non-transparent canvas edge"
        left, top, right, bottom = alpha.getbbox()
        bbox_width = right - left
        bbox_height = bottom - top
        min_width, min_height = min_bbox[width]
        assert bbox_width >= min_width
        assert bbox_height >= min_height
        optical_ratio = bbox_width / bbox_height
        if width <= 48:
            assert 0.97 <= optical_ratio <= 1.03
        else:
            assert optical_ratio >= 0.95
        if width >= 96:
            assert bbox_width >= width * 0.88
        if width in (40, 48, 64):
            assert bbox_width >= width * 0.90
        if width <= 32:
            pixels = list(frame.getdata())
            gold = sum(
                1 for r, g, b, a in pixels
                if a and r > 120 and g > 80 and r > g * 1.1 and b < 140
            )
            ivory = sum(
                1 for r, g, b, a in pixels
                if a and r > 160 and g > 150 and b > 120 and r - g < 90
            )
            navy = sum(
                1 for r, g, b, a in pixels
                if a and b > 40 and b > r * 1.15 and b > g * 1.05
            )
            assert gold >= 5, f"{width}px ICO frame lost the gold page layer"
            assert ivory >= width, f"{width}px ICO frame lost the ivory page layer"
            assert navy >= width, f"{width}px ICO frame lost the navy shell/wedge"


def test_iss_shell_opens_dir_page_tasks_and_brand_icons():
    """P66 INSTALLER-UX-1: the destination page always shows and states where
    the learning data lives (the page moves the payload shell only — the
    managed data root stays pinned at {localappdata}\\CourseLens); the tasks
    page offers the desktop shortcut (checked by default) and the quick-launch
    pin (unchecked); every shortcut targets the pythonw silent host in the
    managed root and carries the product icon, which ships with the payload
    shell; the running-instance refusal no longer mentions closing a window
    that no longer exists."""
    content = _text(ISS)
    assert "DisableDirPage=no" in content
    assert "SelectDirLabel3=" in content
    assert "始终存放在本机的 CourseLens 数据目录" in content
    lines = content.splitlines()
    desktop = next(line for line in lines if 'Name: "desktopicon"' in line)
    assert "{cm:CreateDesktopIcon}" in desktop
    assert "unchecked" not in desktop
    quick = next(line for line in lines if 'Name: "quicklaunchicon"' in line)
    assert "Flags: unchecked" in quick
    icon_lines = [line for line in lines if "IconFilename:" in line]
    assert len(icon_lines) == 3
    for line in icon_lines:
        assert "python312\\pythonw.exe" in line
        # -B disables bytecode at interpreter startup: a bare pythonw would
        # write __pycache__ into the trust-verified launcher tree and the
        # trust walk would refuse the launch (P66 sandbox finding)
        assert 'Parameters: "-B "' in line
        assert "start_managed_courselens.pyw" in line
        assert 'IconFilename: "{app}\\courselens-icon.ico"' in line
    start_menu = next(line for line in icon_lines if "{autoprograms}" in line)
    assert "Tasks:" not in start_menu
    assert any("Tasks: desktopicon" in line for line in icon_lines)
    assert any("Tasks: quicklaunchicon" in line for line in icon_lines)
    assert 'Source: "courselens-icon.ico"; DestDir: "{app}"' in content
    assert "关闭浏览器中的 CourseLens 页面" in content
    assert "关闭 CourseLens 窗口" not in content
