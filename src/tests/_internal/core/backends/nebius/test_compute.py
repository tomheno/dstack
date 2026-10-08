from types import SimpleNamespace

import pytest

from dstack._internal.core.backends.nebius import compute as compute_module
from dstack._internal.core.backends.nebius.compute import NebiusCompute, _supported_instances
from dstack._internal.core.backends.nebius.models import (
    NebiusConfig,
    NebiusServiceAccountCreds,
)
from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.core.models.instances import (
    Gpu,
    InstanceAvailability,
    InstanceOffer,
    InstanceType,
    Resources,
)


def make_compute() -> NebiusCompute:
    return NebiusCompute(
        NebiusConfig(
            creds=NebiusServiceAccountCreds(
                service_account_id="service-account-id",
                public_key_id="public-key-id",
                private_key_content="private-key",
            )
        )
    )


def make_offer(
    price: float, spot: bool, is_preemptible_flat_rate: bool, gpu_count: int = 8
) -> InstanceOffer:
    return InstanceOffer(
        backend=BackendType.NEBIUS,
        instance=InstanceType(
            name=f"gpu-h100-sxm {gpu_count}gpu-128vcpu-1600gb",
            resources=Resources(
                cpus=128,
                memory_mib=1600 * 1024,
                gpus=[Gpu(name="H100", memory_mib=80 * 1024)] * gpu_count,
                spot=spot,
            ),
        ),
        region="eu-north1",
        price=price,
        backend_data={
            "fabrics": ["fabric-2"],
            "is_preemptible_flat_rate": is_preemptible_flat_rate,
        },
    )


class TestGetAllOffersWithAvailability:
    @pytest.fixture(autouse=True)
    def _mock_region_to_project_id(self, mocker):
        mocker.patch.object(NebiusCompute, "_region_to_project_id", {"eu-north1": "project-id"})

    @pytest.mark.parametrize(
        ("price", "gpu_count", "expected_price"),
        [
            (1.23, 1, 1.23),
            (1.2, 1, 1.2),
            (1.2341, 1, 1.235),
            (1.2349, 1, 1.235),
            (1.2350001, 1, 1.236),
            (0.0001, 1, 0.001),
            (2.007, 1, 2.007),
            (9.84, 8, 9.84),
            (1.23, 8, 1.232),
            (1.2341, 0, 1.235),
        ],
    )
    def test_rounds_spot_price_up_to_3_decimal_places_per_gpu(
        self, mocker, price, gpu_count, expected_price
    ):
        mocker.patch.object(
            compute_module,
            "get_catalog_offers",
            return_value=[
                make_offer(price, spot=True, is_preemptible_flat_rate=False, gpu_count=gpu_count)
            ],
        )

        offers = make_compute().get_all_offers_with_availability(unallocated_resources=False)

        assert len(offers) == 1
        assert offers[0].price == expected_price
        assert offers[0].availability == InstanceAvailability.UNKNOWN

    def test_keeps_on_demand_price_as_is(self, mocker):
        mocker.patch.object(
            compute_module,
            "get_catalog_offers",
            return_value=[make_offer(1.2341, spot=False, is_preemptible_flat_rate=False)],
        )

        offers = make_compute().get_all_offers_with_availability(unallocated_resources=False)

        assert offers[0].price == 1.2341

    def test_keeps_flat_rate_spot_price_as_is(self, mocker):
        mocker.patch.object(
            compute_module,
            "get_catalog_offers",
            return_value=[make_offer(1.2341, spot=True, is_preemptible_flat_rate=True)],
        )

        offers = make_compute().get_all_offers_with_availability(unallocated_resources=False)

        assert offers[0].price == 1.2341


def make_rtx_offer(price: float, spot: bool, region: str = "us-central1") -> InstanceOffer:
    return InstanceOffer(
        backend=BackendType.NEBIUS,
        instance=InstanceType(
            name="gpu-rtx6000 1gpu-24vcpu-218gb",
            resources=Resources(
                cpus=24,
                memory_mib=218 * 1024,
                gpus=[Gpu(name="RTXPRO6000", memory_mib=96 * 1024)],
                spot=spot,
            ),
        ),
        region=region,
        price=price,
        backend_data={"fabrics": [], "is_preemptible_flat_rate": False},
    )


