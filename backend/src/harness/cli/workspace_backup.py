"""Export and verify an offline local workspace, then restore to a new directory."""
import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from harness.integrations.credentials import CredentialCipher
from harness.integrations.google import GoogleConnector
from harness.integrations.providers import CredentialVault


FORMAT = 1
DATABASE = 'database/assistant.sqlite'
MANIFEST = 'manifest.json'
STATE_FOLDERS = ('workspace', 'credentials')
STATE_DIRECTORY = '.assistant'


def _database(path, readonly=False):
    mode = 'ro' if readonly else 'rw'
    return sqlite3.connect(path.absolute().as_uri() + f'?mode={mode}', uri=True, timeout=5)


def _schema(connection):
    if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise ValueError('Workspace database failed its integrity check')
    definition = connection.execute('''SELECT type,name,tbl_name,sql FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name''').fetchall()
    tables = {entry[1] for entry in definition if entry[0] == 'table'}
    if not {'messages', 'tasks', 'approvals', 'schedules'} <= tables:
        raise ValueError('Archive does not contain a harness database')
    if 'runtime_owner' in tables and connection.execute('SELECT 1 FROM runtime_owner').fetchone():
        raise ValueError('Stop the source workspace cleanly before backing it up')
    return dict(userVersion=connection.execute('PRAGMA user_version').fetchone()[0],
                schemaSha256=hashlib.sha256(json.dumps(definition).encode()).hexdigest())


def _archive_path(name):
    path = PurePosixPath(name)
    if (path.as_posix() != name or path.is_absolute()
            or any(part in ('.', '..') or any(c in part for c in ('\\', ':', '\x00')) for part in path.parts)
            or (name != DATABASE and (len(path.parts) < 3 or path.parts[0] != STATE_DIRECTORY or path.parts[1] not in STATE_FOLDERS))):
        raise ValueError('Invalid workspace archive path')
    return path


def _copy(reader, writer):
    checksum = hashlib.sha256()
    size = 0
    while chunk := reader.read(1024 * 1024):
        writer.write(chunk)
        checksum.update(chunk)
        size += len(chunk)
    return dict(bytes=size, sha256=checksum.hexdigest())


def _state_files(root):
    for name in STATE_FOLDERS:
        base = root / name
        if base.is_symlink() or (base.exists() and not base.is_dir()):
            raise ValueError('Workspace state must use ordinary directories')
        for folder, directories, files in os.walk(base):
            for subdir in directories:
                if (Path(folder) / subdir).is_symlink():
                    raise ValueError('Workspace backup cannot follow symlinks')
            for file in sorted(files):
                path = Path(folder) / file
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError('Workspace backup requires ordinary files')
                yield f'{STATE_DIRECTORY}/' + path.relative_to(root).as_posix(), path


def export_workspace(state, database, archive, build):
    state, database, archive = Path(state).resolve(strict=True), Path(database).resolve(strict=True), Path(archive).absolute()
    if not state.is_dir() or not state.joinpath('workspace').is_dir():
        raise ValueError('Select the existing workspace state directory')
    if archive.exists() or archive.resolve().is_relative_to(state):
        raise ValueError('Choose a new archive outside the workspace state directory')
    fd, temporary = tempfile.mkstemp(prefix='.workspace-backup-', dir=archive.parent)
    os.close(fd)
    try:
        with tempfile.TemporaryDirectory(prefix='.workspace-database-', dir=archive.parent) as folder:
            snapshot = Path(folder) / 'snapshot.sqlite'
            with closing(_database(database)) as lock:
                lock.execute('BEGIN IMMEDIATE')
                metadata = _schema(lock)
                with closing(_database(database, readonly=True)) as source, closing(sqlite3.connect(snapshot)) as target:
                    source.backup(target)
                    target.execute('PRAGMA journal_mode=DELETE')
                    if _schema(target) != metadata:
                        raise ValueError('Workspace database schema changed during backup')
                with ZipFile(temporary, 'w', compression=ZIP_DEFLATED) as bundle:
                    files = {}
                    for name, path in [(DATABASE, snapshot), *_state_files(state)]:
                        _archive_path(name)
                        with path.open('rb') as reader, bundle.open(name, 'w', force_zip64=True) as writer:
                            files[name] = _copy(reader, writer)
                    manifest = dict(format=FORMAT, build=build, createdAt=datetime.now(timezone.utc).isoformat(),
                                    database=metadata, browserProfile='excluded', files=files)
                    bundle.writestr(MANIFEST, json.dumps(manifest, indent=2))
        with open(temporary, 'r+b') as saved:
            os.fsync(saved.fileno())
        if os.name == 'nt':
            os.rename(temporary, archive)  # Windows rename fails if the destination exists.
        else:
            os.link(temporary, archive)
        return manifest
    finally:
        Path(temporary).unlink(missing_ok=True)


