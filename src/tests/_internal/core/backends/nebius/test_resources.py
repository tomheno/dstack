from dstack._internal.core.backends.nebius import resources
from dstack._internal.core.backends.nebius.models import NebiusServiceAccountCreds


def test_make_sdk_sets_user_agent_prefix(mocker):
    sdk_cls = mocker.patch.object(resources, "SDK")
    mocker.patch.object(resources, "__version__", "1.2.3")
    creds = NebiusServiceAccountCreds(
        service_account_id="service-account-id",
        public_key_id="public-key-id",
        private_key_content="private-key",
    )

    resources.make_sdk(creds)

    assert sdk_cls.call_args.kwargs["user_agent_prefix"] == "dstack/1.2.3"


def _capture_create_request(mocker):
    client = mocker.patch.object(resources, "InstanceServiceClient")
    mocker.patch.object(resources.LOOP, "await_", side_effect=lambda coro: coro)
    return client.return_value.create


def _create(preemptible, pricing_policy_id):
    return resources.create_instance(
        sdk=None,
        name="test-instance",
        project_id="project-test",
        user_data="",
        platform="gpu-rtx6000",
        preset="1gpu-24vcpu-218gb",
        cluster_id=None,
        disk_id="disk-test",
        subnet_id="subnet-test",
        preemptible=preemptible,
        pricing_policy_id=pricing_policy_id,
        labels={},
    )


def test_create_spot_instance_sets_preemptible_and_pricing_policy(mocker):
    # Nebius Dynamic Spot pricing (2026-10-08): a preemptible create needs a pricing
    # model. dstack pins the spot price with a per-instance pricing policy (#4328).
    create = _capture_create_request(mocker)
    _create(preemptible=True, pricing_policy_id="pricingpolicy-test")
    request = create.call_args.args[0]
    assert request.spec.pricing_model is not None
    assert request.spec.pricing_model.field == "spot_pricing_policy"
    assert request.spec.spot_pricing_policy.id == "pricingpolicy-test"
    assert request.spec.recovery_policy is not None


def test_create_on_demand_instance_sets_no_preemptible_spec(mocker):
    create = _capture_create_request(mocker)
    _create(preemptible=False, pricing_policy_id=None)
    request = create.call_args.args[0]
    # An unset sub-message reads as an empty message in the SDK, so check the oneof.
    assert request.spec.pricing_model is None
