"""Where the Git export lives, and cloning to a volume that isn't mounted (#75).

Inside Databricks Apps, ``/Volumes`` isn't a filesystem, so cloning to
``/Volumes/<catalog>/<schema>/<volume>/git-export`` failed with
``[Errno 13] Permission denied: '/Volumes'``, and a bare volume name cloned
into a relative directory on the app's local disk instead of the volume.
"""
import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from src.common import git as git_module
from src.common.git import (
    GIT_VOLUME_SYNC_FAILED,
    GitCloneStatus,
    GitService,
    GitVolumeInvalidError,
    resolve_git_paths,
)


def _settings(
    tmp_path: Path, volume, remote: str = '', catalog='main', schema='gov', work_dir=True
):
    return SimpleNamespace(
        DATABRICKS_VOLUME=volume,
        DATABRICKS_CATALOG=catalog,
        DATABRICKS_SCHEMA=schema,
        GIT_WORK_DIR=str(tmp_path / 'work') if work_dir else None,
        GIT_REPO_URL=remote or None,
        GIT_BRANCH='main',
        GIT_USERNAME=None,
        GIT_PASSWORD=None,
    )


@pytest.fixture
def not_mounted():
    """No /Volumes mount on this machine, as in Databricks Apps."""
    with patch.object(git_module, '_volume_mounted', return_value=False):
        yield


@pytest.fixture
def remote(tmp_path: Path) -> str:
    """A bare repository with one commit on main, as file:// URL."""
    bare = tmp_path / 'remote.git'
    seed = tmp_path / 'seed'
    run = lambda *args, cwd=None: subprocess.run(args, cwd=cwd, check=True, capture_output=True)
    run('git', 'init', '--bare', '-b', 'main', str(bare))
    run('git', 'init', '-b', 'main', str(seed))
    (seed / 'README.md').write_text('# gov\n')
    run('git', 'add', 'README.md', cwd=seed)
    run('git', '-c', 'user.email=t@example.com', '-c', 'user.name=t',
        '-c', 'core.hooksPath=/dev/null', 'commit', '-m', 'init', cwd=seed)
    run('git', '-c', 'core.hooksPath=/dev/null', 'push', str(bare), 'main', cwd=seed)
    return f'file://{bare}'


@pytest.mark.parametrize('volume', [
    '/Volumes/main/gov/exports',
    '/Volumes/main/gov/exports/',
    'main.gov.exports',
    'exports',
])
def test_every_volume_form_gives_a_local_working_copy_and_the_volume_export_path(
    tmp_path, not_mounted, volume
):
    paths = resolve_git_paths(_settings(tmp_path, volume))

    assert paths.repo_dir == tmp_path / 'work' / 'main.gov.exports'
    assert paths.volume_export_path == '/Volumes/main/gov/exports/git-export'


def test_a_mounted_volume_holds_the_working_copy_itself(tmp_path):
    with patch.object(git_module, '_volume_mounted', return_value=True):
        paths = resolve_git_paths(_settings(tmp_path, '/Volumes/main/gov/exports'))

    assert paths.repo_dir == Path('/Volumes/main/gov/exports/git-export')
    assert paths.volume_export_path is None


def test_without_a_work_dir_the_working_copy_goes_under_the_temp_dir(tmp_path, not_mounted):
    with patch.object(git_module.tempfile, 'gettempdir', return_value=str(tmp_path / 'tmp')):
        paths = resolve_git_paths(_settings(tmp_path, 'exports', work_dir=False))

    assert paths.repo_dir == tmp_path / 'tmp' / 'ontos' / 'git-export' / 'main.gov.exports'


def test_a_local_directory_and_no_volume_stay_local(tmp_path):
    local = resolve_git_paths(_settings(tmp_path, str(tmp_path / 'dev')))
    assert local.repo_dir == tmp_path / 'dev' / 'git-export'
    assert resolve_git_paths(_settings(tmp_path, '')).repo_dir == Path('data/git-export')


@pytest.mark.parametrize('volume, catalog', [
    ('/Volumes/main/gov', 'main'),
    ('main.gov', 'main'),
    ('exports', None),
    ('my volume', 'main'),
])
def test_a_volume_setting_that_names_no_volume_is_rejected_with_a_code(tmp_path, volume, catalog):
    with pytest.raises(GitVolumeInvalidError) as raised:
        resolve_git_paths(_settings(tmp_path, volume, catalog=catalog))

    assert raised.value.code == 'git_volume_invalid'
    assert raised.value.params == {'value': volume}


