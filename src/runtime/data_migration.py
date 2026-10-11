"""数据搬家包（D12 数据主权 P0）：一键导出 + 安家向导导入。

设计定谳（承接 D12-RESEARCH 结论，2026-10-07）：
- 学生数据的唯一持久副本是本地数据根；本模块把它压进一个学生自己拿得走的
  加密包：两个 SQLite（经 ``local_data_recovery._backup_database`` 的 backup
  API 一致性快照，零行值 manifest）+ documents/summaries/courseware/subtitles
  四个文件命名空间 + 人话安家说明。
- 加密选密码保护（PBKDF2-HMAC-SHA256 派生 AES-256-GCM 密钥），不选 DPAPI：
  DPAPI 密文出原机原 Windows 用户即解不开（credentials 的安全特性），对
  「搬到新机」这个场景天然无效；凭据永不进包，新机走「重录三件套」。
- 导入是安家向导的落位半步：校验（容器+哈希+schema 版本）→ 解密到数据根内
  暂存 → learning_documents.storage_path 确定性 rebasing（绝对路径按
  ``documents/<document_id>/original<ext>`` 重写为新机路径）→ 命名空间落位 →
  两个库经 SQLite backup API 原子恢复进活文件（stores 是 connection-per-
  operation，与 client-reset 的原地重建同安全前提）。忙碌应用是导入的常态
  而非例外：backup 以单事务写入、与在途连接天然互斥；落位前先对现库做在线
  快照，任何一步失败都自动回滚原位（D-20261009-05：WAL 句柄锁不再判导入
  失败，遗留 -wal/-shm 的清理只是卫生，留给最后一个连接关闭时回收）。
- manifest 只含哈希/字节/行数/表名，绝不含行值——沿用 local_data_recovery
  的既有红线。

错误全部是稳定闭集码（学生面 UI 直接映射），复用 LocalDataRecoveryError 的
「码+人话指引」形态。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import threading
import time
import uuid
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Callable

from .local_data_recovery import (
    CURRENT_SCHEMA_VERSION,
    DATABASE_NAMES,
    LocalDataRecoveryError,
    SUPPORTED_SCHEMA_VERSIONS,
    _backup_database,
    _connect,
    _restrict,
    _sha256,
    _validate_database,
)

MIGRATION_FORMAT = "courselens.migration-package.v1"
MIGRATION_MANIFEST_SCHEMA = "courselens.migration-manifest.v1"
EXPORT_RECEIPT_SCHEMA = "courselens.data-migration-export.v1"
IMPORT_RECEIPT_SCHEMA = "courselens.data-migration-import.v1"

# 容器布局：MAGIC(9) | header 长度(4 BE) | header JSON | AES-256-GCM 密文(含 tag)
MAGIC = b"CLMIGPKG1"
PACKAGE_SUFFIX = ".clmig"
KDF_ID = "pbkdf2-hmac-sha256"
KDF_ITERATIONS = 600_000
KEY_BYTES = 32
SALT_BYTES = 16
NONCE_BYTES = 12
MAX_PASSWORD_CHARS = 256
MIN_PASSWORD_CHARS = 8
MAX_PACKAGE_BYTES = 8 * 1024 * 1024 * 1024  # 8 GiB 单包上限（诚实拒绝，不静默截断）
STREAM_CHUNK = 1024 * 1024

# 打包面闭集：只带走不可再生的本体，可再生缓存/日志/内部备份/凭据永不进包。
FILE_NAMESPACES = ("documents", "summaries", "courseware", "artifacts/subtitles")
MANIFEST_NAME = "manifest.json"
GUIDE_NAME = "SETTLING-GUIDE.txt"

ERROR_PASSWORD_INVALID = "MIGRATION_E_PASSWORD_INVALID"
ERROR_PACKAGE_INVALID = "MIGRATION_E_PACKAGE_INVALID"
ERROR_PACKAGE_TOO_LARGE = "MIGRATION_E_PACKAGE_TOO_LARGE"
ERROR_SCHEMA_TOO_NEW = "MIGRATION_E_SCHEMA_TOO_NEW"
ERROR_WRITE_FAILED = "MIGRATION_E_WRITE_FAILED"
ERROR_BUSY = "MIGRATION_E_BUSY"

ProgressCallback = Callable[[str, int, int], None]

_EXPORT_SINGLE_FLIGHT = threading.Lock()
_IMPORT_SINGLE_FLIGHT = threading.Lock()

# 凭据重录三件套（D12 定谳：DPAPI 不可迁移=安全特性；给清单，不做迁移）。
CREDENTIALS_REENTRY_NOTICE: tuple[dict[str, str], ...] = (
    {
        "id": "fudan_account",
        "title": "重新登录复旦账号",
        "detail": "打开 CourseLens，用学号密码重新登录一次课程平台。",
    },
    {
        "id": "github_authorization",
        "title": "重新连接 GitHub 授权",
        "detail": "到「设置 → 账户与连接」重新完成 GitHub 连接，专属仓库会按账号自动复用。",
    },
    {
        "id": "deepseek_key",
        "title": "重新填写 DeepSeek Key",
        "detail": "到「设置」重新粘贴你的 DeepSeek Key（原机密文已无法在新机解开）。",
    },
)

_GUIDE_TEXT = """CourseLens 搬家包 · 到新机怎么安家

