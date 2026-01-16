import json
from collections.abc import Iterable
from typing import Dict, List, Optional

from verda import VerdaClient
from verda.exceptions import APIException
from verda.instances import Instance

from dstack._internal.core.backends.base.backend import Compute
from dstack._internal.core.backends.base.compute import (
    ComputeWithAllOffersCached,
    ComputeWithCreateInstanceSupport,
    ComputeWithPrivilegedSupport,
    ComputeWithVolumeSupport,
    generate_unique_instance_name,
    get_job_instance_name,
    get_shim_commands,
)
from dstack._internal.core.backends.base.offers import (
    OfferModifier,
    get_catalog_offers,
    get_offers_disk_modifier,
)
from dstack._internal.core.backends.verda.models import VerdaConfig
from dstack._internal.core.errors import ComputeError, NoCapacityError
from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.core.models.instances import (
    InstanceAvailability,
    InstanceConfiguration,
    InstanceOffer,
    InstanceOfferWithAvailability,
    SSHKey,
)
from dstack._internal.core.models.placement import PlacementGroup
from dstack._internal.core.models.resources import Memory, Range
from dstack._internal.core.models.runs import Job, JobProvisioningData, Requirements, Run
from dstack._internal.core.models.volumes import Volume, VolumeMountPoint, VolumeProvisioningData
from dstack._internal.utils.common import get_or_error
from dstack._internal.utils.logging import get_logger
from dstack._internal.utils.ssh import get_public_key_fingerprint

logger = get_logger("verda.compute")

MAX_INSTANCE_NAME_LEN = 60

IMAGE_SIZE = Memory.parse("50GB")

CONFIGURABLE_DISK_SIZE = Range[Memory](min=IMAGE_SIZE, max=None)


