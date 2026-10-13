"""Focused metadata-only tests of actual method resolver and Source fingerprint chain."""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
BASE = "5bf7aeb33366ab4b707dafe3586c1bb50c63c5e1"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


source = module("human_method_source", ROOT / "mcp/boot_birth_source.py")
HARNESS = r"""
param([string]$Root,[string]$Head,[string]$Mode,[string]$Observation)
$ErrorActionPreference='Stop'
$t=$null;$e=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $Root 'tooling/runtime-host/cerebro_boot.ps1'),[ref]$t,[ref]$e)
if($e.Count){throw 'PARSE_FAILED'}
$names=@('Get-CerebroBootSha256Text','Resolve-CerebroHumanInteractionMethod','Resolve-CerebroCivilizationMethod','New-CerebroAwakeningReceipt')
foreach($f in $ast.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -in $names},$true)){Invoke-Expression $f.Extent.Text}
try {
 switch($Mode) {
 'WORKER' {$r=Resolve-CerebroCivilizationMethod -SourceRoot $Root -SourceHead $Head -ActorRole WORKER}
 'UNBOUND' {$r=Resolve-CerebroCivilizationMethod -SourceRoot $Root -SourceHead $Head -ActorRole PRE_ROLE_UNBOUND -MethodProfileOnly}
 'OBSERVATION' {
 $o=Get-Content -Raw -LiteralPath $Observation | ConvertFrom-Json
 $r=Resolve-CerebroCivilizationMethod -SourceRoot $Root -SourceHead $Head -ActorRole PRINCIPAL -HumanInteractionBasis $o
 }
 'AWAKENING' {
 $b=Resolve-CerebroCivilizationMethod -SourceRoot $Root -SourceHead $Head -ActorRole PRINCIPAL
 $r=New-CerebroAwakeningReceipt -Binding $b -SourceRoot $Root -SourceHead $Head -ActorRole PRINCIPAL
 }
 'STALE_READBACK' {
 $b=Resolve-CerebroCivilizationMethod -SourceRoot $Root -SourceHead $Head -ActorRole PRINCIPAL
 $b.method_profile.fingerprint='0'*64
 $r=New-CerebroAwakeningReceipt -Binding $b -SourceRoot $Root -SourceHead $Head -ActorRole PRINCIPAL
 }
 default {$r=Resolve-CerebroCivilizationMethod -SourceRoot $Root -SourceHead $Head -ActorRole PRINCIPAL}
 }
 $r|ConvertTo-Json -Depth 16 -Compress
} catch { @{error=$_.Exception.Message}|ConvertTo-Json -Compress;exit 2 }
"""


