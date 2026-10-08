"""Tests for the fork features of the Verda backend.

The fork adds Verda SFS (shared file system) volumes mounted over NFS, the
caller's keys on the host, and an optional debug SSH key from the server env.
"""

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from dstack._internal.core.backends.verda.compute import (
    VerdaCompute,
    VerdaInstanceBackendData,
    _get_sfs_mount_commands,
)
from dstack._internal.core.errors import ComputeError


def _sfs_volume(name="data", volume_id="vol-1", location="FIN-03", pseudo="/share-1"):
    return SimpleNamespace(
        name=name,
        volume_id=volume_id,
        provisioning_data=SimpleNamespace(
            backend_data=json.dumps(
                {"pseudo_path": pseudo, "location_code": location, "volume_type": "NVMe_Shared"}
            )
        ),
    )


class TestGetSfsMountCommands:
    def test_mounts_a_mapped_volume_over_nfs(self):
        commands = _get_sfs_mount_commands(
            volumes=[_sfs_volume()],
            volume_mounts={"vol-1": "/data"},
            instance_region="FIN-03",
        )
        mount = [c for c in commands if "mount -t nfs" in c]
        assert len(mount) == 1
        assert "nfs.fin-03.datacrunch.io:/share-1 /mnt/disks/dstack-volumes/data" in mount[0]

    def test_skips_a_volume_that_is_not_mounted_by_the_run(self):
        commands = _get_sfs_mount_commands(
            volumes=[_sfs_volume()], volume_mounts={}, instance_region="FIN-03"
        )
        assert commands == []

    def test_skips_a_volume_without_backend_data(self):
        volume = SimpleNamespace(
            name="data", volume_id="vol-1", provisioning_data=SimpleNamespace(backend_data=None)
        )
        commands = _get_sfs_mount_commands(
            volumes=[volume], volume_mounts={"vol-1": "/data"}, instance_region="FIN-03"
        )
        assert commands == []


class TestRegisterVolume:
    def _volume(self):
        return SimpleNamespace(
            configuration=SimpleNamespace(volume_id="vol-1", region="FIN-03"),
        )

    def test_rejects_a_volume_that_is_not_shared(self):
        compute = VerdaCompute.__new__(VerdaCompute)
        compute.config = MagicMock()
        with patch(
            "dstack._internal.core.backends.verda.compute._get_volume_by_id",
            return_value={"type": "NVMe", "location": "FIN-03", "pseudo_path": "/x"},
        ):
            with pytest.raises(ComputeError):
                compute.register_volume(self._volume())

    def test_registers_a_shared_volume_with_nfs_backend_data(self):
        compute = VerdaCompute.__new__(VerdaCompute)
        compute.config = MagicMock()
        compute.backend_type = "verda"
        with patch(
            "dstack._internal.core.backends.verda.compute._get_volume_by_id",
            return_value={
                "type": "NVMe_Shared",
                "location": "FIN-03",
                "pseudo_path": "/share-1",
                "size": 100,
            },
        ):
            data = compute.register_volume(self._volume())
        assert data.attachable is True
        assert json.loads(data.backend_data)["pseudo_path"] == "/share-1"


class TestCreateInstanceForkFeatures:
    def _run(self, env, volumes=None, volume_mounts=None):
        compute = VerdaCompute.__new__(VerdaCompute)
        compute.client = MagicMock()
        instance_offer = SimpleNamespace(
            backend="verda",
            instance=SimpleNamespace(
                name="CPU.4V.16G",
                resources=SimpleNamespace(
                    disk=SimpleNamespace(size_mib=102400), gpus=[], spot=False
                ),
            ),
            region="FIN-03",
            price=0.0279,
        )
        instance_config = SimpleNamespace(
            instance_name="verda-one-node-0",
            get_public_keys=lambda: ["ssh-rsa project"],
            volumes=volumes or [],
        )
        created_keys = []

        def create_key(client, name, public_key):
            created_keys.append((name, public_key))
            return f"key-{len(created_keys)}"

        captured = {}

        def create_script(client, name, script):
            captured["script"] = script
            return "startup-script-id"

        with (
            patch.dict(os.environ, env, clear=False),
            patch(
                "dstack._internal.core.backends.verda.compute.generate_unique_instance_name",
                return_value="verda-one-node-0",
            ),
            patch(
                "dstack._internal.core.backends.verda.compute.get_shim_commands",
                return_value=["echo shim"],
            ),
            patch(
                "dstack._internal.core.backends.verda.compute._create_ssh_key",
                side_effect=create_key,
            ),
            patch(
                "dstack._internal.core.backends.verda.compute._create_startup_script",
                side_effect=lambda client, name, script: create_script(client, name, script),
            ),
            patch(
                "dstack._internal.core.backends.verda.compute._deploy_instance",
                return_value=SimpleNamespace(id="provider-instance-id", location="FIN-03"),
            ),
            patch(
                "dstack._internal.core.backends.verda.compute.JobProvisioningData",
                side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
            ),
        ):
            jpd = compute.create_instance(
                instance_offer, instance_config, None, volume_mounts=volume_mounts
            )
        return jpd, created_keys, captured["script"]

    def test_adds_the_debug_key_as_a_per_instance_key(self):
        env = {"DSTACK_DEBUG_SSH_PUBLIC_KEY": "ssh-ed25519 debug"}
        jpd, keys, _ = self._run(env)
        assert ("verda-one-node-0-debug.key", "ssh-ed25519 debug") in keys
        backend_data = VerdaInstanceBackendData.load(jpd.backend_data)
        assert len(backend_data.ssh_key_ids) == 2

    def test_no_debug_key_when_the_env_is_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DSTACK_DEBUG_SSH_PUBLIC_KEY", None)
            _, keys, _ = self._run({})
        assert [name for name, _ in keys] == ["verda-one-node-0-0.key"]

    def test_mounts_sfs_volumes_before_the_shim_starts(self):
        os.environ.pop("DSTACK_DEBUG_SSH_PUBLIC_KEY", None)
        _, _, script = self._run({}, volumes=[_sfs_volume()], volume_mounts={"vol-1": "/data"})
        assert script.index("mount -t nfs") < script.index("echo shim")


class TestRunJob:
    def test_authorizes_the_project_key_and_the_extra_keys_on_the_host(self):
        compute = VerdaCompute.__new__(VerdaCompute)
        captured = {}

        def fake_create_instance(
            instance_offer, instance_config, placement_group, volume_mounts=None
        ):
            captured["config"] = instance_config
            captured["volume_mounts"] = volume_mounts
            return "jpd"

        compute.create_instance = fake_create_instance
        compute._restrict_instance_offer_az_to_volumes_az = lambda offer, volumes: None
        run = MagicMock()
        run.project_name = "main"
        run.user = "admin"
        run.run_spec.configuration.volumes = []
        run.run_spec.merged_profile.tags = None
        requirements = SimpleNamespace(reservation=None)
        offer = MagicMock()
        with patch(
            "dstack._internal.core.backends.verda.compute.get_job_instance_name",
            return_value="job-0",
        ):
            result = compute.run_job(
                run,
                MagicMock(),
                offer,
                "ssh-rsa project",
                "private",
                [],
                None,
                requirements,
                ["ssh-ed25519 user"],
            )
        assert result == "jpd"
        keys = [k.public for k in captured["config"].ssh_keys]
        assert keys == ["ssh-rsa project", "ssh-ed25519 user"]
