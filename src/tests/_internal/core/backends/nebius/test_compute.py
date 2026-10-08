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
    # Nebius accepts a bid only up to 0.01 USD per GPU-hour below the on-demand price
    # (OUT_OF_RANGE above it), so the cap is the on-demand price minus 0.01, rounded down.
    @pytest.fixture(autouse=True)
    def _mock_region_to_project_id(self, mocker):
        mocker.patch.object(
            NebiusCompute,
            "_region_to_project_id",
            {"us-central1": "project-a", "uk-south2": "project-b"},
        )

    def test_spot_offer_is_capped_just_below_the_on_demand_price_of_the_same_type(self, mocker):
        mocker.patch.object(
            compute_module,
            "get_catalog_offers",
            return_value=[make_rtx_offer(0.95, spot=True), make_rtx_offer(1.79, spot=False)],
        )

        offers = make_compute().get_all_offers_with_availability(unallocated_resources=False)

        spot = [o for o in offers if o.instance.resources.spot][0]
        on_demand = [o for o in offers if not o.instance.resources.spot][0]
        assert spot.price == 1.78
        assert spot.backend_data["on_demand_price"] == 1.79
        assert spot.backend_data["spot_price"] == 0.95
        assert on_demand.price == 1.79

    def test_offers_stay_sorted_by_the_shown_price(self, mocker):
        h100 = make_offer(2.15, spot=True, is_preemptible_flat_rate=False, gpu_count=1)
        h100_od = make_offer(4.5, spot=False, is_preemptible_flat_rate=False, gpu_count=1)
        b200 = make_offer(3.95, spot=True, is_preemptible_flat_rate=False, gpu_count=1)
        b200.instance.name = "gpu-b200-sxm 1gpu-20vcpu-224gb"
        b200_od = make_offer(8.5, spot=False, is_preemptible_flat_rate=False, gpu_count=1)
        b200_od.instance.name = "gpu-b200-sxm 1gpu-20vcpu-224gb"
        mocker.patch.object(
            compute_module,
            "get_catalog_offers",
            return_value=[h100, b200, h100_od, b200_od],
        )
        mocker.patch.object(NebiusCompute, "_region_to_project_id", {"eu-north1": "project-id"})

        offers = make_compute().get_all_offers_with_availability(unallocated_resources=False)

        prices = [o.price for o in offers]
        assert prices == sorted(prices)
        # At an equal-or-lower price the spot offer stays ahead of the on-demand one.
        assert offers[0].instance.resources.spot

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

    def test_create_time_cap_is_the_on_demand_price_per_gpu_minus_one_cent(self):
        offer = make_offer(9.84, spot=True, is_preemptible_flat_rate=False, gpu_count=8)
        offer.backend_data["on_demand_price"] = 23.6
        offer.backend_data["spot_price"] = 9.84
        cap, source = compute_module._get_pricing_policy_cap(offer)
        assert str(cap) == "2.940"
        assert source == "on-demand price"

    def test_create_time_cap_rounds_the_on_demand_price_down(self):
        offer = make_rtx_offer(0.95, spot=True)
        offer.backend_data["on_demand_price"] = 1.7899
        offer.backend_data["spot_price"] = 0.95
        cap, _ = compute_module._get_pricing_policy_cap(offer)
        assert str(cap) == "1.779"

    def test_create_time_cap_uses_the_run_max_price_without_an_on_demand_offer(self):
        offer = make_rtx_offer(0.95, spot=True)
        offer.backend_data["spot_price"] = 0.95
        offer.backend_data["run_max_price"] = 1.5
        cap, source = compute_module._get_pricing_policy_cap(offer)
        assert str(cap) == "1.500"
        assert source == "run max_price"

    def test_create_time_cap_falls_back_to_the_spot_price(self):
        offer = make_rtx_offer(0.95, spot=True)
        cap, source = compute_module._get_pricing_policy_cap(offer)
        assert str(cap) == "0.950"
        assert source == "spot price"

    def test_spot_cap_uses_the_raw_spot_price_not_the_shown_cap(self):
        offer = make_rtx_offer(1.78, spot=True)
        offer.backend_data["on_demand_price"] = 1.79
        offer.backend_data["spot_price"] = 0.95
        assert str(compute_module._get_spot_price_cap(offer)) == "0.950"


