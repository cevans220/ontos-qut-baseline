"""Estates YAML import on startup (#1128)."""
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from src.controller.estate_manager import EstateManager
from src.models.estate import Estate, SharingPolicy, SharingRule, SharingResourceType
from src.repositories.app_settings_repository import app_settings_repo
from src.utils.startup_tasks import import_estates_from_yaml_once, _load_estates_yaml, _count_estates


@pytest.fixture
def clean_app_settings(db_session):
    """Clean up estates_yaml_import_done flag before each test."""
    app_settings_repo.delete(db_session, 'estates_yaml_import_done')
    yield
    app_settings_repo.delete(db_session, 'estates_yaml_import_done')


def test_import_marks_as_done(db_session, clean_app_settings):
    """Import checks the flag and marks as done to prevent re-import."""
    # Initially, no flag is set
    assert app_settings_repo.get_by_key(db_session, 'estates_yaml_import_done') is None

    # Import (no YAML, so nothing imported)
    result = import_estates_from_yaml_once(db_session)

    # Flag is now set
    assert app_settings_repo.get_by_key(db_session, 'estates_yaml_import_done') == 'true'


def test_import_skips_if_already_done(db_session, clean_app_settings):
    """If import flag is set, skip import."""
    # Mark as done
    app_settings_repo.set(db_session, 'estates_yaml_import_done', 'true')

    # Call import
    result = import_estates_from_yaml_once(db_session)

    # Should return False (no import)
    assert result is False


def test_import_skips_if_estates_not_empty(db_session, clean_app_settings):
    """If estates table is not empty, skip import."""
    # Create an estate manually
    manager = EstateManager()
    manager.create_estate(db_session, Estate(
        name="Existing",
        description="Already exists",
        workspace_url="https://example.com",
        cloud_type="aws",
        metastore_name="existing",
    ))

    # Call import (with mocked YAML to ensure it doesn't get called)
    with patch('src.utils.startup_tasks._load_estates_yaml') as mock_load:
        result = import_estates_from_yaml_once(db_session)

    # Should return False and not call YAML loader
    assert result is False
    mock_load.assert_not_called()


def test_count_estates_works(db_session):
    """_count_estates correctly counts estates."""
    assert _count_estates(db_session) == 0

    manager = EstateManager()
    manager.create_estate(db_session, Estate(
        name="First",
        description="First estate",
        workspace_url="https://first.com",
        cloud_type="aws",
        metastore_name="first",
    ))

    assert _count_estates(db_session) == 1


def test_load_estates_yaml_nonexistent():
    """_load_estates_yaml returns None if file doesn't exist."""
    with patch('src.utils.startup_tasks.Path') as mock_path_class:
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        mock_path_class.return_value = mock_path

        result = _load_estates_yaml()

        assert result is None


def test_load_estates_yaml_empty():
    """_load_estates_yaml returns None if file is empty."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write('')
        f.flush()
        temp_path = f.name

    try:
        with patch('src.utils.startup_tasks.Path') as mock_path_class:
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path.open.return_value.__enter__.return_value = open(temp_path, 'r')
            mock_path_class.return_value = mock_path

            result = _load_estates_yaml()

            assert result is None
    finally:
        Path(temp_path).unlink()


def test_load_estates_yaml_not_a_list():
    """_load_estates_yaml returns None if YAML is not a list at top level."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        yaml.dump({'key': 'value'}, f)
        f.flush()
        temp_path = f.name

    try:
        with patch('src.utils.startup_tasks.Path') as mock_path_class:
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path.open.return_value.__enter__.return_value = open(temp_path, 'r')
            mock_path_class.return_value = mock_path

            result = _load_estates_yaml()

            assert result is None
    finally:
        Path(temp_path).unlink()


