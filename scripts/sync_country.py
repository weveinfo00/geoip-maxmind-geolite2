#!/usr/bin/env python3
"""Publish verified free GeoLite2 Country + City MMDB/CSV in one release."""

from __future__ import annotations

import base64
import csv
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

EDITION = 'GeoLite2-Country'
DATABASE = EDITION + '.mmdb'
CSV_EDITION = EDITION + '-CSV'
CSV_ARCHIVE = CSV_EDITION + '.zip'
REQUIRED_CSV = {EDITION + '-Blocks-IPv4.csv', EDITION + '-Blocks-IPv6.csv',
                EDITION + '-Locations-en.csv'}
# Country names, tags and marker remain compatible with existing download URLs.
CITY_EDITION = 'GeoLite2-City'
CITY_DATABASE = CITY_EDITION + '.mmdb'
CITY_CSV_EDITION = CITY_EDITION + '-CSV'
CITY_CSV_ARCHIVE = CITY_CSV_EDITION + '.zip'
CITY_REQUIRED_CSV = {CITY_EDITION + '-Blocks-IPv4.csv', CITY_EDITION + '-Blocks-IPv6.csv',
                     CITY_EDITION + '-Locations-en.csv'}
CITY_LICENSE_ASSETS = {CITY_EDITION + '-COPYRIGHT.txt', CITY_EDITION + '-LICENSE.txt'}
MARKER = '<!-- geolite2-country-sync:v1 '
TAG_RE = re.compile(r'^geolite2-country-([0-9]{9,12})-([0-9a-f]{12})$')
STATE_PATH = '.github/sync-state.json'
MAX_DATABASE_BYTES = 512 * 1024 * 1024
MAX_CSV_UNPACKED_BYTES = 1024 * 1024 * 1024
COUNTRY_REQUIRED_ASSETS = {DATABASE, DATABASE + '.sha256', CSV_ARCHIVE, CSV_ARCHIVE + '.sha256',
                   'SHA256SUMS', 'metadata.json', 'COPYRIGHT.txt', 'LICENSE.txt'} | REQUIRED_CSV
REQUIRED_ASSETS = (COUNTRY_REQUIRED_ASSETS | CITY_REQUIRED_CSV | CITY_LICENSE_ASSETS
                   | {CITY_DATABASE, CITY_DATABASE + '.sha256', CITY_CSV_ARCHIVE,
                      CITY_CSV_ARCHIVE + '.sha256'})
UTC = timezone.utc


class SyncError(RuntimeError):
    """An error that may safely be shown in the workflow log."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def file_sha256(path: Path) -> str:
    """Hash large City CSVs without loading an entire file into memory."""
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def summary(message: str) -> None:
    print(message, flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
            stream.write(message + '\n\n')


def gh(*args: str, payload: Any = None, allow_not_found: bool = False) -> str | None:
    command = ['gh', *args]
    data = json.dumps(payload) if payload is not None else None
    try:
        result = subprocess.run(command, input=data, text=True, capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SyncError('GitHub CLI 실행에 실패했습니다.') from exc
    if result.returncode:
        if allow_not_found and '(HTTP 404)' in result.stderr:
            return None
        # Do not echo raw response bodies or headers into public logs.
        operation = ' '.join(args[:3])
        raise SyncError(f'GitHub 작업 실패: {operation}. 권한과 Actions 실행 로그를 확인하세요.')
    return result.stdout


def api(endpoint: str, *, method: str = 'GET', payload: Any = None,
        allow_not_found: bool = False) -> Any:
    args = ['api', '--method', method, endpoint, '-H', 'Accept: application/vnd.github+json',
            '-H', 'X-GitHub-Api-Version: 2026-03-10']
    if payload is not None:
        args += ['--input', '-']
    output = gh(*args, payload=payload, allow_not_found=allow_not_found)
    return json.loads(output) if output and output.strip() else None


def releases(repository: str) -> list[dict]:
    output = gh('api', f'repos/{repository}/releases?per_page=100', '--paginate', '--slurp')
    return [release for page in json.loads(output or '[]') for release in page]


def release_metadata(release: dict) -> dict | None:
    """Only this updater's own tags AND marker identify deletable releases."""
    tag = TAG_RE.fullmatch(release.get('tag_name', ''))
    body = release.get('body') or ''
    if not tag or MARKER not in body:
        return None
    try:
        encoded = body.split(MARKER, 1)[1].split(' -->', 1)[0]
        meta = json.loads(encoded)
        if (meta.get('database_type') != EDITION or type(meta.get('build_epoch')) is not int
                or not re.fullmatch(r'[0-9a-f]{64}', meta.get('sha256', ''))
                or not re.fullmatch(r'[0-9a-f]{64}', meta.get('csv_sha256', ''))
                or str(meta['build_epoch']) != tag[1] or meta.get('revision') != tag[2]):
            return None
        # Legacy Country-only releases remain recognizable for safe migration/cleanup.
        city = meta.get('city')
        if city is not None and (
                not isinstance(city, dict) or city.get('database_type') != CITY_EDITION
                or type(city.get('build_epoch')) is not int
                or not re.fullmatch(r'[0-9a-f]{64}', city.get('sha256', ''))
                or not re.fullmatch(r'[0-9a-f]{64}', city.get('csv_sha256', ''))):
            return None
        return meta
    except (ValueError, KeyError, TypeError):
        return None


