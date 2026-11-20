"""Bind-mounted local SQLite setup and consistent inspection snapshots.

Run host commands from Linux/WSL with Docker available. No existing database is
overwritten or deleted. This exports SQLite only, not a complete workspace backup.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4


def copy_database(source, target, allow_live=False):
    source=Path(source).resolve(strict=True); target=Path(target).absolute()
    if target.exists(): raise ValueError('Destination database already exists; refusing to overwrite it')
    if not target.parent.is_dir(): raise ValueError('Create the destination directory first')
    temporary=target.parent/('.database-copy-'+uuid4().hex+'.sqlite')
    reader=writer=None
    try:
        reader=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=10)
        if reader.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('Source database integrity check failed')
        has_owner=reader.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_owner'").fetchone()
        if not allow_live and has_owner and reader.execute('SELECT count(*) FROM runtime_owner').fetchone()[0]:
            raise ValueError('Stop the source app cleanly before migration; its ownership lease is still present')
        writer=sqlite3.connect(temporary,timeout=10)
        deadline=time.monotonic()+90
        def progress(status,remaining,total):
            if time.monotonic()>deadline: raise TimeoutError('Database snapshot exceeded its deadline')
        reader.backup(writer,pages=512,progress=progress,sleep=.05)
        writer.execute('PRAGMA journal_mode=DELETE')
        if writer.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('Copied database integrity check failed')
        tables=[row[0] for row in writer.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        counts={name:writer.execute('SELECT count(*) FROM "'+name.replace('"','""')+'"').fetchone()[0] for name in tables}
        if not allow_live:
            for name,count in counts.items():
                actual=reader.execute('SELECT count(*) FROM "'+name.replace('"','""')+'"').fetchone()[0]
                if actual!=count: raise ValueError('Source changed during migration; keep the original volume and retry')
        writer.close(); writer=None; reader.close(); reader=None
        os.chmod(temporary,0o600)
        # Hard-link publication is atomic and refuses an existing destination.
        # Both paths are in the same destination directory/filesystem.
        os.link(temporary,target)
        temporary.unlink()
        return dict(integrity='ok',tables=counts,sha256=hashlib.sha256(target.read_bytes()).hexdigest(),bytes=target.stat().st_size)
    finally:
        if writer is not None: writer.close()
        if reader is not None: reader.close()
        for candidate in (temporary,Path(str(temporary)+'-wal'),Path(str(temporary)+'-shm'),Path(str(temporary)+'-journal')):
            try:candidate.unlink()
            except FileNotFoundError:pass


def docker(*args):
    return subprocess.check_output(['docker',*args],text=True).strip()


def export_snapshot(source, target):
    """Export a completed native snapshot without Unix chmod on Windows shares.

    The Windows directory ACL controls desktop access. Exclusive creation keeps
    an existing export safe, and a failed copy removes only its own new file.
    """
    source=Path(source);target=Path(target)
    created=False
    try:
        with target.open('xb') as output:
            created=True
            with source.open('rb') as reader:shutil.copyfileobj(reader,output)
            output.flush();os.fsync(output.fileno())
        digest=hashlib.sha256(target.read_bytes()).hexdigest()
        if digest!=hashlib.sha256(source.read_bytes()).hexdigest():raise ValueError('Exported snapshot checksum mismatch')
        return digest
    except BaseException:
        if created:target.unlink(missing_ok=True)
        raise


def native_directory(path):
    if sys.platform!='linux': raise ValueError('Run this command inside Linux/WSL, using a native local directory')
    path=Path(path).absolute()
    if path.is_symlink(): raise ValueError('Database directory must not be a symlink')
    path.mkdir(parents=True,exist_ok=True)
    filesystem=subprocess.check_output(['stat','-f','-c','%T',str(path)],text=True).strip()
    if filesystem not in ('ext2/ext3','xfs','btrfs','zfs'):
        raise ValueError('Use a native persistent Linux filesystem for live SQLite WAL; found '+filesystem)
    os.chown(path,10001,10001); os.chmod(path,0o700)
    return path.resolve()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    migrate=commands.add_parser('migrate')
    migrate.add_argument('--database-dir',required=True)
    migrate.add_argument('--source-volume',default='personal-agent-harness_assistant-state')
    migrate.add_argument('--container',default='personal-agent-harness-app-1')
    snapshot=commands.add_parser('snapshot')
    snapshot.add_argument('--database-dir',required=True)
    snapshot.add_argument('--output-dir',required=True)
    for command in (migrate,snapshot):command.add_argument('--image',default='personal-agent-harness:local')
    copy=commands.add_parser('_copy',help=argparse.SUPPRESS)
    copy.add_argument('--source',required=True);copy.add_argument('--target',required=True);copy.add_argument('--allow-live',action='store_true')
    args=parser.parse_args()
    if args.command=='_copy':
        print(json.dumps(copy_database(args.source,args.target,args.allow_live)));return
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/:@-]{0,200}',args.image):raise ValueError('Invalid image name')
    database=native_directory(args.database_dir)
    command=['run','--rm','--network','none','--read-only','--user','10001:10001','--cap-drop','ALL',
             '--security-opt','no-new-privileges:true','--tmpfs','/tmp:rw,nosuid,size=67108864',
             '--mount','type=bind,src='+str(Path(__file__).resolve())+',dst=/opt/local-database.py,readonly']
    if args.command=='migrate':
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,150}',args.source_volume):raise ValueError('Invalid source volume')
        info=json.loads(docker('inspect',args.container))[0]
        if info['State']['Running']:raise ValueError('Stop the app before migrating its database')
        if not any(m.get('Name')==args.source_volume and m.get('Destination')=='/data/.assistant' for m in info['Mounts']):
            raise ValueError('Source volume does not match this app container')
        command+=['--mount','type=volume,src='+args.source_volume+',dst=/source',
                  '--mount','type=bind,src='+str(database)+',dst=/target']
        output=database/'assistant.sqlite'
        command+=['--entrypoint','python',args.image,'/opt/local-database.py','_copy','--source','/source/assistant.sqlite','--target','/target/assistant.sqlite']
    else:
        output_dir=Path(args.output_dir).absolute();output_dir.mkdir(parents=True,exist_ok=True)
        output=output_dir/('assistant-'+time.strftime('%Y%m%d-%H%M%S',time.gmtime())+'-'+uuid4().hex[:8]+'.sqlite')
        # SQLite backup and private chmod run on native storage. Only the closed,
        # standalone copy is exported to the desktop filesystem afterwards.
        snapshots=native_directory(database/'snapshots')
        command+=['--mount','type=bind,src='+str(database)+',dst=/source',
                  '--mount','type=bind,src='+str(snapshots)+',dst=/target',
                  '--entrypoint','python',args.image,'/opt/local-database.py','_copy','--source','/source/assistant.sqlite','--target','/target/'+output.name,'--allow-live']
    report=json.loads(docker(*command));report['path']=str(output)
    if args.command=='snapshot':
        native_copy=snapshots/output.name
        if native_copy!=output:export_snapshot(native_copy,output)
    output.with_suffix('.manifest.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