def test_load_estates_yaml_malformed():
    """_load_estates_yaml handles malformed YAML gracefully."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write('{ invalid: yaml: content: [')
        f.flush()
        temp_path = f.name

    try:
        with patch('src.utils.startup_tasks.Path') as mock_path_class:
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path.open.return_value.__enter__.return_value = open(temp_path, 'r')
            mock_path_class.return_value = mock_path

            result = _load_estates_yaml()

            assert result is None
    finally:
        Path(temp_path).unlink()


def test_import_with_single_estate(db_session, clean_app_settings):
    """Import a single estate from YAML."""
    yaml_data = [
        {
            'name': 'US Production',
            'description': 'Primary production workspace',
            'workspace_url': 'https://us-west.cloud.databricks.com',
            'cloud_type': 'aws',
            'metastore_name': 'us_prod_metastore',
            'connection_type': 'delta_share',
            'is_enabled': True,
            'sync_schedule': '0 0 * * *',
            'export_policies': [
                {
                    'name': 'Share Active US Data Products',
                    'description': 'Shares all active data products',
                    'resource_type': 'data_product',
                    'is_enabled': True,
                    'rules': [
                        {
                            'filter_type': 'status',
                            'operator': 'equals',
                            'filter_value': 'Active',
                        }
                    ],
                }
            ],
        }
    ]

    with patch('src.utils.startup_tasks._load_estates_yaml', return_value=yaml_data):
        result = import_estates_from_yaml_once(db_session)

    # Import should succeed
    assert result is True

    # Estate should be in database
    manager = EstateManager()
    estates = manager.list_estates(db_session)
    assert len(estates) == 1
    assert estates[0].name == 'US Production'
    assert estates[0].description == 'Primary production workspace'
    assert len(estates[0].export_policies) == 1
    assert estates[0].export_policies[0].name == 'Share Active US Data Products'
    assert len(estates[0].export_policies[0].rules) == 1

    # Flag should be set
    assert app_settings_repo.get_by_key(db_session, 'estates_yaml_import_done') == 'true'


def test_import_with_legacy_sharing_policies_and_business_glossary(db_session, clean_app_settings):
    """Import handles legacy sharing_policies and business_glossary resource type."""
    yaml_data = [
        {
            'name': 'Legacy Estate',
            'description': 'Legacy format',
            'workspace_url': 'https://legacy.com',
            'cloud_type': 'azure',
            'metastore_name': 'legacy',
            'sharing_policies': [  # Old format, should become export_policies
                {
                    'name': 'Finance Terms',
                    'resource_type': 'business_glossary',  # Should convert to business_term
                    'is_enabled': True,
                    'rules': [],
                }
            ],
        }
    ]

    with patch('src.utils.startup_tasks._load_estates_yaml', return_value=yaml_data):
        result = import_estates_from_yaml_once(db_session)

    assert result is True

    manager = EstateManager()
    estates = manager.list_estates(db_session)
    assert len(estates) == 1
    assert estates[0].name == 'Legacy Estate'
    assert len(estates[0].export_policies) == 1
    assert estates[0].export_policies[0].name == 'Finance Terms'
    assert estates[0].export_policies[0].resource_type.value == 'business_term'


def test_import_with_import_and_export_policies(db_session, clean_app_settings):
    """Import handles both export and import policies."""
    yaml_data = [
        {
            'name': 'Bidirectional Estate',
            'description': 'Shares and receives',
            'workspace_url': 'https://bidirectional.com',
            'cloud_type': 'gcp',
            'metastore_name': 'bidirectional',
            'export_policies': [
                {
                    'name': 'Export Products',
                    'resource_type': 'data_product',
                    'is_enabled': True,
                    'rules': [],
                }
            ],
            'import_policies': [
                {
                    'name': 'Import Domains',
                    'resource_type': 'data_domain',
                    'is_enabled': True,
                    'rules': [],
                },
                {
                    'name': 'Import Concepts',
                    'resource_type': 'concept',
                    'is_enabled': True,
                    'rules': [],
                },
            ],
        }
    ]

    with patch('src.utils.startup_tasks._load_estates_yaml', return_value=yaml_data):
        result = import_estates_from_yaml_once(db_session)

    assert result is True

    manager = EstateManager()
    estates = manager.list_estates(db_session)
    assert len(estates) == 1
    assert len(estates[0].export_policies) == 1
    assert len(estates[0].import_policies) == 2


def test_import_second_startup_no_duplicates(db_session, clean_app_settings):
    """Second startup doesn't re-import estates (no duplicates)."""
    yaml_data = [
        {
            'name': 'Test Estate',
            'description': 'Test',
            'workspace_url': 'https://test.com',
            'cloud_type': 'aws',
            'metastore_name': 'test',
        }
    ]

    # First startup
    with patch('src.utils.startup_tasks._load_estates_yaml', return_value=yaml_data):
        result1 = import_estates_from_yaml_once(db_session)

    assert result1 is True

    manager = EstateManager()
    estates = manager.list_estates(db_session)
    assert len(estates) == 1

    # Second startup (with a fresh session but same flag)
    # The flag should prevent re-import
    with patch('src.utils.startup_tasks._load_estates_yaml') as mock_load:
        result2 = import_estates_from_yaml_once(db_session)

    assert result2 is False
    mock_load.assert_not_called()


def test_import_no_yaml_no_error(db_session, clean_app_settings):
    """Import handles missing YAML gracefully (not an error)."""
    with patch('src.utils.startup_tasks._load_estates_yaml', return_value=None):
        result = import_estates_from_yaml_once(db_session)

    assert result is False

    # Flag should be set to avoid retry
    assert app_settings_repo.get_by_key(db_session, 'estates_yaml_import_done') == 'true'


def test_import_partial_failure_marks_as_done(db_session, clean_app_settings):
    """If some estates fail to import, still mark as done."""
    yaml_data = [
        {
            'name': 'Valid Estate',
            'description': 'Valid',
            'workspace_url': 'https://valid.com',
            'cloud_type': 'aws',
            'metastore_name': 'valid',
        },
        {
            'name': 'Invalid Estate',
            # Missing required fields
            'workspace_url': 'https://invalid.com',
        },
    ]

    with patch('src.utils.startup_tasks._load_estates_yaml', return_value=yaml_data):
        result = import_estates_from_yaml_once(db_session)

    # Should import the valid one and fail on the invalid one
    manager = EstateManager()
    estates = manager.list_estates(db_session)
    assert len(estates) == 1

    # Flag should still be set
    assert app_settings_repo.get_by_key(db_session, 'estates_yaml_import_done') == 'true'
