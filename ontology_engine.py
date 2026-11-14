#!/usr/bin/env python3
"""ontology_engine.py

オントロジー（オブジェクト型・リンク型・アクション型）の考え方を、
Python の標準ライブラリと SQLite だけで確かめる小さなエンジン。

題材は、このリポジトリの index.html と同じ架空データ
（中古車リースの車両18台・不具合13件）に、架空の担当者3名を足したもの。

状態を書き換える入口は submit / propose / approve / decline の4つだけで、
どれも「引数の確認 → 権限の確認 → 提出条件の確認 → 反映 → 監査ログ」を通る。
AIエージェントは propose（提案）しかできず、反映には人の承認が要る。

外部への通信は行わない。実行：python3 ontology_engine.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

JST = timezone(timedelta(hours=9))
MAX_TEXT = 200          # 引数と理由の文字数の上限
MASK = "＊＊＊"          # 見る資格のない項目に入れる伏せ字
ZERO_HASH = "0" * 64


# --------------------------------------------------------------------------
# オントロジーの定義（型）
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Prop:
    kind: str                      # "str" または "int"
    enum: tuple[str, ...] = ()
    marking: str = ""              # "PII" など。資格のない役割には伏せる


@dataclass(frozen=True)
class ObjectType:
    name: str
    label: str
    props: dict[str, Prop]


@dataclass(frozen=True)
class LinkType:
    name: str
    label: str
    src: str                       # 多の側
    dst: str                       # 一の側
    editable: bool                 # アクションで付け替えられるか


@dataclass(frozen=True)
class Param:
    label: str
    ref: str = ""                  # オブジェクト型の名前（実在の確認をする）
    enum: tuple[str, ...] = ()


@dataclass(frozen=True)
class Change:
    target: str                    # "prop" または "link"
    type_name: str                 # オブジェクト型またはリンク型の名前
    obj_id: str                    # 対象（リンクは多の側）のID
    field: str
    value: Any


@dataclass(frozen=True)
class ActionType:
    name: str
    label: str
    params: dict[str, Param]
    roles: tuple[str, ...]         # 実行・承認できる役割
    needs_reason: bool             # 理由の入力を必ず求めるか
    rule: Callable[["Engine", str, str, dict], list[Change]]


@dataclass
class Result:
    ok: bool
    event: str                     # APPLIED / REJECTED / PROPOSED / DECLINED
    code: str
    message: str
    seq: int
    proposal_id: int | None = None
    changes: list[dict] = field(default_factory=list)


class Rejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


OBJECT_TYPES = {
    "Vehicle": ObjectType("Vehicle", "車両", {
        "make": Prop("str"), "model": Prop("str"), "year": Prop("int"), "km": Prop("int"),
        "status": Prop("str", ("貸出中", "待機", "整備中")),
    }),
    "Issue": ObjectType("Issue", "不具合", {
        "title": Prop("str"), "severity": Prop("str", ("高", "中", "低")),
        "received": Prop("str"), "status": Prop("str", ("未解決", "解決済")), "memo": Prop("str"),
    }),
    "Staff": ObjectType("Staff", "担当者", {
        "name": Prop("str", marking="PII"), "team": Prop("str"),
        "role": Prop("str", ("manager", "mechanic")), "capacity": Prop("int"),
    }),
}

LINK_TYPES = {
    "issue_vehicle": LinkType("issue_vehicle", "不具合→車両", "Issue", "Vehicle", editable=False),
    "issue_assignee": LinkType("issue_assignee", "不具合→担当者", "Issue", "Staff", editable=True),
}

ROLE_LABEL = {"manager": "マネージャー", "mechanic": "整備担当", "ai_agent": "AIエージェント"}
CLEARANCE = {"manager": {"PII"}, "mechanic": {"PII"}, "ai_agent": set()}
SEVERITY_ORDER = {"低": 0, "中": 1, "高": 2}


# --------------------------------------------------------------------------
# アクションごとの提出条件（今の状態を読んで、変更内容を返すか、却下する）
# --------------------------------------------------------------------------
def _open_issue(eng: "Engine", issue_id: str) -> dict:
    issue = eng._get("Issue", issue_id)
    if issue["status"] != "未解決":
        raise Rejected("NOT_OPEN", f"{issue_id} は解決済のため、この操作はできない")
    return issue


def _rule_assign(eng: "Engine", actor: str, role: str, p: dict) -> list[Change]:
    _open_issue(eng, p["issue_id"])
    staff = eng._get("Staff", p["staff_id"])
    if staff["role"] != "mechanic":
        raise Rejected("NOT_MECHANIC", "担当にできるのは整備担当だけ")
    if eng._dst("issue_assignee", p["issue_id"]) == p["staff_id"]:
        raise Rejected("NO_CHANGE", "すでにその担当者に割り当て済み")
    open_now = [i for i in eng._srcs("issue_assignee", p["staff_id"])
                if eng._get("Issue", i)["status"] == "未解決"]
    if len(open_now) >= staff["capacity"]:
        raise Rejected("OVER_CAPACITY",
                       f"{p['staff_id']} は担当中の未解決が{len(open_now)}件で、上限（{staff['capacity']}件）に達している")
    return [Change("link", "issue_assignee", p["issue_id"], "issue_assignee", p["staff_id"])]


def _rule_raise(eng: "Engine", actor: str, role: str, p: dict) -> list[Change]:
    issue = _open_issue(eng, p["issue_id"])
    if SEVERITY_ORDER[p["severity"]] <= SEVERITY_ORDER[issue["severity"]]:
        raise Rejected("NOT_HIGHER", f"重要度は引き上げだけできる（現在は「{issue['severity']}」）")
    return [Change("prop", "Issue", p["issue_id"], "severity", p["severity"])]


def _rule_resolve(eng: "Engine", actor: str, role: str, p: dict) -> list[Change]:
    _open_issue(eng, p["issue_id"])
    assignee = eng._dst("issue_assignee", p["issue_id"])
    if assignee is None:
        raise Rejected("NO_ASSIGNEE", "担当者が決まっていない不具合は解決済にできない")
    if role == "mechanic" and assignee != actor:
        raise Rejected("NOT_YOURS", "整備担当が解決済にできるのは、自分が担当の不具合だけ")
    return [Change("prop", "Issue", p["issue_id"], "status", "解決済"),
            Change("prop", "Issue", p["issue_id"], "memo", p["memo"])]


def _rule_reopen(eng: "Engine", actor: str, role: str, p: dict) -> list[Change]:
    if eng._get("Issue", p["issue_id"])["status"] != "解決済":
        raise Rejected("NOT_RESOLVED", f"{p['issue_id']} は未解決のため、戻す対象ではない")
    return [Change("prop", "Issue", p["issue_id"], "status", "未解決")]


def _rule_vehicle_status(eng: "Engine", actor: str, role: str, p: dict) -> list[Change]:
    vehicle = eng._get("Vehicle", p["vehicle_id"])
    if vehicle["status"] == p["status"]:
        raise Rejected("NO_CHANGE", f"すでに「{p['status']}」になっている")
    if p["status"] == "貸出中":
        blocking = [i for i in eng._srcs("issue_vehicle", p["vehicle_id"])
                    if eng._get("Issue", i)["status"] == "未解決" and eng._get("Issue", i)["severity"] == "高"]
        if blocking:
            raise Rejected("BLOCKED_BY_ISSUE",
                           f"未解決で重要度「高」の不具合（{'、'.join(blocking)}）がある車両は貸出中にできない")
    return [Change("prop", "Vehicle", p["vehicle_id"], "status", p["status"])]


ACTIONS = {a.name: a for a in (
    ActionType("assign_issue", "不具合に担当者を割り当てる",
               {"issue_id": Param("不具合ID", ref="Issue"), "staff_id": Param("担当者ID", ref="Staff")},
               ("manager", "mechanic"), False, _rule_assign),
    ActionType("raise_severity", "不具合の重要度を引き上げる",
               {"issue_id": Param("不具合ID", ref="Issue"), "severity": Param("重要度", enum=("高", "中", "低"))},
               ("manager", "mechanic"), True, _rule_raise),
    ActionType("resolve_issue", "不具合を解決済にする",
               {"issue_id": Param("不具合ID", ref="Issue"), "memo": Param("対応の記録")},
               ("manager", "mechanic"), False, _rule_resolve),
    ActionType("reopen_issue", "解決済の不具合を未解決に戻す",
               {"issue_id": Param("不具合ID", ref="Issue")},
               ("manager",), True, _rule_reopen),
    ActionType("set_vehicle_status", "車両の状態を変える",
               {"vehicle_id": Param("車両ID", ref="Vehicle"), "status": Param("状態", enum=("貸出中", "待機", "整備中"))},
               ("manager",), False, _rule_vehicle_status),
)}


# --------------------------------------------------------------------------
# 架空データ（index.html の「データの掃除」を終えた後と同じ内容）
# --------------------------------------------------------------------------
_VEHICLES = """
C-001 トヨタ プリウス 2019 48210 貸出中|C-002 トヨタ アクア 2020 31200 待機|C-003 ホンダ フィット 2018 72900 貸出中
C-004 ホンダ N-BOX 2021 19880 整備中|C-005 日産 ノート 2019 55430 貸出中|C-006 日産 セレナ 2017 98120 貸出中
C-007 マツダ CX-5 2020 40050 待機|C-008 マツダ デミオ 2016 110300 整備中|C-009 スバル インプレッサ 2019 61700 貸出中
C-010 スズキ スペーシア 2022 12400 貸出中|C-011 スズキ ハスラー 2021 23900 待機|C-012 トヨタ ハイエース 2018 134550 貸出中
C-013 ダイハツ タント 2020 28300 貸出中|C-014 ダイハツ ムーヴ 2017 86020 整備中|C-015 トヨタ ヤリス 2022 9870 貸出中
C-016 ホンダ フリード 2019 58400 貸出中|C-017 日産 デイズ 2021 21050 待機|C-018 マツダ CX-3 2018 69930 貸出中
"""
_ISSUES = [
    ("I-101", "C-001", "タイヤの空気圧低下の警告", "中", "2026-10-20", "未解決"),
    ("I-102", "C-003", "エアコンから異音", "中", "2026-10-02", "未解決"),
    ("I-103", "C-004", "定期点検（車検前整備）", "低", "2026-10-15", "未解決"),
    ("I-104", "C-006", "ブレーキの効きが弱い", "高", "2026-10-22", "未解決"),
    ("I-105", "C-008", "オイル漏れ", "高", "2026-10-05", "未解決"),
    ("I-106", "C-009", "フロントガラスの飛び石傷", "低", "2026-09-28", "未解決"),
    ("I-107", "C-012", "走行距離が整備基準を超過", "高", "2026-10-18", "未解決"),
    ("I-108", "C-012", "後部ドアの閉まりが悪い", "中", "2026-10-11", "未解決"),
    ("I-109", "C-014", "バッテリー上がり", "高", "2026-10-21", "未解決"),
    ("I-110", "C-016", "ナビの地図更新", "低", "2026-09-15", "未解決"),
    ("I-111", "C-018", "右後方のこすり傷", "中", "2026-10-09", "未解決"),
    ("I-113", "C-005", "オイル交換時期の通知", "低", "2026-10-01", "解決済"),
    ("I-114", "C-013", "スライドドアの警告灯", "高", "2026-10-23", "未解決"),
]
_STAFF = [
    ("S-01", "架空 一郎", "リース管理", "manager", 0),
    ("S-02", "見本 花子", "整備", "mechanic", 2),
    ("S-03", "例示 次郎", "整備", "mechanic", 3),
]


def seed() -> tuple[dict[str, dict[str, dict]], dict[str, dict[str, str]]]:
    objects: dict[str, dict[str, dict]] = {"Vehicle": {}, "Issue": {}, "Staff": {}}
    links: dict[str, dict[str, str]] = {"issue_vehicle": {}, "issue_assignee": {}}
    for row in _VEHICLES.replace("\n", "|").split("|"):
        if row.strip():
            vid, make, model, year, km, status = row.split()
            objects["Vehicle"][vid] = {"make": make, "model": model, "year": int(year), "km": int(km), "status": status}
    for iid, vid, title, sev, received, status in _ISSUES:
        objects["Issue"][iid] = {"title": title, "severity": sev, "received": received, "status": status, "memo": ""}
        links["issue_vehicle"][iid] = vid
    for sid, name, team, role, capacity in _STAFF:
        objects["Staff"][sid] = {"name": name, "team": team, "role": role, "capacity": capacity}
    return objects, links


# --------------------------------------------------------------------------
# エンジン本体
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE objects (obj_type TEXT NOT NULL, obj_id TEXT NOT NULL, props TEXT NOT NULL,
                      PRIMARY KEY (obj_type, obj_id));
CREATE TABLE links (link_type TEXT NOT NULL, src_id TEXT NOT NULL, dst_id TEXT NOT NULL,
                    PRIMARY KEY (link_type, src_id));
CREATE TABLE proposals (id INTEGER PRIMARY KEY, at TEXT NOT NULL, proposer TEXT NOT NULL,
                        action TEXT NOT NULL, params TEXT NOT NULL, reason TEXT NOT NULL,
                        status TEXT NOT NULL, decided_by TEXT, decided_at TEXT);
CREATE TABLE audit_log (seq INTEGER PRIMARY KEY, at TEXT NOT NULL, event TEXT NOT NULL,
                        actor TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL,
                        params TEXT NOT NULL, reason TEXT NOT NULL, code TEXT NOT NULL,
                        detail TEXT NOT NULL, proposal_id INTEGER, proposer TEXT,
                        changes TEXT NOT NULL, state_hash TEXT NOT NULL,
                        prev_hash TEXT NOT NULL, entry_hash TEXT NOT NULL);
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log は追記のみ（書き換え不可）'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log は追記のみ（削除不可）'); END;
"""
_BODY_KEYS = ("seq", "at", "event", "actor", "role", "action", "params", "reason", "code",
              "detail", "proposal_id", "proposer", "changes", "state_hash")


