"""Fork: Verda SFS volumes need a member in the backend-tagged volume config union."""

import pytest

from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.core.models.volumes import (
    VerdaVolumeConfiguration,
    parse_volume_configuration,
)
from dstack._internal.server.services.volumes import _validate_volume_configuration


@pytest.mark.parametrize("backend", ["verda", "datacrunch"])
def test_parses_a_verda_sfs_volume(backend):
    configuration = parse_volume_configuration(
        {
            "type": "volume",
            "name": "greencorporafrdata",
            "backend": backend,
            "region": "FIN-03",
            "volume_id": "4a7b-sfs",
        }
    )
    assert isinstance(configuration, VerdaVolumeConfiguration)
    assert configuration.backend == BackendType(backend)
    assert configuration.region == "FIN-03"
    assert configuration.volume_id == "4a7b-sfs"
    assert configuration.external_volume_id == "4a7b-sfs"


def test_loads_a_row_written_by_the_old_fork_with_availability_zone():
    # The old flat VolumeConfiguration (dstack 0.20.3) stored availability_zone: null.
    configuration = parse_volume_configuration(
        {
            "type": "volume",
            "name": "greencorporafrdata",
            "backend": "verda",
            "region": "FIN-03",
            "availability_zone": None,
            "volume_id": "4a7b-sfs",
            "size": None,
            "auto_cleanup_duration": None,
            "tags": None,
        }
    )
    assert isinstance(configuration, VerdaVolumeConfiguration)
    assert "availability_zone" not in configuration.model_dump()


def test_an_external_verda_volume_needs_no_size(monkeypatch):
    monkeypatch.setattr(
        "dstack._internal.server.services.volumes.backends_services.check_backend_type_available",
        lambda backend: None,
    )
    _validate_volume_configuration(
        parse_volume_configuration(
            {"backend": "verda", "region": "FIN-03", "volume_id": "4a7b-sfs"}
        )
    )
