"""The ``hybrid`` block of search_config.yaml (#1061): load, save, partial update."""
import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from src.common.search_config_loader import DEFAULT_CONFIG_PATH, SearchConfigLoader
from src.models.search_config import SearchMode


@pytest.fixture()
def loader(tmp_path):
    path = tmp_path / "search_config.yaml"
    shutil.copy(DEFAULT_CONFIG_PATH, path)
    return SearchConfigLoader(config_path=path)


def test_shipped_defaults(loader):
    h = loader.load(force_reload=True).hybrid
    assert h.mode == SearchMode.AUTO and h.rrf_k == 20 and h.candidate_limit == 50
    assert h.min_vector_similarity == pytest.approx(0.55)


def test_a_file_without_the_block_gets_defaults(loader):
    data = yaml.safe_load(Path(loader.config_path).read_text())
    data.pop("hybrid")
    Path(loader.config_path).write_text(yaml.safe_dump(data))
    assert loader.load(force_reload=True).hybrid.mode == SearchMode.AUTO


def test_partial_update_round_trips(loader):
    loader.update({"hybrid": {"mode": "lexical", "vector_weight": 0.3}})
    h = SearchConfigLoader(config_path=loader.config_path).load(force_reload=True).hybrid
    assert h.mode == SearchMode.LEXICAL and h.vector_weight == pytest.approx(0.3)
    assert h.rrf_k == 20  # untouched keys keep their values


def test_out_of_range_values_are_rejected_not_saved(loader):
    with pytest.raises(ValidationError):
        loader.update({"hybrid": {"rrf_k": 0}})
    assert SearchConfigLoader(config_path=loader.config_path).load(force_reload=True).hybrid.rrf_k == 20


def test_api_partial_update_sends_only_the_fields_set(loader):
    """PUT {"hybrid": {"rrf_k": 30}} must not reset the other hybrid settings."""
    from src.models.search_config import SearchConfigUpdate
    loader.update({"hybrid": {"min_vector_similarity": 0.62}})
    body = SearchConfigUpdate.model_validate({"hybrid": {"rrf_k": 30}})
    loader.update({"hybrid": body.hybrid.model_dump(mode="json", exclude_unset=True)})
    h = SearchConfigLoader(config_path=loader.config_path).load(force_reload=True).hybrid
    assert h.rrf_k == 30 and h.min_vector_similarity == pytest.approx(0.62)