def _canon(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def stepping_clock(start: str = "2026-10-24T09:00:00+09:00", minutes: int = 5) -> Callable[[], str]:
    """呼ぶたびに一定の間隔で進む時計。実行結果を誰でも同じにするために使う。"""
    state = {"t": datetime.fromisoformat(start)}

    def tick() -> str:
        value = state["t"].isoformat(timespec="seconds")
        state["t"] += timedelta(minutes=minutes)
        return value
    return tick


class Engine:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], str] | None = None,
                 ai_agents: tuple[str, ...] = ("AI-01",)):
        self._conn = conn
        self._clock = clock or _now
        self._ai = frozenset(ai_agents)

    @classmethod
    def create(cls, db_path: str = ":memory:", clock: Callable[[], str] | None = None) -> "Engine":
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA)
        eng = cls(conn, clock)
        objects, links = seed()
        with conn:
            for obj_type, rows in objects.items():
                for obj_id, props in rows.items():
                    conn.execute("INSERT INTO objects VALUES (?,?,?)", (obj_type, obj_id, _canon(props)))
            for link_type, pairs in links.items():
                for src, dst in pairs.items():
                    conn.execute("INSERT INTO links VALUES (?,?,?)", (link_type, src, dst))
            eng._log("GENESIS", "system", "system", "", {}, "", "OK", "初期データの読み込み", None, None, [])
        return eng

    def close(self) -> None:
        self._conn.close()

    # ---- 読み取り（内部用） ----
    def _get(self, obj_type: str, obj_id: str) -> dict | None:
        row = self._conn.execute("SELECT props FROM objects WHERE obj_type=? AND obj_id=?",
                                 (obj_type, obj_id)).fetchone()
        return json.loads(row[0]) if row else None

    def _dst(self, link_type: str, src_id: str) -> str | None:
        row = self._conn.execute("SELECT dst_id FROM links WHERE link_type=? AND src_id=?",
                                 (link_type, src_id)).fetchone()
        return row[0] if row else None

    def _srcs(self, link_type: str, dst_id: str) -> list[str]:
        rows = self._conn.execute("SELECT src_id FROM links WHERE link_type=? AND dst_id=? ORDER BY src_id",
                                  (link_type, dst_id)).fetchall()
        return [r[0] for r in rows]

    def _role(self, actor: Any) -> str:
        if isinstance(actor, str) and actor in self._ai:
            return "ai_agent"
        staff = self._get("Staff", actor) if isinstance(actor, str) else None
        if staff is None:
            raise Rejected("UNKNOWN_ACTOR", "登録のない実行者")
        return staff["role"]

    def _state_hash(self) -> str:
        objects = self._conn.execute("SELECT obj_type, obj_id, props FROM objects ORDER BY obj_type, obj_id").fetchall()
        links = self._conn.execute("SELECT link_type, src_id, dst_id FROM links ORDER BY link_type, src_id").fetchall()
        return _sha(_canon({"objects": objects, "links": links}))

    # ---- 読み取り（外向け）：見る資格のない項目は伏せる ----
    def read(self, actor: str, obj_type: str, obj_id: str) -> dict:
        role = self._role(actor)
        otype = OBJECT_TYPES.get(obj_type)
        obj = self._get(obj_type, obj_id) if otype else None
        if obj is None:
            raise Rejected("NOT_FOUND", f"{obj_type}:{obj_id} は存在しない")
        return {k: (MASK if otype.props[k].marking and otype.props[k].marking not in CLEARANCE[role] else v)
                for k, v in obj.items()}

    def count(self, type_name: str) -> int:
        table, col = ("objects", "obj_type") if type_name in OBJECT_TYPES else ("links", "link_type")
        return self._conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (type_name,)).fetchone()[0]

    # ---- 監査ログ ----
    def _log(self, event: str, actor: str, role: str, action: str, params: dict, reason: str, code: str,
             detail: str, proposal_id: int | None, proposer: str | None, changes: list[dict],
             at: str | None = None) -> int:
        last = self._conn.execute("SELECT seq, entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        seq, prev_hash = (last[0] + 1, last[1]) if last else (1, ZERO_HASH)
        body = dict(zip(_BODY_KEYS, (seq, at or self._clock(), event, actor, role, action, params, reason, code,
                                     detail, proposal_id, proposer, changes, self._state_hash())))
        entry_hash = _sha(prev_hash + _canon(body))
        self._conn.execute(
            "INSERT INTO audit_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (seq, body["at"], event, actor, role, action, _canon(params), reason, code, detail,
             proposal_id, proposer, _canon(changes), body["state_hash"], prev_hash, entry_hash))
        return seq

    def audit_rows(self) -> list[dict]:
        cols = _BODY_KEYS + ("prev_hash", "entry_hash")
        rows = self._conn.execute(f"SELECT {', '.join(cols)} FROM audit_log ORDER BY seq").fetchall()
        out = []
        for row in rows:
            item = dict(zip(cols, row))
            item["params"] = json.loads(item["params"])
            item["changes"] = json.loads(item["changes"])
            out.append(item)
        return out

    def verify(self) -> list[str]:
        """監査ログの連鎖と、現在の状態を確かめる。問題が無ければ空のリストを返す。"""
        problems: list[str] = []
        prev_hash, expected_seq, last_state = ZERO_HASH, 1, None
        for item in self.audit_rows():
            body = {k: item[k] for k in _BODY_KEYS}
            if item["seq"] != expected_seq:
                problems.append(f"監査ログの通し番号が飛んでいる（{expected_seq} があるはずの位置に {item['seq']}）")
                expected_seq = item["seq"]
            if item["prev_hash"] != prev_hash or item["entry_hash"] != _sha(prev_hash + _canon(body)):
                problems.append(f"監査ログの {item['seq']} 件目が、記録した時点の内容と一致しない")
            prev_hash, last_state = item["entry_hash"], item["state_hash"]
            expected_seq += 1
        if last_state is None:
            problems.append("監査ログが空")
        elif last_state != self._state_hash():
            problems.append("現在の状態が、最後の記録と一致しない（アクションを通さない変更がある）")
        return problems

    # ---- 書き換えの入口 ----
    def submit(self, actor: str, action: str, params: dict, reason: str = "") -> Result:
        """人が、アクションを直接実行する。"""
        try:
            act, role = self._gate(actor, action, params, reason, proposing=False)
            if role == "ai_agent":
                raise Rejected("AI_MUST_PROPOSE", "AIエージェントは直接実行できない（提案して、人の承認を受ける）")
            if role not in act.roles:
                raise Rejected("FORBIDDEN", f"{ROLE_LABEL[role]}はこのアクションを実行できない")
            changes = act.rule(self, actor, role, params)
            return self._apply(actor, role, act, params, reason, changes, None, None)
        except Rejected as rej:
            return self._reject(actor, action, params, reason, rej, None)

    def propose(self, actor: str, action: str, params: dict, reason: str) -> Result:
        """アクションを提案する（反映はしない）。AIエージェントはこの入口だけを使える。"""
        try:
            act, role = self._gate(actor, action, params, reason, proposing=True)
            act.rule(self, actor, role, params)          # 今の状態で成り立つ提案だけを受け付ける
            at = self._clock()
            with self._conn:
                cur = self._conn.execute(
                    "INSERT INTO proposals (at, proposer, action, params, reason, status) VALUES (?,?,?,?,?,?)",
                    (at, actor, act.name, _canon(params), reason, "PENDING"))
                pid = cur.lastrowid
                seq = self._log("PROPOSED", actor, role, act.name, params, reason, "OK", "", pid, actor, [], at)
            return Result(True, "PROPOSED", "OK", "", seq, pid)
        except Rejected as rej:
            return self._reject(actor, action, params, reason, rej, None)

    def approve(self, actor: str, proposal_id: int) -> Result:
        """人が提案を承認して反映する。承認した時点の状態で、条件を確かめ直す。"""
        row = self._pending(proposal_id)
        action, params, reason, proposer = row if row else ("", {}, "", None)
        try:
            role = self._role(actor)
            if row is None:
                raise Rejected("NO_PENDING_PROPOSAL", f"承認待ちの提案 {proposal_id} は無い")
            act = ACTIONS[action]
            if role == "ai_agent":
                raise Rejected("HUMAN_REQUIRED", "承認できるのは人だけ")
            if actor == proposer:
                raise Rejected("SELF_APPROVAL", "自分の提案を自分で承認することはできない")
            if role not in act.roles:
                raise Rejected("FORBIDDEN", f"{ROLE_LABEL[role]}はこのアクションを承認できない")
            changes = act.rule(self, actor, role, params)
            return self._apply(actor, role, act, params, reason, changes, proposal_id, proposer)
        except Rejected as rej:
            return self._reject(actor, action, params, reason, rej, proposal_id if row else None)

    def decline(self, actor: str, proposal_id: int, reason: str) -> Result:
        """人が提案を見送る。理由を残す。"""
        row = self._pending(proposal_id)
        action, params, _, proposer = row if row else ("", {}, "", None)
        try:
            role = self._role(actor)
            if row is None:
                raise Rejected("NO_PENDING_PROPOSAL", f"承認待ちの提案 {proposal_id} は無い")
            if role == "ai_agent" or role not in ACTIONS[action].roles:
                raise Rejected("FORBIDDEN", "この提案を見送る権限が無い")
            if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_TEXT:
                raise Rejected("REASON_REQUIRED", "見送る理由が必要（200字以内）")
            at = self._clock()
            with self._conn:
                self._conn.execute("UPDATE proposals SET status='DECLINED', decided_by=?, decided_at=? WHERE id=?",
                                   (actor, at, proposal_id))
                seq = self._log("DECLINED", actor, role, action, params, reason, "OK", "", proposal_id, proposer, [], at)
            return Result(True, "DECLINED", "OK", "", seq, proposal_id)
        except Rejected as rej:
            return self._reject(actor, action, params, reason, rej, proposal_id if row else None)

    # ---- 内部の手続き ----
    def _pending(self, proposal_id: Any) -> tuple[str, dict, str, str] | None:
        if not isinstance(proposal_id, int) or isinstance(proposal_id, bool):
            return None
        row = self._conn.execute("SELECT action, params, reason, proposer FROM proposals WHERE id=? AND status='PENDING'",
                                 (proposal_id,)).fetchone()
        return (row[0], json.loads(row[1]), row[2], row[3]) if row else None

    def _gate(self, actor: Any, action: Any, params: Any, reason: Any, proposing: bool) -> tuple[ActionType, str]:
        """実行者・アクション名・引数・理由を確かめる。"""
        role = self._role(actor)
        act = ACTIONS.get(action) if isinstance(action, str) else None
        if act is None:
            raise Rejected("UNKNOWN_ACTION", "定義されていないアクション")
        if not isinstance(params, dict) or not all(isinstance(k, str) for k in params):
            raise Rejected("BAD_PARAMS", "引数は、名前と値の組で渡す")
        unknown = sorted(set(params) - set(act.params))
        missing = sorted(set(act.params) - set(params))
        if unknown or missing:
            raise Rejected("BAD_PARAMS", f"引数が定義と合わない（不足: {missing or 'なし'}／定義外: {unknown or 'なし'}）")
        for name, spec in act.params.items():
            value = params[name]
            if not isinstance(value, str) or not value.strip():
                raise Rejected("BAD_VALUE", f"{spec.label}は空でない文字列で渡す")
            if len(value) > MAX_TEXT:
                raise Rejected("TOO_LONG", f"{spec.label}は{MAX_TEXT}字以内")
            if spec.enum and value not in spec.enum:
                raise Rejected("BAD_VALUE", f"{spec.label}は {'／'.join(spec.enum)} のいずれか")
            if spec.ref and self._get(spec.ref, value) is None:
                raise Rejected("NOT_FOUND", f"{spec.label} {value} は存在しない")
        if not isinstance(reason, str) or len(reason) > MAX_TEXT:
            raise Rejected("BAD_REASON", f"理由は{MAX_TEXT}字以内の文字列で渡す")
        if (act.needs_reason or proposing) and not reason.strip():
            raise Rejected("REASON_REQUIRED", "このアクションは理由の入力が必要")
        return act, role

    def _apply(self, actor: str, role: str, act: ActionType, params: dict, reason: str,
               changes: list[Change], proposal_id: int | None, proposer: str | None) -> Result:
        applied: list[dict] = []
        at = self._clock()
        with self._conn:                                   # 変更と監査ログを、ひとまとまりで書く
            for ch in changes:
                if ch.target == "prop":
                    obj = self._get(ch.type_name, ch.obj_id)
                    before, obj[ch.field] = obj[ch.field], ch.value
                    self._conn.execute("UPDATE objects SET props=? WHERE obj_type=? AND obj_id=?",
                                       (_canon(obj), ch.type_name, ch.obj_id))
                    owner = ch.type_name
                else:
                    before = self._dst(ch.type_name, ch.obj_id)
                    self._conn.execute("INSERT OR REPLACE INTO links VALUES (?,?,?)",
                                       (ch.type_name, ch.obj_id, ch.value))
                    owner = LINK_TYPES[ch.type_name].src
                applied.append({"object": f"{owner}:{ch.obj_id}", "field": ch.field,
                                "before": before, "after": ch.value})
            if proposal_id is not None:
                self._conn.execute("UPDATE proposals SET status='APPROVED', decided_by=?, decided_at=? WHERE id=?",
                                   (actor, at, proposal_id))
            seq = self._log("APPLIED", actor, role, act.name, params, reason, "OK", "", proposal_id, proposer, applied, at)
        return Result(True, "APPLIED", "OK", "", seq, proposal_id, applied)

    def _reject(self, actor: Any, action: Any, params: Any, reason: Any, rej: Rejected,
                proposal_id: int | None) -> Result:
        """却下も記録する。形の崩れた入力は、記録できる形に直してから残す。"""
        try:
            role = self._role(actor)
        except Rejected:
            role = "unknown"
        try:
            safe_params = json.loads(_canon(params)) if isinstance(params, dict) else {"_raw": repr(params)[:MAX_TEXT]}
        except (TypeError, ValueError):
            safe_params = {"_raw": repr(params)[:MAX_TEXT]}
        with self._conn:
            seq = self._log("REJECTED", str(actor)[:50], role, str(action)[:50], safe_params,
                            str(reason)[:MAX_TEXT], rej.code, rej.message, proposal_id, None, [])
        return Result(False, "REJECTED", rej.code, rej.message, seq, proposal_id)