def complete(release: dict, *, require_city: bool = True) -> bool:
    assets = {a['name'] for a in release.get('assets', [])
              if a.get('state') == 'uploaded' and a.get('size', 0) > 0}
    meta = release_metadata(release) or {}
    if not meta or (require_city and not meta.get('city')):
        return False
    expected = (REQUIRED_ASSETS if require_city else COUNTRY_REQUIRED_ASSETS) | set(meta.get('assets', {}))
    return not release.get('draft') and not release.get('prerelease') and expected <= assets


def cleanup_candidates(items: list[dict], current_tag: str, keep: int,
                       max_age_days: int, now: datetime) -> list[dict]:
    managed = [r for r in items if release_metadata(r) and not r.get('draft')
               and not r.get('prerelease')]
    current = next((r for r in managed if r['tag_name'] == current_tag and complete(r)), None)
    if current is None:
        raise SyncError('최신 Release의 첨부파일을 확인하지 못해 이전 Release를 삭제하지 않습니다.')
    managed.sort(key=lambda r: release_metadata(r)['build_epoch'], reverse=True)
    protected = {r['tag_name'] for r in managed[:keep]} | {current_tag}
    cutoff = int((now - timedelta(days=max_age_days)).timestamp())
    current_epoch = release_metadata(current)['build_epoch']
    return [r for r in managed if r['tag_name'] != current_tag
            and release_metadata(r)['build_epoch'] <= current_epoch
            and (r['tag_name'] not in protected
                 or min(release_metadata(r)['build_epoch'],
                        release_metadata(r).get('city', {}).get('build_epoch',
                                                                 release_metadata(r)['build_epoch'])) < cutoff)]


def curl_config(account: str, key: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in account + key):
        raise SyncError('MaxMind 인증정보에 잘못된 제어 문자가 포함되어 있습니다.')
    credential = (account + ':' + key).replace('\\', '\\\\').replace('"', '\\"')
    return f'user = "{credential}"\n'


def download(account: str, key: str, suffix: str, output: Path, *, head: bool = False,
             edition: str = EDITION) -> None:
    # Credentials are sent through stdin, never through the process argument list.
    # curl does not forward Basic Auth to a different redirect host.
    args = ['curl', '--config', '-', '--fail', '--silent', '--show-error', '--location',
            '--proto', '=https', '--proto-redir', '=https', '--connect-timeout', '30',
            '--max-time', '300', '--retry', '3', '--retry-delay', '3',
            '--max-filesize', str(MAX_DATABASE_BYTES), '--output', str(output)]
    if head:
        args += ['--head']
    args += [f'https://download.maxmind.com/geoip/databases/{edition}/download?suffix={suffix}']
    try:
        result = subprocess.run(args, input=curl_config(account, key), text=True,
                                capture_output=True, timeout=1200)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SyncError('MaxMind 다운로드 실행에 실패했습니다.') from exc
    if result.returncode:
        raise SyncError(f'MaxMind 요청 실패(curl 종료코드 {result.returncode}). '
                        '계정 ID, 라이선스 키, 다운로드 한도를 확인하세요.')