class VerdaCompute(
    ComputeWithAllOffersCached,
    ComputeWithCreateInstanceSupport,
    ComputeWithPrivilegedSupport,
    ComputeWithVolumeSupport,
    Compute,
):
    def __init__(self, config: VerdaConfig, backend_type: BackendType):
        super().__init__()
        self.config = config
        self.client = VerdaClient(
            client_id=self.config.creds.client_id,
            client_secret=self.config.creds.client_secret,
        )
        self.backend_type = backend_type

    def get_all_offers_with_availability(self) -> List[InstanceOfferWithAvailability]:
        offers = get_catalog_offers(
            backend=self.backend_type,
            locations=self.config.regions,
        )
        offers_with_availability = self._get_offers_with_availability(offers)
        return offers_with_availability

    def get_offers_modifiers(self, requirements: Requirements) -> Iterable[OfferModifier]:
        return [get_offers_disk_modifier(CONFIGURABLE_DISK_SIZE, requirements)]

    def _get_offers_with_availability(
        self, offers: List[InstanceOffer]
    ) -> List[InstanceOfferWithAvailability]:
        raw_availabilities: List[Dict] = self.client.instances.get_availabilities()

        region_availabilities = {}
        for location in raw_availabilities:
            location_code = location["location_code"]
            availabilities = location["availabilities"]
            for name in availabilities:
                key = (name, location_code)
                region_availabilities[key] = InstanceAvailability.AVAILABLE

        availability_offers = []
        for offer in offers:
            key = (offer.instance.name, offer.region)
            availability = region_availabilities.get(key, InstanceAvailability.NOT_AVAILABLE)
            availability_offers.append(
                InstanceOfferWithAvailability(**offer.dict(), availability=availability)
            )

        return availability_offers

    def run_job(
        self,
        run: Run,
        job: Job,
        instance_offer: InstanceOfferWithAvailability,
        project_ssh_public_key: str,
        project_ssh_private_key: str,
        volumes: List[Volume],
        placement_group: Optional[PlacementGroup],
    ) -> JobProvisioningData:
        """
        Override run_job to handle SFS volume mounting.
        SFS volumes are mounted via NFS in the startup script.
        """
        instance_config = InstanceConfiguration(
            project_name=run.project_name,
            instance_name=get_job_instance_name(run, job),
            user=run.user,
            ssh_keys=[SSHKey(public=project_ssh_public_key.strip())],
            volumes=volumes,
            reservation=run.run_spec.configuration.reservation,
            tags=run.run_spec.merged_profile.tags,
        )

        # Build volume mount mapping: volume_id -> mount_path
        volume_mounts: Dict[str, str] = {}
        if run.run_spec.configuration.volumes:
            for mount_point in run.run_spec.configuration.volumes:
                if isinstance(mount_point, VolumeMountPoint):
                    # Find the matching volume
                    names = (
                        mount_point.name
                        if isinstance(mount_point.name, list)
                        else [mount_point.name]
                    )
                    for vol in volumes:
                        if vol.name in names and vol.volume_id:
                            volume_mounts[vol.volume_id] = mount_point.path
                            break

        instance_offer = instance_offer.copy()
        self._restrict_instance_offer_az_to_volumes_az(instance_offer, volumes)

        return self.create_instance(
            instance_offer,
            instance_config,
            placement_group=placement_group,
            volume_mounts=volume_mounts,
        )

    def create_instance(
        self,
        instance_offer: InstanceOfferWithAvailability,
        instance_config: InstanceConfiguration,
        placement_group: Optional[PlacementGroup],
        volume_mounts: Optional[Dict[str, str]] = None,
    ) -> JobProvisioningData:
        instance_name = generate_unique_instance_name(
            instance_config, max_length=MAX_INSTANCE_NAME_LEN
        )
        public_keys = instance_config.get_public_keys()
        ssh_ids = []
        for ssh_public_key in public_keys:
            ssh_ids.append(
                # verda allows you to use the same name
                _get_or_create_ssh_key(
                    client=self.client,
                    name=f"dstack-{instance_config.instance_name}.key",
                    public_key=ssh_public_key,
                )
            )

        # Generate SFS mount commands for volumes
        sfs_mount_commands = _get_sfs_mount_commands(
            volumes=instance_config.volumes or [],
            volume_mounts=volume_mounts or {},
            instance_region=instance_offer.region,
        )

        # Build startup script with SFS mounts first, then shim commands
        shim_commands = get_shim_commands()
        all_commands = sfs_mount_commands + shim_commands
        startup_script = " ".join([" && ".join(all_commands)])
        script_name = f"dstack-{instance_config.instance_name}.sh"
        startup_script_ids = _get_or_create_startup_scrpit(
            client=self.client,
            name=script_name,
            script=startup_script,
        )

        disk_size = round(instance_offer.instance.resources.disk.size_mib / 1024)
        image_id = _get_vm_image_id(instance_offer)

        logger.debug(
            "Deploying Verda instance",
            {
                "instance_type": instance_offer.instance.name,
                "ssh_key_ids": ssh_ids,
                "startup_script_id": startup_script_ids,
                "hostname": instance_name,
                "description": instance_name,
                "image": image_id,
                "disk_size": disk_size,
                "location": instance_offer.region,
            },
        )
        instance = _deploy_instance(
            client=self.client,
            instance_type=instance_offer.instance.name,
            ssh_key_ids=ssh_ids,
            startup_script_id=startup_script_ids,
            hostname=instance_name,
            description=instance_name,
            image=image_id,
            disk_size=disk_size,
            is_spot=instance_offer.instance.resources.spot,
            location=instance_offer.region,
        )
        return JobProvisioningData(
            backend=instance_offer.backend,
            instance_type=instance_offer.instance,
            instance_id=instance.id,
            hostname=None,
            internal_ip=None,
            region=instance.location,
            price=instance_offer.price,
            username="root",
            ssh_port=22,
            dockerized=True,
            ssh_proxy=None,
            backend_data=None,
        )

    def terminate_instance(
        self, instance_id: str, region: str, backend_data: Optional[str] = None
    ):
        try:
            self.client.instances.action(id_list=[instance_id], action="delete")
        except APIException as e:
            if e.message in [
                "Invalid instance id",
                "Can't discontinue a discontinued instance",
            ]:
                logger.debug("Skipping instance %s termination. Instance not found.", instance_id)
                return
            raise

    def update_provisioning_data(
        self,
        provisioning_data: JobProvisioningData,
        project_ssh_public_key: str,
        project_ssh_private_key: str,
    ):
        instance = _get_instance_by_id(self.client, provisioning_data.instance_id)
        if instance is not None and instance.status == "running":
            provisioning_data.hostname = instance.ip

    def register_volume(self, volume: Volume) -> VolumeProvisioningData:
        """
        Register an existing SFS (Shared File System) volume from Verda.

        SFS volumes are NFS-based shared filesystems that can be mounted to instances.
        The volume must already exist in Verda and be of type *_Shared (e.g. NVMe_Shared).
        """
        volume_id = get_or_error(volume.configuration.volume_id)
        volume_data = _get_volume_by_id(self.client, volume_id)

        if volume_data is None:
            raise ComputeError(f"Volume {volume_id} not found in Verda")

        volume_type = volume_data.get("type", "")
        if "Shared" not in volume_type:
            raise ComputeError(
                f"Volume {volume_id} is not an SFS volume (type: {volume_type}). "
                "Only shared filesystem volumes (HDD_Shared, NVMe_Shared) are supported."
            )

        location = volume_data.get("location", "")
        if volume.configuration.region and location.upper() != volume.configuration.region.upper():
            raise ComputeError(
                f"Volume {volume_id} is in region {location}, "
                f"but configuration specifies region {volume.configuration.region}"
            )

        pseudo_path = volume_data.get("pseudo_path")
        if not pseudo_path:
            raise ComputeError(f"Volume {volume_id} has no pseudo_path for NFS mounting")

        size_gb = volume_data.get("size", 0)

        # Store SFS-specific data for NFS mounting
        backend_data = json.dumps({
            "pseudo_path": pseudo_path,
            "location_code": location,
            "volume_type": volume_type,
        })

        return VolumeProvisioningData(
            backend=self.backend_type,
            volume_id=volume_id,
            size_gb=size_gb,
            availability_zone=location,
            # SFS volumes don't have dynamic pricing; use monthly price / 730 hours as estimate
            price=volume_data.get("monthly_price"),
            # SFS volumes are mounted via NFS at instance creation, not attached via cloud API
            attachable=False,
            detachable=False,
            backend_data=backend_data,
        )

    def create_volume(self, volume: Volume) -> VolumeProvisioningData:
        """
        Creating SFS volumes via dstack is not supported.
        Create volumes directly in the Verda dashboard and use volume_id to register them.
        """
        raise ComputeError(
            "Creating SFS volumes via dstack is not supported for Verda/DataCrunch. "
            "Please create the volume in the Verda dashboard and register it using volume_id."
        )

    def delete_volume(self, volume: Volume):
        """
        Deleting SFS volumes via dstack is not supported.
        Delete volumes directly in the Verda dashboard.
        """
        raise ComputeError(
            "Deleting SFS volumes via dstack is not supported for Verda/DataCrunch. "
            "Please delete the volume in the Verda dashboard."
        )