这个包里有你的全部学习数据：观看进度、AI 总结与问答、复习卡和间隔重复记录、
上传的资料、课件、字幕，以及任务与自动化配置。它是加密的，只有设置密码的人
能打开。

在新电脑上安家（三步）：
  1. 安装并打开 CourseLens（同一官网下载）。
  2. 打开「数据管理」页，点「从搬家包导入…」，选中这个文件，输入导出时设的
     密码。导入完成后应用会自动关闭，重新打开即可。
  3. 重新登录一次：复旦账号（学号密码）、GitHub 授权、DeepSeek Key。
     出于安全设计，保存的密码和密钥不会进包，需要在新机重新录入。

这份包本身就是你的学习档案备份：放在 U 盘、网盘或任何你想放的地方都行。
CourseLens 不会读取它，也不会向任何服务器上传它。

（本文件由你的电脑在导出时生成，不包含你的任何个人信息。）
"""


class DataMigrationError(RuntimeError):
    """稳定、已脱敏的搬家包错误（学生面 UI 可直接映射闭集码）。"""

    def __init__(self, code: str, instruction: str):
        self.code = code
        self.instruction = instruction
        super().__init__(f"{code}: {instruction}")


def _fail(code: str, instruction: str) -> DataMigrationError:
    return DataMigrationError(code, instruction)


def validate_migration_password(password: str) -> str:
    """闭集校验导出/导入密码；8..256 字符，仅做形状检查，不做强度武断。"""
    if not isinstance(password, str):
        raise _fail(ERROR_PASSWORD_INVALID, "请输入 8 位以上的密码。")
    if len(password) < MIN_PASSWORD_CHARS or len(password) > MAX_PASSWORD_CHARS:
        raise _fail(
            ERROR_PASSWORD_INVALID,
            f"密码需要 {MIN_PASSWORD_CHARS}-{MAX_PASSWORD_CHARS} 个字符，再检查一下。",
        )
    return password


def _derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, KDF_ITERATIONS, dklen=KEY_BYTES,
    )


def _atomic_package_write(target: Path, payload: Callable[[Path], None]) -> None:
    """先写同目录临时文件再原子替换，失败的写出不留下半截包。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        payload(temporary)
        _restrict(temporary)
        os.replace(temporary, target)
        _restrict(target)
    finally:
        temporary.unlink(missing_ok=True)


def _walk_namespace(root: Path, namespace: str) -> list[tuple[Path, str, int]]:
    """收集一个命名空间下的全部文件：(绝对路径, 包内相对名, 字节)。"""
    base = root / Path(namespace)
    entries: list[tuple[Path, str, int]] = []
    if not base.is_dir():
        return entries
    for current, _dirs, names in os.walk(base):
        for name in sorted(names):
            path = Path(current) / name
            try:
                if not path.is_file():
                    continue
                size = path.stat().st_size
            except OSError:
                continue
            entries.append((path, f"{namespace}/{path.relative_to(base).as_posix()}", size))
    return entries