class HumanMethod(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.temp.name) / "source"
        cls.fixture.mkdir()
        names = set(source.REFS) | {source.OWNER, "standards/principalstambok.yaml",
              "engines/context/control-context-state.schema.json",
              "tooling/runtime-host/cerebro_boot.ps1"}
        for name in names:
            p = cls.fixture / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes((ROOT / name).read_bytes())
        def git(*args):
            p = subprocess.run(["git", "-C", str(cls.fixture), *args], capture_output=True)
            if p.returncode:
                raise RuntimeError(p.stderr.decode())
            return p.stdout
        cls.git = staticmethod(git)
        git("init", "-q")
        git("-c", "core.autocrlf=false", "add", ".")
        git("-c", "user.name=Offline Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "-qm", "public method metadata fixture")
        cls.head = git("rev-parse", "HEAD").decode().strip()
        cls.harness = Path(cls.temp.name) / "harness.ps1"
        cls.harness.write_text(HARNESS, encoding="utf-8")
        cls.profile, cls.material, cls.fingerprint = source._method_contract_at_head(
            cls.head, cls.fetch)
        cls.role = source.verify_role_method_projection(cls.head, "PRINCIPAL", fetch=cls.fetch)
        old_code = subprocess.run(["git", "-C", str(ROOT), "show", BASE + ":mcp/boot_birth_source.py"], capture_output=True, check=True).stdout
        old_path = Path(cls.temp.name) / "old_boot_birth_source.py"
        old_path.write_bytes(old_code)
        old_source = module("human_method_old_source", old_path)
        def old_fetch(url):
            name = url.split(cls.head + "/", 1)[1]
            return subprocess.run(["git", "-C", str(ROOT), "show", BASE + ":" + name], capture_output=True, check=True).stdout
        _, _, cls.old_fingerprint = old_source._method_contract_at_head(cls.head, old_fetch)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def fetch(cls, url):
        name = url.split(cls.head + "/", 1)[1]
        return (cls.fixture / name).read_bytes()

    def invoke(self, mode="PRINCIPAL", observation=None):
        cmd = [PS, "-NoProfile", "-File", str(self.harness), "-Root",
               str(self.fixture), "-Head", self.head, "-Mode", mode]
        if observation is not None:
            p = Path(self.temp.name) / "observation.json"
            p.write_text(json.dumps(observation), encoding="utf-8")
            cmd += ["-Observation", str(p)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode, json.loads(r.stdout)

    def observation(self, status="INITIAL_OR_UNCONFIRMED"):
        consumption = {"INITIAL_OR_UNCONFIRMED": "FULL_RELEVANT_CONSUMED",
             "UNCHANGED_ACCEPTED": "REUSED_ACCEPTED_BASIS",
             "DELTA_OR_CONTEXT_BREAK": "TARGETED_DELTA_CONSUMED"}[status]
        refs = {"living_family": "gdocs:1d6YzpBu8b3t_497bUsL_dlvlRtBMZgHtr6_lmwYiJrE",
                "shared_base": "gdocs:1hqT7XWKXxLar9GsFxyAQE1g5vwhuTQL0LUoN2idLVzQ",
                "actor_family": "fixture:own-family", "red_thread": "fixture:own-front"}
        return {"schema": "cerebro-human-method-basis/v1",
                "method_fingerprint": self.fingerprint, "basis_status": status,
                "sources": {k: {"source_ref": ref, "revision": "fixture:r1",
                    "consumption": consumption, "evidence_ref": "fixture:read-" + k}
                    for k, ref in refs.items()},
                "uptake": {"state": "NOT_ATTESTED", "evidence_ref": None},
                "behavior": {"state": "NOT_OBSERVED", "evidence_ref": None}}

    def test_selected_ps_and_python_fingerprint_role_chain_agree(self):
        code, result = self.invoke()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["method_profile"]["fingerprint"], self.fingerprint)
        self.assertEqual(result["role_method_projection"]["role_contract_fingerprint"],
                         self.role["contract_fingerprint"])
        self.assertIn("standards/human-interaction-method.yaml", self.role["contract_refs"])

    def test_missing_basis_names_exact_local_gap_not_birth_failure(self):
        code, result = self.invoke()
        self.assertEqual(code, 0)
        h = result["role_method_projection"]["human_interaction"]
        self.assertEqual(h["missing_basis"], ["living_family", "shared_base", "actor_family", "red_thread"])
        self.assertEqual(h["uptake"], "NOT_ATTESTED")
        self.assertFalse(h["runtime_cognition_proven"])

    def test_first_reuse_and_targeted_delta_modes_remain_reports(self):
        for mode in ("INITIAL_OR_UNCONFIRMED", "UNCHANGED_ACCEPTED", "DELTA_OR_CONTEXT_BREAK"):
            code, result = self.invoke("OBSERVATION", self.observation(mode))
            self.assertEqual(code, 0, result)
            h = result["role_method_projection"]["human_interaction"]
            self.assertEqual(h["missing_basis"], [])
            self.assertEqual(h["uptake"], "NOT_ATTESTED")
            self.assertEqual(h["observable_behavior"], "NOT_OBSERVED")
            self.assertFalse(h["source_consumption_attested"])

    def test_old_fingerprint_refused_for_changed_method(self):
        observation = self.observation()
        self.assertNotEqual(self.old_fingerprint, self.fingerprint)
        observation["method_fingerprint"] = self.old_fingerprint
        code, result = self.invoke("OBSERVATION", observation)
        self.assertEqual(code, 2)
        self.assertEqual(result["error"], "HUMAN_METHOD_BASIS_BINDING_MISMATCH")

    def test_actor_local_missing_and_positive_claim_refused(self):
        observation = self.observation()
        del observation["sources"]["actor_family"]
        code, result = self.invoke("OBSERVATION", observation)
        self.assertEqual(code, 0)
        self.assertEqual(result["role_method_projection"]["human_interaction"]["missing_basis"], ["actor_family"])
        observation["uptake"] = {"state": "USED", "evidence_ref": "fixture:uptake"}
        code, result = self.invoke("OBSERVATION", observation)
        self.assertEqual(code, 2)
        self.assertEqual(result["error"], "HUMAN_METHOD_OBSERVATION_BASIS_REQUIRED")

    def test_caller_used_and_behavior_do_not_attest_cognition(self):
        observation = self.observation()
        observation["uptake"] = {"state": "USED", "evidence_ref": "fixture:uptake"}
        observation["behavior"] = {"state": "OBSERVED", "evidence_ref": "fixture:behavior"}
        code, result = self.invoke("OBSERVATION", observation)
        self.assertEqual(code, 0, result)
        h = result["role_method_projection"]["human_interaction"]
        self.assertEqual(h["uptake"], "USED")
        self.assertFalse(h["runtime_cognition_proven"])
        self.assertFalse(h["identity_attested"])

    def test_worker_and_role_neutral_projection_have_no_human_persona_gate(self):
        for mode in ("WORKER", "UNBOUND"):
            code, result = self.invoke(mode)
            self.assertEqual(code, 0, result)
            self.assertNotIn("human_interaction", result.get("role_method_projection", {}))
            self.assertEqual(result["method_profile"]["authority"], "NONE")

    def test_downstream_receipt_recomputes_and_refuses_stale_binding(self):
        code, result = self.invoke("AWAKENING")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["method_profile"]["fingerprint"], self.fingerprint)
        self.assertFalse(result["role_method_projection"]["human_interaction"]["runtime_cognition_proven"])
        code, result = self.invoke("STALE_READBACK")
        self.assertEqual(code, 2)
        self.assertEqual(result["error"], "CIVILIZATION_METHOD_BINDING_MISMATCH")

    def test_private_body_fields_not_accepted_as_metadata(self):
        observation = self.observation()
        observation["sources"]["actor_family"]["private_body"] = "synthetic forbidden field"
        code, result = self.invoke("OBSERVATION", observation)
        self.assertEqual(code, 2)
        self.assertEqual(result["error"], "HUMAN_METHOD_SOURCE_FIELDS_INVALID")


if __name__ == "__main__":
    unittest.main(verbosity=2)
