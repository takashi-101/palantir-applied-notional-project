"""ontology_engine.py のテスト。実行：python3 -m unittest discover -s tests -v"""
import contextlib
import io
import json
import pathlib
import re
import sqlite3
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ontology_engine as oe  # noqa: E402


def new_engine(case: unittest.TestCase) -> oe.Engine:
    eng = oe.Engine.create(clock=oe.stepping_clock())
    case.addCleanup(eng.close)
    return eng


class SeedTest(unittest.TestCase):
    def test_counts_match_the_page(self):
        eng = new_engine(self)
        self.assertEqual((eng.count("Vehicle"), eng.count("Issue"), eng.count("Staff")), (18, 13, 3))
        self.assertEqual((eng.count("issue_vehicle"), eng.count("issue_assignee")), (13, 0))

    def test_every_issue_points_to_an_existing_vehicle(self):
        objects, links = oe.seed()
        self.assertEqual(set(links["issue_vehicle"]), set(objects["Issue"]))
        self.assertTrue(set(links["issue_vehicle"].values()) <= set(objects["Vehicle"]))

    def test_seed_values_fit_the_declared_types(self):
        objects, _ = oe.seed()
        for type_name, rows in objects.items():
            props = oe.OBJECT_TYPES[type_name].props
            for obj_id, obj in rows.items():
                self.assertEqual(set(obj), set(props), obj_id)
                for name, spec in props.items():
                    self.assertIsInstance(obj[name], int if spec.kind == "int" else str, f"{obj_id}.{name}")
                    if spec.enum:
                        self.assertIn(obj[name], spec.enum, f"{obj_id}.{name}")


class ParameterTest(unittest.TestCase):
    def setUp(self):
        self.eng = new_engine(self)

    def code(self, *args, **kwargs):
        return self.eng.submit(*args, **kwargs).code

    def test_unknown_action(self):
        self.assertEqual(self.code("S-01", "delete_everything", {}), "UNKNOWN_ACTION")

    def test_unknown_actor(self):
        self.assertEqual(self.code("S-99", "reopen_issue", {"issue_id": "I-113"}, "x"), "UNKNOWN_ACTOR")

    def test_missing_and_extra_params(self):
        self.assertEqual(self.code("S-01", "assign_issue", {"issue_id": "I-104"}), "BAD_PARAMS")
        self.assertEqual(self.code("S-01", "assign_issue",
                                   {"issue_id": "I-104", "staff_id": "S-02", "status": "解決済"}), "BAD_PARAMS")

    def test_params_must_be_a_mapping_of_strings(self):
        self.assertEqual(self.code("S-01", "assign_issue", ["I-104", "S-02"]), "BAD_PARAMS")
        self.assertEqual(self.code("S-01", "assign_issue", {"issue_id": 104, "staff_id": "S-02"}), "BAD_VALUE")

    def test_enum_and_reference(self):
        self.assertEqual(self.code("S-01", "set_vehicle_status", {"vehicle_id": "C-001", "status": "廃車"}), "BAD_VALUE")
        self.assertEqual(self.code("S-01", "set_vehicle_status", {"vehicle_id": "C-999", "status": "待機"}), "NOT_FOUND")

    def test_too_long_text(self):
        self.eng.submit("S-02", "assign_issue", {"issue_id": "I-104", "staff_id": "S-02"})
        self.assertEqual(self.code("S-02", "resolve_issue", {"issue_id": "I-104", "memo": "あ" * 201}), "TOO_LONG")

    def test_sql_text_in_a_parameter_is_just_a_value(self):
        res = self.eng.submit("S-01", "set_vehicle_status",
                              {"vehicle_id": "C-001'; DROP TABLE objects;--", "status": "待機"})
        self.assertEqual(res.code, "NOT_FOUND")
        self.assertEqual(self.eng.count("Vehicle"), 18)