def last_modified(headers: str) -> str | None:
    # -L writes all response headers. Use only the final response's headers.
    blocks = re.split(r'\r?\n\r?\n', headers.strip())
    final = blocks[-1] if blocks else ''
    values = re.findall(r'^last-modified:\s*(.+)$', final, re.I | re.M)
    if not values:
        return None
    try:
        value = parsedate_to_datetime(values[-1].strip())
        if value.tzinfo is None:
            return None
        return value.astimezone(UTC).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def verify_archive(archive: Path, checksum: Path) -> None:
    if not 0 < archive.stat().st_size <= MAX_DATABASE_BYTES:
        raise SyncError('다운로드한 압축파일 크기가 올바르지 않습니다.')
    match = re.match(r'^([0-9a-fA-F]{64})(?:\s|$)', checksum.read_text().strip())
    digest = file_sha256(archive)
    if not match or digest != match[1].lower():
        raise SyncError('MaxMind 압축파일 SHA-256 검증에 실패했습니다. 게시하지 않습니다.')


def extract_files(archive: Path, target: Path, *, edition: str = EDITION) -> None:
    wanted = {edition + '.mmdb', 'COPYRIGHT.txt', 'LICENSE.txt'}
    found = set()
    with tarfile.open(archive, 'r:gz') as source:
        for member in source:
            path = PurePosixPath(member.name)
            name = path.name
            if name not in wanted:
                continue
            if (not member.isfile() or path.is_absolute() or '..' in path.parts
                    or name in found or not 0 < member.size <= MAX_DATABASE_BYTES):
                raise SyncError('압축파일의 MMDB 또는 라이선스 항목이 올바르지 않습니다.')
            # Copy only known regular files; never extract archive-supplied paths/links.
            stream = source.extractfile(member)
            if stream is None:
                raise SyncError('압축파일 항목을 읽을 수 없습니다.')
            with stream, (target / name).open('wb') as output:
                shutil.copyfileobj(stream, output)
            found.add(name)
    if found != wanted:
        raise SyncError('압축파일에 MMDB, COPYRIGHT.txt 또는 LICENSE.txt가 없습니다.')


def extract_csv(archive: Path, target: Path, *, edition: str = EDITION) -> list[str]:
    csv_re = re.compile(r'^' + re.escape(edition)
                        + r'-(?:Blocks-IPv[46]|Locations-[A-Za-z-]+)\.csv$')
    required_csv = {edition + '-Blocks-IPv4.csv', edition + '-Blocks-IPv6.csv',
                    edition + '-Locations-en.csv'}
    found = set()
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if len(entries) > 100 or sum(i.file_size for i in entries) > MAX_CSV_UNPACKED_BYTES:
            raise SyncError('CSV ZIP의 크기 또는 항목 수가 허용 범위를 벗어났습니다.')
        for member in entries:
            path = PurePosixPath(member.filename)
            name = path.name
            if not csv_re.fullmatch(name):
                continue
            if (member.is_dir() or path.is_absolute() or '..' in path.parts or name in found
                    or (member.external_attr >> 16) & 0o170000 == 0o120000
                    or not 0 < member.file_size <= MAX_DATABASE_BYTES):
                raise SyncError('CSV ZIP 항목이 올바르지 않습니다.')
            with source.open(member) as stream, (target / name).open('wb') as output:
                shutil.copyfileobj(stream, output)
            validate_csv(target / name, edition=edition)
            found.add(name)
    if not required_csv <= found:
        raise SyncError(edition + ' IPv4·IPv6 Blocks 또는 영어 Locations CSV가 없습니다.')
    return sorted(found)


def validate_csv(path: Path, *, edition: str = EDITION) -> None:
    blocks = '-Blocks-IPv' in path.name
    required = ({'network', 'geoname_id', 'registered_country_geoname_id'} if blocks else
                {'geoname_id', 'locale_code', 'country_iso_code', 'country_name'})
    if edition == CITY_EDITION:
        required |= ({'latitude', 'longitude', 'accuracy_radius'} if blocks else
                     {'city_name', 'time_zone'})
    with path.open(encoding='utf-8-sig', newline='') as source:
        reader = csv.DictReader(source)
        columns = reader.fieldnames or []
        if not required <= set(columns) or len(set(columns)) != len(columns):
            raise SyncError('CSV 필수 헤더가 없습니다: ' + path.name)
        count = 0
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise SyncError('CSV 행의 열 수가 올바르지 않습니다: ' + path.name)
            if blocks:
                try:
                    network = ipaddress.ip_network(row['network'])
                    expected_version = 4 if '-IPv4.' in path.name else 6
                    if network.version != expected_version:
                        raise ValueError('wrong IP version')
                except ValueError as exc:
                    raise SyncError('CSV 네트워크 값이 올바르지 않습니다: ' + path.name) from exc
            count += 1
        if count == 0:
            raise SyncError('CSV 데이터가 비어 있습니다: ' + path.name)