def test_cloning_to_a_volume_that_is_not_mounted_works(tmp_path, not_mounted, remote):
    service = GitService(_settings(tmp_path, '/Volumes/main/gov/exports', remote))

    status = service.clone()

    assert status.clone_status == GitCloneStatus.CLONED, status.error_message
    assert (tmp_path / 'work' / 'main.gov.exports' / 'README.md').exists()
    assert status.volume_path == '/Volumes/main/gov/exports/git-export'
    assert status.working_copy_path == str(tmp_path / 'work' / 'main.gov.exports')


def test_an_existing_working_copy_is_found_again(tmp_path, not_mounted, remote):
    GitService(_settings(tmp_path, 'exports', remote)).clone()

    assert GitService(_settings(tmp_path, 'exports', remote)).is_cloned


def test_an_invalid_volume_setting_fails_the_clone_with_its_code(tmp_path, remote):
    service = GitService(_settings(tmp_path, 'main.gov', remote))

    status = service.clone()

    assert status.clone_status == GitCloneStatus.ERROR
    assert status.error_code == 'git_volume_invalid'
    assert status.error_params == {'value': 'main.gov'}
    assert service.get_status().to_dict()['error_code'] == 'git_volume_invalid'


def test_exported_files_are_written_to_and_removed_from_the_volume(tmp_path, not_mounted, remote):
    service = GitService(_settings(tmp_path, 'exports', remote))
    service.clone()
    client = MagicMock()

    with patch('src.common.workspace_client.get_workspace_client', return_value=client):
        assert service.save_yaml('orders.yaml', {'name': 'orders'}, subdir='data_products')
        assert service.delete_file('orders.yaml', subdir='data_products')

    path, contents = client.files.upload.call_args.args
    assert path == '/Volumes/main/gov/exports/git-export/data_products/orders.yaml'
    assert contents.read() == b'name: orders\n'
    assert client.files.upload.call_args.kwargs == {'overwrite': True}
    client.files.delete.assert_called_once_with(
        '/Volumes/main/gov/exports/git-export/data_products/orders.yaml'
    )
    assert service.get_status().volume_sync_error_code is None


def test_a_failed_volume_write_keeps_the_export_and_shows_a_warning(tmp_path, not_mounted, remote):
    service = GitService(_settings(tmp_path, 'exports', remote))
    service.clone()
    client = MagicMock()
    client.files.upload.side_effect = PermissionError('WRITE VOLUME missing')

    with patch('src.common.workspace_client.get_workspace_client', return_value=client):
        assert service.save_yaml('orders.yaml', {'name': 'orders'})

    assert (tmp_path / 'work' / 'main.gov.exports' / 'orders.yaml').exists()
    status = service.get_status().to_dict()
    assert status['volume_sync_error_code'] == GIT_VOLUME_SYNC_FAILED
    assert status['volume_sync_error_params'] == {
        'path': '/Volumes/main/gov/exports/git-export/orders.yaml'
    }


def test_a_mounted_or_local_working_copy_is_not_copied(tmp_path, remote):
    service = GitService(_settings(tmp_path, str(tmp_path / 'dev'), remote))
    service.clone()
    client = MagicMock()

    with patch('src.common.workspace_client.get_workspace_client', return_value=client):
        assert service.save_yaml('orders.yaml', {'name': 'orders'})

    client.files.upload.assert_not_called()


def test_the_clone_route_answers_an_invalid_volume_with_the_code(tmp_path, remote):
    from src.routes.settings_routes import clone_git_repository

    service = GitService(_settings(tmp_path, 'main.gov', remote))
    request = SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'))

    with patch('src.common.git.get_git_service', return_value=service):
        with pytest.raises(HTTPException) as raised:
            asyncio.run(clone_git_repository(
                request=request,
                background_tasks=MagicMock(),
                db=MagicMock(),
                audit_manager=MagicMock(),
                current_user=SimpleNamespace(username='admin'),
                manager=MagicMock(),
                _=True,
            ))

    assert raised.value.status_code == 400
    assert raised.value.detail['code'] == 'git_volume_invalid'
    assert raised.value.detail['params'] == {'value': 'main.gov'}
    assert 'main.gov' in raised.value.detail['message']