def _get_volume_by_id(client: VerdaClient, volume_id: str) -> Optional[Dict]:
    """
    Fetch volume details from Verda API.
    Uses the client's internal auth to make the request.
    """
    try:
        # The verda client has a volumes property that we can use
        # If not available, we fall back to direct API call
        if hasattr(client, "volumes") and hasattr(client.volumes, "get_by_id"):
            return client.volumes.get_by_id(volume_id)
        # Fall back to using the client's HTTP methods
        if hasattr(client, "_get"):
            response = client._get(f"/volumes/{volume_id}")
            return response
        # Last resort: use the client's session/auth
        import requests
        headers = {"Authorization": f"Bearer {client._get_access_token()}"}
        response = requests.get(
            f"https://api.datacrunch.io/v1/volumes/{volume_id}",
            headers=headers,
            timeout=30,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()
    except APIException as e:
        if "not found" in str(e.message).lower():
            return None
        raise
    except Exception as e:
        logger.warning(f"Failed to fetch volume {volume_id}: {e}")
        raise ComputeError(f"Failed to fetch volume {volume_id}: {e}")


def _get_sfs_mount_commands(
    volumes: List[Volume],
    volume_mounts: Dict[str, str],
    instance_region: str,
) -> List[str]:
    """
    Generate NFS mount commands for SFS volumes.

    SFS (Shared File System) volumes in Verda are NFS-based.
    Mount command format: mount -t nfs -o nconnect=16 nfs.<DC>.datacrunch.io:<PSEUDO> <HOST_PATH>

    The volume is mounted at /mnt/disks/dstack-volumes/<volume_name> on the host.
    The shim then bind-mounts this to the user-specified container path.

    Args:
        volumes: List of Volume objects with provisioning data
        volume_mounts: Mapping of volume_id -> mount_path (used to filter which volumes to mount)
        instance_region: The region/datacenter of the instance (e.g., "FIN-01")

    Returns:
        List of shell commands to mount SFS volumes
    """
    commands = []

    for volume in volumes:
        if not volume.provisioning_data or not volume.provisioning_data.backend_data:
            continue

        volume_id = volume.volume_id
        if not volume_id or volume_id not in volume_mounts:
            continue

        try:
            backend_data = json.loads(volume.provisioning_data.backend_data)
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"Failed to parse backend_data for volume {volume_id}")
            continue

        pseudo_path = backend_data.get("pseudo_path")
        location_code = backend_data.get("location_code", instance_region)

        if not pseudo_path:
            logger.warning(f"Volume {volume_id} has no pseudo_path, skipping mount")
            continue

        # Datacenter code for NFS server (lowercase, e.g., "fin-01")
        dc = location_code.lower()

        # Mount at the path the shim expects: /mnt/disks/dstack-volumes/<volume_name>
        # The shim will bind-mount this to the user-specified container path
        host_mount_path = f"/mnt/disks/dstack-volumes/{volume.name}"

        # Generate mount commands
        # 1. Create mount directory
        commands.append(f"mkdir -p {host_mount_path}")
        # 2. Mount the SFS volume via NFS
        commands.append(
            f"mount -t nfs -o nconnect=16 nfs.{dc}.datacrunch.io:{pseudo_path} {host_mount_path}"
        )

        logger.debug(
            f"SFS mount for volume {volume.name}: "
            f"nfs.{dc}.datacrunch.io:{pseudo_path} -> {host_mount_path}"
        )

    return commands