def build_migration_package(
    data_root: str | Path,
    output_path: str | Path,
    *,
    password: str,
    app_version: str = "",
    progress: ProgressCallback | None = None,
) -> dict:
    """一键导出：数据根 → 加密搬家包（密码保护）。

    两个库经 SQLite backup API 取一致性快照（可在线执行，零停服）；四个文件
    命名空间直读打包；manifest 永远只有哈希/字节/行数，零行值。
    """
    validate_migration_password(password)
    root = Path(data_root)
    target = Path(output_path)
    if not _EXPORT_SINGLE_FLIGHT.acquire(blocking=False):
        raise _fail(ERROR_BUSY, "已经在打包了，等这一包完成再试。")
    staging = root / f".migration-export-{uuid.uuid4().hex}"
    notify = progress or (lambda *_args: None)
    try:
        staging.mkdir(parents=True, exist_ok=True)
        _restrict(staging)
        databases: dict[str, dict[str, object]] = {}
        for name in DATABASE_NAMES:
            notify("backup-databases", len(databases), len(DATABASE_NAMES))
            source = root / name
            if not source.is_file():
                with closing(_connect(staging / name)):
                    pass
                evidence = _validate_database(staging / name)
                databases[name] = {
                    "bytes": 0, "sha256": _sha256(staging / name),
                    "schema_version": evidence["schema_version"],
                    "tables": evidence["tables"], "counts": evidence["counts"],
                }
            else:
                try:
                    databases[name] = _backup_database(source, staging / name)
                except LocalDataRecoveryError as exc:
                    raise _fail(
                        ERROR_WRITE_FAILED,
                        "本地数据库没能完成一致性快照（" + str(exc.instruction) + "）",
                    ) from exc

        # 文件命名空间：一次遍历收集，直读打包，不在应用目录造第二份副本。
        notify("collect-files", 0, 1)
        collected: dict[str, list[tuple[Path, str, int]]] = {}
        namespace_summary: dict[str, dict[str, int]] = {}
        for namespace in FILE_NAMESPACES:
            collected[namespace] = _walk_namespace(root, namespace)
            namespace_summary[namespace] = {
                "files": len(collected[namespace]),
                "bytes": sum(item[2] for item in collected[namespace]),
            }

        manifest: dict[str, object] = {
            "schema": MIGRATION_MANIFEST_SCHEMA,
            "format": MIGRATION_FORMAT,
            "created_at": int(time.time()),
            "app_version": str(app_version),
            "target_schema_version": CURRENT_SCHEMA_VERSION,
            "databases": {
                name: {
                    "bytes": item["bytes"], "sha256": item["sha256"],
                    "schema_version": item["schema_version"],
                    "tables": item["tables"], "counts": item["counts"],
                }
                for name, item in databases.items()
            },
            "namespaces": namespace_summary,
        }
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        _restrict(staging / MANIFEST_NAME)
        (staging / GUIDE_NAME).write_text(_GUIDE_TEXT, encoding="utf-8")
        _restrict(staging / GUIDE_NAME)

        notify("zip", 0, 1)
        payload_path = staging / "payload.zip"
        with closing(zipfile.ZipFile(payload_path, "w", zipfile.ZIP_DEFLATED)) as archive:
            for name in DATABASE_NAMES:
                archive.write(staging / name, arcname=name)
            archive.write(staging / MANIFEST_NAME, arcname=MANIFEST_NAME)
            archive.write(staging / GUIDE_NAME, arcname=GUIDE_NAME)
            for namespace in FILE_NAMESPACES:
                for source_path, arcname, _size in collected[namespace]:
                    archive.write(source_path, arcname=arcname)

        notify("encrypt", 0, 1)
        payload_sha = _sha256(payload_path)
        salt = os.urandom(SALT_BYTES)
        nonce = os.urandom(NONCE_BYTES)
        header = {
            "format": MIGRATION_FORMAT,
            "kdf": KDF_ID,
            "iterations": KDF_ITERATIONS,
            "salt": salt.hex(),
            "nonce": nonce.hex(),
            "payload_sha256": payload_sha,
            "created_at": manifest["created_at"],
            "app_version": str(app_version),
        }
        header_bytes = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
        cipher_state = _new_gcm(_derive_key(password, salt), nonce)

        def _write_encrypted(temporary: Path) -> None:
            with temporary.open("wb") as out:
                out.write(MAGIC)
                out.write(len(header_bytes).to_bytes(4, "big"))
                out.write(header_bytes)
                with payload_path.open("rb") as source:
                    while True:
                        chunk = source.read(STREAM_CHUNK)
                        if not chunk:
                            break
                        out.write(cipher_state.encrypt(chunk))
                out.write(cipher_state.digest())
                out.flush()
                os.fsync(out.fileno())

        _atomic_package_write(target, _write_encrypted)
        return {
            "schema": EXPORT_RECEIPT_SCHEMA,
            "format": MIGRATION_FORMAT,
            "filename": target.name,
            "path": str(target),
            "bytes": target.stat().st_size,
            "sha256": _sha256(target),
            "payload_sha256": payload_sha,
            "created_at": manifest["created_at"],
            "app_version": str(app_version),
            "databases": {
                name: {"schema_version": item["schema_version"], "counts": item["counts"]}
                for name, item in databases.items()
            },
            "namespaces": namespace_summary,
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        _EXPORT_SINGLE_FLIGHT.release()


def _new_gcm(key: bytes, nonce: bytes):
    from Crypto.Cipher import AES  # pycryptodome 是既有运行时依赖；延迟导入降低模块耦合

    return AES.new(key, AES.MODE_GCM, nonce=nonce)


def _read_container_header(package_path: Path) -> dict[str, object]:
    """读容器头（不碰密文）。魔法数/格式不对 = 诚实拒绝，不猜不兜。"""
    try:
        with package_path.open("rb") as handle:
            magic = handle.read(len(MAGIC))
            if magic != MAGIC:
                raise ValueError("magic mismatch")
            raw_length = handle.read(4)
            if len(raw_length) != 4:
                raise ValueError("truncated header length")
            length = int.from_bytes(raw_length, "big")
            if length <= 0 or length > 64 * 1024:
                raise ValueError("header length out of range")
            header_bytes = handle.read(length)
            if len(header_bytes) != length:
                raise ValueError("truncated header")
    except OSError as exc:
        raise _fail(ERROR_PACKAGE_INVALID, "搬家包读不到，可能被移动或删除了。") from exc
    try:
        header = json.loads(header_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail(ERROR_PACKAGE_INVALID, "这不是 CourseLens 搬家包。") from exc
    if not isinstance(header, dict) or header.get("format") != MIGRATION_FORMAT:
        raise _fail(ERROR_PACKAGE_INVALID, "这不是 CourseLens 搬家包，或版本不被支持。")
    if header.get("kdf") != KDF_ID:
        raise _fail(ERROR_PACKAGE_INVALID, "搬家包的加密方式不被这版客户端支持，请更新客户端。")
    try:
        int(header["iterations"]); bytes.fromhex(str(header["salt"])); bytes.fromhex(str(header["nonce"]))
        bytes.fromhex(str(header["payload_sha256"]))
        int(header["created_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise _fail(ERROR_PACKAGE_INVALID, "搬家包头不完整，文件可能已损坏。") from exc
    return header


def _decrypt_payload(
    package_path: Path, header: dict[str, object], staging: Path, *, password: str,
) -> Path:
    """流式解密密文到暂存 zip；GCM 校验失败 = 密码不对或包被改过（零落半截）。"""
    salt = bytes.fromhex(str(header["salt"]))
    nonce = bytes.fromhex(str(header["nonce"]))
    payload_sha = str(header["payload_sha256"])
    payload_path = staging / "payload.zip"
    try:
        size = package_path.stat().st_size
        with package_path.open("rb") as handle:
            handle.seek(len(MAGIC))
            length = int.from_bytes(handle.read(4), "big")
            ciphertext_start = len(MAGIC) + 4 + length
            ciphertext_len = size - ciphertext_start - 16
            if ciphertext_len <= 0:
                raise ValueError("ciphertext is empty")
            handle.seek(ciphertext_start)
            cipher = _new_gcm(_derive_key(password, salt), nonce)
            with payload_path.open("wb") as out:
                remaining = ciphertext_len
                while remaining > 0:
                    chunk = handle.read(min(STREAM_CHUNK, remaining))
                    if not chunk:
                        raise ValueError("truncated ciphertext")
                    out.write(cipher.decrypt(chunk))
                    remaining -= len(chunk)
                tag = handle.read(16)
                cipher.verify(tag)
    except ValueError as exc:
        payload_path.unlink(missing_ok=True)
        raise _fail(
            ERROR_PASSWORD_INVALID,
            "密码不对，或者包在传输中被改动了；确认密码后重试。",
        ) from exc
    except OSError as exc:
        payload_path.unlink(missing_ok=True)
        raise _fail(ERROR_PACKAGE_INVALID, "搬家包读不完，文件可能不完整。") from exc
    if _sha256(payload_path) != payload_sha:
        payload_path.unlink(missing_ok=True)
        raise _fail(ERROR_PACKAGE_INVALID, "搬家包校验不过（内容哈希不符），文件可能已损坏。")
    return payload_path


# 密码只在调用栈内传参传递，不挂长存对象、不进日志与回执。


def _open_package_zip(payload_path: Path) -> tuple[zipfile.ZipFile, dict[str, object]]:
    try:
        archive = zipfile.ZipFile(payload_path)
        manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
    except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        try:
            archive.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        raise _fail(ERROR_PACKAGE_INVALID, "搬家包内部结构不完整，文件可能已损坏。") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != MIGRATION_MANIFEST_SCHEMA:
        archive.close()
        raise _fail(ERROR_PACKAGE_INVALID, "搬家包清单不被支持，请用同一版本或更新版本的 CourseLens 导入。")
    return archive, manifest


def _validate_staged_databases(staging: Path, manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    """逐库对哈希 + 完整性校验 + schema 版本闭集（钉：哈希校验+版本匹配）。"""
    databases = manifest.get("databases")
    if not isinstance(databases, dict):
        raise _fail(ERROR_PACKAGE_INVALID, "搬家包清单缺少数据库证据，文件可能不完整。")
    validated: dict[str, dict[str, object]] = {}
    for name in DATABASE_NAMES:
        item = databases.get(name)
        path = staging / name
        if not isinstance(item, dict) or not path.is_file():
            raise _fail(ERROR_PACKAGE_INVALID, f"搬家包缺少 {name}，文件可能不完整。")
        if _sha256(path) != str(item.get("sha256") or ""):
            raise _fail(ERROR_PACKAGE_INVALID, f"{name} 校验不过（哈希不符），包可能已损坏。")
        try:
            evidence = _validate_database(path)
        except LocalDataRecoveryError as exc:
            raise _fail(ERROR_PACKAGE_INVALID, f"{name} 没能通过完整性校验，包可能已损坏。") from exc
        version = int(evidence.get("schema_version") or 0)
        if version > CURRENT_SCHEMA_VERSION:
            raise _fail(
                ERROR_SCHEMA_TOO_NEW,
                "这个搬家包来自更新版本的 CourseLens；请先把这台电脑的客户端升级到最新再导入。",
            )
        validated[name] = {
            "schema_version": version,
            "counts": evidence.get("counts") or {},
        }
    return validated


def _rebase_document_paths(staged_db: Path, documents_root: Path) -> int:
    """storage_path 确定性 rebasing（D12 定谳的零自愈痛点，导入时一次性治好）。

    旧库里的绝对路径指向原机；documents 按
    ``documents/<document_id>/original<ext>`` 内容寻址，新机路径可以不靠旧前缀
    确定性重写。返回重写行数（学生面收尾会报「已修正 N 条资料路径」）。
    """
    path = staged_db
    if not path.is_file():
        return 0
    documents_root = documents_root.resolve()
    rebased = 0
    with closing(_connect(path)) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_documents'"
        ).fetchone()
        if not exists:
            return 0
        rows = db.execute(
            "SELECT document_id, extension FROM learning_documents ORDER BY document_id"
        ).fetchall()
        for document_id, extension in rows:
            document_id = str(document_id or "").strip()
            extension = str(extension or "").strip().lstrip(".").casefold()
            if not document_id or not extension:
                continue
            new_path = str(documents_root / document_id / f"original.{extension}")
            db.execute(
                "UPDATE learning_documents SET storage_path=? WHERE document_id=?",
                (new_path, document_id),
            )
            rebased += 1
        db.commit()
    return rebased


def _place_namespace(staged_root: Path, namespace: str, root: Path, trash: Path) -> dict[str, int | str | None]:
    staged = staged_root / Path(namespace)
    target = root / Path(namespace)
    if not staged.is_dir():
        return {"files": 0, "trash_name": None}
    files = sum(1 for current, _dirs, names in os.walk(staged) for name in names)
    target.parent.mkdir(parents=True, exist_ok=True)
    trash_name: str | None = None
    if target.exists():
        trash_name = f"{target.name}-{uuid.uuid4().hex[:8]}"
        target.rename(trash / trash_name)
    staged.rename(target)
    return {"files": files, "trash_name": trash_name}


def _restore_databases(staging: Path, root: Path) -> None:
    """把暂存库经 SQLite backup API 原子恢复进活文件（connection-per-operation）。

    忙碌应用（他方连接、在途轮询/任务）是搬家导入的常态而非例外：backup 以
    单事务写入活库，与在途读写天然互斥，不需要「先静默」。提交后在活连接上
    做 TRUNCATE checkpoint，把已提交页冲进主库文件；遗留 -wal/-shm 的清理是
    卫生不是成败项——他方连接握着句柄时（Windows WinError 32）清不掉就留给
    SQLite（最后一个连接关闭时自行回收），WAL 在任何时刻都是有效一致的状态，
    绝不为它判导入失败（D-20261009-05 的根修）。
    """
    for name in DATABASE_NAMES:
        with closing(_connect(staging / name, read_only=True)) as source:
            with closing(_connect(root / name)) as live:
                source.backup(live)
                try:
                    live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.DatabaseError:
                    pass  # 他方连接长期占锁时等待超时；已提交内容仍在（主库或 WAL，均一致）
        for suffix in ("-wal", "-shm"):
            try:
                (root / (name + suffix)).unlink(missing_ok=True)
            except OSError:
                pass  # 句柄被在途连接占用：清理失败不影响导入成败，也不损数据


def _rollback_import(
    root: Path,
    staging: Path,
    trash: Path,
    placements: list[dict[str, object]],
    snapshots: dict[str, dict[str, object]],
) -> list[str]:
    """失败回滚：已落位命名空间退场、原件复位，两库从在线快照原样恢复。

    回滚尽力而为但绝不静默：任何一步失手都记入问题清单（原数据仍在 trash
    里可寻回），回执的诚实取决于这份清单。全部成功时调用方可以安全清掉 trash。
    """
    problems: list[str] = []
    for record in reversed(placements):
        namespace = str(record.get("namespace") or "")
        if not namespace:
            continue
        target = root / Path(namespace)
        staged = staging / Path(namespace)
        trash_name = str(record.get("trash_name") or "")
        try:
            if target.exists() and not staged.exists():
                target.rename(staged)  # 新件退回暂存区（finally 统一清除）
            if trash_name and not target.exists():
                original = trash / trash_name
                if original.exists():
                    original.rename(target)  # 原件复位
        except OSError as exc:
            problems.append(f"{namespace}: {exc.strerror or exc}")
    for name in snapshots:
        try:
            with closing(_connect(trash / f"snapshot-{name}", read_only=True)) as source:
                with closing(_connect(root / name)) as live:
                    source.backup(live)
        except (OSError, sqlite3.Error, LocalDataRecoveryError) as exc:
            problems.append(f"{name}: {getattr(exc, 'strerror', None) or '数据库回滚未完成'}")
    # 没有快照的库 = 导入开始时不存在（新机首装安家）：本次新建的半套库直接
    # 清除，不留「新库 + 旧文件」的混合态。
    for name in DATABASE_NAMES:
        if name in snapshots:
            continue
        try:
            (root / name).unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                (root / (name + suffix)).unlink(missing_ok=True)
        except OSError as exc:
            problems.append(f"{name}: {exc.strerror or exc}")
    return problems


def _extract_package(archive: zipfile.ZipFile, staging: Path) -> None:
    for member in archive.namelist():
        name = member.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            raise _fail(ERROR_PACKAGE_INVALID, "搬家包含有不被允许的路径，已拒绝导入。")
    archive.extractall(staging)


def import_migration_package(
    data_root: str | Path,
    package_path: str | Path,
    *,
    password: str,
    app_version: str = "",
    progress: ProgressCallback | None = None,
) -> dict:
    """安家向导落位：校验 → 解密 → rebasing → 落位 → 原子恢复两库。

    单飞（同一时刻只允许一个导入）；落位前先对现库做在线快照，任何一步失败
    都自动回滚到原位——学生看到的失败永远意味着「原数据一点没动」或「原数据
    完好保存在 trash 文件夹」，绝不是一个混合态（fail-closed）。
    """
    validate_migration_password(password)
    root = Path(data_root)
    package = Path(package_path)
    if not _IMPORT_SINGLE_FLIGHT.acquire(blocking=False):
        raise _fail(ERROR_BUSY, "已经有一个导入在进行了，等它结束再试。")
    staging = root / f".migration-import-{uuid.uuid4().hex}"
    trash = root / f"migration-import-trash-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    notify = progress or (lambda *_args: None)
    try:
        try:
            size = package.stat().st_size
        except OSError as exc:
            raise _fail(ERROR_PACKAGE_INVALID, "搬家包读不到，确认文件位置后重试。") from exc
        if size > MAX_PACKAGE_BYTES:
            raise _fail(ERROR_PACKAGE_TOO_LARGE, "这个包超过了 8GB 的导入上限，换一个更小的包。")
        header = _read_container_header(package)
        notify("decrypt", 0, 1)
        staging.mkdir(parents=True, exist_ok=True)
        _restrict(staging)
        payload_path = _decrypt_payload(package, header, staging, password=password)
        archive = None
        try:
            archive, manifest = _open_package_zip(payload_path)
            notify("verify", 0, 1)
            _extract_package(archive, staging)
        finally:
            if archive is not None:
                archive.close()
        payload_path.unlink(missing_ok=True)
        validated = _validate_staged_databases(staging, manifest)
        rebased = _rebase_document_paths(staging / "learning.db", root / "documents")
        notify("place", 0, 1)
        trash.mkdir(parents=True, exist_ok=True)
        namespaces: dict[str, dict[str, int]] = {}
        placements: list[dict[str, object]] = []
        snapshots: dict[str, dict[str, object]] = {}
        try:
            # 先给现库拍在线快照（backup API，只读不锁死）：任何后续失败都可
            # 原样恢复。现库不存在（新机首装安家）则无需快照。
            for name in DATABASE_NAMES:
                live_path = root / name
                if live_path.is_file():
                    snapshots[name] = _backup_database(live_path, trash / f"snapshot-{name}")
            for namespace in FILE_NAMESPACES:
                placed = _place_namespace(staging, namespace, root, trash)
                placements.append({
                    "namespace": namespace,
                    "trash_name": placed.get("trash_name"),
                })
                namespaces[namespace] = {"files": int(placed["files"])}
            notify("restore-databases", 0, 1)
            _restore_databases(staging, root)
        except (OSError, sqlite3.Error, LocalDataRecoveryError) as exc:
            problems = _rollback_import(root, staging, trash, placements, snapshots)
            if not problems:
                # 回滚全清：trash 里只剩冗余快照，可以安全清掉，零残留。
                shutil.rmtree(trash, ignore_errors=True)
            if problems:
                instruction = (
                    "导入没能完成，而且有几个文件没能自动回到原位。别担心，你的原有"
                    f"数据完好保存在数据目录的「{trash.name}」文件夹里，一点都没丢。"
                    "请完全退出 CourseLens（关闭所有窗口）再重新打开，然后再试一次导入。"
                )
            elif placements:
                instruction = (
                    "导入没能完成。你的原有数据已经自动恢复原位，一点没丢，"
                    "可以直接再试一次；如果反复出现，先完全退出 CourseLens 再打开重试。"
                )
            elif isinstance(exc, LocalDataRecoveryError):
                instruction = (
                    f"导入还没开始落位就没通过（{exc.instruction}）。"
                    "你的原有数据一点没动，确认后可以直接再试。"
                )
            else:
                instruction = (
                    "导入还没开始落位就没通过：数据目录暂时写不进去。"
                    "你的原有数据一点没动，稍后再试一次。"
                )
            raise _fail(ERROR_WRITE_FAILED, instruction) from exc
        shutil.rmtree(trash, ignore_errors=True)
        return {
            "schema": IMPORT_RECEIPT_SCHEMA,
            "format": MIGRATION_FORMAT,
            "imported_at": int(time.time()),
            "app_version": str(app_version),
            "exported_app_version": str(manifest.get("app_version") or ""),
            "databases": validated,
            "namespaces": namespaces,
            "rebased_document_paths": rebased,
            "credentials_reentry": [dict(item) for item in CREDENTIALS_REENTRY_NOTICE],
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        _IMPORT_SINGLE_FLIGHT.release()


def inspect_migration_package(package_path: str | Path, *, password: str) -> dict:
    """只读探针：校验容器/密码/哈希/schema，返回脱敏摘要，不落位。"""
    validate_migration_password(password)
    package = Path(package_path)
    staging_parent = package.parent
    if not staging_parent.is_dir():
        raise _fail(ERROR_PACKAGE_INVALID, "搬家包读不到，确认文件位置后重试。")
    staging = Path(staging_parent) / f".migration-inspect-{uuid.uuid4().hex}"
    try:
        if package.stat().st_size > MAX_PACKAGE_BYTES:
            raise _fail(ERROR_PACKAGE_TOO_LARGE, "这个包超过了 8GB 的上限。")
        header = _read_container_header(package)
        staging.mkdir(parents=True, exist_ok=True)
        _restrict(staging)
        payload_path = _decrypt_payload(package, header, staging, password=password)
        archive = None
        try:
            archive, manifest = _open_package_zip(payload_path)
            try:
                for name in DATABASE_NAMES:
                    info = archive.getinfo(name)
                    if info.file_size <= 0:
                        raise _fail(ERROR_PACKAGE_INVALID, f"搬家包缺少 {name}，文件可能不完整。")
            except KeyError as exc:
                raise _fail(ERROR_PACKAGE_INVALID, "搬家包缺少数据库文件，文件可能不完整。") from exc
        finally:
            if archive is not None:
                archive.close()
        databases = manifest.get("databases")
        summary: dict[str, object] = {}
        if isinstance(databases, dict):
            for name, item in databases.items():
                if isinstance(item, dict):
                    summary[str(name)] = {"schema_version": item.get("schema_version")}
        namespaces = manifest.get("namespaces")
        return {
            "schema": "courselens.data-migration-inspect.v1",
            "format": MIGRATION_FORMAT,
            "created_at": header.get("created_at"),
            "app_version": header.get("app_version"),
            "databases": summary,
            "namespaces": namespaces if isinstance(namespaces, dict) else {},
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)


__all__ = [
    "CREDENTIALS_REENTRY_NOTICE",
    "CURRENT_SCHEMA_VERSION",
    "DataMigrationError",
    "ERROR_BUSY",
    "ERROR_PACKAGE_INVALID",
    "ERROR_PACKAGE_TOO_LARGE",
    "ERROR_PASSWORD_INVALID",
    "ERROR_SCHEMA_TOO_NEW",
    "ERROR_WRITE_FAILED",
    "EXPORT_RECEIPT_SCHEMA",
    "FILE_NAMESPACES",
    "GUIDE_NAME",
    "IMPORT_RECEIPT_SCHEMA",
    "KDF_ID",
    "KDF_ITERATIONS",
    "MAGIC",
    "MAX_PACKAGE_BYTES",
    "MAX_PASSWORD_CHARS",
    "MIN_PASSWORD_CHARS",
    "MIGRATION_FORMAT",
    "MIGRATION_MANIFEST_SCHEMA",
    "PACKAGE_SUFFIX",
    "SUPPORTED_SCHEMA_VERSIONS",
    "build_migration_package",
    "import_migration_package",
    "inspect_migration_package",
    "validate_migration_password",
]