async def _verify_credentials(db, state, key):
    has_mcp = db.execute("SELECT 1 FROM sqlite_master WHERE name='mcp_connections' AND type='table'").fetchone()
    mcp = db.execute('SELECT id,envelope FROM mcp_connections') if has_mcp else ()
    google = state / 'credentials/google.enc'
    providers = list((state / 'credentials/providers').glob('*.enc'))
    mcp = list(mcp)
    if not (google.exists() or providers or mcp):
        return 0
    if not key:
        raise ValueError('Supply the original credential key through CREDENTIAL_KEY or HARNESS_TOKEN')
    try:
        if google.exists():
            connector = GoogleConnector(None, None, '', google, key)
            try:
                await connector._load()
            finally:
                await connector.close()
        vault = CredentialVault(state / 'credentials/providers', key)
        for provider in providers:
            vault.load(provider.stem)
        cipher = CredentialCipher(key)
        for identifier, envelope in mcp:
            cipher.open(json.loads(envelope), 'mcp:' + identifier)
    except Exception:
        raise ValueError('Could not unlock restored credentials with the original key') from None
    return int(google.exists()) + len(providers) + len(mcp)


def restore_workspace(archive, target, build, key=None):
    target = Path(target).absolute()
    target.mkdir(mode=0o700)
    try:
        with ZipFile(archive) as bundle:
            entries = bundle.namelist()
            if len(set(entries)) != len(entries) or MANIFEST not in entries:
                raise ValueError('Invalid workspace archive manifest')
            if bundle.getinfo(MANIFEST).file_size > 16 * 1024 * 1024:
                raise ValueError('Workspace archive manifest exceeds the supported size')
            manifest = json.loads(bundle.read(MANIFEST))
            if manifest.get('format') != FORMAT or manifest.get('build') != build:
                raise ValueError('Archive format or source build is incompatible; restore with the original build')
            files = manifest['files']
            if set(entries) != {MANIFEST, *files} or DATABASE not in files:
                raise ValueError('Workspace archive files do not match its manifest')
            for name, expected in files.items():
                path = target.joinpath(*_archive_path(name).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(name) as reader, path.open('xb') as writer:
                    if _copy(reader, writer) != expected:
                        raise ValueError('Workspace archive checksum mismatch')
                path.chmod(0o600)
        state = target / STATE_DIRECTORY
        (state / 'workspace').mkdir(parents=True, exist_ok=True)
        with closing(_database(target / DATABASE, readonly=True)) as db:
            if _schema(db) != manifest['database']:
                raise ValueError('Restored database schema does not match the backup')
            credentials = asyncio.run(_verify_credentials(db, state, key))
        return dict(path=str(target), build=build, integrity='ok', verifiedCredentials=credentials, files=len(files))
    except BaseException:
        shutil.rmtree(target)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    export = commands.add_parser('backup')
    export.add_argument('--state', required=True)
    export.add_argument('--database', required=True)
    restore = commands.add_parser('restore')
    restore.add_argument('--target', required=True)
    for command in (export, restore):
        command.add_argument('--archive', required=True)
        command.add_argument('--build', required=True, help='Git commit or immutable image identifier for this workspace')
    args = parser.parse_args()
    try:
        if args.command == 'backup':
            result = export_workspace(args.state, args.database, args.archive, args.build)
            result = dict(path=args.archive, build=args.build, files=len(result['files']))
        else:
            result = restore_workspace(args.archive, args.target, args.build, os.getenv('CREDENTIAL_KEY') or os.getenv('HARNESS_TOKEN'))
    except (OSError, ValueError, KeyError, sqlite3.Error, BadZipFile) as error:
        parser.exit(1, f'Workspace backup: {error}\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
