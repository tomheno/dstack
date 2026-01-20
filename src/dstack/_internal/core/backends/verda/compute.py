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
from dstack._internal.core.models.volumes import (
    Volume,
    VolumeAttachmentData,
    VolumeMountPoint,
    VolumeProvisioningData,
)
from dstack._internal.utils.common import get_or_error
from dstack._internal.utils.logging import get_logger
from dstack._internal.utils.ssh import get_public_key_fingerprint

logger = get_logger(__name__)

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
        # Include both user SSH key (if provided) and project SSH key
        ssh_keys = []
        if run.run_spec.ssh_key_pub:
            ssh_keys.append(SSHKey(public=run.run_spec.ssh_key_pub.strip()))
        ssh_keys.append(SSHKey(public=project_ssh_public_key.strip()))

        instance_config = InstanceConfiguration(
            project_name=run.project_name,
            instance_name=get_job_instance_name(run, job),
            user=run.user,
            ssh_keys=ssh_keys,
            volumes=volumes,
            reservation=run.run_spec.configuration.reservation,
            tags=run.run_spec.merged_profile.tags,
        )

        # Build volume mount mapping: volume_id -> mount_path
        logger.info(
            "run_job called with %d volumes: %s",
            len(volumes),
            [(v.name, v.volume_id, v.provisioning_data.backend_data if v.provisioning_data else None) for v in volumes]
        )

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
                            logger.info(
                                "Mapped volume %s (id=%s) to mount path %s",
                                vol.name, vol.volume_id, mount_point.path
                            )
                            break

        logger.info("Final volume_mounts: %s", volume_mounts)

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

        # Add additional SSH key from environment for debugging (optional)
        import os
        debug_ssh_key = os.environ.get("DSTACK_DEBUG_SSH_PUBLIC_KEY")
        if debug_ssh_key:
            logger.info("Adding debug SSH key from DSTACK_DEBUG_SSH_PUBLIC_KEY")
            ssh_ids.append(
                _get_or_create_ssh_key(
                    client=self.client,
                    name="dstack-debug.key",
                    public_key=debug_ssh_key,
                )
            )

        # Generate SFS mount commands for volumes
        sfs_mount_commands = _get_sfs_mount_commands(
            volumes=instance_config.volumes or [],
            volume_mounts=volume_mounts or {},
            instance_region=instance_offer.region,
        )

        logger.info(
            "SFS mount commands for instance %s: %s",
            instance_name, sfs_mount_commands
        )

        # Build startup script with SFS mounts first, then shim commands
        shim_commands = get_shim_commands()
        all_commands = sfs_mount_commands + shim_commands
        startup_script = " ".join([" && ".join(all_commands)])

        logger.info(
            "Full startup script for instance %s: %s",
            instance_name, startup_script[:500] + "..." if len(startup_script) > 500 else startup_script
        )

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
        volume_data = _get_volume_by_id(self.config, volume_id)

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
            # SFS volumes must be attached via Verda API to authorize instance IP access
            attachable=True,
            detachable=True,
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

    def attach_volume(
        self, volume: Volume, provisioning_data: JobProvisioningData
    ) -> VolumeAttachmentData:
        """
        Attach an SFS volume to an instance via Verda API.

        This authorizes the instance IP to access the NFS share.
        The actual NFS mount is done via the startup script.

        API: PUT /v1/volumes with body {"action": "attach", "id": "<volume_id>", "instance_ids": ["<instance_id>"]}
        """
        import requests

        volume_id = volume.volume_id
        instance_id = provisioning_data.instance_id

        logger.info(
            "Attaching SFS volume %s to instance %s",
            volume_id, instance_id
        )

        try:
            token = _get_verda_access_token(
                self.config.creds.client_id,
                self.config.creds.client_secret,
            )
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }

            payload = {
                "action": "attach",
                "id": volume_id,
                "instance_ids": [instance_id],
            }

            logger.info(
                "Calling Verda volume API: PUT /v1/volumes with payload=%s",
                payload
            )

            response = requests.put(
                "https://api.datacrunch.io/v1/volumes",
                headers=headers,
                json=payload,
                timeout=60,
            )

            logger.info(
                "Verda volume API response: status=%d body=%s",
                response.status_code, response.text[:500] if response.text else "(empty)"
            )

            # 202 Accepted is a valid response - means the request was accepted for async processing
            if response.status_code not in (200, 201, 202, 204):
                error_msg = response.text
                try:
                    error_data = response.json()
                    error_msg = error_data.get("message", error_data)
                except Exception:
                    pass
                raise ComputeError(
                    f"Failed to attach volume {volume_id} to instance {instance_id}: "
                    f"HTTP {response.status_code} - {error_msg}"
                )

            logger.info(
                "Successfully attached SFS volume %s to instance %s",
                volume_id, instance_id
            )

            # SFS volumes don't have a device_name - they're mounted via NFS
            return VolumeAttachmentData(device_name=None)

        except requests.exceptions.RequestException as e:
            raise ComputeError(f"Failed to attach volume {volume_id}: {e}")

    def detach_volume(
        self, volume: Volume, provisioning_data: JobProvisioningData, force: bool = False
    ):
        """
        Detach an SFS volume from an instance via Verda API.

        This revokes the instance IP's authorization to access the NFS share.

        API: PUT /v1/volumes with body {"action": "detach", "id": "<volume_id>", "instance_ids": ["<instance_id>"]}
        """
        import requests

        volume_id = volume.volume_id
        instance_id = provisioning_data.instance_id

        logger.info(
            "Detaching SFS volume %s from instance %s (force=%s)",
            volume_id, instance_id, force
        )

        try:
            token = _get_verda_access_token(
                self.config.creds.client_id,
                self.config.creds.client_secret,
            )
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }

            payload = {
                "action": "detach",
                "id": volume_id,
                "instance_ids": [instance_id],
            }

            response = requests.put(
                "https://api.datacrunch.io/v1/volumes",
                headers=headers,
                json=payload,
                timeout=60,
            )

            # 202 Accepted is a valid response - means the request was accepted for async processing
            if response.status_code not in (200, 201, 202, 204):
                error_msg = response.text
                try:
                    error_data = response.json()
                    error_msg = error_data.get("message", error_data)
                except Exception:
                    pass
                # Don't fail on detach errors unless not forcing
                if not force:
                    raise ComputeError(
                        f"Failed to detach volume {volume_id} from instance {instance_id}: "
                        f"HTTP {response.status_code} - {error_msg}"
                    )
                else:
                    logger.warning(
                        "Failed to detach volume %s from instance %s (force=True, ignoring): %s",
                        volume_id, instance_id, error_msg
                    )
                    return

            logger.info(
                "Successfully detached SFS volume %s from instance %s",
                volume_id, instance_id
            )

        except requests.exceptions.RequestException as e:
            if not force:
                raise ComputeError(f"Failed to detach volume {volume_id}: {e}")
            else:
                logger.warning(
                    "Failed to detach volume %s (force=True, ignoring): %s",
                    volume_id, e
                )