def tool_definitions() -> list[dict]:
    """アクション型を、LLM に「使える道具」として渡すときの定義（JSON Schema）にして返す。"""
    tools = []
    for act in ACTIONS.values():
        props: dict[str, dict] = {}
        for name, spec in act.params.items():
            props[name] = {"type": "string", "description": spec.label}
            if spec.enum:
                props[name]["enum"] = list(spec.enum)
        props["reason"] = {"type": "string", "description": "この操作を提案する理由（必須・200字以内）"}
        tools.append({"name": act.name, "description": act.label + "（提案として受け付け、人の承認後に反映）",
                      "input_schema": {"type": "object", "properties": props,
                                       "required": list(act.params) + ["reason"],
                                       "additionalProperties": False}})
    return tools


# --------------------------------------------------------------------------
# 実行例
# --------------------------------------------------------------------------
_TAG = {"APPLIED": "反映", "REJECTED": "却下", "PROPOSED": "提案", "DECLINED": "見送り", "GENESIS": "開始"}


def _show(no: int, who: str, text: str, res: Result) -> None:
    print(f"{no:>2}. [{_TAG[res.event]}] {who}：{text}")
    if res.event == "REJECTED":
        print(f"      理由：{res.message}")
    if res.event == "PROPOSED":
        print(f"      提案番号 {res.proposal_id}（人の承認待ち。まだ反映していない）")
    for c in res.changes:
        before = "なし" if c["before"] in (None, "") else c["before"]
        print(f"      変更：{c['object']} の {c['field']}　{before} → {c['after']}")


