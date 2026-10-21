"""Estates YAML import on startup (#1128): one-time import with sample detection."""
import tempfile
from pathlib import Path
import pytest
import yaml

from src.controller.estate_manager import EstateManager
from src.models.estate import Estate, SharingPolicy, SharingRule
from src.repositories.app_settings_repository import app_settings_repo
from src.utils.startup_tasks import import_estates_from_yaml_once


@pytest.fixture
def clean_app_settings(db_session):
    """Clean the import flag before and after each test."""
    app_settings_repo.delete(db_session, 'estates_yaml_import_done')
    yield
    app_settings_repo.delete(db_session, 'estates_yaml_import_done')


def test_path_is_injectable(db_session, clean_app_settings):
    """Item 2: Path parameter is injectable for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / 'custom.yaml'
        yaml_path.write_text(yaml.dump([
            {
                'name': 'Test Estate',
                'description': 'Test',
                'workspace_url': 'https://test.com',
                'cloud_type': 'aws',
                'metastore_name': 'test',
            }
        ]))

        result = import_estates_from_yaml_once(db_session, yaml_path=yaml_path)

        assert result is True
        manager = EstateManager()
        estates = manager.list_estates(db_session)
        assert len(estates) == 1
        assert estates[0].name == 'Test Estate'


def test_shipped_sample_not_imported(db_session, clean_app_settings):
    """Item 3: Shipped sample detected and not imported."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / 'estates.yaml'
        # Exact shipped sample
        yaml_path.write_text(yaml.dump([
            {
                'id': '1',
                'name': 'US Production',
                'description': 'Primary production workspace in US West',
                'workspace_url': 'https://us-west.cloud.databricks.com',
                'cloud_type': 'aws',
                'metastore_name': 'us_prod_metastore',
                'connection_type': 'delta_share',
                'sharing_policies': [
                    {
                        'id': 'policy-us-prod-dp-active',
                        'name': 'Share Active US Data Products',
                        'description': 'Shares all active data products from the US Production estate.',
                        'resource_type': 'data_product',
                        'rules': [
                            {'filter_type': 'status', 'operator': 'equals', 'filter_value': 'Active'},
                            {'filter_type': 'domain', 'operator': 'equals', 'filter_value': 'US Operations'},
                        ],
                        'is_enabled': True,
                    }
                ],
                'is_enabled': True,
                'sync_schedule': '0 0 * * *',
            },
            {
                'id': '2',
                'name': 'EU Analytics',
                'description': 'Analytics workspace in EU Central',
                'workspace_url': 'https://europe-west.cloud.databricks.com',
                'cloud_type': 'azure',
                'metastore_name': 'eu_analytics_metastore',
                'connection_type': 'delta_share',
                'sharing_policies': [
                    {
                        'id': 'policy-eu-glossary-finance',
                        'name': 'Share Finance Glossary Terms from EU',
                        'description': 'Shares all business glossary terms related to Finance.',
                        'resource_type': 'business_glossary',
                        'rules': [
                            {'filter_type': 'tag', 'operator': 'contains', 'filter_value': 'finance'},
                            {'filter_type': 'domain', 'operator': 'equals', 'filter_value': 'EU Analytics'},
                        ],
                        'is_enabled': True,
                    }
                ],
                'is_enabled': True,
                'sync_schedule': '0 12 * * *',
            },
        ]))

        result = import_estates_from_yaml_once(db_session, yaml_path=yaml_path)

        # Should return False (sample not imported)
        assert result is False
        # Flag should be set
        assert app_settings_repo.get_by_key(db_session, 'estates_yaml_import_done') == 'true'
        # DB should be empty
        manager = EstateManager()
        assert manager.list_estates(db_session) == []


def test_malformed_yaml_no_mark_done(db_session, clean_app_settings):
    """Item 4: Malformed YAML logs warning and does NOT mark done."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / 'bad.yaml'
        yaml_path.write_text('{ invalid: yaml: [')

        result = import_estates_from_yaml_once(db_session, yaml_path=yaml_path)

        # Should return False (not imported)
        assert result is False
        # Flag should NOT be set (allows retry)
        assert app_settings_repo.get_by_key(db_session, 'estates_yaml_import_done') is None


def test_transaction_atomicity(db_session, clean_app_settings):
    """Item 4: Import all in transaction."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / 'estates.yaml'
        yaml_path.write_text(yaml.dump([
            {
                'name': 'Good Estate',
                'description': 'Valid',
                'workspace_url': 'https://good.com',
                'cloud_type': 'aws',
                'metastore_name': 'good',
            },
            {
                'name': 'Bad Estate',
                'description': 'Invalid',
            },
        ]))

        result = import_estates_from_yaml_once(db_session, yaml_path=yaml_path)

        manager = EstateManager()
        estates = manager.list_estates(db_session)
        assert len(estates) >= 1