def _get_verda_access_token(client_id: str, client_secret: str) -> str:
    """
    Get an OAuth2 access token from the Verda/DataCrunch API using client credentials.
    """
    import requests

    token_url = "https://api.datacrunch.io/v1/oauth2/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }

    response = requests.post(token_url, data=data, timeout=30)
    response.raise_for_status()
    token_data = response.json()
    return token_data["access_token"]


def _get_volume_by_id(config: VerdaConfig, volume_id: str) -> Optional[Dict]:
    """
    Fetch volume details from Verda API via direct HTTP request.
    The SDK's volume methods may not return all fields we need, so we use the REST API directly.
    """
    import requests

    try:
        # Get access token using OAuth2 client credentials
        token = _get_verda_access_token(
            config.creds.client_id,
            config.creds.client_secret,
        )
        headers = {"Authorization": f"Bearer {token}"}

        response = requests.get(
            f"https://api.datacrunch.io/v1/volumes/{volume_id}",
            headers=headers,
            timeout=30,
        )

        if response.status_code == 404:
            return None

        response.raise_for_status()
        data = response.json()
        logger.debug(f"Volume API response for {volume_id}: {data}")
        return data

    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise ComputeError(f"Failed to fetch volume {volume_id}: {e}")
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

    logger.info(
        "_get_sfs_mount_commands: processing %d volumes, volume_mounts=%s",
        len(volumes), volume_mounts
    )

    # Check if we have any volumes to mount - if so, ensure nfs-common is installed
    has_volumes_to_mount = any(
        volume.provisioning_data and volume.provisioning_data.backend_data
        and volume.volume_id and volume.volume_id in volume_mounts
        for volume in volumes
    )
    if has_volumes_to_mount:
        # Install nfs-common if not already installed (needed for NFS mounts)
        # Also unmask rpcbind.socket which may be masked on some images
        commands.append(
            "(which mount.nfs > /dev/null 2>&1 || "
            "(systemctl unmask rpcbind.socket 2>/dev/null; "
            "apt-get update -qq && apt-get install -y -qq nfs-common)) || "
            "echo 'WARNING: Failed to install nfs-common'"
        )

    for volume in volumes:
        if not volume.provisioning_data or not volume.provisioning_data.backend_data:
            logger.info(
                "Skipping volume %s: no provisioning_data or backend_data",
                volume.name
            )
            continue

        volume_id = volume.volume_id
        if not volume_id or volume_id not in volume_mounts:
            logger.info(
                "Skipping volume %s (id=%s): not in volume_mounts",
                volume.name, volume_id
            )
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

        # Generate mount commands with error logging but non-blocking
        # Use a subshell so failures don't stop the entire startup script
        mount_cmd = (
            f"(mkdir -p {host_mount_path} && "
            f"mount -t nfs -o nconnect=16 nfs.{dc}.datacrunch.io:{pseudo_path} {host_mount_path} && "
            f"echo 'SFS volume {volume.name} mounted successfully') || "
            f"echo 'WARNING: Failed to mount SFS volume {volume.name}'"
        )
        commands.append(mount_cmd)

        logger.info(
            "SFS mount for volume %s: nfs.%s.datacrunch.io:%s -> %s",
            volume.name, dc, pseudo_path, host_mount_path
        )

    logger.info("_get_sfs_mount_commands: returning %d commands: %s", len(commands), commands)
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