def demo(db_path: str = ":memory:") -> int:
    eng = Engine.create(db_path, clock=stepping_clock())
    mgr, mech_a, mech_b, ai = "S-01", "S-02", "S-03", "AI-01"
    who = {mgr: "S-01 マネージャー", mech_a: "S-02 整備担当", mech_b: "S-03 整備担当", ai: "AI-01 AIエージェント"}

    print("■ 定義")
    print("  オブジェクト型：" + "／".join(f"{t.name}（{t.label}）{eng.count(t.name)}件" for t in OBJECT_TYPES.values()))
    print("  リンク型　　　：" + "／".join(f"{t.name}（{t.label}）{eng.count(t.name)}本" for t in LINK_TYPES.values()))
    print("  アクション型　：" + "／".join(ACTIONS))

    print("\n■ 操作（状態の書き換えは、すべてアクションを通る）")
    steps = [
        (mech_a, "submit", "assign_issue", {"issue_id": "I-104", "staff_id": "S-02"}, "", "I-104（ブレーキの効きが弱い・高）を自分に割り当てる"),
        (mech_a, "submit", "assign_issue", {"issue_id": "I-105", "staff_id": "S-02"}, "", "I-105（オイル漏れ・高）を自分に割り当てる"),
        (mech_a, "submit", "assign_issue", {"issue_id": "I-109", "staff_id": "S-02"}, "", "I-109（バッテリー上がり・高）も自分に割り当てる"),
        (mgr, "submit", "set_vehicle_status", {"vehicle_id": "C-008", "status": "貸出中"}, "", "C-008（デミオ・整備中）を貸出中にする"),
        (mech_a, "submit", "resolve_issue", {"issue_id": "I-105", "memo": "オイルパンのガスケットを交換。漏れが無いことを確認"}, "", "I-105 を解決済にする"),
        (mgr, "submit", "set_vehicle_status", {"vehicle_id": "C-008", "status": "待機"}, "", "C-008 を待機にする"),
        (ai, "submit", "resolve_issue", {"issue_id": "I-109", "memo": "対応不要と判断"}, "", "I-109 を解決済にしようとする"),
        (ai, "propose", "assign_issue", {"issue_id": "I-109", "staff_id": "S-03"}, "重要度が高く、受付から3日たっている。S-03 は担当中の未解決が0件", "I-109 を S-03 に割り当てる案を出す"),
        (mgr, "approve", 1, None, "", "提案番号1を承認する"),
        (mech_b, "submit", "reopen_issue", {"issue_id": "I-113"}, "警告が再表示された", "I-113（解決済）を未解決に戻す"),
        (mgr, "submit", "reopen_issue", {"issue_id": "I-113"}, "", "I-113 を、理由を書かずに未解決に戻す"),
        (mgr, "submit", "reopen_issue", {"issue_id": "I-113"}, "オイル交換後に警告が再表示されたと連絡があった", "I-113 を、理由を書いて未解決に戻す"),
    ]
    for no, (actor, entry, action, params, reason, text) in enumerate(steps, 1):
        if entry == "submit":
            res = eng.submit(actor, action, params, reason)
        elif entry == "propose":
            res = eng.propose(actor, action, params, reason)
        else:
            res = eng.approve(actor, action)
        _show(no, who[actor], text, res)

    print("\n■ 読み取り（氏名は個人情報の印つき。見る資格のない役割には伏せる）")
    print(f"  S-01 マネージャーが見た S-02：{eng.read(mgr, 'Staff', 'S-02')['name']}")
    print(f"  AI-01 AIエージェントが見た S-02：{eng.read(ai, 'Staff', 'S-02')['name']}")

    rows = eng.audit_rows()
    print(f"\n■ 監査ログ（{len(rows)}件。却下も残る）")
    for r in rows:
        via = f"（提案 {r['proposer']}）" if r["event"] == "APPLIED" and r["proposer"] else ""
        print(f"  {r['seq']:>2}  {r['at'][11:16]}  {_TAG[r['event']]:<3}  {r['actor']:<6} {r['action'] or '-':<19}"
              f" {r['code']:<17} {r['entry_hash'][:8]}{via}")

    print("\n■ 検証")
    print("  連鎖と現在の状態：" + ("問題なし" if not eng.verify() else "；".join(eng.verify())))
    try:
        eng._conn.execute("UPDATE audit_log SET actor='S-01' WHERE seq=8")
        print("  監査ログの書き換え：通ってしまった")
    except sqlite3.DatabaseError as exc:
        print(f"  監査ログを書き換えようとする：拒否された（{exc}）")
    obj = eng._get("Issue", "I-101")                     # わざと、アクションを通さずに書き換える
    obj["status"] = "解決済"
    eng._conn.execute("UPDATE objects SET props=? WHERE obj_type='Issue' AND obj_id='I-101'", (_canon(obj),))
    print("  アクションを通さず I-101 を解決済に書き換える：" + "；".join(eng.verify()))
    eng._conn.rollback()                                 # 確かめたので、この書き換えは取り消す
    print("  書き換えを取り消したあと：" + ("問題なし" if not eng.verify() else "；".join(eng.verify())))
    eng.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="オントロジー（オブジェクト型・リンク型・アクション型）の実行例")
    parser.add_argument("--db", default=":memory:", help="SQLite のファイルに残す場合の保存先（新しいファイル名）")
    parser.add_argument("--tools", action="store_true", help="LLM に渡す道具の定義（JSON）を表示して終わる")
    args = parser.parse_args(argv)
    if args.tools:
        print(json.dumps(tool_definitions(), ensure_ascii=False, indent=2))
        return 0
    if args.db != ":memory:" and os.path.exists(args.db):
        print(f"{args.db} はすでにあります。新しいファイル名を指定してください。", file=sys.stderr)
        return 2
    return demo(args.db)


if __name__ == "__main__":
    sys.exit(main())