class RuleTest(unittest.TestCase):
    def setUp(self):
        self.eng = new_engine(self)

    def test_assignment_respects_capacity(self):
        for issue in ("I-104", "I-105"):
            self.assertTrue(self.eng.submit("S-02", "assign_issue", {"issue_id": issue, "staff_id": "S-02"}).ok)
        res = self.eng.submit("S-02", "assign_issue", {"issue_id": "I-109", "staff_id": "S-02"})
        self.assertEqual(res.code, "OVER_CAPACITY")
        self.eng.submit("S-02", "resolve_issue", {"issue_id": "I-105", "memo": "交換して確認"})
        self.assertTrue(self.eng.submit("S-02", "assign_issue", {"issue_id": "I-109", "staff_id": "S-02"}).ok)

    def test_only_mechanics_can_be_assignees(self):
        res = self.eng.submit("S-01", "assign_issue", {"issue_id": "I-104", "staff_id": "S-01"})
        self.assertEqual(res.code, "NOT_MECHANIC")

    def test_resolve_needs_an_assignee_and_mechanic_resolves_only_own(self):
        self.assertEqual(self.eng.submit("S-01", "resolve_issue", {"issue_id": "I-104", "memo": "対応"}).code, "NO_ASSIGNEE")
        self.eng.submit("S-01", "assign_issue", {"issue_id": "I-104", "staff_id": "S-03"})
        self.assertEqual(self.eng.submit("S-02", "resolve_issue", {"issue_id": "I-104", "memo": "対応"}).code, "NOT_YOURS")
        res = self.eng.submit("S-03", "resolve_issue", {"issue_id": "I-104", "memo": "パッドを交換"})
        self.assertTrue(res.ok)
        self.assertEqual([c["field"] for c in res.changes], ["status", "memo"])
        self.assertEqual(self.eng.submit("S-03", "resolve_issue", {"issue_id": "I-104", "memo": "再度"}).code, "NOT_OPEN")

    def test_severity_can_only_go_up_and_needs_a_reason(self):
        params = {"issue_id": "I-102", "severity": "高"}
        self.assertEqual(self.eng.submit("S-02", "raise_severity", params).code, "REASON_REQUIRED")
        self.assertTrue(self.eng.submit("S-02", "raise_severity", params, "異音が大きくなった").ok)
        self.assertEqual(self.eng.submit("S-02", "raise_severity", {"issue_id": "I-102", "severity": "中"}, "戻す").code,
                         "NOT_HIGHER")

    def test_vehicle_with_open_high_issue_cannot_be_leased(self):
        res = self.eng.submit("S-01", "set_vehicle_status", {"vehicle_id": "C-008", "status": "貸出中"})
        self.assertEqual(res.code, "BLOCKED_BY_ISSUE")
        self.assertIn("I-105", res.message)
        self.assertTrue(self.eng.submit("S-01", "set_vehicle_status", {"vehicle_id": "C-004", "status": "貸出中"}).ok)
        self.assertEqual(self.eng.submit("S-01", "set_vehicle_status", {"vehicle_id": "C-004", "status": "貸出中"}).code,
                         "NO_CHANGE")

    def test_reopen_is_for_managers_with_a_reason(self):
        params = {"issue_id": "I-113"}
        self.assertEqual(self.eng.submit("S-03", "reopen_issue", params, "再発").code, "FORBIDDEN")
        self.assertEqual(self.eng.submit("S-01", "reopen_issue", params).code, "REASON_REQUIRED")
        self.assertTrue(self.eng.submit("S-01", "reopen_issue", params, "再発の連絡").ok)
        self.assertEqual(self.eng.submit("S-01", "reopen_issue", params, "もう一度").code, "NOT_RESOLVED")

    def test_a_rejected_action_changes_nothing(self):
        before = self.eng._state_hash()
        self.eng.submit("S-01", "set_vehicle_status", {"vehicle_id": "C-008", "status": "貸出中"})
        self.assertEqual(self.eng._state_hash(), before)