class TestSpotCapAtOnDemandPrice:
    # Fork (operator decision 2026-10-08): the pricing policy caps a spot VM at the
    # on-demand price of the same instance type and region, not at the current spot price.
    @pytest.fixture(autouse=True)
    def _mock_region_to_project_id(self, mocker):
        mocker.patch.object(
            NebiusCompute,
            "_region_to_project_id",
            {"us-central1": "project-a", "uk-south2": "project-b"},
        )

    def test_spot_offer_is_capped_at_the_on_demand_price_of_the_same_type(self, mocker):
        mocker.patch.object(
            compute_module,
            "get_catalog_offers",
            return_value=[make_rtx_offer(0.95, spot=True), make_rtx_offer(1.79, spot=False)],
        )

        offers = make_compute().get_all_offers_with_availability(unallocated_resources=False)

        spot = [o for o in offers if o.instance.resources.spot][0]
        on_demand = [o for o in offers if not o.instance.resources.spot][0]
        assert spot.price == 1.79
        assert spot.backend_data["on_demand_price"] == 1.79
        assert on_demand.price == 1.79

    def test_falls_back_to_the_spot_price_without_an_on_demand_offer(self, mocker):
        mocker.patch.object(
            compute_module,
            "get_catalog_offers",
            return_value=[
                make_rtx_offer(0.95, spot=True, region="us-central1"),
                make_rtx_offer(1.79, spot=False, region="uk-south2"),
            ],
        )

        offers = make_compute().get_all_offers_with_availability(unallocated_resources=False)

        spot = [o for o in offers if o.instance.resources.spot][0]
        assert spot.price == 0.95
        assert spot.backend_data.get("on_demand_price") is None

    def test_flat_rate_spot_price_is_not_capped(self, mocker):
        flat = make_rtx_offer(0.95, spot=True)
        flat.backend_data["is_preemptible_flat_rate"] = True
        mocker.patch.object(
            compute_module,
            "get_catalog_offers",
            return_value=[flat, make_rtx_offer(1.79, spot=False)],
        )

        offers = make_compute().get_all_offers_with_availability(unallocated_resources=False)

        spot = [o for o in offers if o.instance.resources.spot][0]
        assert spot.price == 0.95

    def test_create_time_cap_is_the_on_demand_price_per_gpu(self):
        offer = make_offer(9.84, spot=True, is_preemptible_flat_rate=False, gpu_count=8)
        offer.backend_data["on_demand_price"] = 23.6
        assert str(compute_module._get_pricing_policy_max_price(offer)) == "2.950"

    def test_create_time_cap_falls_back_to_the_offer_price(self):
        offer = make_rtx_offer(0.95, spot=True)
        assert str(compute_module._get_pricing_policy_max_price(offer)) == "0.950"


class TestSupportedInstances:
    # Fork: the RTX PRO 6000 runs as `gpu-rtx6000` (us-central1) and `gpu-rtx6000-a`
    # (uk-south2). The deployed fork served both. Upstream lists only `gpu-rtx6000`.
    @pytest.mark.parametrize(
        "instance_name",
        [
            "gpu-rtx6000 1gpu-24vcpu-218gb",
            "gpu-rtx6000-a 1gpu-24vcpu-218gb",
        ],
    )
    def test_accepts_both_rtx_pro_6000_platforms(self, instance_name):
        offer = SimpleNamespace(instance=SimpleNamespace(name=instance_name))
        assert _supported_instances(offer)

    def test_rejects_an_unknown_platform(self):
        offer = SimpleNamespace(instance=SimpleNamespace(name="gpu-unknown 1gpu-8vcpu-32gb"))
        assert not _supported_instances(offer)
