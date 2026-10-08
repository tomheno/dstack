"""Fork: the server sends the NFS details of a Verda SFS volume to the shim."""

import json
from types import SimpleNamespace

from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.server.services.runner.client import _volume_to_shim_volume_info


def _volume(backend_data):
    return SimpleNamespace(
        name="data",
        volume_id="vol-1",
        external=True,
        configuration=SimpleNamespace(backend=BackendType.VERDA),
        provisioning_data=SimpleNamespace(backend_data=backend_data),
        get_attachment_data_for_instance=lambda instance_id: None,
    )


def test_fills_nfs_details_from_sfs_backend_data():
    info = _volume_to_shim_volume_info(
        _volume(json.dumps({"pseudo_path": "/share-1", "location_code": "FIN-03"})),
        instance_id="instance-1",
    )
    assert info.nfs_host == "nfs.fin-03.datacrunch.io"
    assert info.nfs_pseudo == "/share-1"
    assert info.device_name is None


def test_no_nfs_details_for_a_block_volume():
    info = _volume_to_shim_volume_info(_volume(None), instance_id="instance-1")
    assert info.nfs_host is None
    assert info.nfs_pseudo is None
