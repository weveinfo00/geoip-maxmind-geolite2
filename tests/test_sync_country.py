import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import sync_country as sync

NOW = datetime(2026, 10, 30, 13, tzinfo=timezone.utc)


def release(age=0, *, draft=False, complete=True, digest='a' * 64):
    epoch = int((NOW - timedelta(days=age)).timestamp())
    csv_digest = 'b' * 64
    revision = hashlib.sha256((digest + ':' + csv_digest).encode()).hexdigest()[:12]
    meta = {'database_type': sync.EDITION, 'build_epoch': epoch, 'sha256': digest,
            'csv_sha256': csv_digest, 'revision': revision,
            'csv_last_modified': '2026-10-30T10:00:00+00:00',
            'last_modified': '2026-10-30T10:00:00+00:00'}
    return {'tag_name': f'geolite2-country-{epoch}-{revision}',
            'body': sync.MARKER + json.dumps(meta) + ' -->',
            'draft': draft, 'prerelease': False,
            'assets': [{'name': name, 'size': 1, 'state': 'uploaded'}
                       for name in sync.REQUIRED_ASSETS] if complete else []}


def write_archive(path, files):
    with tarfile.open(path, 'w:gz') as tar:
        for name, data in files:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))


class RetentionTests(unittest.TestCase):
    def test_only_latest_three_are_retained(self):
        items = [release(age) for age in (0, 3, 7, 10, 14)]
        actual = sync.cleanup_candidates(items, items[0]['tag_name'], 3, 30, NOW)
        self.assertEqual([i['tag_name'] for i in actual], [i['tag_name'] for i in items[3:]])

    def test_latest_one_can_be_selected(self):
        items = [release(0), release(3)]
        self.assertEqual(sync.cleanup_candidates(items, items[0]['tag_name'], 1, 30, NOW), items[1:])

    def test_expired_copy_is_removed_even_inside_keep_count(self):
        items = [release(0), release(31)]
        self.assertEqual(sync.cleanup_candidates(items, items[0]['tag_name'], 3, 30, NOW), items[1:])

    def test_manual_release_tag_and_marker_are_both_required(self):
        current = release()
        manual_tag, manual_body = release(10), release(14)
        manual_tag['tag_name'] = 'my-important-release'
        manual_body['body'] = 'manual'
        items = [current, manual_tag, manual_body]
        self.assertEqual(sync.cleanup_candidates(items, current['tag_name'], 1, 30, NOW), [])

    def test_current_release_must_be_complete_before_deletion(self):
        items = [release(0, complete=False), release(3)]
        with self.assertRaises(sync.SyncError):
            sync.cleanup_candidates(items, items[0]['tag_name'], 1, 30, NOW)

    def test_current_and_newer_releases_are_never_deleted(self):
        items = [release(-1), release(0), release(5)]
        actual = sync.cleanup_candidates(items, items[1]['tag_name'], 1, 30, NOW)
        self.assertEqual(actual, [items[2]])

    def test_drafts_and_prereleases_are_not_retention_targets(self):
        current, draft, pre = release(0), release(3, draft=True), release(5)
        pre['prerelease'] = True
        self.assertEqual(sync.cleanup_candidates([current, draft, pre], current['tag_name'], 1, 30, NOW), [])

    def test_malformed_metadata_is_not_managed(self):
        item = release()
        item['body'] = sync.MARKER + '{"sha256": 42} -->'
        self.assertIsNone(sync.release_metadata(item))