def _get_vm_image_id(instance_offer: InstanceOfferWithAvailability) -> str:
    # https://api.verda.com/v1/images
    if len(instance_offer.instance.resources.gpus) > 0 and instance_offer.instance.resources.gpus[
        0
    ].name in ["V100", "A6000"]:
        # Ubuntu 22.04 + CUDA 12.0 + Docker
        return "2088da25-bb0d-41cc-a191-dccae45d96fd"
    # Ubuntu 24.04 + CUDA 12.8 Open + Docker
    return "77777777-4f48-4249-82b3-f199fb9b701b"


def _get_or_create_ssh_key(client: VerdaClient, name: str, public_key: str) -> str:
    fingerprint = get_public_key_fingerprint(public_key)
    keys = client.ssh_keys.get()
    found_keys = [key for key in keys if fingerprint == get_public_key_fingerprint(key.public_key)]
    if found_keys:
        key = found_keys[0]
        return key.id
    key = client.ssh_keys.create(name, public_key)
    return key.id


def _get_or_create_startup_scrpit(client: VerdaClient, name: str, script: str) -> str:
    scripts = client.startup_scripts.get()
    found_scripts = [startup_script for startup_script in scripts if script == startup_script]
    if found_scripts:
        startup_script = found_scripts[0]
        return startup_script.id

    startup_script = client.startup_scripts.create(name, script)
    return startup_script.id


def _get_instance_by_id(
    client: VerdaClient,
    instance_id: str,
) -> Optional[Instance]:
    try:
        return client.instances.get_by_id(instance_id)
    except APIException as e:
        if e.message == "Invalid instance id":
            return None
        raise


def _deploy_instance(
    client: VerdaClient,
    instance_type: str,
    image: str,
    ssh_key_ids: List[str],
    hostname: str,
    description: str,
    startup_script_id: str,
    disk_size: int,
    is_spot: bool,
    location: str,
) -> Instance:
    try:
        instance = client.instances.create(
            instance_type=instance_type,
            image=image,
            ssh_key_ids=ssh_key_ids,
            hostname=hostname,
            description=description,
            startup_script_id=startup_script_id,
            pricing="FIXED_PRICE",
            is_spot=is_spot,
            location=location,
            os_volume={"name": "OS volume", "size": disk_size},
        )
    except APIException as e:
        # FIXME: Catch only no capacity errors
        raise NoCapacityError(f"Verda API error: {e.message}")

    return instance