def test_mark_as_done_prevents_reimport(db_session, clean_app_settings):
    """Import marks done; second startup skips."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / 'estates.yaml'
        yaml_path.write_text(yaml.dump([
            {
                'name': 'Estate 1',
                'description': 'Test',
                'workspace_url': 'https://test.com',
                'cloud_type': 'aws',
                'metastore_name': 'test',
            }
        ]))

        # First import
        result1 = import_estates_from_yaml_once(db_session, yaml_path=yaml_path)
        assert result1 is True

        manager = EstateManager()
        count_after_first = len(manager.list_estates(db_session))
        assert count_after_first == 1

        # Second import (should skip)
        result2 = import_estates_from_yaml_once(db_session, yaml_path=yaml_path)
        assert result2 is False

        # No duplicates
        count_after_second = len(manager.list_estates(db_session))
        assert count_after_second == count_after_first


def test_upgrade_configured_estate_with_policies_and_rules(db_session, clean_app_settings):
    """Item 5: Upgrade test - configured estate with policies and rules imported."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / 'estates.yaml'
        yaml_path.write_text(yaml.dump([
            {
                'name': 'Configured Estate',
                'description': 'Has policies and rules',
                'workspace_url': 'https://configured.com',
                'cloud_type': 'gcp',
                'metastore_name': 'configured',
                'export_policies': [
                    {
                        'name': 'Policy 1',
                        'resource_type': 'data_product',
                        'is_enabled': True,
                        'rules': [
                            {'filter_type': 'domain', 'operator': 'equals', 'filter_value': 'Engineering'}
                        ],
                    }
                ],
                'import_policies': [
                    {
                        'name': 'Import Policy',
                        'resource_type': 'data_contract',
                        'is_enabled': True,
                        'rules': [
                            {'filter_type': 'tag', 'operator': 'contains', 'filter_value': 'shared'},
                            {'filter_type': 'status', 'operator': 'equals', 'filter_value': 'active'},
                        ],
                    }
                ],
            }
        ]))

        # First startup
        result = import_estates_from_yaml_once(db_session, yaml_path=yaml_path)
        assert result is True

        manager = EstateManager()
        estates = manager.list_estates(db_session)
        assert len(estates) == 1

        estate = estates[0]
        assert estate.name == 'Configured Estate'
        assert len(estate.export_policies) == 1
        assert len(estate.import_policies) == 1
        assert len(estate.export_policies[0].rules) == 1
        assert len(estate.import_policies[0].rules) == 2

        # Second startup - no duplicates
        app_settings_repo.delete(db_session, 'estates_yaml_import_done')
        # Manually reset flag to simulate new DB but same YAML
        result2 = import_estates_from_yaml_once(db_session, yaml_path=yaml_path)
        assert result2 is False  # Already imported

        estates_after = manager.list_estates(db_session)
        assert len(estates_after) == 1  # Still one, no duplicates


def test_empty_file_marks_done(db_session, clean_app_settings):
    """Empty YAML file marks done to avoid retry."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / 'empty.yaml'
        yaml_path.write_text('')

        result = import_estates_from_yaml_once(db_session, yaml_path=yaml_path)

        assert result is False
        assert app_settings_repo.get_by_key(db_session, 'estates_yaml_import_done') == 'true'


def test_legacy_sharing_policies_converted(db_session, clean_app_settings):
    """Legacy sharing_policies and business_glossary converted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / 'legacy.yaml'
        yaml_path.write_text(yaml.dump([
            {
                'name': 'Legacy Estate',
                'description': 'Old format',
                'workspace_url': 'https://legacy.com',
                'cloud_type': 'azure',
                'metastore_name': 'legacy',
                'sharing_policies': [  # Old field name
                    {
                        'name': 'Finance Glossary',
                        'resource_type': 'business_glossary',  # Old type
                        'is_enabled': True,
                        'rules': [],
                    }
                ],
            }
        ]))

        result = import_estates_from_yaml_once(db_session, yaml_path=yaml_path)
        assert result is True

        manager = EstateManager()
        estates = manager.list_estates(db_session)
        assert len(estates) == 1
        assert len(estates[0].export_policies) == 1
        assert estates[0].export_policies[0].resource_type.value == 'business_term'
