"""BatchSpec identity: canonical, deterministic, and bound to every field the work order lists."""
import unittest

from _world import ART_NEW, OPS, default_spec  # noqa: F401  (also puts src on sys.path)
from controlled_effect_executor import BatchSpec, BatchSpecError, Operation

GOLDEN_DIGEST = "0fdfad43b108a38e49412961e05e18c912a9e714f73c140112139e0f3f5d04aa"


class BatchSpecTests(unittest.TestCase):
    def test_digest_is_stable_across_runs_and_matches_the_golden_vector(self):
        self.assertEqual(default_spec().digest, GOLDEN_DIGEST)
        self.assertEqual(default_spec().digest, default_spec().digest)
        self.assertEqual(default_spec().batch_ref, "BATCH-" + GOLDEN_DIGEST[:24].upper())

    def test_param_keyword_order_does_not_change_identity(self):
        a = Operation.of("SYNTH-OP-SET", key="alpha", value=1)
        b = Operation.of("SYNTH-OP-SET", value=1, key="alpha")
        self.assertEqual(a, b)

    def test_every_bound_field_changes_the_digest(self):
        base = default_spec().digest
        changed = {
            "work_order_ref": "SYNTH-WO-2", "effect_ref": "SYNTH-EFFECT-2", "target_identity": "SYNTH-TARGET-B",
            "target_precondition_version": "4", "artifact_version": "SYNTH-ARTIFACT-V3", "actor_ref": "SYNTH-ACTOR-2",
            "actor_generation": 5, "delegation_ref": "SYNTH-DELEG-2", "delegation_revision": 2,
            "delegation_expiry": 1003601, "human_approval_ref": "SYNTH-APPROVAL-2", "idempotency_key": "SYNTH-IDEM-2",
            "rollback_ref": "SYNTH-ROLLBACK-1",
            "operations": (Operation.of("SYNTH-OP-SET", key="alpha", value=2), OPS[1]),
        }
        for field, value in changed.items():
            with self.subTest(field=field):
                self.assertNotEqual(default_spec(**{field: value}).digest, base)

    def test_operation_order_is_part_of_the_identity(self):
        self.assertNotEqual(default_spec(operations=tuple(reversed(OPS))).digest, default_spec().digest)

    def test_the_basis_contains_no_derived_or_mutable_field(self):
        basis = default_spec().basis()
        self.assertNotIn("digest", basis)
        self.assertEqual(basis["schema"], "cerebro-controlled-effect-batch-spec/v1")
        self.assertEqual([o["name"] for o in basis["operations"]], ["SYNTH-OP-SET", "SYNTH-OP-TAG"])

    def test_malformed_specs_are_refused_not_normalized(self):
        with self.assertRaises(BatchSpecError):
            Operation.of("SYNTH-OP-SET", value=1.5)  # floats have no canonical text
        with self.assertRaises(BatchSpecError):
            Operation.of("bad name with spaces")
        with self.assertRaises(BatchSpecError):
            default_spec(operations=())
        with self.assertRaises(BatchSpecError):
            default_spec(actor_generation=True)  # bool is not an int here
        with self.assertRaises(BatchSpecError):
            default_spec(delegation_revision=0)
        with self.assertRaises(BatchSpecError):
            default_spec(idempotency_key="")
        with self.assertRaises(BatchSpecError):
            default_spec(operations=[OPS[0]])  # list, not tuple

    def test_round_trip_through_the_canonical_basis(self):
        spec = default_spec(rollback_ref="SYNTH-ROLLBACK-1")
        self.assertEqual(BatchSpec.from_mapping(spec.basis()), spec)


if __name__ == "__main__":
    unittest.main()