def validate_database(path: Path, now: datetime, max_age_days: int,
                      *, edition: str = EDITION) -> dict:
    import maxminddb

    with maxminddb.open_database(str(path)) as reader:
        metadata = reader.metadata()
        if metadata.database_type != edition or metadata.node_count <= 0:
            raise SyncError(edition + ' 형식의 MMDB가 아닙니다.')
        built = datetime.fromtimestamp(metadata.build_epoch, UTC)
        if built > now + timedelta(days=1) or built < now - timedelta(days=max_age_days):
            raise SyncError('MMDB 생성일이 허용 범위를 벗어났습니다. 게시하지 않습니다.')
        # Exercise both IP address families; do not assert a specific country's value.
        reader.get('1.1.1.1')
        if metadata.ip_version == 6:
            reader.get('2606:4700:4700::1111')
        return {'database_type': metadata.database_type, 'build_epoch': metadata.build_epoch,
                'ip_version': metadata.ip_version,
                'sha256': file_sha256(path)}


def publish(repository: str, target: str, folder: Path, meta: dict, items: list[dict]) -> dict:
    tag = f"geolite2-country-{meta['build_epoch']}-{meta['revision']}"
    existing = next((r for r in items if r['tag_name'] == tag), None)
    if existing:
        if release_metadata(existing) is None:
            raise SyncError('동일 태그의 수동 Release가 있어 덮어쓰지 않습니다.')
        if complete(existing):
            return existing
        if not existing.get('draft'):
            raise SyncError('기존 공개 Release가 불완전합니다. 기존 파일을 자동으로 덮어쓰지 않습니다.')
        # Retry only our own unfinished draft. Published releases remain untouched.
        gh('release', 'delete', tag, '--repo', repository, '--yes')

    built = datetime.fromtimestamp(meta['build_epoch'], UTC).isoformat()
    city = meta['city']
    city_built = datetime.fromtimestamp(city['build_epoch'], UTC).isoformat()
    (folder / (DATABASE + '.sha256')).write_text(f"{meta['sha256']}  {DATABASE}\n")
    (folder / (CSV_ARCHIVE + '.sha256')).write_text(f"{meta['csv_sha256']}  {CSV_ARCHIVE}\n")
    (folder / (CITY_DATABASE + '.sha256')).write_text(f"{city['sha256']}  {CITY_DATABASE}\n")
    (folder / (CITY_CSV_ARCHIVE + '.sha256')).write_text(f"{city['csv_sha256']}  {CITY_CSV_ARCHIVE}\n")
    asset_names = ((REQUIRED_ASSETS - {'SHA256SUMS', 'metadata.json'})
                   | set(meta.get('csv_files', [])) | set(city.get('csv_files', [])))
    meta['assets'] = {name: file_sha256(folder / name)
                      for name in sorted(asset_names)}
    (folder / 'metadata.json').write_text(json.dumps(meta, indent=2) + '\n')
    asset_names.add('metadata.json')
    hashes = [f"{file_sha256(folder / name)}  {name}\n"
              for name in sorted(asset_names)]
    (folder / 'SHA256SUMS').write_text(''.join(hashes))
    asset_names.add('SHA256SUMS')
    notes = (f"{MARKER}{json.dumps(meta, separators=(',', ':'))} -->\n\n"
             f"Free GeoLite2 Country + City MMDB and CSV\n\n"
             f"Country database build: {built}\n\n"
             f"City database build: {city_built}\n\n"
             'Assets include both original MMDBs, original CSV ZIPs, individual CSV files, '
             'metadata and SHA256SUMS.\n\n'
             'This product includes GeoLite Data created by MaxMind, available from '
             'https://www.maxmind.com/.\n\n'
             'Data: https://creativecommons.org/licenses/by-sa/4.0/ and '
             'https://www.maxmind.com/en/geolite/eula. '
             'Original database bytes are unmodified. Use current data and delete superseded copies.\n')
    notes_path = folder / 'release-notes.md'
    notes_path.write_text(notes, encoding='utf-8')
    files = [str(folder / name) for name in sorted(asset_names)]
    gh('release', 'create', tag, *files, '--repo', repository, '--target', target, '--draft',
       '--title', f'GeoLite2 Country + City {built[:10]}', '--notes-file', str(notes_path))
    # Publish only after every asset was uploaded successfully.
    gh('release', 'edit', tag, '--repo', repository, '--draft=false', '--latest')
    created = api(f'repos/{repository}/releases/tags/{tag}')
    if not complete(created):
        raise SyncError('게시된 Release의 첨부파일 확인에 실패했습니다. 이전 Release는 유지합니다.')
    for asset in created['assets']:
        if asset['name'] in asset_names:
            expected = file_sha256(folder / asset['name'])
            if asset['size'] != (folder / asset['name']).stat().st_size:
                raise SyncError('게시된 첨부파일 크기가 다릅니다. 이전 Release는 유지합니다.')
            if asset.get('digest') and asset['digest'] != 'sha256:' + expected:
                raise SyncError('게시된 첨부파일 해시가 다릅니다. 이전 Release는 유지합니다.')
    return created