class DownloadTests(unittest.TestCase):
    def test_only_final_redirect_headers_are_used(self):
        headers = ('HTTP/1.1 302 Found\r\nLast-Modified: Mon, 26 Oct 2026 10:00:00 GMT\r\n\r\n'
                   'HTTP/2 200\r\nLast-Modified: Fri, 30 Oct 2026 10:00:00 GMT\r\n\r\n')
        self.assertEqual(sync.last_modified(headers), '2026-10-30T10:00:00+00:00')

    def test_missing_or_bad_last_modified_falls_back_to_full_download(self):
        self.assertIsNone(sync.last_modified('HTTP/2 200\nContent-Type: x\n\n'))
        self.assertIsNone(sync.last_modified('HTTP/2 200\nLast-Modified: nonsense\n\n'))

    def test_secret_is_passed_via_stdin_not_command_line(self):
        with patch.object(sync.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            sync.download('12345', 'test-secret', 'tar.gz', Path('/tmp/download'))
        args, options = run.call_args
        self.assertNotIn('test-secret', ' '.join(args[0]))
        self.assertIn('test-secret', options['input'])
        self.assertNotIn('--location-trusted', args[0])

    def test_checksum_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, checksum = Path(directory) / 'file', Path(directory) / 'hash'
            archive.write_bytes(b'test')
            checksum.write_text('0' * 64)
            with self.assertRaises(sync.SyncError):
                sync.verify_archive(archive, checksum)
            checksum.write_text(hashlib.sha256(b'test').hexdigest() + '  country.tar.gz\n')
            sync.verify_archive(archive, checksum)

    def test_only_known_files_are_copied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'source.tar.gz'
            write_archive(archive, [('data/' + sync.DATABASE, b'db'),
                                    ('data/LICENSE.txt', b'license'),
                                    ('data/COPYRIGHT.txt', b'copyright'),
                                    ('../../ignored.txt', b'ignored')])
            sync.extract_files(archive, root)
            self.assertEqual((root / sync.DATABASE).read_bytes(), b'db')
            self.assertFalse((root / 'ignored.txt').exists())

    def test_traversal_path_for_wanted_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'source.tar.gz'
            write_archive(archive, [('../../' + sync.DATABASE, b'bad')])
            with self.assertRaises(sync.SyncError):
                sync.extract_files(archive, Path(directory))

    def test_duplicate_mmdb_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'source.tar.gz'
            write_archive(archive, [('one/' + sync.DATABASE, b'1'), ('two/' + sync.DATABASE, b'2')])
            with self.assertRaises(sync.SyncError):
                sync.extract_files(archive, Path(directory))

    def test_invalid_mmdb_is_rejected_by_real_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / sync.DATABASE
            path.write_bytes(b'this is not an mmdb')
            from maxminddb import InvalidDatabaseError
            with self.assertRaises(InvalidDatabaseError):
                sync.validate_database(path, NOW, 30)

    def test_valid_csv_zip_is_unpacked_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'source.zip'
            with zipfile.ZipFile(archive, 'w') as target:
                target.writestr('data/GeoLite2-Country-Blocks-IPv4.csv',
                                'network,geoname_id,registered_country_geoname_id\n1.1.1.0/24,1,1\n')
                target.writestr('data/GeoLite2-Country-Blocks-IPv6.csv',
                                'network,geoname_id,registered_country_geoname_id\n2606:4700::/32,1,1\n')
                target.writestr('data/GeoLite2-Country-Locations-en.csv',
                                'geoname_id,locale_code,country_iso_code,country_name\n1,en,KR,Korea\n')
            files = sync.extract_csv(archive, root)
            self.assertEqual(set(files), sync.REQUIRED_CSV)

    def test_csv_header_and_address_family_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'GeoLite2-Country-Blocks-IPv4.csv'
            for contents in ('wrong_header\nvalue\n',
                             'network,geoname_id,registered_country_geoname_id\n::/0,1,1\n',
                             'network,geoname_id,registered_country_geoname_id\n'):
                path.write_text(contents)
                with self.assertRaises(sync.SyncError):
                    sync.validate_csv(path)

    def test_missing_required_csv_prevents_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'source.zip'
            with zipfile.ZipFile(archive, 'w') as target:
                target.writestr('README.txt', 'no csv')
            with self.assertRaises(sync.SyncError):
                sync.extract_csv(archive, Path(directory))


class FlowTests(unittest.TestCase):
    def test_failed_download_never_deletes_any_release(self):
        with patch.object(sync, 'releases', return_value=[release(0), release(7)]), \
                patch.object(sync, 'download', side_effect=sync.SyncError('download failed')), \
                patch.object(sync, 'gh') as gh, patch.object(sync, 'record_check') as record:
            with self.assertRaises(sync.SyncError):
                sync.sync('owner/repo', 'main', 'account', 'key', 1, 30, NOW)
        gh.assert_not_called()
        record.assert_not_called()

    def test_unchanged_database_skips_file_download_but_still_cleans_old_releases(self):
        current, old = release(), release(7)

        def head_only(account, key, suffix, output, *, head=False, edition=sync.EDITION):
            self.assertTrue(head, 'an unchanged database should not be downloaded')
            output.write_text('HTTP/2 200\nLast-Modified: Fri, 30 Oct 2026 10:00:00 GMT\n\n')

        with patch.object(sync, 'releases', return_value=[current, old]), \
                patch.object(sync, 'download', side_effect=head_only) as download, \
                patch.object(sync, 'gh') as gh, patch.object(sync, 'record_check') as record:
            sync.sync('owner/repo', 'main', 'account', 'key', 1, 30, NOW)
        self.assertEqual(download.call_count, 2)
        self.assertEqual(gh.call_args.args[:3], ('release', 'delete', old['tag_name']))
        record.assert_called_once()

    def test_upload_failure_never_deletes_previous_release(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            meta = sync.release_metadata(release())
            for name in sync.REQUIRED_ASSETS:
                (folder / name).write_bytes(b'test')
            with patch.object(sync, 'gh', side_effect=sync.SyncError('upload failed')) as gh:
                with self.assertRaises(sync.SyncError):
                    sync.publish('owner/repo', 'main', folder, meta, [release(7)])
            self.assertEqual(gh.call_count, 1)
            self.assertEqual(gh.call_args.args[:2], ('release', 'create'))
            self.assertIn('--draft', gh.call_args.args)

    def test_success_publishes_mmdb_and_csv_before_returning_release(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in sync.REQUIRED_ASSETS:
                (folder / name).write_bytes(b'test')
            meta = sync.release_metadata(release())

            def published(endpoint):
                item = release()
                item['body'] = sync.MARKER + json.dumps(meta) + ' -->'
                item['assets'] = [
                    {'name': name, 'state': 'uploaded', 'size': (folder / name).stat().st_size,
                     'digest': 'sha256:' + hashlib.sha256((folder / name).read_bytes()).hexdigest()}
                    for name in sync.REQUIRED_ASSETS]
                return item

            with patch.object(sync, 'gh') as gh, patch.object(sync, 'api', side_effect=published):
                result = sync.publish('owner/repo', 'main', folder, meta, [])
            self.assertTrue(sync.complete(result))
            self.assertEqual(gh.call_args_list[0].args[:2], ('release', 'create'))
            self.assertEqual(gh.call_args_list[1].args[:2], ('release', 'edit'))
            self.assertIn(str(folder / sync.CSV_ARCHIVE), gh.call_args_list[0].args)
            self.assertIn(str(folder / sync.DATABASE), gh.call_args_list[0].args)
            manifest = (folder / 'SHA256SUMS').read_text()
            self.assertIn(sync.CSV_ARCHIVE, manifest)
            self.assertIn(sync.DATABASE, manifest)

    def test_published_digest_mismatch_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in sync.REQUIRED_ASSETS:
                (folder / name).write_bytes(b'test')
            created = release()
            created['assets'][0]['size'] = 4
            created['assets'][0]['digest'] = 'sha256:' + '0' * 64
            with patch.object(sync, 'gh') as gh, patch.object(sync, 'api', return_value=created):
                with self.assertRaises(sync.SyncError):
                    sync.publish('owner/repo', 'main', folder, sync.release_metadata(release()), [])
            self.assertFalse(any(call.args[:2] == ('release', 'delete') for call in gh.call_args_list))

    def test_invalid_retention_configuration_stops_before_network(self):
        with patch.object(sync, 'releases') as items:
            for keep, days in [(0, 30), (101, 30), (3, 31)]:
                with self.assertRaises(sync.SyncError):
                    sync.sync('owner/repo', 'main', 'account', 'key', keep, days, NOW)
        items.assert_not_called()


if __name__ == '__main__':
    unittest.main()