class TestRunMaxPriceModifier:
    def make_requirements(self, max_price):
        from dstack._internal.core.models.resources import ResourcesSpec
        from dstack._internal.core.models.runs import Requirements

        return Requirements(resources=ResourcesSpec(), max_price=max_price, spot=True)

    def test_sets_the_run_max_price_on_a_spot_offer_without_an_on_demand_price(self):
        offer = make_rtx_offer(0.95, spot=True).with_availability(
            availability=InstanceAvailability.UNKNOWN
        )
        offer.backend_data["spot_price"] = 0.95
        modified = compute_module._get_run_max_price_modifier(self.make_requirements(1.5))(offer)
        assert modified.backend_data["run_max_price"] == 1.5
        assert modified.price == 0.95
        assert "run_max_price" not in offer.backend_data

    def test_leaves_an_offer_with_an_on_demand_price_alone(self):
        offer = make_rtx_offer(1.78, spot=True).with_availability(
            availability=InstanceAvailability.UNKNOWN
        )
        offer.backend_data["on_demand_price"] = 1.79
        modified = compute_module._get_run_max_price_modifier(self.make_requirements(1.9))(offer)
        assert "run_max_price" not in modified.backend_data

    def test_leaves_the_offer_alone_without_a_run_max_price(self):
        offer = make_rtx_offer(0.95, spot=True).with_availability(
            availability=InstanceAvailability.UNKNOWN
        )
        modified = compute_module._get_run_max_price_modifier(self.make_requirements(None))(offer)
        assert "run_max_price" not in modified.backend_data


class TestCreatePricingPolicyOutOfRangeRetry:
    def test_retries_once_with_the_spot_cap_on_out_of_range(self, mocker):
        from nebius.aio.service_error import RequestError, StatusCode

        calls = []

        def fake_create(sdk, name, project_id, platform, max_price):
            calls.append(max_price)
            if len(calls) == 1:
                err = RequestError.__new__(RequestError)
                err.status = SimpleNamespace(code=StatusCode.OUT_OF_RANGE)
                raise err
            return SimpleNamespace(resource_id="pp-1", successful=lambda: True)

        mocker.patch.object(compute_module.resources, "create_pricing_policy", fake_create)
        mocker.patch.object(
            compute_module.resources, "wait_for_operation", lambda op, timeout: None
        )
        offer = make_rtx_offer(1.78, spot=True)
        offer.backend_data["on_demand_price"] = 1.79
        offer.backend_data["spot_price"] = 0.95

        op = compute_module._create_pricing_policy_with_fallback(
            sdk=None, name="n", project_id="p", platform="gpu-rtx6000", offer=offer
        )

        assert calls == ["1.780", "0.950"]
        assert op.resource_id == "pp-1"

    def test_does_not_retry_when_the_cap_already_is_the_spot_cap(self, mocker):
        from nebius.aio.service_error import RequestError, StatusCode

        def fake_create(sdk, name, project_id, platform, max_price):
            err = RequestError.__new__(RequestError)
            err.status = SimpleNamespace(code=StatusCode.OUT_OF_RANGE)
            raise err

        mocker.patch.object(compute_module.resources, "create_pricing_policy", fake_create)
        offer = make_rtx_offer(0.95, spot=True)

        with pytest.raises(RequestError):
            compute_module._create_pricing_policy_with_fallback(
                sdk=None, name="n", project_id="p", platform="gpu-rtx6000", offer=offer
            )


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