def record_check(repository: str, target: str, release: dict, now: datetime) -> None:
    # A small status commit also keeps scheduled workflows active in public repos.
    endpoint = f'repos/{repository}/contents/{STATE_PATH}'
    previous = api(endpoint + '?ref=' + target, allow_not_found=True)
    value = {**release_metadata(release), 'release_tag': release['tag_name'],
             'checked_at': now.isoformat()}
    body = {'message': 'chore: record GeoLite2 Country + City update check', 'branch': target,
            'content': base64.b64encode((json.dumps(value, indent=2) + '\n').encode()).decode()}
    if previous:
        body['sha'] = previous['sha']
    api(endpoint, method='PUT', payload=body)


def sync(repository: str, target: str, account: str, key: str, keep: int,
         max_age_days: int, now: datetime) -> None:
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise SyncError('GITHUB_REPOSITORY 값이 올바르지 않습니다.')
    if not 1 <= keep <= 100 or not 1 <= max_age_days <= 30:
        raise SyncError('KEEP_RELEASES는 1~100, MAX_AGE_DAYS는 1~30이어야 합니다.')
    items = releases(repository)
    published = [r for r in items if release_metadata(r) and complete(r, require_city=False)]
    previous = max(published, key=lambda r: (
        release_metadata(r)['build_epoch'],
        release_metadata(r).get('city', {}).get('build_epoch', 0),
        r.get('published_at') or ''), default=None)
    with tempfile.TemporaryDirectory(prefix='geolite2-country-') as directory:
        folder = Path(directory)
        head = folder / 'headers.txt'
        download(account, key, 'tar.gz', head, head=True)
        modified = last_modified(head.read_text())
        csv_head = folder / 'csv-headers.txt'
        download(account, key, 'zip', csv_head, head=True, edition=CSV_EDITION)
        csv_modified = last_modified(csv_head.read_text())
        city_head = folder / 'city-headers.txt'
        download(account, key, 'tar.gz', city_head, head=True, edition=CITY_EDITION)
        city_modified = last_modified(city_head.read_text())
        city_csv_head = folder / 'city-csv-headers.txt'
        download(account, key, 'zip', city_csv_head, head=True, edition=CITY_CSV_EDITION)
        city_csv_modified = last_modified(city_csv_head.read_text())
        old = release_metadata(previous) if previous else None
        old_city = old.get('city') if old else None
        fresh = (old and old_city and min(old['build_epoch'], old_city['build_epoch'])
                 >= int((now - timedelta(days=max_age_days)).timestamp()))
        if (old and complete(previous) and modified and csv_modified
                and city_modified and city_csv_modified
                and modified == old.get('last_modified')
                and csv_modified == old.get('csv_last_modified')
                and city_modified == old_city.get('last_modified')
                and city_csv_modified == old_city.get('csv_last_modified') and fresh):
            current = previous
            summary('Country·City MMDB·CSV 새 버전이 없어 파일 다운로드와 Release 생성을 건너뛰었습니다.')
        else:
            archive, checksum = folder / 'country.tar.gz', folder / 'country.tar.gz.sha256'
            download(account, key, 'tar.gz', archive)
            download(account, key, 'tar.gz.sha256', checksum)
            verify_archive(archive, checksum)
            extract_files(archive, folder)
            csv_archive = folder / CSV_ARCHIVE
            csv_checksum = folder / 'csv-download.sha256'
            download(account, key, 'zip', csv_archive, edition=CSV_EDITION)
            download(account, key, 'zip.sha256', csv_checksum, edition=CSV_EDITION)
            verify_archive(csv_archive, csv_checksum)
            csv_files = extract_csv(csv_archive, folder)
            meta = validate_database(folder / DATABASE, now, max_age_days)
            meta['last_modified'] = modified
            meta['csv_last_modified'] = csv_modified
            meta['csv_files'] = csv_files
            meta['csv_sha256'] = file_sha256(csv_archive)

            # City is a free GeoLite2 edition. Keep its license files separate from Country.
            city_folder = folder / 'city'
            city_folder.mkdir()
            city_archive = folder / 'city.tar.gz'
            city_checksum = folder / 'city.tar.gz.sha256'
            download(account, key, 'tar.gz', city_archive, edition=CITY_EDITION)
            download(account, key, 'tar.gz.sha256', city_checksum, edition=CITY_EDITION)
            verify_archive(city_archive, city_checksum)
            extract_files(city_archive, city_folder, edition=CITY_EDITION)
            city_csv_archive = folder / CITY_CSV_ARCHIVE
            city_csv_checksum = folder / 'city-csv-download.sha256'
            download(account, key, 'zip', city_csv_archive, edition=CITY_CSV_EDITION)
            download(account, key, 'zip.sha256', city_csv_checksum, edition=CITY_CSV_EDITION)
            verify_archive(city_csv_archive, city_csv_checksum)
            city_csv_files = extract_csv(city_csv_archive, folder, edition=CITY_EDITION)
            city_meta = validate_database(city_folder / CITY_DATABASE, now, max_age_days,
                                          edition=CITY_EDITION)
            city_meta.update({'last_modified': city_modified,
                              'csv_last_modified': city_csv_modified,
                              'csv_files': city_csv_files,
                              'csv_sha256': file_sha256(city_csv_archive)})
            shutil.move(str(city_folder / CITY_DATABASE), str(folder / CITY_DATABASE))
            for name in ('COPYRIGHT.txt', 'LICENSE.txt'):
                shutil.move(str(city_folder / name), str(folder / (CITY_EDITION + '-' + name)))
            meta['city'] = city_meta
            # A City-only update must create a new release even if Country is unchanged.
            revision_parts = [meta['sha256'], meta['csv_sha256'],
                              city_meta['sha256'], city_meta['csv_sha256']]
            meta['revision'] = hashlib.sha256(':'.join(revision_parts).encode()).hexdigest()[:12]
            if old and meta['build_epoch'] < old['build_epoch']:
                raise SyncError('기존 배포본보다 오래된 MMDB가 내려와 게시하지 않습니다.')
            if old_city and city_meta['build_epoch'] < old_city['build_epoch']:
                raise SyncError('기존 배포본보다 오래된 City MMDB가 내려와 게시하지 않습니다.')
            current = publish(repository, target, folder, meta, items)
            summary('검증한 Country·City MMDB·CSV가 Release에 준비되었습니다: ' + current['tag_name'])

        # Re-fetch after publish. A failed download/upload never reaches deletion.
        candidates = cleanup_candidates(releases(repository), current['tag_name'], keep, max_age_days, now)
        for item in candidates:
            gh('release', 'delete', item['tag_name'], '--repo', repository, '--yes', '--cleanup-tag')
            summary('이전 Release와 태그를 삭제했습니다: ' + item['tag_name'])
        record_check(repository, target, current, now)
        summary(f"최신 다운로드: https://github.com/{repository}/releases/latest/download/{DATABASE}")
        summary(f"최신 CSV ZIP: https://github.com/{repository}/releases/latest/download/{CSV_ARCHIVE}")
        summary(f"최신 City MMDB: https://github.com/{repository}/releases/latest/download/{CITY_DATABASE}")
        summary(f"최신 City CSV ZIP: https://github.com/{repository}/releases/latest/download/{CITY_CSV_ARCHIVE}")


def main() -> int:
    required = ['GITHUB_REPOSITORY', 'MAXMIND_ACCOUNT_ID', 'MAXMIND_LICENSE_KEY', 'GH_TOKEN', 'RELEASE_TARGET']
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SyncError('필수 환경변수가 없습니다: ' + ', '.join(missing))
    sync(os.environ['GITHUB_REPOSITORY'], os.environ['RELEASE_TARGET'],
         os.environ['MAXMIND_ACCOUNT_ID'], os.environ['MAXMIND_LICENSE_KEY'],
         int(os.environ.get('KEEP_RELEASES', '3')), int(os.environ.get('MAX_AGE_DAYS', '30')), utc_now())
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        if isinstance(error, SyncError):
            print('::error::' + str(error), file=sys.stderr)
        else:
            # Avoid exposing request URLs, signed redirects, or credentials in tracebacks.
            print(f'::error::처리에 실패했습니다({type(error).__name__}). 기존 Release를 확인하세요.', file=sys.stderr)
        sys.exit(1)
