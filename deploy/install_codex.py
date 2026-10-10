"""Install the official x86_64 Linux Codex binary with release digest verification."""
import hashlib
import json
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

with urllib.request.urlopen('https://api.github.com/repos/openai/codex/releases/latest', timeout=30) as response:
    release = json.load(response)
name = 'codex-x86_64-unknown-linux-musl.tar.gz'
asset = next(item for item in release['assets'] if item['name'] == name)
with tempfile.TemporaryDirectory(prefix='codex-install-') as folder:
    archive = Path(folder) / name
    with urllib.request.urlopen(asset['browser_download_url'], timeout=120) as source:
        with archive.open('wb') as target:
            shutil.copyfileobj(source, target)
    with archive.open('rb') as handle:
        digest = 'sha256:' + hashlib.file_digest(handle, 'sha256').hexdigest()
    if asset.get('digest') != digest:
        raise SystemExit('Release digest unavailable or mismatched; installation stopped.')
    with tarfile.open(archive) as package:
        member = next(item for item in package.getmembers()
                      if Path(item.name).name == 'codex-x86_64-unknown-linux-musl' and item.isfile())
        with package.extractfile(member) as source, open('/usr/local/bin/codex', 'wb') as target:
            shutil.copyfileobj(source, target)
    Path('/usr/local/bin/codex').chmod(0o755)
print('Installed Codex release:', release['tag_name'])