class ProposalTest(unittest.TestCase):
    def setUp(self):
        self.eng = new_engine(self)
        self.params = {"issue_id": "I-109", "staff_id": "S-03"}

    def test_ai_cannot_apply_directly(self):
        before = self.eng._state_hash()
        res = self.eng.submit("AI-01", "assign_issue", self.params)
        self.assertEqual((res.ok, res.code), (False, "AI_MUST_PROPOSE"))
        self.assertEqual(self.eng._state_hash(), before)

    def test_proposal_needs_a_reason_and_does_not_change_state(self):
        before = self.eng._state_hash()
        self.assertEqual(self.eng.propose("AI-01", "assign_issue", self.params, "").code, "REASON_REQUIRED")
        res = self.eng.propose("AI-01", "assign_issue", self.params, "重要度が高い")
        self.assertEqual((res.event, res.proposal_id), ("PROPOSED", 1))
        self.assertEqual(self.eng._state_hash(), before)

    def test_approval_applies_and_records_both_people(self):
        pid = self.eng.propose("AI-01", "assign_issue", self.params, "重要度が高い").proposal_id
        res = self.eng.approve("S-01", pid)
        self.assertTrue(res.ok)
        last = self.eng.audit_rows()[-1]
        self.assertEqual((last["event"], last["actor"], last["proposer"], last["proposal_id"]),
                         ("APPLIED", "S-01", "AI-01", pid))
        self.assertEqual(self.eng.approve("S-01", pid).code, "NO_PENDING_PROPOSAL")

    def test_ai_cannot_approve_and_nobody_approves_their_own(self):
        pid = self.eng.propose("AI-01", "assign_issue", self.params, "重要度が高い").proposal_id
        self.assertEqual(self.eng.approve("AI-01", pid).code, "HUMAN_REQUIRED")
        own = self.eng.propose("S-02", "assign_issue", {"issue_id": "I-104", "staff_id": "S-02"}, "手が空いた").proposal_id
        self.assertEqual(self.eng.approve("S-02", own).code, "SELF_APPROVAL")
        self.assertTrue(self.eng.approve("S-03", own).ok)

    def test_approver_needs_the_role_for_that_action(self):
        self.eng.submit("S-01", "assign_issue", {"issue_id": "I-101", "staff_id": "S-02"})
        self.eng.submit("S-02", "resolve_issue", {"issue_id": "I-101", "memo": "空気圧を調整"})
        pid = self.eng.propose("AI-01", "reopen_issue", {"issue_id": "I-101"}, "警告が再表示").proposal_id
        self.assertEqual(self.eng.approve("S-02", pid).code, "FORBIDDEN")
        self.assertTrue(self.eng.approve("S-01", pid).ok)

    def test_rules_are_checked_again_at_approval(self):
        pid = self.eng.propose("AI-01", "assign_issue", self.params, "重要度が高い").proposal_id
        self.eng.submit("S-01", "assign_issue", self.params)          # 承認の前に、人が同じ割当を済ませた
        self.assertEqual(self.eng.approve("S-01", pid).code, "NO_CHANGE")

    def test_decline_keeps_a_reason(self):
        pid = self.eng.propose("AI-01", "assign_issue", self.params, "重要度が高い").proposal_id
        self.assertEqual(self.eng.decline("S-01", pid, "").code, "REASON_REQUIRED")
        res = self.eng.decline("S-01", pid, "S-03 は来週から休暇")
        self.assertEqual(res.event, "DECLINED")
        self.assertEqual(self.eng.approve("S-01", pid).code, "NO_PENDING_PROPOSAL")
        self.assertIsNone(self.eng._dst("issue_assignee", "I-109"))


class ReadTest(unittest.TestCase):
    def test_marked_property_is_masked_for_roles_without_clearance(self):
        eng = new_engine(self)
        self.assertEqual(eng.read("S-01", "Staff", "S-02")["name"], "見本 花子")
        masked = eng.read("AI-01", "Staff", "S-02")
        self.assertEqual(masked["name"], oe.MASK)
        self.assertEqual(masked["team"], "整備")

    def test_unknown_reader_or_object(self):
        eng = new_engine(self)
        with self.assertRaises(oe.Rejected):
            eng.read("S-99", "Staff", "S-02")
        with self.assertRaises(oe.Rejected):
            eng.read("S-01", "Staff", "S-99")


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.eng = new_engine(self)
        self.eng.submit("S-02", "assign_issue", {"issue_id": "I-104", "staff_id": "S-02"})
        self.eng.submit("S-01", "set_vehicle_status", {"vehicle_id": "C-008", "status": "貸出中"})

    def test_rejections_are_logged_and_chain_verifies(self):
        rows = self.eng.audit_rows()
        self.assertEqual([r["event"] for r in rows], ["GENESIS", "APPLIED", "REJECTED"])
        self.assertEqual(rows[1]["changes"], [{"object": "Issue:I-104", "field": "issue_assignee",
                                              "before": None, "after": "S-02"}])
        self.assertEqual(rows[2]["prev_hash"], rows[1]["entry_hash"])
        self.assertEqual(self.eng.verify(), [])

    def test_malformed_input_is_still_logged(self):
        res = self.eng.submit(None, 123, object())
        self.assertEqual(res.code, "UNKNOWN_ACTOR")
        self.assertEqual(self.eng.audit_rows()[-1]["role"], "unknown")
        self.assertEqual(self.eng.verify(), [])

    def test_audit_log_is_append_only(self):
        with self.assertRaises(sqlite3.DatabaseError):
            self.eng._conn.execute("UPDATE audit_log SET actor='S-01' WHERE seq=2")
        with self.assertRaises(sqlite3.DatabaseError):
            self.eng._conn.execute("DELETE FROM audit_log WHERE seq=3")

    def test_tampered_log_is_detected_even_without_the_triggers(self):
        conn = self.eng._conn
        conn.execute("DROP TRIGGER audit_log_no_update")
        with conn:
            conn.execute("UPDATE audit_log SET actor='S-03' WHERE seq=2")
        self.assertTrue(any("2 件目" in p for p in self.eng.verify()))

    def test_removed_entry_is_detected(self):
        conn = self.eng._conn
        conn.execute("DROP TRIGGER audit_log_no_delete")
        with conn:
            conn.execute("DELETE FROM audit_log WHERE seq=2")
        problems = self.eng.verify()
        self.assertTrue(any("通し番号" in p for p in problems))

    def test_change_that_bypassed_actions_is_detected(self):
        conn = self.eng._conn
        with conn:
            conn.execute("UPDATE objects SET props=? WHERE obj_type='Vehicle' AND obj_id='C-001'",
                         (oe._canon({"make": "トヨタ", "model": "プリウス", "year": 2019, "km": 1, "status": "貸出中"}),))
        self.assertTrue(any("アクションを通さない変更" in p for p in self.eng.verify()))


class ToolAndDemoTest(unittest.TestCase):
    def test_tool_definitions_cover_every_action(self):
        tools = oe.tool_definitions()
        self.assertEqual([t["name"] for t in tools], list(oe.ACTIONS))
        for tool in tools:
            schema = tool["input_schema"]
            self.assertFalse(schema["additionalProperties"])
            self.assertIn("reason", schema["required"])
            self.assertEqual(set(schema["required"]), set(schema["properties"]))
        json.dumps(tools)

    def test_demo_is_reproducible_and_matches_the_readme(self):
        outputs = []
        for _ in range(2):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(oe.demo(), 0)
            outputs.append(buf.getvalue().strip())
        self.assertEqual(outputs[0], outputs[1])
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        block = re.search(r"<!-- demo:start -->\s*```text\n(.*?)\n```\s*<!-- demo:end -->", readme, re.S)
        self.assertIsNotNone(block, "README に実行結果のブロックが無い")
        self.assertEqual(block.group(1).strip(), outputs[0])

    def test_db_file_persists_and_existing_file_is_refused(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fleet.db")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(oe.main(["--db", path]), 0)
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], 13)
            conn.close()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(oe.main(["--db", path]), 2)


if __name__ == "__main__":
    unittest.main()
